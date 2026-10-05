"""과학 보고서와 내부 예시의 숫자·문맥·PDF 경계를 검사한다."""
from hashlib import sha256
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from probe.agent_context import compile_context
from probe.autonomous_loop import AutonomousResearchLoop
from probe.control_plane import ControlError
from probe.final_report import export_final_report
from probe.providers.fake import FakeProvider
from probe.research_examples import freeze_reference_examples, reference_examples_context
from probe.research_report import ReportDraft, ReportNumber, persist_report_draft, validate_draft, report_record, compact_design, requested_measurements
from probe.report_pdf import render_pdf
from probe.report_ux import friendly_report, report_visual_specs, _axis_label_unit
from test_workbench import app


def write_utf8(path, text):
    path.write_bytes(text.encode("utf-8", errors="strict"))


def bare(app, question="5종 음료의 온도와 기체 방출 원리"):
    state = app.read._state
    rid = state.create_research(question)
    state.configure_budget(rid, 1, 1, 2)
    return state, rid, SimpleNamespace(state=state)


def test_principle_and_scope_preserved_without_any_model_call(app):
    state, rid, runtime = bare(app)
    draft = ReportDraft(report_type="principle", summary="5종 음료에서 온도가 바뀌면 기체의 용해 평형도 달라집니다.",
        numeric_mentions=[ReportNumber(text="5종", kind="scope", location="/summary")],
        explanation="기체 용해와 확산은 별개 과정입니다. 용기·표면 상태·압력도 적용 조건입니다.")
    first = persist_report_draft(runtime, app.store, rid, {}, draft, request_key="science:1")
    assert first["draft"]["summary"] == draft.summary and first["draft"]["explanation"] == draft.explanation
    assert report_record(state, rid)["report_type"] == "principle"
    assert persist_report_draft(runtime, app.store, rid, {}, draft, request_key="science:1")["revision"] == first["revision"]
    assert not app.store.ledger()["requests"]


@pytest.mark.parametrize('question,label,count', [
    ('5종 식물의 빛 조건에 따른 생장을 비교한다', '식물', 5),
    ('식물 5종의 생장 속도를 비교한다', '식물', 5),
    ('4종 재료의 전도성을 비교한다', '재료', 4),
    ('5종 미지의 대상을 비교할 실험을 설계한다', '비교 대상', 5),
    ('음료에 관한 문헌 5종을 요약한다', '비교 대상', 0),
    ('5종 문헌을 비교하여 요약한다', '비교 대상', 0),
    ('5종 식물 생장에 관한 논문을 요약한다', '비교 대상', 0),
])
def test_science_scope_counts_comparison_targets_without_counting_references(app, question, label, count):
    state, rid, runtime = bare(app, question)
    draft = ReportDraft(report_type='principle', summary='확인한 원리와 앞으로 비교할 계획을 구분합니다.')
    record = persist_report_draft(runtime, app.store, rid, {'execution_mode': 'SCIENCE_AUTO'}, draft)
    scope = record['requested_measurements']
    assert scope['schema_version'] == 2 and scope['requested'] == count and scope['verified'] == 0
    assert len(scope['rows']) == count
    assert all(row['item'].startswith(label) and row['rate'] == row['unit'] == '미확인' for row in scope['rows'])
    assert report_record(state, rid)['requested_measurements'] == scope


def test_legacy_scope_records_remain_readable_and_keep_old_beverage_shape(app):
    state, rid, runtime = bare(app, '대표 음료 5종의 방출 속도를 비교한다')
    draft = ReportDraft(report_type='principle', summary='온도에 따른 기체 용해의 원리를 설명합니다.')
    record = persist_report_draft(runtime, app.store, rid, {}, draft)
    expected = {'requested': 5, 'verified': 0, 'status': 'NOT_CONFIRMED',
        'rows': [{'item': '음료 ' + str(i + 1) + ' · 종류 미확인', 'rate': '미확인', 'unit': '미확인'} for i in range(5)]}
    assert record['requested_measurements'] == expected
    assert report_record(state, rid)['requested_measurements'] == expected
    state, plant, runtime = bare(app, '5종 식물의 생장을 비교한다')
    legacy = persist_report_draft(runtime, app.store, plant, {}, draft)
    assert legacy['requested_measurements']['requested'] == 0
    assert report_record(state, plant)['requested_measurements'] == legacy['requested_measurements']
    upgraded = persist_report_draft(runtime, app.store, plant, {'execution_mode': 'SCIENCE_AUTO'}, draft, request_key='new-science-scope')
    assert upgraded['revision'] == 2 and upgraded['requested_measurements']['requested'] == 5


