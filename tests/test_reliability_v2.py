"""개선 계획의 저장·수치·자원·발행·배치 실패 경계를 검사한다."""
import asyncio
import base64
from copy import deepcopy
import gzip
import io
import json
from pathlib import Path
import socket
import sqlite3
import tarfile
import threading
import time

import httpx
import numpy as np
import pytest

from probe.analysis_skills import _ridge_fit, SkillApplicabilityError
from probe.bounded_http import bounded_bytes, ResponseLimitError
from probe.control_plane import ControlError, ControlStore
from probe.database import transaction
from probe.final_report import _trusted_stat, export_final_report, ReportValidationError
from probe.providers.streams import decode_stream
from probe.real_schemas import StatsArgs
from probe.real_tools import _compute_stats
from probe.research_report import ReportDraft, ReportNumber, validate_draft, rewrite_report, _save
from probe.report_publication import report_root
from probe.report_maintenance import run_batch
from probe.release import export_release, ReleaseExportError
from probe.sandbox_io import decode_console, collect_tar, SandboxFailure
from probe.web_sources import extract_html
from test_real_tools import real_context, CSV, imported, request
from test_workbench import app, configure
from test_research_report_flow import rig, run


def test_nested_failure_rolls_back_its_own_savepoint(cycle):
    db, state, rid, _, _ = cycle
    with transaction(db):
        db.execute("UPDATE research_runs SET goal='외부 저장' WHERE research_id=?", (rid,))
        with pytest.raises(RuntimeError):
            with transaction(db):
                db.execute("UPDATE research_runs SET goal='중간 실패' WHERE research_id=?", (rid,))
                raise RuntimeError("주입한 오류")
        assert db.execute("SELECT goal FROM research_runs WHERE research_id=?", (rid,)).fetchone()[0] == '외부 저장'
    assert not db.in_transaction


def test_duplicate_contract_does_not_leave_partial_task(cycle):
    db, state, _, contract, _ = cycle
    before = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        state.issue_contract(contract)
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
    assert not db.in_transaction


def test_commit_middle_error_keeps_all_scientific_records_pending(cycle):
    db, state, rid, _, payload = cycle
    mutation = state.stage(payload)
    state.verify(mutation)
    db.execute("CREATE TEMP TRIGGER fail_event BEFORE INSERT ON state_events BEGIN SELECT RAISE(ABORT,'검사 오류'); END")
    with pytest.raises(sqlite3.IntegrityError):
        state.commit(mutation)
    assert state.mutation_status(mutation) == 'PENDING'
    assert state.state_version(rid) == 0
    assert state.artifact(payload.agent_result.result_id) is None


def _verified(state):
    from probe.real_runtime import execute_real_tools
    payload, ids = execute_real_tools(state, CSV)
    mutation = state.stage(payload)
    state.verify(mutation)
    state.commit(mutation)
    return payload, ids


def test_dataset_invalidation_cascades_without_restarting_finished_research(real_context):
    db, state, _, _, _, _, _ = real_context
    payload, ids = _verified(state)
    rid = ids['research_id']
    state.stop_research(rid, 'USER_STOP')
    state.invalidate_dataset(rid, ids['dataset_id'], '원자료 무효화 검사')
    assert db.execute("SELECT run_status FROM research_runs WHERE research_id=?", (rid,)).fetchone()[0] == 'STOPPED'
    assert db.execute("SELECT status FROM experiments WHERE experiment_id=?", (payload.scientific.experiment_id,)).fetchone()[0] == 'INVALIDATED'
    assert db.execute("SELECT status FROM evidence WHERE evidence_id=?", (payload.scientific.evidence_id,)).fetchone()[0] == 'INVALIDATED'
    with pytest.raises(ReportValidationError):
        _trusted_stat(state, rid, payload.scientific.experiment_id, 'n')


