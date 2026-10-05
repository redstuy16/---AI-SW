"""앱 기본 자동 검색 설정으로 네 계산 문제를 실행·검산·보고서·PDF까지 검사한다."""
import asyncio
import csv
import io
import json
from pathlib import Path

import httpx
import pytest
from pypdf import PdfReader

from probe.control_runtime import execute, RoutedGateway
from probe.control_plane import PriceRecord
from probe.schemas import utc_now
from probe.research_report import verified_numbers
from probe.science_tables import question_csv
from test_science_context_start import setup_model, body
from test_multi_provider import app

CASES = json.loads((Path(__file__).resolve().parents[1]/'qa/climate_questions.json').read_text(encoding='utf-8'))
PLANS = {
    1: {'inputs':{'S':1361,'A':.3,'sigma':5.6704e-8},'calculations':[
        {'name':'flux','expression':'S*(1-A)/4','unit':'W/m²'},
        {'name':'kelvin','expression':'(flux/sigma)**.25','unit':'K'},
        {'name':'celsius','expression':'kelvin-273.15','unit':'°C'}]},
    2: {'inputs':{'area':3.61e14,'depth':100,'rho':1025,'cp':3990,'delta':.2,'earth':5.10e14,'imbalance':.7},
        'calculations':[
        {'name':'energy','expression':'area*depth*rho*cp*delta','unit':'J'},
        {'name':'energy_unit','expression':'energy/1e22','unit':'10^22 J'},
        {'name':'years','expression':'energy/(earth*imbalance)/(365*24*3600)','unit':'년'}]},
    3: {'inputs':{'C':420,'C0':280,'lambda':.8},'calculations':[
        {'name':'forcing','expression':'5.35*log(C/C0)','unit':'W/m²'},
        {'name':'warming','expression':'lambda*forcing','unit':'K'}]},
}


def table(case):
    rows=list(csv.reader(io.StringIO(case['csv'])))
    return '\n'.join(['| '+' | '.join(rows[0])+' |','| '+' | '.join(['---']*len(rows[0]))+' |']+
        ['| '+' | '.join(row)+' |' for row in rows[1:]])


@pytest.mark.parametrize('case_id', [1,2,3,4])
def test_app_default_auto_search_completes_given_calculation_and_pdf(app, monkeypatch, case_id):
    case=CASES[case_id-1]
    question=case['question'] + ('\n'+table(case) if case_id==4 else '')
    profile=setup_model(app,monkeypatch)
    profile.price=PriceRecord(input_per_million='.1',output_per_million='.5',source='모의 단가',
        checked_at=utc_now(),revision='incident-price',owner_verified=True)
    revision=app.store.db.execute("SELECT revision FROM control_configs WHERE kind='model' AND id=?",
        (profile.profile_id,)).fetchone()[0]
    app.store.put('model',profile.profile_id,profile,revision)
    sent=[]
    def handler(request):
        wire=json.loads(request.content)
        assert not wire.get('tools'), '주어진 계산 문제에서 유료 검색을 호출하면 안 됩니다.'
        sent.append(wire)
        context=json.loads(json.loads(wire['input'][0]['content'])['active_state']['contract']['objective'].split('\n',1)[1])
        if case_id<4 and len(sent)==1:
            answer={'action':'CALCULATE','rationale':'주어진 값으로 계산하고 검산합니다.','calculation_plan':PLANS[case_id]}
        elif case_id==4 and len(sent)<=2:
            answer={'action':'CALCULATE_DATASET','rationale':'주어진 표의 회귀를 독립 검산합니다.',
                'dataset_calculation_plan':{'dataset_id':context['dataset']['dataset_id'],
                    'name':'simple' if len(sent)==1 else 'adjusted','operation':'OLS','target':'T',
                    'predictors':['t'] if len(sent)==1 else ['t','ENSO','V'],'target_unit':'°C',
                    'predictor_units':{'t':'년','ENSO':'지수','V':'지표'}}}
        else:
            answer={'action':'COMPLETE','rationale':'실제로 계산한 결과를 정리합니다.','report_draft':{
                'report_type':'analysis','summary':'주어진 값과 가정으로 계산한 결과다.',
                'purpose':'입력된 과학 문제의 계산 결과를 확인한다.',
                'explanation':'모델의 가정에 따른 계산이며 실제 관측과 구분한다.',
                'method':'제공된 값과 자료를 사용하여 서로 다른 계산으로 검산하였다.',
                'results':'\n'.join(item['sentence'] for item in context['verified_numbers']),
                'conclusion':'제공 자료의 결과이며 인과관계나 실제 관측을 증명하지 않는다.'}}
        return httpx.Response(200,json={'id':'offline-case-'+str(len(sent)),'model':'gpt-6-luna','status':'completed',
            'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(answer,ensure_ascii=False)}]}],
            'usage':{'input_tokens':1000,'output_tokens':500,'input_tokens_details':{'cached_tokens':0}}})
    created=app.request('POST','/api/control/research',body(profile,question=question,title=case['title'],
        search_policy='AUTO',public_search_consent=True,run_limit_usd='0.10'))
    assert created.status==201,created.body
    rid=created.body['research_id']
    started=app.request('POST',f'/api/control/research/{rid}/start',{'expected_version':0,'idempotency_key':'given-start'})
    assert started.status==200,started.body
    def factory(store,research_id,snapshot):
        return RoutedGateway(store,app.credentials,research_id,snapshot,
            client_factory=lambda *_:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(execute(app.database,app.workspace,rid,provider_factory=factory))
    assert app.store.run(rid)['status']=='COMPLETED',app.store.run(rid)
    assert len(sent)==(3 if case_id==4 else 2)
    values={item['field']:item['value'] for item in verified_numbers(app.read._state,rid)}
    expected={1:{'flux':238.175,'kelvin':254.577852995,'celsius':-18.5721470049},
        2:{'energy':2.9527995e22,'years':2.9527995e22/(.7*5.10e14)/(365*24*3600)},
        3:{'forcing':2.16923832838,'warming':1.73539066270},
        4:{'simple_intercept':-.043,'simple_coef_t':.02181818181818,'adjusted_coef_t':.018,
            'adjusted_coef_ENSO':.12,'adjusted_coef_V':-.15}}[case_id]
    for key,value in expected.items():
        assert values[key]==pytest.approx(value,rel=1e-9,abs=1e-10)
    before=app.store.ledger(rid)
    for _ in range(2):
        response=app.request('GET',f'/api/control/research/{rid}/report.pdf')
        assert response.status==200,response.body
        assert PdfReader(io.BytesIO(response.body)).pages
    assert app.store.ledger(rid)==before
    assert app.read._state.runtime_step(rid,'science:initial_search')['output']['reason']=='SELF_CONTAINED_CALCULATION'


@pytest.mark.parametrize('payload',[
    '|a|b|\n|---|---|\n|1|2|\n|3|4|\n|5|__import__(\"os\")|',
    '|a|a|\n|---|---|\n|1|2|\n|3|4|\n|5|6|',
    '|a|b|\n|---|---|\n|1|2|\n|3|4|\n|5|1e999|',
])
def test_inline_table_refuses_code_duplicates_and_nonfinite(payload):
    assert question_csv(payload) is None
