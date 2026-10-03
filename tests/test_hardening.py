"""검색·업로드·자원·공개 준비 경계를 실제 저장소와 오프라인 제공사로 검사한다."""
import asyncio
import base64
from copy import deepcopy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pathspec
import pytest

from htrsa.control_plane import ControlError
from htrsa.input_upload import upload_csv
from htrsa.resource_policy import activity_page, preferences, save_preferences, low_spec
from htrsa.scholarly import SearchRequest, SearchResult, NormalizedSource, ScholarlyHTTPClient, ScholarlyError
from htrsa.search_policy import decision, PolicyProvider, run_search
from test_workbench import app, configure
from test_product_ux import product

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prepublish_check', ROOT / 'qa/prepublish_check.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def upload(content=b'x,y\n1,2\n', filename='한글 자료.csv'):
    return {'filename': filename, 'content_base64': base64.b64encode(content).decode()}


def test_upload_and_preflight_no_canonical_or_paid_mutation(app):
    configure(app)
    response = app.request('POST', '/api/control/inputs/upload', upload())
    assert response.status == 201 and response.body['rows'] == 1
    request = {'title': '새 연구', 'question': '측정값 관계', 'model_profile_id': 'm', 'egress': 'selected', 'source_relative': response.body['source_relative'], 'performance_profile': 'BALANCED', 'search_policy': 'AUTO'}
    response = app.request('POST', '/api/control/research/preflight', request)
    assert response.status == 200 and response.body['ready'], response.body
    assert response.body['paid_calls'] == 0
    assert app.store.db.execute('SELECT COUNT(*) FROM research_runs').fetchone()[0] == 0
    assert app.store.db.execute('SELECT COUNT(*) FROM spend_ledger').fetchone()[0] == 0


@pytest.mark.parametrize('content,name', [(b'x,x\n1,2\n','a.csv'),(b'x,y\n1\n','a.csv'),(b'\xff','a.csv'),(b'x,y\n','a.csv'),(b'x,y\n1,2\n','../a.csv'),(b'x,y\n1,2\n','C:\\a.csv'),(b'x,y\n1,2\n','a.txt'),(b'x,y\n1,sk-qa-canary-0123456789abcdef\n','a.csv')])
def test_upload_invalid_never_writes(tmp_path, content, name):
    with pytest.raises(ControlError):
        upload_csv(tmp_path / 'inputs', upload(content, name))
    assert not list(tmp_path.rglob('*.csv'))


def test_upload_limit_active_secret_symlink(app, tmp_path):
    with pytest.raises(ControlError):
        upload_csv(tmp_path/'inputs', upload(b'x,y\n1,private-value\n'), protected_values=['private-value'])
    with pytest.raises(ControlError):
        upload_csv(tmp_path/'inputs', upload(b'x,y\n'+b'0,0\n'*1_300_000))
    first = upload_csv(tmp_path/'inputs', upload())
    assert first == upload_csv(tmp_path/'inputs', upload())
    assert len(list((tmp_path/'inputs').glob('*.csv'))) == 1


@pytest.mark.parametrize('policy,question,egress,query,required,budget,status', [
    ('DISABLED','최근 논문','research','public topic',False,True,'SEARCH_DISABLED'),
    ('DISABLED','최근 논문','research','public topic',True,True,'SEARCH_REQUIRED_BUT_DISABLED'),
    ('AUTO','로컬 CSV 평균','research','public topic',False,True,'SEARCH_NOT_NEEDED'),
    ('AUTO','최근 논문','research','public topic',False,True,'SEARCH_ALLOWED'),
    ('ALLOWED','로컬 CSV 평균','research','public topic',False,True,'SEARCH_NOT_NEEDED'),
    ('AUTO','최근 논문','selected','public topic',False,True,'SEARCH_EGRESS_DENIED'),
    ('AUTO','최근 논문','research','C:/Users/private/data.csv',False,True,'SEARCH_PRIVATE_QUERY_BLOCKED'),
    ('AUTO','최근 논문','research','public topic',False,False,'SEARCH_OPTIONAL_REDUCED'),
    ('AUTO','최근 논문','research','public topic',True,False,'SEARCH_ALLOWED')])
