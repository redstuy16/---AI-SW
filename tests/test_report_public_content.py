"""내부 정보 차단과 과학적 내용·출처·무결성 보존을 검사한다."""
import io
import json
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from probe.control_plane import ControlError
from probe.final_report import export_final_report
from probe.report_content import internal_content, public_sections
from probe.report_pdf import render_pdf
from probe.report_publication import report_root
from probe.report_ux import friendly_report, markdown_report
from probe.research_report import ReportDraft, validate_draft, persist_report_draft, _save
from test_workbench import app


@pytest.mark.parametrize("field", ["summary", "purpose", "explanation", "method", "results", "conclusion", "measurement", "limitations", "procedure", "materials", "variables"])
@pytest.mark.parametrize("internal", ["Docker / Live LLM / Live Search 환경 검증", "manifest와 provenance 기록을 확인하세요.", "NOT_VALIDATED 상태입니다.", "실험 ID: EXP-" + "a" * 32, "SHA-256: " + "b" * 64])
def test_model_internal_information_is_rejected_in_every_display_field(app, field, internal):
    state = app.read._state
    rid = state.create_research("온도와 용해도의 관계를 설명한다.")
    value = {key: "온도에 따른 용해 평형의 적용 범위를 설명한다." for key in ("summary", "purpose", "explanation", "method", "results", "conclusion")}
    value[field] = [{"name": "온도", "role": internal}] if field == "variables" else [internal] if field in {"limitations", "procedure", "materials"} else internal
    with pytest.raises(ControlError, match="REPORT_INTERNAL_CONTENT_BLOCKED"):
        validate_draft(state, rid, ReportDraft(**value))
    assert not app.store.ledger()["requests"]