def test_science_scope_honors_literature_approach_and_owner_outcome_without_confirming_values(app):
    from probe.research_design import initialize_design
    from test_research_design import variable
    state, rid, _ = bare(app, '식물 5종의 생장을 비교할 실험 설계')
    initialize_design(state, rid, {'question': '식물 5종의 생장 비교',
        'detailed_design': {'approach': 'LITERATURE'}})
    assert requested_measurements(state, rid, schema_version=2)['requested'] == 0
    state, other, runtime = bare(app, '식물 5종의 생장을 비교한다')
    initialize_design(state, other, {'question': '식물 5종의 생장 비교',
        'detailed_design': {'variables': [variable('생장 길이', 'outcome')]}})
    draft = ReportDraft(report_type='principle', summary='생장에 영향을 주는 조건을 구분하고 비교 계획을 세웁니다.')
    record = persist_report_draft(runtime, app.store, other, {'execution_mode': 'SCIENCE_AUTO'}, draft)
    assert record['requested_measurements']['value_label'] == '생장 길이'
    assert all(row['rate'] == row['unit'] == '미확인' for row in record['requested_measurements']['rows'])


def test_science_scope_pdf_uses_generic_target_and_value_labels(app):
    from probe.research_design import initialize_design
    from test_research_design import variable
    state, rid, runtime = bare(app, '식물 5종의 생장 길이를 비교한다')
    initialize_design(state, rid, {'question': '식물 5종의 생장 길이 비교',
        'detailed_design': {'variables': [variable('생장 길이', 'outcome')]}})
    draft = ReportDraft(report_type='principle', summary='생장에 영향을 주는 조건을 구분하고 비교 계획을 세웁니다.')
    persist_report_draft(runtime, app.store, rid, {'execution_mode': 'SCIENCE_AUTO'}, draft)
    state.stop_research(rid, 'INSUFFICIENT_DATA')
    export_final_report(state, rid)
    rendered = render_pdf(state, rid)
    text = '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(rendered['data'])).pages)
    assert '요청한 식물별 측정값' in text and '생장 길이' in text and '식물 5' in text
    assert '음료별' not in text and '방출 속도' not in text


def test_science_scope_metadata_tamper_is_still_rejected(app):
    state, rid, runtime = bare(app, '식물 5종의 생장을 비교한다')
    draft = ReportDraft(report_type='principle', summary='생장 조건을 구분합니다.')
    record = persist_report_draft(runtime, app.store, rid, {'execution_mode': 'SCIENCE_AUTO'}, draft)
    record['requested_measurements']['rows'][0]['rate'] = '999'
    from probe.research_report import _save
    _save(app.store, 'ai_report', rid, record)
    with pytest.raises(ControlError, match='REPORT_MEASUREMENT_CHANGED'):
        report_record(state, rid)


def test_science_beverage_ph_scope_does_not_assume_gas_release(app):
    from probe.research_design import initialize_design
    from test_research_design import variable
    state, rid, runtime = bare(app, '5종 음료의 pH를 비교한다')
    draft = ReportDraft(report_type='principle', summary='산성과 수소 이온 농도의 관계를 비교할 계획입니다.')
    record = persist_report_draft(runtime, app.store, rid, {'execution_mode': 'SCIENCE_AUTO'}, draft)
    assert record['requested_measurements']['scope_label'] == '요청한 음료별 측정값'
    assert record['requested_measurements']['value_label'] == '측정값'
    assert '방출 속도' not in json.dumps(record['requested_measurements'], ensure_ascii=False)
    state, other, runtime = bare(app, '5종 음료의 pH를 비교한다')
    initialize_design(state, other, {'question': '5종 음료의 산성 비교',
        'detailed_design': {'variables': [variable('pH', 'outcome')]}})
    record = persist_report_draft(runtime, app.store, other, {'execution_mode': 'SCIENCE_AUTO'}, draft)
    assert record['requested_measurements']['value_label'] == 'pH'
    state, legacy, runtime = bare(app, '5종 음료의 pH를 비교한다')
    assert 'value_label' not in persist_report_draft(runtime, app.store, legacy, {}, draft)['requested_measurements']