def test_search_semantics(policy, question, egress, query, required, budget, status):
    assert decision(dict(search_policy=policy,question=question,egress=egress,public_search_query=query,search_required=required),budget_ok=budget) == status


class Provider:
    name = 'offline.search'
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail
    async def search(self, request):
        self.calls += 1
        if self.fail:
            raise TimeoutError('모의 전송 결과 불명')
        return SearchResult(provider=self.name,request=request,sources=[NormalizedSource(title='Public temperature association',abstract='Temperature is associated with energy use in public measurements.',doi='10.1234/public',url='https://doi.org/10.1234/public',provider=self.name)])


def search_case(app, **changes):
    result = product(app, question='최근 문헌에서 temperature association 검토',search_policy='AUTO',public_search_query='public temperature association',egress='research',**changes)
    return result['research_id'], result['snapshot']


@pytest.mark.parametrize('policy',['DISABLED','AUTO','ALLOWED'])
def test_no_unnecessary_dispatch(app, policy):
    result = product(app,question='로컬 CSV 평균 계산',search_policy=policy)
    p = Provider()
    asyncio.run(run_search(app.read._state,app.store,app.credentials,result['research_id'],result['snapshot'],provider=p,price=Decimal(0),price_source='offline'))
    assert p.calls == 0


def test_search_cost_provenance_cache_and_untrusted_content(app):
    rid,s = search_case(app)
    p = Provider()
    asyncio.run(run_search(app.read._state,app.store,app.credentials,rid,s,provider=p,price=Decimal('0.001'),price_source='offline-known-price'))
    assert p.calls == 2
    rows = list(app.store.db.execute("SELECT * FROM spend_ledger WHERE research_id=?",(rid,)))
    assert len(rows) == 2 and all(r['purpose']=='web_search' and r['status']=='SETTLED' for r in rows)
    before = deepcopy(app.store.defaults().model_dump())
    trace = [json.loads(r[0]) for r in app.store.db.execute("SELECT payload FROM control_audit WHERE kind='SEARCH_COMPLETED'")]
    assert all(t['provider']==p.name and t['query'] and t['query_hash'] and t['started_at'] and t['completed_at'] and t['sources'][0]['retrieved_at'] and t['selected_result_ids'] for t in trace)
    assert app.store.defaults().model_dump() == before
    asyncio.run(run_search(app.read._state,app.store,app.credentials,rid,s,provider=p,price=Decimal('0.001'),price_source='offline-known-price'))
    assert p.calls == 2


def test_unknown_price_and_private_query_zero_dispatch(app):
    rid,s = search_case(app)
    p=Provider()
    wrapped=PolicyProvider(p,app.store,app.credentials,s)
    asyncio.run(wrapped.search(SearchRequest(research_id=rid,query='public topic')))
    assert p.calls == 0
    assert 'PRICE_UNKNOWN' in app.store.db.execute("SELECT payload FROM control_audit WHERE kind='SEARCH_POLICY_DECISION'").fetchone()[0]
    wrapped=PolicyProvider(p,app.store,app.credentials,s,price=Decimal(0),price_source='offline')
    asyncio.run(wrapped.search(SearchRequest(research_id=rid,query='private API_KEY data')))
    assert p.calls == 0
    assert 'private API_KEY data' not in '\n'.join(r[0] for r in app.store.db.execute('SELECT payload FROM control_audit'))