def test_changed_dataset_is_rechecked_at_numeric_read(real_context):
    _, state, workspace, _, _, _, _ = real_context
    payload, ids = _verified(state)
    data = state.dataset_record(ids['dataset_id'], ids['research_id'])
    path = workspace.path(ids['research_id'], data['stored_path'])
    path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(ReportValidationError):
        _trusted_stat(state, ids['research_id'], payload.scientific.experiment_id, 'n')


@pytest.mark.parametrize('field', ['summary', 'explanation', 'measurement', 'materials', 'limitations', 'variables'])
@pytest.mark.parametrize('text', ['속도999', '９９９', '값−2.5e+3'])
def test_unproven_number_is_blocked_in_every_display_field(cycle, field, text):
    _, state, rid, _, _ = cycle
    data = {'summary': '정량 결론을 확인하지 못했습니다.'}
    data[field] = [{'name': text, 'role': '확인할 항목'}] if field == 'variables' else [text] if field in {'materials','limitations'} else text
    with pytest.raises(ControlError, match='REPORT_UNPROVEN_NUMBER'):
        validate_draft(state, rid, ReportDraft.model_validate(data))


def test_number_from_sample_size_cannot_be_used_as_speed(real_context):
    _, state, _, _, _, _, _ = real_context
    payload, ids = _verified(state)
    text = '방출 속도는 8 m/s입니다.'
    with pytest.raises(ControlError, match='REPORT_UNPROVEN_NUMBER'):
        validate_draft(state, ids['research_id'], ReportDraft(summary=text,
            numeric_mentions=[ReportNumber(text=text, kind='observed', location='/summary', experiment_id=payload.scientific.experiment_id, field='n')]))


def test_perfect_regression_has_explicit_undefined_t():
    result = _compute_stats(StatsArgs(dataset_id='D', method='linear_regression', variables={'x':'x','y':'y'}),
                            [{'x':str(v),'y':str(2*v)} for v in range(1,6)])
    assert result['test_statistics']['slope_t'] is None
    assert result['test_statistics_status']['slope_t'] == 'ZERO_STANDARD_ERROR'
    json.dumps(result, allow_nan=False)


def test_ridge_imputation_uses_train_observations_only():
    x = np.array([[1., np.nan], [3., 2.], [5., 4.]])
    y = np.array([1., 2., 3.])
    fit = _ridge_fit(x, y)
    filled = x.copy()
    filled[0,1] = 3.
    expected = _ridge_fit(filled, y)
    for key in fit:
        assert np.allclose(fit[key], expected[key])
    with pytest.raises(SkillApplicabilityError, match='ALL_MISSING_TRAIN_FEATURE'):
        _ridge_fit(np.array([[np.nan],[np.nan],[np.nan]]), y)


class Raw(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data
    async def __aiter__(self):
        yield self.data


@pytest.mark.parametrize('encoding,data,code', [('gzip', gzip.compress(b'x'*200000), 'RESPONSE_TOO_LARGE'),
    ('br', b'abcd', 'UNSUPPORTED_CONTENT_ENCODING'), ('gzip', b'bad', 'INVALID_CONTENT_ENCODING')], ids=['gzip-limit','unsupported','invalid'])
def test_raw_compression_is_limited_before_large_decoding(encoding, data, code):
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={'Content-Encoding':encoding}, stream=Raw(data)))) as client:
            async with client.stream('GET','https://example.org') as response:
                await bounded_bytes(response, 100000)
    with pytest.raises(ResponseLimitError, match=code):
        asyncio.run(fetch())


def test_html_depth_limit_and_excluded_parent_are_preserved():
    with pytest.raises(ControlError, match='WEB_SOURCE_DEPTH_LIMIT'):
        extract_html(('<html>'+ '<div>'*129+'text'+'</div>'*129+'</html>').encode('utf-8'))
    result = extract_html(b'<html><head><title>Title</title></head><body><nav><p>hidden</p></nav><p>shown</p></body></html>')
    assert result['title'] == 'Title'
    assert [v['text'] for v in result['paragraphs']] == ['shown']