def test_required_literature_cannot_be_bypassed_by_a_completed_draft(app):
    state, rid, runtime = bare(app)
    draft = ReportDraft(report_type="principle", summary="기체의 용해와 확산 원리를 설명합니다.")
    saved = persist_report_draft(runtime, app.store, rid, {"search_required": True}, draft)
    assert saved["status"] == "NEEDS_REVIEW" and saved["error"] == "SEARCH_REQUIRED_EVIDENCE_MISSING"
    assert not app.store.ledger()["requests"]


@pytest.mark.parametrize("kind,text", [("scope", "5종 음료의 방출 속도는 999 mg으로 측정되었습니다."), ("planned", "실험 결과는 999 mg으로 나타났습니다."), ("observed", "999 mg으로 측정되었습니다.")])
def test_observed_numbers_cannot_be_laundered_as_scope_or_plan(app, kind, text):
    state, rid, _ = bare(app)
    with pytest.raises(ControlError):
        validate_draft(state, rid, ReportDraft(report_type="principle", summary=text,
            numeric_mentions=[ReportNumber(text=text, kind=kind, location="/summary")]))


def test_planned_numbers_are_separate_from_observed_results(app):
    state, rid, _ = bare(app)
    draft = ReportDraft(summary="20°C 조건에서 측정할 계획입니다.", numeric_mentions=[ReportNumber(text="20°C 조건에서 측정할 계획입니다.", kind="planned", location="/summary"), ReportNumber(text="20°C에서 준비하고 3회 반복할 계획입니다.", kind="planned", location="/procedure/0")],
        procedure=["20°C에서 준비하고 3회 반복할 계획입니다."])
    result = validate_draft(state, rid, draft)
    assert result["numeric_mentions"][0]["kind"] == "planned"


@pytest.mark.parametrize("text", ["표준 중력 가속도는 이론 설명에서 9.8 m/s²를 근삿값으로 씁니다.", "플랑크 상수의 정의값은 6.62607015e-34 J·s입니다.", "원의 둘레 공식은 2πr로 표현합니다."])
def test_defined_constants_are_not_observed_measurements(app, text):
    state, rid, _ = bare(app)
    draft = ReportDraft(report_type="principle", summary=text, numeric_mentions=[ReportNumber(text=text, kind="constant", location="/summary")])
    assert validate_draft(state, rid, draft)["summary"] == text


@pytest.mark.parametrize("text", ["표준 중력 가속도 상수는 999 m/s²입니다.", "이번 실험에서 중력 가속도 상수를 9.8 m/s²로 측정했습니다.", "원주율 상수의 정의값은 2입니다."])
def test_false_constants_and_measurement_laundering_are_blocked(app, text):
    state, rid, _ = bare(app)
    with pytest.raises(ControlError, match="REPORT_CONSTANT_INVALID"):
        validate_draft(state, rid, ReportDraft(report_type="principle", summary=text, numeric_mentions=[ReportNumber(text=text, kind="constant", location="/summary")]))


def test_added_validation_notices_preserve_long_model_body_and_all_limits(app):
    state, rid, runtime = bare(app)
    body = "측정 계획과 적용 조건을 충분히 설명합니다. " * 130
    draft = ReportDraft(summary=body, limitations=["한계 항목 " + chr(0xAC00 + i) for i in range(32)])
    saved = persist_report_draft(runtime, app.store, rid, {}, draft)
    assert body in saved["draft"]["summary"]
    assert report_record(state, rid)["draft"]["limitations"] == draft.limitations


