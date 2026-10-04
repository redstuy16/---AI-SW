"""연구 설정의 기본 전송 범위와 검색 예산·완료 비용 보존을 검사한다."""
import asyncio
from decimal import Decimal

import httpx
import pytest

from htrsa.control_plane import ControlError, Defaults
from htrsa.product_policy import completion_budget
from htrsa.search_policy import PolicyProvider, decision, search_allocation
from htrsa.scholarly import CrossrefProvider, ScholarlyHTTPClient, SearchRequest
from test_workbench import app, configure
from test_product_ux import paid


def research(app, **changes):
    configure(app)
    return app.create({'beginner_mode': True, 'question': '최근 문헌 검토', 'model_profile_id': 'm',
        'run_limit_usd': '.10', 'public_search_query': 'public relationship',
        'public_search_consent': True, **changes})


def test_beginner_selected_default_and_explicit_legacy_policy(app):
    value = research(app)
    assert value['snapshot']['egress'] == 'selected'
    assert decision(value['snapshot']) == 'SEARCH_ALLOWED'
    assert app.store.ledger(value['research_id'])['spent'] == '0'
    explicit = app.prepare({'beginner_mode': True, 'question': '질문만', 'egress': 'none'})
    assert explicit['egress'] == 'none'
    legacy = app.prepare({'question': '이전 연구', 'source_relative': 'data.csv', 'routing': value['snapshot']['routing']})
    assert legacy['egress'] == 'none'


@pytest.mark.parametrize('egress,consent,query,status', [
    ('selected', False, 'public relationship', 'SEARCH_EGRESS_DENIED'),
    ('selected', True, '', 'SEARCH_QUERY_REQUIRED'),
    ('selected', True, '비공개 자료', 'SEARCH_PRIVATE_QUERY_BLOCKED'),
    ('none', True, 'public relationship', 'SEARCH_EGRESS_DENIED'),
    ('selected', True, 'public relationship', 'SEARCH_ALLOWED'),
    ('research', False, 'public relationship', 'SEARCH_ALLOWED')])
def test_search_permission_remains_separate(egress, consent, query, status):
    assert decision({'question':'최근 문헌', 'search_policy':'AUTO', 'search_required':True,
        'egress':egress, 'public_search_consent':consent, 'public_search_query':query}) == status


@pytest.mark.parametrize('available,reserve,unit,expected', [
    ('.10','.04','.02',3), ('.08','.04','.02',2), ('.05','.04','.02',0),
    ('.10','.04','0',5), ('.10','.10','0',5),
    ('.000003','0','.0000001',3)])
def test_allocation_separate_model_search_and_micro_rounding(app, available, reserve, unit, expected):
    value = research(app)
    view={'available_usd': available, 'completion_reserve_usd':reserve,
          'can_complete':Decimal(available)>=Decimal(reserve)}
    result=search_allocation(app.store,value['research_id'],value['snapshot'],
        price=Decimal(unit),price_source='오프라인 단가',view=view)
    assert result['allowed_attempts'] == expected
    assert result['model_completion_reserve_usd'] == reserve
    assert Decimal(result['search_unit_price_usd']) == Decimal(unit)


@pytest.mark.parametrize('price,source',[(None,'fixture'),(Decimal('.01'),None)])
def test_unknown_search_price_stays_blocked(app,price,source):
    value=research(app)
    assert search_allocation(app.store,value['research_id'],value['snapshot'],price=price,price_source=source)['allowed_attempts']==0


def test_counter_pending_limits_manual_zero_and_unknown_completion(app):
    value=research(app);rid,snap=value['research_id'],value['snapshot']
    view={'available_usd':'.10','completion_reserve_usd':'.04','can_complete':True}
    app.store.put('defaults','global',Defaults(search_attempt_limit=2))
    app.store.put('search_attempts',rid,{'used':1})
    assert search_allocation(app.store,rid,snap,price=Decimal(0),price_source='fixture',view=view)['allowed_attempts']==1
    app.store.put('research_settings',rid,{'settings':{'search_attempt_limit':0,'run_limit_usd':'.10'},'applied':False})
    assert search_allocation(app.store,rid,snap,price=Decimal(0),price_source='fixture',view=view)['allowed_attempts']==0
    snap={**snap,'adaptive_budget':False}
    app.store.put('research_settings',rid,{'settings':{'search_attempt_limit':2,'run_limit_usd':'.10'},'applied':False},1)
    view['can_complete']=False;view['completion_reserve_usd']=None
    assert search_allocation(app.store,rid,snap,price=Decimal('.10'),price_source='fixture',view=view)['allowed_attempts']==1
    snap['adaptive_budget']=True
    assert search_allocation(app.store,rid,snap,price=Decimal(0),price_source='fixture',view=view)['allowed_attempts']==0