@pytest.mark.parametrize('encoding,body,code', [('gzip', gzip.compress(b'x'*2_000_001), 'PROFILE_SOURCE_LIMIT'),
    ('br', b'abcd', 'PROFILE_SOURCE_ENCODING_INVALID')], ids=['gzip-limit','unsupported'])
def test_qualified_source_uses_bounded_raw_stream(monkeypatch, encoding, body, code):
    from probe.qualified_workflow import _fetch_text
    client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={'Content-Encoding':encoding}, stream=Raw(body)))
    monkeypatch.setattr('probe.qualified_workflow.httpx.AsyncClient', lambda **kwargs: client(transport=transport))
    with pytest.raises(ControlError, match=code):
        asyncio.run(_fetch_text('https://example.org/data'))


@pytest.mark.parametrize('encoding,body,code', [('gzip', gzip.compress(b'x'*200001), 'PROVIDER_RESPONSE_TOO_LARGE'),
    ('br', b'abcd', 'PROVIDER_INVALID_RESPONSE')], ids=['gzip-limit','unsupported'])
def test_scholarly_source_uses_bounded_raw_stream(encoding, body, code):
    from probe.scholarly import ScholarlyHTTPClient, ScholarlyError
    transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={'Content-Encoding':encoding}, stream=Raw(body)))
    client = ScholarlyHTTPClient(max_bytes=100000, transport=transport)
    with pytest.raises(ScholarlyError) as caught:
        asyncio.run(client.get_json('https://api.openalex.org/works'))
    assert caught.value.code == code and client.last_error_code == code


@pytest.mark.parametrize('encoding', ['identity', 'gzip', 'deflate'])
def test_supported_raw_compression_preserves_utf8_bytes(encoding):
    import zlib
    data = '검증된 관측 기록'.encode('utf-8', errors='strict')
    wire = gzip.compress(data) if encoding == 'gzip' else zlib.compress(data) if encoding == 'deflate' else data
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200,
                headers={'Content-Encoding':encoding}, stream=Raw(wire)))) as client:
            async with client.stream('GET', 'https://example.org') as response:
                return await bounded_bytes(response, 100000)
    assert asyncio.run(fetch()) == data


@pytest.mark.parametrize('console,code', [(b'\xff','INVALID_UTF8_OUTPUT'), ('가'.encode('utf-8')*33334,'OUTPUT_LIMIT')], ids=['invalid-utf8','byte-limit'])
def test_binary_console_limits_bytes_and_returns_structured_utf8_failure(console, code):
    data = json.dumps({'exit_code':0, 'stdout':base64.b64encode(console).decode('ascii'),'stderr':''}).encode('ascii')
    with pytest.raises(SandboxFailure, match=code):
        decode_console(data)


@pytest.mark.parametrize('kind', ['link','escape','alternate-stream','large'])
def test_container_recovery_rejects_links_paths_and_file_total(tmp_path, kind):
    output = io.BytesIO()
    with tarfile.open(fileobj=output,mode='w') as archive:
        item = tarfile.TarInfo('../escape' if kind == 'escape' else 'result.bin:stream' if kind == 'alternate-stream' else 'result.bin')
        if kind == 'link':
            item.type = tarfile.SYMTYPE
            item.linkname = '/etc/passwd'
            archive.addfile(item)
        else:
            item.size = 10000001 if kind == 'large' else 1
            archive.addfile(item,io.BytesIO(b'x'*item.size))
    target = tmp_path/'output'
    target.mkdir()
    with pytest.raises(SandboxFailure):
        collect_tar(output.getvalue(),target)
    assert not list(target.iterdir())


def test_gemini_multipart_text_keeps_event_order():
    value = {'candidates':[{'index':0,'content':{'parts':[{'text':'첫째 '},{'thought':True,'text':'내부'},{'text':'둘째'}]},'finishReason':'STOP'}]}
    data = ('data: '+json.dumps(value)+'\n\n').encode('utf-8')
    document, events = decode_stream(data,'generate_content')
    assert [e.data['text'] for e in events if e.event == 'text_delta'] == ['첫째 ','둘째']
    assert len(document['candidates'][0]['content']['parts']) == 3