def test_ambiguous_search_cost_blocks_retry(app):
    rid,s=search_case(app)
    p=Provider(fail=True)
    wrapped=PolicyProvider(p,app.store,app.credentials,s,price=Decimal('0.001'),price_source='offline')
    with pytest.raises(TimeoutError):
        asyncio.run(wrapped.search(SearchRequest(research_id=rid,query='public topic')))
    with pytest.raises(ControlError):
        asyncio.run(wrapped.search(SearchRequest(research_id=rid,query='public topic')))
    assert p.calls == 1
    assert app.store.db.execute("SELECT status FROM spend_ledger").fetchone()[0] == 'UNRESOLVED'


@pytest.mark.parametrize('url',['http://api.crossref.org/works','https://localhost/','https://api.crossref.org.evil/','https://u:p@api.crossref.org/works'])
def test_search_destinations_blocked(url):
    calls=[]
    client=ScholarlyHTTPClient(transport=httpx.MockTransport(lambda r:calls.append(r)))
    with pytest.raises(ScholarlyError):
        asyncio.run(client.get_json(url))
    assert not calls


def test_activity_bounded_and_all_records_preserved(app):
    rid=product(app)['research_id']
    for i in range(205):
        app.read._state.runtime_event(rid,'TEST_EVENT',{'index':i})
    count=app.store.db.execute('SELECT COUNT(*) FROM runtime_events').fetchone()[0]
    first=activity_page(app.read._state,rid,limit=25)
    second=activity_page(app.read._state,rid,limit=25,offset=first['next_offset'])
    assert len(first['items'])==25 and len(second['items'])==25
    assert first['items'][0] != second['items'][0]
    assert app.store.db.execute('SELECT COUNT(*) FROM runtime_events').fetchone()[0] == count
    assert 'idx_runtime_research_seq' in str([tuple(r) for r in app.store.db.execute('EXPLAIN QUERY PLAN SELECT seq FROM runtime_events WHERE research_id=? ORDER BY seq DESC LIMIT 25',(rid,))])


def test_lazy_views_do_not_import_heavy_modules_or_project_hidden_pages(app, monkeypatch):
    result=subprocess.run([sys.executable,'-c',"import sys;from pathlib import Path;from htrsa.workbench import WorkbenchAPI;a=WorkbenchAPI(Path(sys.argv[1])/'state.sqlite',Path(sys.argv[1])/'workspace',launch=False);a.request('GET','/api/control/research');assert not any(k in sys.modules for k in ('numpy','scipy','matplotlib','sklearn','pandas'));a.close()",str(app.database.parent/'lazy-probe')],capture_output=True,check=False)
    assert result.returncode==0,result.stderr
    rid=product(app)['research_id']
    import htrsa.dashboard as dashboard
    monkeypatch.setattr(dashboard,'project_timeline',lambda *_:pytest.fail('숨겨진 기록 조회'))
    assert app.request('GET',f'/api/research/{rid}').status==200


def test_safe_preferences_and_low_spec_do_not_enable_experiments(app):
    configure(app)
    save_preferences(app.store,{'model_profile_id':'m','low_spec_mode':'ON'})
    assert low_spec(app.store)
    p=preferences(app.store)
    assert not {'run_limit_usd','egress','verification_repair'} & set(p)
    with pytest.raises(Exception):
        save_preferences(app.store,p|{'egress':'research'})
    result=product(app)
    assert not result['snapshot']['verification_repair'] and not result['snapshot']['verified_analysis_skills']


@pytest.mark.parametrize('relative,ignored',[('.env',True),('secrets.env',True),('state.sqlite',True),('workspace/input.csv',True),('models/model.gguf',True),('build/x.txt',True),('src/htrsa/workbench.py',False),('docs/README.md',False),('tests/test_workbench.py',False),('.env.example',False),('qa/fixtures/f3p_eval_config.json',False),('.github/workflows/offline.yml',False),('qa/live_api_test/session/manifest.json',True)])
def test_gitignore_coverage(relative,ignored):
    rules=pathspec.GitIgnoreSpec.from_lines((ROOT/'.gitignore').read_text(encoding='utf-8').splitlines())
    assert rules.match_file(relative)==ignored