def test_relationship_diagram_preserves_detailed_controls_and_roles():
    design = {"variables": [{"name": "수온", "role": "manipulated", "details": {"unit": {"value": "°C"}}},
        {"name": "기체 질량", "role": "outcome", "details": {"unit": {"value": "g"}}},
        {"name": "용기", "role": "fixed", "details": {"maintain": {"value": "같은 용기를 유지"}, "check": {"value": "눈금으로 대조"}}}],
        "procedure": [{"text": "원래 사용자의 상세 절차"}]}
    relationship, procedure = report_visual_specs({"variables": [{"name": "수온", "role": "독립변인"}], "procedure": ["모델이 보완한 절차"]}, design)
    assert len(relationship["nodes"]) == 3 and len(relationship["edges"]) == 2
    assert relationship["nodes"][0]["unit"] == "°C" and "눈금" in relationship["nodes"][2]["control"]
    assert len(procedure["steps"]) == 2


def test_only_explicit_verified_axis_units_are_recognized():
    assert _axis_label_unit("degC") == "°C"
    assert _axis_label_unit("온도 (°C)") == "°C"
    assert _axis_label_unit("생장 [cm]") == "cm"
    assert _axis_label_unit("temperature") is None
    assert _axis_label_unit("growth") is None


def test_compact_design_keeps_units_controls_repetition_and_all_steps():
    design = {"sample_count": 5, "variables": [{"name": "온도", "role": "manipulated", "details": {"unit": {"value": "°C"}, "maintain": {"value": "수조로 유지"}}}],
        "procedure": [{"text": "측정 단계 " + str(i)} for i in range(8)], "fields": {"repetition": {"value": "서로 독립된 용기", "state": "SPECIFIED"}}}
    value = compact_design(design)
    assert value["sample_count"] == 5 and len(value["procedure"]) == 8
    assert value["variables"][0]["details"]["unit"]["value"] == "°C"
    assert "수조" in value["variables"][0]["details"]["maintain"]["value"]


def test_reference_examples_are_fixed_local_and_never_evidence(app, tmp_path):
    state, rid, _ = bare(app, "온도에 따른 탄산 기체 방출")
    root = tmp_path / "reference_examples"; root.mkdir()
    for index in range(5):
        write_utf8(root / ("탄산 온도 " + str(index) + ".md"), "# 탄산 온도\n## 가설\n온도에 따른 기체 용해를 비교합니다.\n## 변인\n온도와 기체 변화, 용기를 통제합니다.\n## 절차\n수조 조건을 정하고 원자료를 기록합니다.\n## 자료\n시간과 질량, 단위를 기록합니다.\n## 그림\n온도와 변화를 산점도로 비교합니다.")
    write_utf8(root / "소설.md", "문학 작품의 등장인물 묘사")
    (root / "bad.txt").write_bytes(b"\xff\xfe\x80")
    first = freeze_reference_examples(state, app.store, rid, {"question": "탄산 온도 기체"}, examples_dir=root)
    # 동일 내용은 중복 문맥으로 쓰지 않는다.
    assert len(first["examples"]) == 1
    assert first["issues"][0]["code"] == "REFERENCE_READ_FAILED"
    assert str(root) not in str(reference_examples_context(state, rid))
    assert state._db.execute("SELECT COUNT(*) FROM evidence WHERE research_id=?", (rid,)).fetchone()[0] == 0
    assert state._db.execute("SELECT COUNT(*) FROM sources WHERE research_id=?", (rid,)).fetchone()[0] == 0
    write_utf8(root / "탄산 온도 0.md", "바뀐 원문")
    assert freeze_reference_examples(state, app.store, rid, {"question": "다른 질문"}, examples_dir=root) == first


