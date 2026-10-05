"""실제 자식 종료·원문 재시도·복구·판정·비용 차단의 실패 경계를 검사한다."""
import asyncio
from copy import deepcopy
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from probe.analysis_process import run_tool
from probe.control_plane import ControlBoundary, ControlError, ControlStore, process_alive
from probe.database import transaction, to_json
from probe.research_report import rewrite_report, recover_report_rewrites, _save, report_record
from probe.report_maintenance import run_batch
from probe.report_publication import report_root
from probe.resource_queue import ResourcePool
from probe.schemas import ContextRef, RefType, new_id
from probe.service import ContractViolationError
from test_autonomous_loop import SCORE
from test_real_tools import real_context, imported, request
from test_workbench import app, configure
from test_research_report_flow import rig, run, request as research_request


@pytest.mark.parametrize('boundary', ['deadline', 'stop'])
def test_slow_calculation_is_dead_before_its_resource_is_released(real_context, monkeypatch, boundary):
    _, state, _, _, rid, contract, task_id = real_context
    dataset = imported(real_context)
    store = ControlStore(state._db)
    pool = ResourcePool(store)
    original = subprocess.Popen
    children, stopped = [], threading.Event()
    def slow(*args, **kwargs):
        process = original([sys.executable, '-B', '-X', 'utf8', '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr('probe.analysis_process.subprocess.Popen', slow)
    if boundary == 'stop':
        def check():
            if stopped.is_set():
                raise ControlBoundary('STOP_REQUESTED')
        state.analysis_boundary = check
        threading.Timer(.2, stopped.set).start()
    req = request(rid, task_id, 'stats.run', {'dataset_id': dataset, 'method': 'pearson_correlation', 'variables': {'x':'temperature', 'y':'growth'}})
    expected = ControlBoundary if boundary == 'stop' else ControlError
    with pytest.raises(expected):
        with pool.sync_lease(rid, 'worker'):
            run_tool(state, contract, req, time.monotonic() + (10 if boundary == 'stop' else .3))
    assert children and children[0].poll() is not None and not process_alive(children[0].pid)
    assert pool.rows(rid)[-1]['status'] == 'CANCELLED'
    assert store.configs('analysis_process')[-1]['status'] == 'CANCELLED'


def test_failed_process_termination_keeps_resource_and_blocks_next_calculation(real_context, monkeypatch):
    _, state, _, _, rid, contract, task_id = real_context
    dataset = imported(real_context)
    store, children = ControlStore(state._db), []
    pool = ResourcePool(store)
    original = subprocess.Popen
    def slow(*args, **kwargs):
        child = original([sys.executable, '-B', '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(child)
        return child
    def fail(process):
        raise ControlError('ANALYSIS_TERMINATION_FAILED')
    monkeypatch.setattr('probe.analysis_process.subprocess.Popen', slow)
    monkeypatch.setattr('probe.analysis_process.terminate', fail)
    req = request(rid, task_id, 'stats.run', {'dataset_id':dataset,'method':'pearson_correlation','variables':{'x':'temperature','y':'growth'}})
    try:
        with pytest.raises(ControlError, match='ANALYSIS_TERMINATION_FAILED'):
            with pool.sync_lease(rid, 'worker'):
                run_tool(state, contract, req, time.monotonic() + .2)
        assert pool.rows(rid)[-1]['status'] == 'RUNNING'
        with pytest.raises(ControlError, match='ANALYSIS_TERMINATION_FAILED'):
            pool.enqueue(rid, 'worker', 'heavy-analysis', capacity=1, purpose='research')
    finally:
        for child in children:
            child.kill()
            child.wait(timeout=3)


@pytest.mark.parametrize('estimate,p_value,expected', [(1,.001,'support'),(-1,.001,'contradict'),(1,.9,'neutral'),(0,.001,'neutral')])
def test_hypothesis_criterion_is_fixed_before_analysis_and_controls_direction(cycle, estimate, p_value, expected):
    _, state, rid, contract, _ = cycle
    proposal = {'statement':'온도와 생장에 양의 연관성이 있다.', 'rationale':'사전에 방향을 고정합니다.', 'score':SCORE,
        'criterion':{'method':'pearson_correlation','variables':{'x':'temperature','y':'growth'},'expected_direction':'positive','alpha':.05}}
    hypothesis = state.create_hypothesis(rid, proposal, created_by='manager')
    linked = contract.model_copy(update={'inputs':[ContextRef(type=RefType.hypothesis,id=hypothesis)]})
    assert state.analysis_polarity(rid, linked, 'pearson_correlation', {'x':'temperature','y':'growth'}, {'estimate':estimate,'p_value':p_value}) == expected
    assert state.analysis_polarity(rid, linked, 'spearman_correlation', {'x':'temperature','y':'growth'}, {'estimate':1,'p_value':.001}) == 'neutral'
    other = state.create_hypothesis(rid, {k:v for k,v in proposal.items() if k!='criterion'} | {'statement':'방향 기준 없는 가설'}, created_by='manager')
    linked.inputs = [ContextRef(type=RefType.hypothesis,id=other)]
    assert state.analysis_polarity(rid, linked, 'pearson_correlation', {'x':'temperature','y':'growth'}, {'estimate':1,'p_value':.001}) == 'neutral'


@pytest.mark.parametrize('status,polarity,relation,allowed', [('REJECTED','contradict','contradicts',True), ('REJECTED','support','supports',False),
    ('SUPPORTED','support','supports',True), ('SUPPORTED','contradict','contradicts',False), ('INCONCLUSIVE','neutral','depends_on',True)])
def test_hypothesis_transition_requires_correct_verified_evidence(cycle,status,polarity,relation,allowed):
    db, state, rid, _, _ = cycle
    hypothesis = state.create_hypothesis(rid, {'statement':'검증할 가설', 'rationale':'사전 계획', 'score':SCORE}, created_by='manager')
    for phase in ('SHORTLISTED','ACTIVE'):
        state.set_hypothesis_status(rid,hypothesis,phase,decided_by='manager',rationale='계획에 따라 활성화')
    refs=[]
    for _ in range(2):
        identity=new_id('EV')
        db.execute("INSERT INTO evidence(evidence_id,research_id,source_id,payload_json,claim,polarity,status) VALUES(?,?,?,'{}',?,?, 'VERIFIED')", (identity,rid,'fixture-source', '검증한 근거',polarity))
        ref=ContextRef(type=RefType.evidence,id=identity)
        state.add_entity_edge(rid,ContextRef(type=RefType.hypothesis,id=hypothesis),relation,ref)
        refs.append(ref)
    action=lambda: state.set_hypothesis_status(rid,hypothesis,status,decided_by='manager',rationale='확인한 근거로 판정',evidence_refs=refs)
    if allowed:
        action()
        assert db.execute('SELECT status FROM hypotheses WHERE hypothesis_id=?',(hypothesis,)).fetchone()[0] == status
    else:
        with pytest.raises(ContractViolationError):
            action()


def test_worker_launch_failure_updates_command_and_run_together(app,monkeypatch):
    configure(app)
    created=app.create(research_request(search_policy='DISABLED',search_required=False))
    rid=created['research_id']
    app.launch=True
    def failure(*args,**kwargs):
        raise OSError('주입한 생성 오류')
    monkeypatch.setattr('probe.workbench.subprocess.Popen',failure)
    with pytest.raises(ControlError,match='WORKER_START_FAILED'):
        app.command(rid,'start',{'expected_version':0,'idempotency_key':'launch-failure'})
    row=app.store.run(rid)
    command=json.loads(app.store.db.execute("SELECT result FROM control_commands WHERE key='launch-failure'").fetchone()[0])
    assert row['status']==command['status']=='FAILED' and row['pid'] is None
    assert not app.store.ledger()['requests']


def test_worker_termination_failure_is_saved_and_blocks_new_jobs(app, monkeypatch):
    from io import BytesIO
    configure(app)
    rid = app.create(research_request(search_policy='DISABLED', search_required=False))['research_id']
    app.launch = True
    child = SimpleNamespace(pid=os.getpid(), stdout=BytesIO(b'wrong-ready-token\n'), poll=lambda: None,
        terminate=lambda: (_ for _ in ()).throw(PermissionError('주입한 종료 오류')))
    monkeypatch.setattr('probe.workbench.subprocess.Popen', lambda *args, **kwargs: child)
    with pytest.raises(ControlError, match='ANALYSIS_TERMINATION_FAILED'):
        app.command(rid, 'start', {'expected_version': 0, 'idempotency_key': 'terminate-failure'})
    command = json.loads(app.store.db.execute("SELECT result FROM control_commands WHERE key='terminate-failure'").fetchone()[0])
    assert app.store.run(rid)['status'] == command['status'] == 'FAILED'
    assert app.store.config('analysis_process', 'worker:' + rid)['status'] == 'TERMINATION_FAILED'
    with pytest.raises(ControlError, match='ANALYSIS_TERMINATION_FAILED'):
        ResourcePool(app.store).enqueue(rid, 'STATISTICS', 'local-cpu', capacity=1, purpose='research')


def test_dead_owner_cannot_release_resource_with_living_child(app):
    from probe.analysis_process import _process_record
    configure(app)
    rid = app.create(research_request(search_policy='DISABLED', search_required=False))['research_id']
    pool = ResourcePool(app.store)
    job = pool.enqueue(rid, 'worker', 'heavy-analysis', capacity=1, purpose='research')
    assert pool.try_start(job)
    with app.store.transaction():
        pool._write({**app.store.config('resource_job', job['id']), 'pid': -1})
        _process_record(app.read._state, 'orphaned-child', {'pid': os.getpid(), 'research_id': rid, 'status': 'RUNNING'})
        pool.recover()
    assert pool.rows(rid)[-1]['status'] == 'RUNNING'
    assert app.store.config('analysis_process', 'orphaned-child')['status'] == 'TERMINATION_FAILED'
    with pytest.raises(ControlError, match='ANALYSIS_TERMINATION_FAILED'):
        pool.enqueue(rid, 'worker', 'heavy-analysis', capacity=1, purpose='research')


def test_research_list_does_not_repeat_startup_recovery(app, monkeypatch):
    def forbidden():
        raise AssertionError('요청 도중 초기 복구를 실행하면 안 됩니다.')
    monkeypatch.setattr(app, '_recover', forbidden)
    assert app.request('GET', '/api/control/research').status == 200


def test_failed_container_removal_blocks_even_after_docker_client_exits(tmp_path, monkeypatch):
    from io import BytesIO
    from probe.sandbox_io import execute_container, SandboxFailure
    record = json.dumps({'exit_code': 0, 'error': None, 'stdout': '', 'stderr': ''}).encode('utf-8', errors='strict') + b'\n'
    child = SimpleNamespace(stdout=BytesIO(record), stdin=BytesIO(), wait=lambda **kwargs: 0, poll=lambda: 0)
    monkeypatch.setattr('probe.sandbox_io.subprocess.Popen', lambda *args, **kwargs: child)
    monkeypatch.setattr('probe.sandbox_io.subprocess.run', lambda *args, **kwargs: subprocess.CompletedProcess([], 1, b'', b''))
    monkeypatch.setattr('probe.sandbox_io._binary_command', lambda *args: b'')
    monkeypatch.setattr('probe.sandbox_io.collect_tar', lambda *args: None)
    with pytest.raises(SandboxFailure, match='ANALYSIS_TERMINATION_FAILED'):
        execute_container(['docker', 'run', '--name', 'fixed-offline-fixture'], tmp_path, 10)


@pytest.mark.parametrize('fault', ['pointer-write', 'input-version'])
def test_publication_failure_keeps_previous_pointer(app, monkeypatch, fault):
    import shutil
    from probe.report_publication import new_revision, publish_revision
    from probe.release import validate_report_snapshot
    gateway, _, _ = rig(app, monkeypatch)
    rid = run(app, gateway)
    previous = report_root(app.read._state, rid)
    pointer = app.read._state.workspace.path(rid, 'research_output/current.json')
    original = pointer.read_bytes()
    candidate = new_revision(app.read._state, rid)
    shutil.copytree(previous, candidate, dirs_exist_ok=True)
    if fault == 'pointer-write':
        def fail(*args):
            raise OSError('주입한 포인터 교체 오류')
        monkeypatch.setattr('probe.report_publication.os.replace', fail)
        error = OSError
    else:
        def changed(state, research_id):
            manifest = validate_report_snapshot(state, research_id)
            state._db.execute('UPDATE research_runs SET state_version=state_version+1 WHERE research_id=?', (research_id,))
            return manifest
        monkeypatch.setattr('probe.release.validate_report_snapshot', changed)
        error = ControlError
    with pytest.raises(error):
        publish_revision(app.read._state, rid, candidate)
    assert pointer.read_bytes() == original and report_root(app.read._state, rid) == previous


def test_legacy_report_is_history_only_and_never_current(app, monkeypatch):
    gateway, calls, _ = rig(app, monkeypatch)
    rid = run(app, gateway)
    prior = app.store.config('ai_report', rid)
    prior.pop('validation_version')
    _save(app.store, 'ai_report', rid, prior)
    count = len(calls)
    assert not report_record(app.read._state, rid, validate=False)['current']
    with pytest.raises(ControlError, match='REPORT_STALE'):
        report_record(app.read._state, rid)
    assert len(calls) == count


def test_completed_staged_revision_is_recovered_without_another_provider_call(app,monkeypatch):
    gateway,calls,_=rig(app,monkeypatch)
    rid=run(app,gateway)
    old=report_root(app.read._state,rid)
    pointer=app.read._state.workspace.path(rid,'research_output/current.json')
    before=pointer.read_bytes()
    def interrupted(*args):
        raise ControlBoundary('PROCESS_EXIT')
    monkeypatch.setattr('probe.report_publication.publish_revision',interrupted)
    body={'idempotency_key':'publish-before-exit','expected_version':app.store.run(rid)['version'],'state_version':app.read._state.state_version(rid)}
    with pytest.raises(ControlBoundary):
        asyncio.run(rewrite_report(app,rid,body,provider_factory=gateway))
    assert pointer.read_bytes()==before and old.exists()
    item=app.store.config('report_rewrite',rid)
    # 강제 종료는 finally를 실행하지 않으므로 저장된 실행 소유자를 복구 대상으로 만든다.
    roots=sorted(app.read._state.workspace.path(rid,'report_revisions').iterdir(),key=lambda p:p.stat().st_mtime_ns)
    _save(app.store,'report_rewrite',rid,{**item,'status':'RUNNING','owner_pid':None,'candidate_revision':roots[-1].name,
        'input_fingerprint':report_record(app.read._state,rid)['input_fingerprint']})
    monkeypatch.undo()
    count=len(calls)
    recover_report_rewrites(app)
    assert app.store.config('report_command',body['idempotency_key'])['status']=='READY'
    assert report_root(app.read._state,rid)==roots[-1] and len(calls)==count


@pytest.mark.parametrize('reason', ['budget','unsettled'])
def test_batch_admission_blocks_without_model_or_ledger_reset(app,monkeypatch,reason):
    gateway,calls,_=rig(app,monkeypatch)
    rid=run(app,gateway)
    if reason=='budget':
        from probe.control_plane import PriceRecord
        from probe.schemas import utc_now
        snapshot=app.store.run(rid)['snapshot']
        snapshot['run_limit_usd']='0'
        snapshot['models']['manager']['local_api_unmetered']=False
        snapshot['models']['manager']['price']=PriceRecord(input_per_million='1',output_per_million='2',
            checked_at=utc_now(),source='고정 오프라인 가격',revision='test',owner_verified=True).model_dump(mode='json')
        app.store.db.execute("UPDATE control_runs SET snapshot=? WHERE research_id=?",(to_json(snapshot),rid))
    else:
        app.store.db.execute("UPDATE spend_ledger SET status='UNRESOLVED' WHERE research_id=? AND id=(SELECT id FROM spend_ledger WHERE research_id=? LIMIT 1)",(rid,rid))
    prior=[dict(r) for r in app.store.db.execute('SELECT * FROM spend_ledger')]
    count=len(calls)
    batch=asyncio.run(run_batch(app,provider_factory=gateway))
    assert batch['status']=='PARTIAL' and batch['items'][0]['status']=='BLOCKED'
    assert len(calls)==count and [dict(r) for r in app.store.db.execute('SELECT * FROM spend_ledger')]==prior


def test_same_batch_cannot_run_concurrently(app,monkeypatch):
    gateway,_,_=rig(app,monkeypatch)
    run(app,gateway)
    entered=asyncio.Event()
    release=asyncio.Event()
    async def hold(*args,**kwargs):
        entered.set()
        await release.wait()
        raise asyncio.CancelledError()
    monkeypatch.setattr('probe.report_maintenance.rewrite_report',hold)
    async def parallel():
        first=asyncio.create_task(run_batch(app,provider_factory=gateway))
        await entered.wait()
        with pytest.raises(ControlError,match='REPORT_BATCH_IN_PROGRESS'):
            await run_batch(app,provider_factory=gateway)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
    asyncio.run(parallel())
    assert app.store.configs('report_batch')[0]['status']=='INTERRUPTED'


def test_cached_pdf_extraction_retries_twice_without_redownloading(real_context,monkeypatch):
    from probe.source_documents import acquire_document, document_record
    from probe.scholarly import NormalizedSource
    _,state,_,_,rid,_,_=real_context
    store=ControlStore(state._db)
    store.run=lambda _: {'status':'COMPLETED','started_at':None}
    source,_=state.upsert_source(rid,NormalizedSource(title='Temperature study',provider='scholarly.fake',provider_ids={'oa_pdf_url':'https://papers.example.org/study.pdf'}))
    downloads,attempts=[],[]
    async def fetch(url,client,headers=None):
        downloads.append(url)
        return b'%PDF-fixed',url
    def failed(path):
        attempts.append(path)
        raise ControlError('PDF_EXTRACTION_TIMEOUT')
    monkeypatch.setattr('probe.source_documents.extract_pdf',failed)
    policy=SimpleNamespace(rid=rid,store=store,snapshot={'fulltext_enabled':True},before_dispatch=None,
        approve_document_url=lambda *args: None,fetch_document=fetch,protected=lambda: [])
    for _ in range(4):
        previous=document_record(state,rid,source)
        if previous:
            state.save_source_document(rid,source,{**previous,'retry_at':'1970-01-01T00:00:00+00:00'})
        record=asyncio.run(acquire_document(state,policy,source))
    assert len(downloads)==1 and len(attempts)==3 and record['extraction_attempts']==3


def test_extreme_endpoint_is_neutral_at_predeclared_significance(cycle):
    from probe.real_schemas import StatsArgs
    from probe.real_tools import _compute_stats
    _,state,rid,contract,_=cycle
    proposal={'statement':'양의 상관 가설','rationale':'분석 전 기준','score':SCORE,'criterion':{'method':'pearson_correlation',
        'variables':{'x':'temperature','y':'growth'},'expected_direction':'positive','alpha':.05}}
    hypothesis=state.create_hypothesis(rid,proposal,created_by='manager')
    linked=contract.model_copy(update={'inputs':[ContextRef(type=RefType.hypothesis,id=hypothesis)]})
    rows=[{'temperature':str(v),'growth':str(v if v<12 else 1000)} for v in range(1,13)]
    result=_compute_stats(StatsArgs(dataset_id='D',method='pearson_correlation',variables={'x':'temperature','y':'growth'}),rows)
    assert result['estimate']>0 and result['p_value']>.05
    assert state.analysis_polarity(rid,linked,'pearson_correlation',{'x':'temperature','y':'growth'},result)=='neutral'