@pytest.mark.parametrize("mode", ["local", "legacy", "inquiry", "old_polluted"])
def test_actual_reports_omit_internal_templates_and_preserve_science(app, tmp_path, mode):
    state = app.read._state
    rid = state.create_research("온도 변화의 원리를 보고서로 설명한다.")
    state.configure_budget(rid, 1, 1, 2)
    runtime = SimpleNamespace(state=state)
    if mode != "local":
        body = {"purpose": "기체 용해 평형을 설명한다.", "explanation": "압력과 온도는 용해 평형에 영향을 준다.",
                "method": "평형 조건을 비교하였다.", "results": "직접 측정값이 없어 정확한 속도를 확정할 수 없다.",
                "conclusion": "용해 평형과 방출 속도는 구분해야 한다."} if mode == "inquiry" else {}
        record = persist_report_draft(runtime, app.store, rid, {}, ReportDraft(summary="용해 평형과 방출 속도는 구분해야 한다.", **body))
        if mode == "old_polluted":
            record["draft"]["summary"] += "\nDocker / Live LLM / Live Search 환경 검증\nmanifest와 provenance 기록"
            _save(app.store, "ai_report", rid, record)
    state.stop_research(rid, "INSUFFICIENT_DATA")
    export_final_report(state, rid)
    view = friendly_report(state, rid)
    if mode == "old_polluted":
        assert "Docker" in view["ai_report"]["draft"]["summary"]
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(render_pdf(state, rid)["data"])).pages)
    root = report_root(state, rid)
    markdown = markdown_report(view)
    final = (root / "final_report.md").read_text(encoding="utf-8")
    for output in [text, markdown, final]:
        assert not internal_content(output)
        assert not any(phrase in output for phrase in ["검증된 그림 없음", "한눈에 보기", "추가 분석과 비교", "보고서 출처 및 원본 해시", "전체 타당성 보증", "산출물", "재현 방법"])
        assert "부분 보고서" in output
    if mode != "local":
        assert "용해 평형과 방출 속도는 구분해야 한다." in text and "용해 평형과 방출 속도는 구분해야 한다." in final
    manifest = json.loads((root / "manifests/artifact_manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"] and "cost_report.json" in manifest["files"]
    assert json.loads((root / "state_snapshot.json").read_text(encoding="utf-8"))["research"]["research_id"] == rid
    (tmp_path / (mode + ".pdf")).write_bytes(render_pdf(state, rid)["data"])


def test_public_projection_keeps_units_periods_links_and_skips_empty_sections():
    view = {"complete": True, "conclusion": "두 기간을 비교하였다.", "analyses": [{"method": "period_mean_difference"}],
            "display_numbers": [{"label": "기온 편차 평균 (°C)", "value": 0.405}], "images": [], "conclusion_card": {"available": True, "scope": "두 기간의 관측 비교만 가능하다.", "unconfirmed": ["인과관계는 확인하지 않았다."]}}
    sections = public_sections(view, [{"title": "GISTEMP v4 · " + "a" * 64, "url": "https://data.giss.nasa.gov/gistemp/"}])
    text = "\n".join(v["text"] for v in sections)
    assert "GISTEMP v4" in text and "https://data.giss.nasa.gov/gistemp/" in text
    assert "인과관계는 확인하지 않았다." in text and not internal_content(text)
    assert any(v["table"] == (["분석 지표", "값"], [["기온 편차 평균 (°C)", "0.405"]]) for v in sections)
    assert all(v["text"] or v["table"] or v["images"] for v in sections)


@pytest.mark.parametrize("text", ["검증된 그림 없음", "추가로 기록된 미해결 문제 없음 · 전체 타당성 보증은 아님", "추가 분석은 별도 승인된 방법별 결과로 비교합니다.", "검증 후 내보내기의 manifest, 입력 hash를 사용하세요.", "입력한 연구 의도이며 실제 관측이나 검증 완료를 뜻하지 않습니다.", "자료·열 연결과 실행 증거를 확인합니다.", "계획과 실제 사용 여부를 구분합니다.", "실제 자료와 검증된 분석의 지원 범위"])
def test_unrelated_template_notice_is_blocked(app, text):
    state = app.read._state
    rid = state.create_research("온도와 기체의 관계를 설명한다.")
    with pytest.raises(ControlError, match="REPORT_INTERNAL_CONTENT_BLOCKED"):
        validate_draft(state, rid, ReportDraft(summary="용해 평형을 설명한다.", limitations=[text]))


def test_unread_reference_is_not_presented_as_report_bibliography():
    view = {"complete": True, "conclusion": "원리를 비교하였다."}
    sources = [{"title": "읽은 문헌", "url": "https://example.com/read", "status": "VERIFIED"},
               {"title": "제목만 수집한 문헌", "url": "https://example.com/unread", "status": "RELEVANT"}]
    sections = public_sections(view, sources)
    bibliography = next(item for item in sections if item["title"] == "참고문헌")["text"]
    assert "읽은 문헌" in bibliography and "https://example.com/read" in bibliography
    assert "제목만 수집한 문헌" not in bibliography and "https://example.com/unread" not in bibliography


@pytest.mark.parametrize("suffix", ["", "\n| | |", "\n\n실측 결과는 확보하지 못했다."])
def test_empty_ai_table_is_blocked_and_legacy_table_is_omitted(app, suffix):
    from probe.report_content import public_text
    state = app.read._state
    rid = state.create_research("온도와 기체 용해 평형을 설명한다.")
    blank = "| 분석 방법 | 실험 ID | 분석 계획 |\n|---|---|---|" + suffix
    with pytest.raises(ControlError, match="REPORT_INTERNAL_CONTENT_BLOCKED|REPORT_EMPTY_TABLE_BLOCKED"):
        validate_draft(state, rid, ReportDraft(summary="관측 자료를 검토한다.", explanation=blank))
    plain = "| 분석 지표 | 값 |\n|---|---|" + suffix
    with pytest.raises(ControlError, match="REPORT_EMPTY_TABLE_BLOCKED"):
        validate_draft(state, rid, ReportDraft(summary="관측 자료를 검토한다.", explanation=plain))
    assert "|" not in public_text(plain)
    actual = "| 분석 지표 | 값 |\n|---|---|\n| 조건 | 온도 |"
    assert public_text(actual) == actual