def test_reference_max_three_and_optional_context_without_evidence_refs(app, tmp_path):
    state, rid, _ = bare(app, "탄산 온도")
    root = tmp_path / "examples"; root.mkdir()
    for index in range(5):
        write_utf8(root / ("탄산" + str(index) + ".txt"), "가설\n탄산 온도 조건을 비교합니다. " + str(index) + "\n변인\n온도 조건\n절차\n자료를 기록합니다.\n그림\n변인 관계도")
    frozen = freeze_reference_examples(state, app.store, rid, {"question": "탄산 온도"}, examples_dir=root)
    assert len(frozen["examples"]) == 3
    runtime = AutonomousResearchLoop(state, FakeProvider([]))
    contract, _ = runtime._role_contract(rid, "manager", "탄산 온도 원리를 설명합니다.", "ScienceDecision")
    bundle = compile_context(state, contract)
    assert bundle.active_state["reference_examples"]["purpose"] == "STRUCTURE_REFERENCE_ONLY"
    assert all(not ref.id.startswith("EXAMPLE-") for ref in bundle.mandatory + bundle.semantic + bundle.dependencies)
    runtime.context_budget = bundle.metrics.estimated_tokens - 100
    small, _ = runtime._role_contract(rid, "manager", "탄산 온도 원리를 설명합니다.", "ScienceDecision")
    reduced = compile_context(state, small)
    assert reduced.metrics.estimated_tokens <= small.context_policy.max_context_tokens


def test_text_pdf_and_scan_pdf_are_distinguished(app, tmp_path):
    from reportlab.pdfgen import canvas
    state, rid, _ = bare(app, "temperature")
    root = tmp_path / "examples"; root.mkdir()
    for name, text in [("temperature.pdf", "hypothesis temperature changes pressure"), ("scan.pdf", "")]:
        target = root / name
        output = canvas.Canvas(str(target))
        if text: output.drawString(20, 700, text)
        else: output.rect(10, 10, 100, 100)
        output.showPage(); output.save()
    frozen = freeze_reference_examples(state, app.store, rid, {"question": "temperature"}, examples_dir=root)
    assert len(frozen["examples"]) == 1 and frozen["examples"][0]["text_only"]
    assert any(v["code"] == "PDF_OCR_REQUIRED" for v in frozen["issues"])


def test_ai_pdf_preserves_design_all_visuals_and_generated_chart(app, tmp_path):
    from test_research_design import execute, intent, variable
    design = {"fields": {"purpose": intent("교사용 원래 목적"), "uncertainty": intent("온도 측정 오차를 기록")},
        "variables": [variable("연간 기온 편차", "outcome", details={"unit": intent("°C"), "maintain": intent("같은 기준 기간")})],
        "procedure": [{"id": "step-row-" + str(i), "text": "자료 확인 단계 " + str(i)} for i in range(7)]}
    rid, snapshot, runtime, provider, _ = execute(app, design)
    before = len(provider.calls)
    draft = ReportDraft(report_type="analysis", summary="공식 자료의 두 기간을 비교했습니다.", explanation="연간 기온 편차는 기준 기간 평균과의 차이입니다.",
        variables=[{"name": "연간 기온 편차의 실제 측정 정의와 기준 기간을 모두 보존하는 긴 이름", "role": "종속변인", "unit": "°C", "control": "같은 기준 기간"}],
        procedure=["자료 확인 단계 " + str(i) for i in range(7)],
        numeric_mentions=[ReportNumber(text="자료 확인 단계 " + str(i), kind="planned", location="/procedure/" + str(i)) for i in range(7)])
    persist_report_draft(runtime, app.store, rid, snapshot, draft, request_key="science:pdf")
    app.read._state.stop_research(rid, "QUALIFIED_PROCEDURE_COMPLETED")
    export_final_report(app.read._state, rid)
    view = friendly_report(app.read._state, rid)
    rendered = render_pdf(app.read._state, rid)
    reader = PdfReader(io.BytesIO(rendered["data"]))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "교사용 원래 목적" in text and "같은 기준 기간" in text
    assert "변인과 통제 조건" in text and "탐구 절차 흐름" in text and "자료 확인 단계 6" in text
    assert "긴 이름" in text and "°C" in text
    assert len(view["visual_specs"]) == 2 and view["images"]
    assert all("검증된 분석 입력 자료" in item["caption"] for item in view["images"])
    assert all(item["plot"]["units"]["value"] == "°C" and item["plot"]["unit_sources"]["value"] == "QUALIFIED_TRANSFORM" for item in view["images"])
    assert "세로축: degC (단위: °C)" in text
    assert "원자료에 제공되지 않음" not in text
    assert any(obj.get_object().get("/Subtype") == "/Image" for page in reader.pages for obj in page.get("/Resources", {}).get("/XObject", {}).values())
    assert len(provider.calls) == before
    assert rendered["sha256"] == sha256(rendered["data"]).hexdigest()
    (tmp_path / "science-report-qa.pdf").write_bytes(rendered["data"])