def test_paid_search_uses_own_ledger_and_reduces_until_model_completion(app):
    paid(app)
    created=app.create({'beginner_mode':True,'question':'최근 문헌 검토','model_profile_id':'m',
        'run_limit_usd':'.08','public_search_query':'public relationship','public_search_consent':True})
    rid,snap=created['research_id'],created['snapshot']
    calls=[]
    def respond(request):
        calls.append(str(request.url));return httpx.Response(200,json={'message':{'items':[]}})
    provider=PolicyProvider(CrossrefProvider(ScholarlyHTTPClient(retries=0,transport=httpx.MockTransport(respond))),
        app.store,app.credentials,snap,price=Decimal('.02'),price_source='오프라인 유료 검색')
    request=SearchRequest(research_id=rid,query='public relationship')
    view=completion_budget(app.store,rid,snap)
    expected=min(5,int((Decimal(view['available_usd'])-Decimal(view['completion_reserve_usd']))//Decimal('.02')))
    assert expected==2
    for _ in range(expected):asyncio.run(provider.search(request))
    with pytest.raises(ControlError,match='COMPLETION_RESERVE_BLOCKED'):asyncio.run(provider.search(request))
    assert len(calls)==2
    assert app.store.ledger(rid)['spent']=='0.04'
    assert completion_budget(app.store,rid,snap)['can_complete']
    assert all(x['role']=='search' and x['purpose']=='web_search' for x in app.store.ledger(rid)['requests'])
    assert app.store.config('search_attempts',rid)['used']==2


@pytest.mark.parametrize('kind',['run','month','request'])
def test_tightened_budget_between_reserve_and_transport_blocks(app,kind):
    value=research(app);rid,snap=value['research_id'],value['snapshot'];calls=[];count=0
    def before():
        nonlocal count
        count+=1
        if count==2:
            if kind=='run':app.store.put('research_settings',rid,{'settings':{'run_limit_usd':'.01'},'applied':False})
            else:app.store.put('defaults','global',Defaults(**{'monthly_limit_usd' if kind=='month' else 'request_limit_usd':'.01'}))
    client=ScholarlyHTTPClient(retries=0,transport=httpx.MockTransport(lambda r:calls.append(r) or httpx.Response(200,json={'message':{'items':[]}})))
    provider=PolicyProvider(CrossrefProvider(client),app.store,app.credentials,snap,price=Decimal('.02'),price_source='fixture',before_dispatch=before)
    with pytest.raises(ControlError,match='COMPLETION_RESERVE_BLOCKED|REQUEST_BUDGET_BLOCKED'):
        asyncio.run(provider.search(SearchRequest(research_id=rid,query='public relationship')))
    assert not calls
    assert app.store.ledger(rid)['reserved']=='0'
    assert app.store.ledger(rid)['spent']=='0'


def test_paid_uncertain_search_keeps_exposure_and_counter(app):
    value=research(app);rid,snap=value['research_id'],value['snapshot']
    client=ScholarlyHTTPClient(retries=0,transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ReadTimeout('모의 실패'))))
    provider=PolicyProvider(CrossrefProvider(client),app.store,app.credentials,snap,price=Decimal('.02'),price_source='fixture')
    with pytest.raises(Exception):asyncio.run(provider.search(SearchRequest(research_id=rid,query='public relationship')))
    assert app.store.ledger(rid)['unresolved']=='0.02'
    assert app.store.config('search_attempts',rid)['used']==1


@pytest.mark.parametrize('price',[Decimal('-1'),Decimal('NaN'),Decimal('Infinity')])
def test_invalid_price_never_admitted(app,price):
    value=research(app)
    with pytest.raises(ControlError,match='INVALID_MONEY'):
        search_allocation(app.store,value['research_id'],value['snapshot'],price=price,price_source='fixture')