def test_masked_secret_local_path_large_artifact_and_tracked_detection(tmp_path):
    text='OPENAI_API_KEY='+'sk-proj-'+('Z'*40)+'\npath=C:/Users/person/private\n'
    found=checker.scan_text('config.txt',text)
    assert any(f['kind']=='SUSPECTED_SECRET' for f in found)
    assert any(f['kind']=='ABSOLUTE_LOCAL_PATH' for f in found)
    assert 'Z'*40 not in json.dumps(found)
    assert checker.sensitive_path('.env') and checker.sensitive_path('workspace/private.csv')
    (tmp_path/'.gitignore').write_bytes(b'.env\n')
    (tmp_path/'large.txt').write_bytes(b'x'*32)
    value=checker.inspect(tmp_path,max_bytes=16)
    assert value['large_files'] and value['publication_status']=='BLOCKED'


@pytest.mark.parametrize('research,task,required,status', [
    ('DISABLED','ALLOWED',False,'SEARCH_DISABLED'),
    ('AUTO','DISABLED',True,'SEARCH_REQUIRED_BUT_DISABLED'),
    ('ALLOWED','DISABLED',False,'SEARCH_DISABLED')])
def test_task_search_policy_cannot_expand_approval(app, research, task, required, status):
    rid,s=search_case(app)
    s['search_policy']=research
    p=Provider()
    wrapped=PolicyProvider(p,app.store,app.credentials,s,price=Decimal(0),price_source='offline')
    request=SearchRequest(research_id=rid,query='public topic',search_policy=task,evidence_required=required)
    if required:
        with pytest.raises(ControlError,match=status):
            asyncio.run(wrapped.search(request))
    else:
        assert not asyncio.run(wrapped.search(request)).sources
    assert p.calls==0


def test_required_evidence_cannot_be_removed_by_task(app):
    rid,s=search_case(app,search_required=True)
    wrapped=PolicyProvider(Provider(),app.store,app.credentials,s)
    with pytest.raises(ControlError,match='PRICE_UNKNOWN'):
        asyncio.run(wrapped.search(SearchRequest(research_id=rid,query='public topic',evidence_required=False)))


@pytest.mark.parametrize('command',['PAUSE_REQUESTED','STOP_REQUESTED'])
def test_search_safe_boundary_prevents_network_and_reservation(app, command):
    from htrsa.control_plane import ControlBoundary
    from htrsa.control_runtime import boundary
    rid,s=search_case(app)
    app.store.db.execute('UPDATE control_runs SET status=? WHERE research_id=?',(command,rid))
    p=Provider()
    wrapped=PolicyProvider(p,app.store,app.credentials,s,price=Decimal(0),price_source='offline',
        before_dispatch=lambda:boundary(app.store,app.read._state,rid,snapshot=s))
    with pytest.raises(ControlBoundary):
        asyncio.run(wrapped.search(SearchRequest(research_id=rid,query='public topic')))
    assert p.calls==0
    assert app.store.db.execute('SELECT COUNT(*) FROM spend_ledger').fetchone()[0]==0


def test_search_cache_invalidated_source_is_not_reused(app):
    rid,s=search_case(app)
    p=Provider()
    asyncio.run(run_search(app.read._state,app.store,app.credentials,rid,s,provider=p,price=Decimal(0),price_source='offline'))
    source=app.store.db.execute("SELECT source_id FROM sources WHERE research_id=? AND status='VERIFIED'",(rid,)).fetchone()
    assert source
    app.read._state.invalidate_source(rid,source[0],'오프라인 무효화 주입')
    asyncio.run(run_search(app.read._state,app.store,app.credentials,rid,s,provider=p,price=Decimal(0),price_source='offline'))
    assert p.calls==4
    assert app.store.db.execute('SELECT status FROM sources WHERE source_id=?',(source[0],)).fetchone()[0]=='INVALIDATED'