@pytest.mark.parametrize("field", ["purpose", "method", "results", "conclusion"])
def test_inquiry_body_rejects_unproven_numbers(app, field):
    """새 본문 항목에도 기존 수치 출처 검사를 적용한다."""
    state, rid, _ = bare(app)
    body = {key: "질문에 필요한 원리를 검토한다." for key in ("purpose", "explanation", "method", "results", "conclusion")}
    body[field] = "측정값은 999 mg입니다."
    draft = ReportDraft(summary="원리를 비교합니다.", **body)
    with pytest.raises(ControlError, match="REPORT_UNPROVEN_NUMBER"):
        validate_draft(state, rid, draft)


def test_inquiry_report_has_consistent_body_without_design_scaffolding(app, tmp_path):
    """원리 탐구의 실제 PDF·내보내기에 빈 설계와 그림을 붙이지 않는다."""
    from probe.report_ux import inquiry_sections, markdown_report
    state, rid, runtime = bare(app, "온도와 탄산 방출")
    draft = ReportDraft(summary="온도는 기체 방출 조건에 영향을 줍니다.",
        purpose="탄산음료의 온도와 방출 관계를 분석한다.",
        explanation="용해 평형과 기체 방출 속도는 다른 개념이다.",
        method="용해도와 확산의 원리를 비교하였다.",
        results="직접 측정값은 없다. 높은 온도에서 빠른 방출이 가능하지만 용기 조건도 영향을 준다.",
        conclusion="온도는 주요 조건이며 정확한 방출량은 자료 없이 확정할 수 없다.")
    assert draft.report_type == "principle" and not draft.procedure
    saved = persist_report_draft(runtime, app.store, rid, {}, draft)
    assert not saved["draft"]["limitations"]
    state.stop_research(rid, "SCIENCE_INQUIRY_COMPLETED")
    export_final_report(state, rid)
    view = friendly_report(state, rid)
    assert not view["visual_specs"]
    labels = [item["title"] for item in inquiry_sections(saved["draft"])]
    assert labels == ["탐구 목적", "이론적 배경", "탐구 방법", "결과 및 해석", "결론"]
    pdf = render_pdf(state, rid)
    reader = PdfReader(io.BytesIO(pdf["data"]))
    text = "\n".join(page.extract_text() for page in reader.pages)
    positions = [text.index(label) for label in labels]
    assert positions == sorted(positions)
    assert all(value in text for value in (draft.method, draft.results, draft.conclusion))
    assert "실험 설계안" not in text and "준비물" not in text and "SHA-256" not in text
    assert "비교할 조건과 측정 방법을 정합니다" not in text
    markdown = markdown_report(view)
    assert all("## " + label in markdown for label in labels)
    (tmp_path / "inquiry-report.pdf").write_bytes(pdf["data"])


def test_bundled_examples_use_one_report_format_and_include_interpretation(app):
    """기본 예시에서 목적부터 결론까지 참고하고 근거로 등록하지 않는다."""
    from probe.report_ux import INQUIRY_SECTIONS
    root = Path(__file__).resolve().parents[1] / "src/probe/report_examples"
    files = sorted(root.glob("*.md"))
    assert len(files) == 14
    for path in files:
        text = path.read_text(encoding="utf-8", errors="strict")
        text.encode("utf-8", errors="strict")
        headings = [line.removeprefix("## ") for line in text.splitlines() if line.startswith("## ")]
        assert headings == [label for label, _ in INQUIRY_SECTIONS]
        assert "예상 결과" not in text and "준비물" not in text
    state, rid, _ = bare(app, "탄산음료 온도와 방출")
    frozen = freeze_reference_examples(state, app.store, rid, {"question": "탄산음료 온도와 방출"})
    assert frozen["examples"]
    sections = {entry["section"] for example in frozen["examples"] for entry in example["excerpts"]}
    assert {"purpose", "background", "method", "interpretation", "conclusion"} <= sections
    assert not state._db.execute("SELECT 1 FROM evidence WHERE research_id=?", (rid,)).fetchone()