def test_unfinished_request_does_not_block_other_requests(app):
    from probe.workbench import create_server
    server = create_server(app)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    slow = socket.create_connection(server.server_address,timeout=1)
    try:
        slow.sendall(b'GET / HTTP/1.1\r\n')
        started = time.monotonic()
        response = httpx.get('http://127.0.0.1:'+str(server.server_port)+'/auth/status',timeout=1)
        assert response.status_code == 200 and time.monotonic()-started < 1
    finally:
        slow.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_publication_failure_preserves_previous_revision(app, monkeypatch):
    gateway, _, _ = rig(app, monkeypatch)
    rid = run(app,gateway)
    state = app.read._state
    old = report_root(state,rid)
    pointer = state.workspace.path(rid,'research_output/current.json').read_bytes()
    def fail(*args, **kwargs):
        raise ControlError('PDF_RENDER_FAILED')
    monkeypatch.setattr('probe.report_pdf.render_pdf',fail)
    with pytest.raises(ControlError):
        export_final_report(state,rid)
    assert state.workspace.path(rid,'research_output/current.json').read_bytes() == pointer
    assert report_root(state,rid) == old and (old/'report.pdf').is_file()


def test_nonempty_release_destination_is_never_mixed_or_deleted(app, monkeypatch, tmp_path):
    gateway, _, _ = rig(app,monkeypatch)
    rid = run(app,gateway)
    destination = tmp_path/'release'
    destination.mkdir()
    (destination/'existing').write_bytes(b'preserve')
    with pytest.raises(ReleaseExportError,match='EXPORT_OUTPUT_NOT_EMPTY'):
        export_release(app.read._state,rid,destination)
    assert (destination/'existing').read_bytes() == b'preserve' and len(list(destination.iterdir())) == 1


def test_rewrite_setup_cancellation_is_saved_and_not_resent(app, monkeypatch):
    gateway, calls, _ = rig(app,monkeypatch)
    rid = run(app,gateway)
    body = {'idempotency_key':'cancelled-rewrite','expected_version':app.store.run(rid)['version'],'state_version':app.read._state.state_version(rid)}
    before = len(calls)
    def cancelled(*args):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rewrite_report(app,rid,body,provider_factory=cancelled))
    saved = asyncio.run(rewrite_report(app,rid,body,provider_factory=cancelled))
    assert saved['status'] == 'FAILED' and len(calls) == before


def test_batch_completed_work_is_reused_and_changed_input_is_blocked(app, monkeypatch):
    gateway, calls, _ = rig(app,monkeypatch)
    rid = run(app,gateway)
    before = len(calls)
    dry = asyncio.run(run_batch(app,dry_run=True))
    assert len(calls) == before and dry['items'][0]['research_id'] == rid
    batch = asyncio.run(run_batch(app,provider_factory=gateway))
    assert batch['status'] == 'COMPLETED', batch
    before = len(calls)
    reused = asyncio.run(run_batch(app,resume=batch['batch_id'],provider_factory=gateway))
    assert reused['status'] == 'COMPLETED' and len(calls) == before
    with transaction(app.store.db):
        app.store.db.execute("UPDATE research_runs SET goal='입력이 바뀐 연구' WHERE research_id=?",(rid,))
    blocked = asyncio.run(run_batch(app,resume=batch['batch_id'],provider_factory=gateway))
    assert blocked['status'] == 'PARTIAL' and blocked['items'][0]['reason'] == 'STALE_INPUT' and len(calls) == before


def test_preflight_child_uses_same_interpreter_without_parent_secrets(monkeypatch):
    from probe.runtime_environment import child_environment, interpreter_preflight
    monkeypatch.setenv('PRIVATE_PARENT_SECRET','must-not-inherit')
    assert 'PRIVATE_PARENT_SECRET' not in child_environment()
    assert interpreter_preflight()['child_started'] and interpreter_preflight()['status'] == 'READY'