def test_low_spec_serializes_runs_without_changing_roles(app):
    save_preferences(app.store,{'low_spec_mode':'ON'})
    a=product(app)['research_id'];b=product(app)['research_id']
    app.command(a,'start',{'idempotency_key':'first-low-spec','expected_version':0})
    with pytest.raises(ControlError,match='LOW_SPEC_BUSY'):
        app.command(b,'start',{'idempotency_key':'second-low-spec','expected_version':0})
    assert len(app.store.run(b)['snapshot']['models'])==4
    assert app.store.run(b)['status']=='DRAFT'


def test_workflows_have_pinned_actions_minimal_permissions_and_no_live_secrets():
    import re
    import yaml
    for file in (ROOT/'.github/workflows').glob('*.yml'):
        text=file.read_text(encoding='utf-8')
        value=yaml.safe_load(text)
        assert value['permissions']=={'contents':'read'}
        assert 'pull_request_target' not in text and 'secrets.' not in text
        for job in value['jobs'].values():
            for step in job['steps']:
                if 'uses' in step:
                    assert re.fullmatch(r'[\w/-]+@[a-f0-9]{40}',step['uses'])
                assert not re.search(r'live_api|live_search|test_live',step.get('run',''))
        if file.stem=='codeql':
            job=value['jobs']['analyze']
            assert job['permissions']=={'contents':'read','security-events':'write'}
            assert set(job['strategy']['matrix']['language'])=={'python','javascript-typescript'}


def test_search_secret_response_never_enters_sources_or_audit(app):
    rid,s=search_case(app)
    canary='sk-search-offline-canary-0123456789'
    class Hostile(Provider):
        async def search(self,request):
            result=await super().search(request)
            result.sources[0].abstract=canary
            return result
    wrapped=PolicyProvider(Hostile(),app.store,app.credentials,s,price=Decimal(0),price_source='offline')
    with pytest.raises(ControlError,match='SECRET_IN_SEARCH_RESPONSE'):
        asyncio.run(wrapped.search(SearchRequest(research_id=rid,query='public topic')))
    assert app.store.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]==0
    assert not any(canary in r[0] for r in app.store.db.execute('SELECT payload FROM control_audit'))
    assert app.store.db.execute('SELECT status FROM spend_ledger').fetchone()[0]=='SETTLED'


def test_search_provenance_export_hash_and_secret_gate(tmp_path):
    from hashlib import sha256
    from htrsa.control_plane import ControlStore
    from htrsa.database import initialize
    from htrsa.demo import run_demo_a
    from htrsa.release import export_release, ReleaseExportError
    from htrsa.service import StateService
    from htrsa.storage import Workspace
    database=tmp_path/'state.sqlite';workspace=tmp_path/'workspace'
    demo=run_demo_a(database,workspace)
    db=initialize(database)
    try:
        state=StateService(db,Workspace(workspace));store=ControlStore(db);rid=demo['research_id']
        store.audit(rid,'SEARCH_COMPLETED',{'query_hash':sha256(b'public topic').hexdigest(),'provider':'offline','search_cost':'0','status':'COMPLETED'})
        result=export_release(state,rid,tmp_path/'release')
        manifest=json.loads(Path(result['manifest']).read_text(encoding='utf-8'))
        entry=next(f for f in manifest['files'] if f['path']=='search_provenance.json')
        payload=(Path(result['output'])/entry['path']).read_bytes()
        assert sha256(payload).hexdigest()==entry['sha256']
        assert json.loads(payload)[0]['kind']=='SEARCH_COMPLETED'
        canary='qa-secret-export-canary-0123456789'
        store.audit(rid,'SEARCH_COMPLETED',{'secret_fault_fixture':canary})
        with pytest.raises(ReleaseExportError):
            export_release(state,rid,tmp_path/'blocked-release',protected_values=[canary])
    finally:
        db.close()
