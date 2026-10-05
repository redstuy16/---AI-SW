"""미리보기의 실제 보고서 검증·현재성·비밀 값 차단을 검사한다."""
from hashlib import sha256

import pytest

from probe.final_report import export_final_report
from probe.qualified_workflow import amend_question
from test_cycle12 import paired
from test_workbench import app


def ready(app):
    rid, _, _, _, _ = paired(app)
    app.read._state.stop_research(rid, "QUALIFIED_PROCEDURE_COMPLETED")
    export_final_report(app.read._state, rid)
    return rid


def test_preview_matches_the_actual_verified_pdf(app):
    rid = ready(app)
    preview = app.request("GET", f"/api/control/research/{rid}/report-preview")
    pdf = app.request("GET", f"/api/control/research/{rid}/report.pdf")
    assert preview.status == pdf.status == 200
    assert preview.body["pdf_sha256"] == sha256(pdf.body).hexdigest()
    assert preview.body["state_version"] == app.read._state.state_version(rid)
    assert preview.body["view"]["conclusion_card"]["current"]
    assert "0.405" in preview.body["view"]["conclusion"]
    assert "data" not in preview.body and "preview" not in preview.body["view"]


@pytest.mark.parametrize("fault", ["tamper", "question", "secret"])
def test_preview_preserves_pdf_export_gates(app, monkeypatch, fault):
    rid = ready(app)
    state = app.read._state
    if fault == "tamper":
        path = (__import__("probe.report_publication", fromlist=["report_root"]).report_root(state, rid) / 'final_report.md')
        data = (path.read_text(encoding="utf-8") + "\n검사용 변조\n").encode("utf-8", errors="strict")
        path.write_bytes(data)
    elif fault == "question":
        amend_question(state, rid, "1986~1995년과 2011~2020년의 전 지구 연간 기온 편차 평균을 비교해 주세요.",
                       expected_version=state.state_version(rid))
    else:
        monkeypatch.setenv("PROBE_UI_PREVIEW_SECRET_KEY", "1981~2000년과 2001~2020년")
    preview = app.request("GET", f"/api/control/research/{rid}/report-preview")
    pdf = app.request("GET", f"/api/control/research/{rid}/report.pdf")
    assert preview.status == pdf.status == 409
    assert "view" not in preview.body and "data" not in preview.body
    assert "1981~2000년과 2001~2020년" not in str(preview.body)
