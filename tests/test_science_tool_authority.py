"""관리자 계획과 작업자 권한·실행 기록·중복 방지를 검증한다."""
import asyncio

import pytest

from test_science_loop import runtime
from probe.science_loop import ScienceDecision
from probe.science_tools import execute_science_plan


def prepared(tmp_path):
    db, state, provider, loop = runtime(tmp_path, [])
    rid = state.create_research('실제 과학 계산')
    state.workspace.prepare(rid)
    state.configure_budget(rid, .1, .1, .1)
    parent, task = loop._role_contract(rid, 'manager', '계획만 작성합니다.', 'ScienceDecision')
    return db, state, loop, rid, parent


def test_calculation_worker_has_one_permission_and_replays(tmp_path):
    db, state, loop, rid, parent = prepared(tmp_path)
    decision = ScienceDecision(action='CALCULATE', rationale='입력 계산', calculation_plan={
        'inputs': {'x': 2}, 'calculations': [{'name': 'square', 'expression': 'x*x', 'unit': '무차원'}]})
    first = asyncio.run(execute_science_plan(loop, rid, 0, decision, parent))
    second = asyncio.run(execute_science_plan(loop, rid, 0, decision, parent))
    assert first == second and first['results'][0]['value'] == 4
    assert parent.allowed_tools == [] and parent.constraints.max_tool_calls == 0
    worker, task = state.runtime_contract(rid, 'science:tool:0')
    assert worker.allowed_tools == ['science.calculate'] and worker.constraints.max_tool_calls == 1
    assert db.execute('SELECT COUNT(*) FROM tool_calls').fetchone()[0] == 1
    assert state.file_artifact(first['artifact_id'], rid)['contract_id'] == worker.contract_id
    db.close()


def test_async_source_worker_records_and_replays_without_request(tmp_path, monkeypatch):
    db, state, loop, rid, parent = prepared(tmp_path)
    calls = []
    async def fetch(runtime, research_id, contract_id, plan):
        calls.append(contract_id)
        assert state.contract(contract_id)[0].allowed_tools == ['science.fetch_data']
        return {'status': 'IMPORTED', 'dataset_id': 'D-mock'}
    monkeypatch.setattr('probe.climate_data.fetch_climate_data', fetch)
    decision = ScienceDecision(action='FETCH_DATA', rationale='공식 자료 요청', data_plan={'sources': ['noaa_co2']})
    first = asyncio.run(execute_science_plan(loop, rid, 1, decision, parent))
    second = asyncio.run(execute_science_plan(loop, rid, 1, decision, parent))
    assert first == second and len(calls) == 1
    assert db.execute('SELECT status FROM tool_dispatches').fetchone()[0] == 'FINISHED'
    assert db.execute('SELECT COUNT(*) FROM tool_calls').fetchone()[0] == 1
    db.close()


def test_history_projection_removes_duplicate_data_without_changing_record():
    from probe.science_loop import _compact_observations
    history=[{'iteration':0,'action':'CALCULATE_DATASET','rationale':'검산',
              'result':{'status':'VERIFIED','artifact_id':'ART-x','results':[{'value':42}],'plan':{'name':'fit'}}}]
    projected=_compact_observations(history)
    assert 'results' not in projected[0]['result'] and history[0]['result']['results'][0]['value']==42
    assert projected[0]['result']['artifact_id']=='ART-x'


def test_pdf_keeps_negative_sign_and_subscript_and_escapes_input():
    from probe.report_pdf import _pdf_text
    assert _pdf_text('−18.6 °C, C₀, m⁻² <script>')=='-18.6 °C, C<sub>0</sub>, m<super>-</super>² &lt;script&gt;'


def test_pdf_binary_encoding_preserves_image_and_text():
    import io
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from PIL import Image
    from pypdf import PdfReader
    from probe.report_pdf import _encode_binary_streams
    output=io.BytesIO();doc=canvas.Canvas(output,invariant=True)
    doc.drawString(40,700,'CHECK -18.6 K')
    doc.drawImage(ImageReader(Image.new('RGB',(16,16),(10,90,200))),40,600,width=16,height=16)
    doc.save();before=PdfReader(io.BytesIO(output.getvalue()))
    encoded=_encode_binary_streams(output.getvalue());after=PdfReader(io.BytesIO(encoded))
    assert before.pages[0].extract_text()==after.pages[0].extract_text()
    first=next(iter(before.pages[0]['/Resources']['/XObject'].values())).get_object()
    second=next(iter(after.pages[0]['/Resources']['/XObject'].values())).get_object()
    assert first.get_data()==second.get_data() and '/ASCIIHexDecode' in second['/Filter']
    assert _encode_binary_streams(output.getvalue())==encoded
    assert _encode_binary_streams(encoded)==encoded


@pytest.mark.parametrize('repeated', [False, True])
def test_distinct_math_plans_are_progress_and_repeated_plan_is_not(tmp_path, repeated):
    from test_science_loop import complete
    replies=[{'action':'CALCULATE','rationale':'계산을 검산합니다.','calculation_plan':{
        'inputs':{'x':2 if repeated else n},'calculations':[{'name':'square','expression':'x*x','unit':'무차원'}]}}
        for n in (2,3,4)]
    db,state,provider,loop=runtime(tmp_path,replies+[complete()])
    loop.science_settings.update(search_policy='DISABLED',max_decisions=6,no_progress_limit=2)
    result=asyncio.run(loop.run('입력별 공식 검산',None))
    assert result['stop_reason']==('UNRESOLVED_VERIFICATION' if repeated else 'SCIENCE_INQUIRY_COMPLETED')
    assert len(provider.calls)==(3 if repeated else 4)
    db.close()