def test_new_inquiry_requires_all_sections_but_reads_legacy_drafts():
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="모두 작성"):
        ReportDraft(summary="원리 설명", purpose="목적만 작성했다.")
    assert ReportDraft(summary="기존 저장 본문").summary == "기존 저장 본문"


@pytest.mark.parametrize("format", ["inquiry", "legacy", "local"])
@pytest.mark.parametrize("complete", [True, False])
def test_report_opening_does_not_copy_owner_request(app, tmp_path, format, complete):
    """내부 요청·검증 기록을 보존하면서 PDF와 내보내기 서두에서 요청 복사를 막는다."""
    from probe.report_publication import report_root
    from probe.report_ux import markdown_report
    question = "요청 원문 표식: 온도와 탄산 방출을 설명하고 보고서만 출력해 주세요. 인사와 요청 재진술은 빼 주세요."
    state, rid, runtime = bare(app, question)
    if format != "local":
        body = dict(purpose="온도 변화와 기체 방출 관계를 분석한다.",
            explanation="용해 평형과 방출 속도는 구분된다.", method="관련 원리의 적용 조건을 비교하였다.",
            results="실측 자료는 없다. 용기와 압력도 기체 방출에 영향을 준다.",
            conclusion="정확한 방출 속도는 측정 자료가 필요하다.") if format == "inquiry" else {}
        persist_report_draft(runtime, app.store, rid, {}, ReportDraft(summary="온도는 기체 방출에 영향을 준다.", **body))
    state.stop_research(rid, "SCIENCE_INQUIRY_COMPLETED" if complete else "INSUFFICIENT_DATA")
    export_final_report(state, rid)
    view = friendly_report(state, rid)
    assert view["question"] == question and view["complete"] == complete
    root = report_root(state, rid)
    stored = json.loads((root / "research_summary.json").read_text(encoding="utf-8"))
    assert stored["research_question"] == question
    final = (root / "final_report.md").read_text(encoding="utf-8")
    assert final.startswith("# 연구 결과\n\n## 배경과 문헌") and "## 연구 질문" not in final
    markdown = markdown_report(view)
    assert question not in markdown and "요청 원문 표식" not in markdown
    pdf = render_pdf(state, rid)
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf["data"])).pages)
    assert question not in text and "요청 원문 표식" not in text
    if format == "inquiry":
        if complete:
            assert markdown.startswith("# 과학 탐구 보고서\n\n## 탐구 목적")
            assert "과학 탐구 보고서\n탐구 목적\n" in text
        else:
            assert "부분 보고서 · 확인된 범위까지 작성됨" in text and "부분 보고서 · 확인된 범위까지 작성됨" in markdown
        assert "AI 작성" not in markdown and "AI 작성" not in text
        assert all(body[key] in text for key in body)
    else:
        assert "AI 작성" not in text.split("핵심 답변" if format == "legacy" else "한눈에 보기", 1)[0]
    (tmp_path / (format + "-opening.pdf")).write_bytes(pdf["data"])


def test_writer_instruction_keeps_request_in_context_and_body_in_report():
    """독립 작성과 과학 순환의 완료 초안에 같은 서두 지침을 전달한다."""
    from probe.science_loop import ScienceDecision
    instructions = ReportDraft.INSTRUCTIONS
    assert "보고서 본문만 작성한다" in instructions
    assert "질문·요청 전문이나 산출물 목록을 서두에 복사하지 않고" in instructions
    assert "작성 예고" in instructions and "인사" in instructions
    assert instructions in ScienceDecision.INSTRUCTIONS
    assert "requested_outputs" in instructions
