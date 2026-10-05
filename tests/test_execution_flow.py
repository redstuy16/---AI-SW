"""공유 자원·읽기 전용 흐름도·대형 목록의 실제 저장 경계를 확인한다."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from probe.control_plane import ControlBoundary, ControlError, ControlStore
from probe.control_runtime import execute
from probe.dashboard import project_overview, project_experiments, project_verification
from probe.database import connect
from probe.demo import run_demo_a, run_demo_b
from probe.providers.fake import FakeProvider
from probe.providers.native import normalized_error
from probe.resource_policy import low_spec, preferences, save_preferences
from probe.resource_queue import ResourcePool, resource_key
from probe.research_flow import project_flow, flow_node
from probe.workbench import WorkbenchAPI
from test_workbench import app, configure, create
from test_autonomous_loop import fake_replies
from test_verification_repair import prepare_case, corrupt_first_output


@pytest.mark.parametrize('skill', [False, True])
@pytest.mark.parametrize('failure', [OSError, MemoryError])
def test_failed_figure_save_closes_figure_without_commit(tmp_path, monkeypatch, skill, failure):
    import matplotlib.pyplot as plt
    from matplotlib.figure import Figure
    from probe.agent_runtime import RuntimeFailure
    db, state, agent, prepared = prepare_case(tmp_path, skill=skill)
    before = set(plt.get_fignums())
    def fail(*args, **kwargs):
        raise failure('합성 그림 저장 실패')
    monkeypatch.setattr(Figure, 'savefig', fail)
    from probe.analysis_process import run_tool
    from probe.real_tools import VerifiedAnalysisSkillTool, VisualizationTool
    def injected_save(state, contract, request, expires):
        if request.tool_name == 'stats.run':
            return run_tool(state, contract, request, expires)
        # 그림 도구의 정리 동작과 부모의 실패 전파를 같은 오류로 확인한다.
        tool = VerifiedAnalysisSkillTool if request.tool_name == 'analysis.skill' else VisualizationTool
        return tool(state, contract.contract_id).run(request)
    monkeypatch.setattr('probe.analysis_process.run_tool', injected_save)
    try:
        with pytest.raises(RuntimeFailure):
            asyncio.run(agent.resume(prepared['research_id']))
        assert set(plt.get_fignums()) == before
        assert db.execute("SELECT COUNT(*) FROM staged_mutations WHERE status='COMMITTED'").fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize('case_id,status', [('insufficient_budget','BLOCKED'),('unsupported_repair','NEEDS_REVIEW'),('faulty_checker','NEEDS_REVIEW')])
def test_real_f3p_fault_is_visible_without_claiming_success(tmp_path, monkeypatch, case_id, status):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'qa'))
    from f3p_eval import run_case, CONFIG
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    case = next(c for c in config['cases'] if c['id']==case_id)
    observed = run_case(case, tmp_path, config['dataset_seed'])
    assert observed['fixture_correct'] and observed['committed']==0
    api = WorkbenchAPI(tmp_path/'state.sqlite',tmp_path/'workspace',launch=False)
    try:
        rid = api.store.db.execute('SELECT research_id FROM research_runs').fetchone()[0]
        before = api.store.db.total_changes
        result = project_flow(api,rid,view='recovery',limit=150)
        events = [n for n in result['nodes'] if n['kind']=='runtime_event']
        assert events and events[-1]['status']==status
        assert next(n for n in result['nodes'] if n['kind']=='research')['status']==status
        assert not result['counts']['running']
        assert flow_node(api,rid,events[-1]['id'])['detail']['사유']==case['expected']
        assert api.store.db.total_changes==before
    finally:
        api.close()


@pytest.fixture(scope='module')
def demos(tmp_path_factory):
    folder=tmp_path_factory.mktemp('flow-demos')
    a=run_demo_a(folder/'state.sqlite',folder/'workspace')
    b=run_demo_b(folder/'state.sqlite',folder/'workspace')
    return folder,a['research_id'],b['research_id']


@pytest.fixture
def demo_api(demos):
    folder,a,b=demos
    api=WorkbenchAPI(folder/'state.sqlite',folder/'workspace',launch=False)
    yield api,a,b
    api.close()


def test_list_matches_original_overviews_with_constant_queries(demo_api):
    api,a,b=demo_api;queries=[];api.store.db.set_trace_callback(queries.append)
    rows=api.research_list();api.store.db.set_trace_callback(None)
    assert len(queries)<25
    for r in rows:
        assert {k:r[k] for k in project_overview(api.read._state,r['research_id']).model_dump(mode='json')} == project_overview(api.read._state,r['research_id']).model_dump(mode='json')


@pytest.mark.parametrize('mode,expected',[('ON',True),('LOW_SPEC',True),('OFF',False),('NORMAL',False)])
def test_modes_preserve_depth_flags_and_quality(app,mode,expected):
    configure(app);rid=create(app);before=deepcopy(app.store.run(rid)['snapshot'])
    save_preferences(app.store,{'low_spec_mode':mode,'performance_profile':'DEEP'})
    assert low_spec(app.store) is expected
    assert preferences(app.store)['performance_profile']=='DEEP'
    assert app.store.run(rid)['snapshot']==before
    assert not any(before[k] for k in ('verified_analysis_skills','verification_repair','ridge_arithmetic_check'))


def test_queue_fifo_cap_and_no_budget_reservation(app):
    configure(app);rid=create(app);pool=ResourcePool(app.store)
    first=pool.enqueue(rid,'manager','one',capacity=1,purpose='research')
    second=pool.enqueue(rid,'analysis_planner_worker','one',capacity=1,purpose='research')
    assert not pool.try_start(second)
    assert pool.try_start(first) is True
    assert not pool.try_start(second)
    assert pool.summary(rid)[1]['ahead_count']==1
    assert not app.store.ledger()['requests']
    pool.finish(first);assert pool.try_start(second) is True;pool.finish(second)
    assert [r['status'] for r in pool.rows()]==['COMPLETED','COMPLETED']


def test_global_queue_shared_by_connections_and_processes(app):
    connection,_=configure(app);rid=create(app)
    assert resource_key(connection)==resource_key(connection.model_copy(update={'connection_id':'other','base_url':connection.base_url.replace('/v1','/other')}))
    first=ResourcePool(app.store).enqueue(rid,'manager','shared',capacity=1,purpose='research')
    assert ResourcePool(app.store).try_start(first)
    code="from probe.database import connect;from probe.control_plane import ControlStore;from probe.resource_queue import ResourcePool;import sys;d=connect(sys.argv[1]);p=ResourcePool(ControlStore(d));v=p.enqueue(sys.argv[2],'worker','shared',capacity=1,purpose='research');assert p.try_start(v) is False;d.close()"
    child=subprocess.run([sys.executable,'-c',code,str(app.database),rid],capture_output=True,timeout=15)
    assert child.returncode==0,child.stderr
    with app.store.transaction():ResourcePool(app.store).recover()
    assert ResourcePool(app.store).rows()[1]['status']=='CANCELLED'
    assert ResourcePool(app.store).rows()[0]['status']=='RUNNING'
    ResourcePool(app.store).finish(first)


@pytest.mark.parametrize('command',['PAUSE_REQUESTED','STOP_REQUESTED'])
def test_queue_cancelled_before_reservation(app,command):
    configure(app);rid=create(app)
    app.store.db.execute('UPDATE control_runs SET status=? WHERE research_id=?',(command,rid))
    async def call():
        async with ResourcePool(app.store).lease(rid,'manager','one',timeout=1):
            pytest.fail('중단된 요청 실행')
    with pytest.raises(ControlBoundary):asyncio.run(call())
    assert ResourcePool(app.store).rows()[0]['status']=='CANCELLED'
    assert not app.store.ledger()['requests']


def test_queue_timeout_bounded_and_task_cancel_releases(app):
    configure(app);rid=create(app);pool=ResourcePool(app.store,poll_sec=.005)
    first=pool.enqueue(rid,'manager','one',capacity=1,purpose='research');pool.try_start(first)
    async def wait():
        async with pool.lease(rid,'worker','one',timeout=.02):pass
    with pytest.raises(ControlError,match='RESOURCE_QUEUE_TIMEOUT'):asyncio.run(wait())
    assert pool.rows()[1]['status']=='TIMED_OUT'
    assert pool.rows()[0]['status']=='RUNNING'
    pool.finish(first)
    async def cancelled():
        async with pool.lease(rid,'worker','one'):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):asyncio.run(cancelled())
    assert pool.rows()[-1]['status']=='CANCELLED'


def test_queue_bound_and_live_owner_not_reclaimed(app):
    configure(app);rid=create(app);pool=ResourcePool(app.store,max_pending=1)
    value=pool.enqueue(rid,'manager','one',capacity=1,purpose='research')
    assert pool.try_start(value)
    with app.store.transaction():pool.recover()
    assert pool.rows()[0]['status']=='RUNNING'
    with pytest.raises(ControlError,match='RESOURCE_QUEUE_FULL'):pool.enqueue(rid,'worker','two',capacity=1,purpose='research')
    pool.finish(value)


def test_crashed_owner_projection_preserves_read_only_state(app):
    configure(app);rid=create(app)
    app.store.db.execute("UPDATE control_runs SET status='RUNNING',pid=2147483647 WHERE research_id=?",(rid,))
    before=app.store.db.execute('SELECT COUNT(*) FROM control_audit').fetchone()[0]
    value=project_flow(app,rid)
    assert value['control_status']=='PAUSED' and not value['current_ids']
    assert app.store.run(rid)['status']=='RUNNING'
    assert app.store.db.execute('SELECT COUNT(*) FROM control_audit').fetchone()[0]==before


def test_network_and_server_limits_acquired_atomically(app):
    configure(app);rid=create(app);pool=ResourcePool(app.store)
    a=pool.enqueue(rid,'manager','server-a',capacity=1,purpose='research',shared_limits={'network':1})
    b=pool.enqueue(rid,'worker','server-b',capacity=1,purpose='research',shared_limits={'network':1})
    assert pool.try_start(a) and not pool.try_start(b)
    assert pool.summary(rid)[1]['ahead_count']==1
    pool.finish(a);assert pool.try_start(b);pool.finish(b)


def test_gateway_queue_wait_cancel_does_not_reserve_or_dispatch(app):
    import httpx
    from probe.control_runtime import RoutedGateway
    from probe.providers.normalized import GenerationRequest
    configure(app);rid=create(app);sent=[]
    async def scenario():
        entered,release=asyncio.Event(),asyncio.Event()
        async def response(request):
            sent.append(request);entered.set();await release.wait()
            return httpx.Response(200,json={'id':'offline-response','model':'manual-id','choices':[{'message':{'content':'ok'},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':2}})
        gateway=RoutedGateway(app.store,app.credentials,rid,app.store.run(rid)['snapshot'],client_factory=lambda *_:httpx.AsyncClient(transport=httpx.MockTransport(response)))
        req=GenerationRequest(request_id='first',research_id=rid,role='manager',model_profile_id='m',input_text='공개 모의 입력')
        first=asyncio.create_task(gateway.generate(req));await entered.wait()
        second=asyncio.create_task(gateway.generate(req.model_copy(update={'request_id':'second'})))
        for _ in range(100):
            if len(ResourcePool(app.store).rows())==2:break
            await asyncio.sleep(.005)
        assert len(app.store.ledger(rid)['requests'])==1 and len(sent)==1
        assert ResourcePool(app.store).rows()[1]['status']=='QUEUED'
        app.store.db.execute("UPDATE control_runs SET status='PAUSE_REQUESTED' WHERE research_id=?",(rid,))
        with pytest.raises(ControlBoundary):await second
        release.set();await first
    asyncio.run(scenario())
    assert len(sent)==1 and len(app.store.ledger(rid)['requests'])==1
    assert [v['status'] for v in ResourcePool(app.store).rows()]==['COMPLETED','CANCELLED']


def test_gateway_oom_releases_resource_and_keeps_uncertain_billing(app):
    import httpx
    from probe.control_runtime import RoutedGateway
    from probe.providers.normalized import GenerationRequest,GenerationError
    configure(app);rid=create(app);sent=[]
    def response(request):sent.append(request);return httpx.Response(500,json={'error':{'code':'out_of_memory'}})
    gateway=RoutedGateway(app.store,app.credentials,rid,app.store.run(rid)['snapshot'],client_factory=lambda *_:httpx.AsyncClient(transport=httpx.MockTransport(response)))
    req=GenerationRequest(request_id='oom',research_id=rid,role='manager',model_profile_id='m',input_text='공개 모의 입력')
    with pytest.raises(GenerationError,match='RESOURCE_EXHAUSTED'):asyncio.run(gateway.generate(req))
    assert len(sent)==1 and ResourcePool(app.store).rows()[0]['status']=='CANCELLED'
    assert app.store.ledger(rid)['requests'][0]['status']=='UNRESOLVED'


@pytest.mark.parametrize('mode', ['NORMAL', 'LOW_SPEC'])
def test_distinct_local_servers_share_inference_limit(app, mode):
    import httpx
    from probe.control_runtime import RoutedGateway
    from probe.providers.normalized import GenerationRequest
    connection, model = configure(app)
    save_preferences(app.store, {'low_spec_mode':mode})
    first_rid = create(app)
    second_connection = connection.model_copy(update={'connection_id':'local2', 'base_url':'http://127.0.0.1:1235/v1'})
    second_model = model.model_copy(update={'profile_id':'m2', 'connection_id':'local2'})
    app.store.put('connection', 'local2', second_connection)
    app.store.put('model', 'm2', second_model)
    second_rid = app.create({'title':'두 번째 로컬 연구', 'question':'Association?', 'source_relative':'data.csv',
        'routing':{role:'m2' for role in ('manager','experiment_coordinator','analysis_planner_worker','verification_coordinator')},
        'egress':'selected'})['research_id']
    sent, active, peak = [], 0, 0
    async def scenario():
        nonlocal active, peak
        entered, release = asyncio.Event(), asyncio.Event()
        async def response(request):
            nonlocal active, peak
            sent.append(request);active += 1;peak = max(peak, active);entered.set()
            try:
                await release.wait()
                return httpx.Response(200,json={'id':'offline-local', 'model':'manual-id',
                    'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':10,'completion_tokens':2}})
            finally:
                active -= 1
        gateways = [RoutedGateway(app.store,app.credentials,rid,app.store.run(rid)['snapshot'],
            client_factory=lambda *_:httpx.AsyncClient(transport=httpx.MockTransport(response)))
            for rid in (first_rid,second_rid)]
        request = GenerationRequest(request_id='local-first',research_id=first_rid,role='manager',
            model_profile_id='m',input_text='공개 모의 입력')
        first = asyncio.create_task(gateways[0].generate(request));await entered.wait()
        second = asyncio.create_task(gateways[1].generate(request.model_copy(update={
            'request_id':'local-second', 'research_id':second_rid, 'model_profile_id':'m2'})))
        try:
            for _ in range(100):
                if ResourcePool(app.store).rows(second_rid):break
                await asyncio.sleep(.005)
            assert ResourcePool(app.store).rows(second_rid)[0]['status']=='QUEUED'
            assert len(app.store.ledger()['requests'])==1 and len(sent)==1
            assert ResourcePool(app.store).summary(second_rid)[0]['ahead_count']==1
        finally:
            release.set()
            await asyncio.gather(first, second)
    asyncio.run(scenario())
    assert peak==1 and len(sent)==2 and len(app.store.ledger()['requests'])==2
    assert all(row['status']=='COMPLETED' for row in ResourcePool(app.store).rows())


@pytest.mark.parametrize('status,body,code',[(500,{'error':{'code':'oom'}},'RESOURCE_EXHAUSTED'),(503,{'error':{'message':'CUDA out of memory'}},'RESOURCE_EXHAUSTED'),(429,{'error':{'code':'quota'}},'SPEND_LIMIT'),(500,{'error':{'message':'unavailable'}},'PROVIDER_UNAVAILABLE')])
def test_resource_error_classification_keeps_distinct_failures(status,body,code):
    assert normalized_error(status,body)==code


@pytest.mark.parametrize('view',['current','all','recovery','verification'])
def test_flow_reads_real_relations_without_writes_or_hidden_pages(demo_api,monkeypatch,view):
    api,rid,b=demo_api
    tables=('state_events','research_actions','runtime_events','spend_ledger','control_audit')
    before={t:api.store.db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in tables}
    monkeypatch.setattr(api,'repair_projection',lambda _:pytest.fail('선택 전 복구 원문 조회'))
    result=project_flow(api,rid,view=view,limit=150)
    assert result['read_only'] and result['plan_progress'] is None and result['position_semantics']=='LOGICAL_DEPENDENCY'
    ids={n['id'] for n in result['nodes']}
    assert all(e['source'] in ids and e['target'] in ids for e in result['edges'])
    assert before=={t:api.store.db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in tables}
    assert any(n['kind']=='tool' for n in project_flow(api,rid,view='all',limit=150)['nodes'])


def test_current_path_includes_accepted_evidence_and_report(demo_api):
    api,rid,b=demo_api;v=project_flow(api,rid,limit=150)
    assert any(n['kind']=='conclusion' and n['on_current_path'] for n in v['nodes'])
    assert any(n['kind']=='evidence' and n['on_current_path'] for n in v['nodes'])
    assert any(n['kind']=='report' for n in project_flow(api,rid,view='all',limit=150)['nodes'])
    assert not v['current_ids']


def test_demo_b_keeps_invalidated_history(demo_api):
    api,a,rid=demo_api;v=project_flow(api,rid,view='all',limit=150)
    assert any(n['history']=='invalidated' for n in v['nodes'])
    assert any(n['kind']=='experiment' and n['status']=='INVALIDATED' for n in v['nodes'])


@pytest.mark.parametrize('polarity,relation',[('SUPPORT','supports'),('CONTRADICT','contradicts'),('NEUTRAL','uses')])
def test_literature_polarity_never_turns_contradiction_into_support(demo_api,polarity,relation):
    api,rid,b=demo_api;db=api.store.db
    row=db.execute("SELECT evidence_id,polarity,target_hypothesis_id FROM evidence WHERE research_id=? AND source_type='LITERATURE' LIMIT 1",(rid,)).fetchone()
    hypothesis=db.execute('SELECT hypothesis_id FROM hypotheses WHERE research_id=? LIMIT 1',(rid,)).fetchone()[0]
    db.execute('UPDATE evidence SET polarity=?,target_hypothesis_id=? WHERE evidence_id=?',(polarity,hypothesis,row[0]))
    try:
        v=project_flow(api,rid,view='all',limit=150)
        assert any(e['source']=='evidence:'+row[0] and e['target']=='hypothesis:'+hypothesis and e['relation']==relation for e in v['edges'])
    finally:db.execute('UPDATE evidence SET polarity=?,target_hypothesis_id=? WHERE evidence_id=?',(row[1],row[2],row[0]))


def test_verification_pages_include_existing_literature_checks(demo_api):
    api,rid,b=demo_api
    value=api.request('GET',f'/api/control/research/{rid}/items?kind=verification&limit=100').body
    assert len(value['items'])==len(project_verification(api.read._state,rid))
    assert any(r['node_kind']=='evidence' for r in value['items'])


@pytest.mark.parametrize('kind',['evidence','experiments','verification'])
def test_pages_bounded_and_inspection_matches_full_projection(demo_api,kind):
    api,rid,b=demo_api;r=api.request('GET',f'/api/control/research/{rid}/items?kind={kind}&limit=1')
    assert r.status==200 and len(r.body['items'])==1 and r.body['next_offset']==1
    item=r.body['items'][0];key=item.get('evidence_id') or item.get('experiment_id') or item.get('item_id')
    v=flow_node(api,rid,{'experiments':'experiment','verification':'verification'}.get(kind,kind)+':'+key)
    assert v['detail']
    assert not {'input_text','system_instructions','request_json','contract_json','messages'} & set(v['detail'])


@pytest.mark.parametrize('path',['flow?limit=151','flow?view=invalid','flow?offset=-1','items?kind=evidence&limit=0','items?kind=invalid','flow-node?id=artifact:outside'])
def test_invalid_pages_and_foreign_items_denied(demo_api,path):
    api,rid,b=demo_api
    assert api.request('GET',f'/api/control/research/{rid}/'+path).status!=200


def test_flow_structure_stable_when_only_status_changes(app):
    configure(app);rid=create(app);db=app.store.db
    db.execute("INSERT INTO hypotheses VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",('H-one',rid,None,'가설','근거','PROPOSED','{}','owner','all','now','now',1))
    first=project_flow(app,rid,view='all')
    db.execute("UPDATE hypotheses SET status='ACTIVE' WHERE hypothesis_id='H-one'")
    second=project_flow(app,rid,view='all')
    assert first['layout_revision']==second['layout_revision']
    assert first['nodes']!=second['nodes']


def test_large_graph_paged_and_selection_neighbors(app):
    configure(app);rid=create(app);db=app.store.db
    with app.store.transaction():
        for i in range(120):db.execute('INSERT INTO hypotheses VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(f'H-{i}',rid,None,f'합성 가설 {i}','합성','PROPOSED','{}','owner','all','now','now',1))
        for i in range(1,120):
            for parent in range(max(0,i-3),i):db.execute('INSERT INTO entity_edges VALUES(?,?,?,?,?,?,?,?,?)',(f'e-{i}-{parent}',rid,'hypothesis',f'H-{parent}','depends_on','hypothesis',f'H-{i}','now','ACTIVE'))
    first=project_flow(app,rid,view='all',limit=25)
    assert first['total_nodes']>=121 and len(first['nodes'])==25 and first['next_offset']==25
    all_ids=set();offset=0
    while offset is not None:
        p=project_flow(app,rid,view='all',limit=25,offset=offset);all_ids.update(n['id'] for n in p['nodes']);offset=p['next_offset']
    assert len(all_ids)==first['total_nodes']
    selected=project_flow(app,rid,selected='hypothesis:H-10',limit=150)
    assert any(n['id']=='hypothesis:H-10' and n['selected_neighbor'] for n in selected['nodes'])


def test_ledger_pagination_preserves_full_exposure(app):
    configure(app);rid=create(app)
    for i in range(4):
        reservation=app.store.reserve(rid=rid,connection='local',model='m',role='manager',purpose='test',bound=.001,run_limit=1,monthly_limit=10,request_limit=1,attempts=10,revision='offline')
        if i<3:app.store.transition(reservation,'RELEASED')
    full=app.store.ledger(rid);page=app.store.ledger(rid,limit=1)
    for key in ('spent','reserved','unresolved','available','monthly_exposure'):assert page[key]==full[key]
    assert len(page['requests'])==1 and page['next_offset']==1


def test_summary_activity_lazy_and_private_fields_excluded(app):
    from probe.resource_policy import activity_detail
    configure(app);rid=create(app)
    app.read._state.runtime_event(rid,'PRIVATE_FIELD_PROBE',{'contract_id':'c','input_text':'private-input','hidden_answer':'private-oracle','nested':{'cot':'private-thought','visible':'참조'}})
    page=app.request('GET',f'/api/control/research/{rid}/activity?summary=1&limit=1').body
    assert page['items'][0]['details']=={}
    record=activity_detail(app.read._state,rid,page['items'][0]['id'])
    text=json.dumps(record);assert 'private-' not in text and record['nested']['visible']=='참조'


def test_flow_does_not_return_tool_prompts_or_hidden_oracle(demo_api):
    api,rid,b=demo_api;db=api.store.db
    row=db.execute('SELECT tc.request_id,tc.request_json,tc.result_json FROM tool_calls tc JOIN agent_runs ar USING(agent_run_id) JOIN contracts c USING(contract_id) WHERE c.research_id=? LIMIT 1',(rid,)).fetchone()
    request=json.loads(row['request_json']);request['private_prompt']='private-input-marker'
    output=json.loads(row['result_json']);output['hidden_answer']='private-oracle-marker';output['reasoning_content']='private-cot-marker'
    db.execute('UPDATE tool_calls SET request_json=?,result_json=? WHERE request_id=?',(json.dumps(request),json.dumps(output),row['request_id']))
    try:
        assert 'private-' not in json.dumps(project_flow(api,rid,view='all'))
        assert 'private-' not in json.dumps(flow_node(api,rid,'tool:'+row['request_id']))
    finally:db.execute('UPDATE tool_calls SET request_json=?,result_json=? WHERE request_id=?',(row['request_json'],row['result_json'],row['request_id']))


@pytest.mark.parametrize('endpoint',['flow','flow-node?id=research:unknown','items?kind=evidence','resources','activity-event?id=runtime:1'])
def test_flow_routes_require_owner_session(app,endpoint):
    from threading import Thread
    import httpx
    from probe.workbench import create_server
    configure(app);rid=create(app);server=create_server(app)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        response=httpx.get(f'http://127.0.0.1:{server.server_port}/api/control/research/{rid}/{endpoint}',trust_env=False)
        assert response.status_code==401 and response.json()['error']=='OWNER_SESSION_REQUIRED'
        response=httpx.get(f'http://127.0.0.1:{server.server_port}/api/control/research/{rid}/{endpoint}',headers={'Origin':'http://outside.invalid'},trust_env=False)
        assert response.status_code==403
    finally:server.shutdown();thread.join();server.server_close()


def test_normal_and_low_spec_offline_scientific_equivalence(app):
    configure(app);results=[]
    for mode in ('NORMAL','LOW_SPEC'):
        save_preferences(app.store,{'low_spec_mode':mode});rid=create(app)
        app.command(rid,'start',{'idempotency_key':mode+'-offline','expected_version':0})
        asyncio.run(execute(app.database,app.workspace,rid,provider_factory=lambda *_:FakeProvider(fake_replies())))
        assert app.store.run(rid)['status']=='COMPLETED'
        result=project_experiments(app.read._state,rid)
        checks=project_verification(app.read._state,rid)
        results.append(([r.method for r in result],[[ (c['check_id'],c['passed']) for c in r.automatic_checks] for r in checks],app.store.db.execute('SELECT COUNT(*) FROM state_events WHERE research_id=?',(rid,)).fetchone()[0]))
    assert results[0]==results[1]


def test_f3p_repair_branch_keeps_original_and_required_revalidation(tmp_path):
    db,state,agent,prepared=prepare_case(tmp_path);corrupt_first_output(state)
    assert asyncio.run(agent.resume(prepared['research_id']))['verdict']=='PASS';db.close()
    api=WorkbenchAPI(tmp_path/'state.sqlite',tmp_path/'workspace',launch=False)
    try:
        rid=prepared['research_id'];v=project_flow(api,rid,view='recovery',limit=150)
        assert any(e['relation']=='supersedes' for e in v['edges'])
        assert any(e['relation']=='repairs' for e in v['edges'])
        assert any(n['history']=='superseded' for n in v['nodes'])
        revalidation=next(n for n in v['nodes'] if n['title']=='필수 재검증')
        assert revalidation['status']=='COMPLETED'
        detail=flow_node(api,rid,revalidation['id'])
        assert not detail['verifier_result']['남은 검사'] and detail['verifier_result']['최종 상태']=='VERIFIED'
        row=api.store.db.execute('SELECT relative_path FROM artifacts WHERE artifact_id=?',(revalidation['ref'],)).fetchone()
        path=api.read.workspace.path(rid,row[0]);old=path.read_bytes()
        try:
            path.write_bytes(b'corrupted')
            bad=project_flow(api,rid,view='recovery',limit=150)
            assert next(n for n in bad['nodes'] if n['id']==revalidation['id'])['status']=='NEEDS_REVIEW'
        finally:path.write_bytes(old)
        failure=next(n for n in v['nodes'] if n['title']=='검증 실패 증거')
        row=api.store.db.execute('SELECT relative_path FROM artifacts WHERE artifact_id=?',(failure['ref'],)).fetchone()
        failure_path=api.read.workspace.path(rid,row[0]);old=failure_path.read_bytes()
        try:
            failure_path.write_bytes(b'changed-failure')
            bad=project_flow(api,rid,view='recovery',limit=150)
            assert next(n for n in bad['nodes'] if n['id']==revalidation['id'])['status']=='NEEDS_REVIEW'
        finally:failure_path.write_bytes(old)
    finally:api.close()
