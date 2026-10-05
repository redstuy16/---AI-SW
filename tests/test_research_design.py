"""선택 설계의 초안·과학 의미·실제 도구·현재성·출시 경계를 검사한다."""
import asyncio
from copy import deepcopy
from hashlib import sha256
import io
import json

import pytest
from pydantic import ValidationError

from probe.research_design import (DetailedDesign, normalize_design, semantic_design, meaningful_count,
    resolve_design, organize, current_design, amend_design, summary, verify_design, check_tool, catalog)
from probe.control_plane import ControlError, ROLES
from probe.qualified_workflow import execute_profile
from probe.qualified_profiles import conclusion_card
from probe.final_report import export_final_report
from probe.release import validate_report_snapshot
from probe.report_ux import friendly_report
from probe.autonomous_loop import AutonomousResearchLoop
from probe.providers.fake import FakeProvider
from probe.schemas import StagedResult
from test_workbench import app, configure
from test_beginner_v4 import QUESTION, SOURCE


def intent(value="", **extra):
    return dict(value=value, **extra)


def variable(name, role="item", **extra):
    return dict(id="variable-" + str(extra.pop("index", 1)), name=name, role=role, **extra)


def detailed(app, design, question=QUESTION):
    configure(app)
    created = app.create({"beginner_mode": True, "question": question, "egress": "selected", "detailed_design": design})
    provider = FakeProvider([{"decision": "PROCEED", "reason": "입력 조건에 맞는 고정 공개 비교"}])
    runtime = AutonomousResearchLoop(app.read._state, provider, models={r: "manual-id" for r in ROLES})
    return created["research_id"], created["snapshot"], runtime, provider


def execute(app, design, question=QUESTION):
    rid, snap, runtime, provider = detailed(app, design, question)
    result = asyncio.run(execute_profile(runtime, rid, snap, source_text=SOURCE.read_text(encoding="utf-8")))
    return rid, snap, runtime, provider, result


def test_di01_02_03_empty_same_semantics_and_default_off(app):
    configure(app)
    plain = app.prepare({"beginner_mode": True, "question": "자료 조사", "egress": "selected"})
    opened = app.prepare({"beginner_mode": True, "question": "자료 조사", "egress": "selected", "detailed_design": {"editor_open": True}})
    assert plain["design_resolution"]["hash"] == opened["design_resolution"]["hash"]
    assert meaningful_count(opened["detailed_design"]) == 0 and app.preflight(opened)["ready"]
    assert not any(opened[k] for k in ("verified_analysis_skills", "verification_repair", "ridge_arithmetic_check"))
    assert not app.store.ledger()["requests"]


def test_di04_05_06_08_draft_restore_conflict_and_clear(app):
    design = {"fields": {"purpose": intent("비교")}, "variables": [variable("자료", binding=None)]}
    first = app.request("POST", "/api/control/research/draft", {"draft": {"question": "자료", "detailed_design": design}, "expected_revision": 0})
    assert first.status == 200
    saved = app.request("GET", "/api/control/research/draft").body
    assert saved["draft"]["detailed_design"]["variables"][0]["id"] == "variable-1"
    stale = app.request("POST", "/api/control/research/draft", {"draft": {"question": "늦은 값"}, "expected_revision": 0})
    assert stale.status == 409
    assert app.request("GET", "/api/control/research/draft").body == saved
    clear = app.request("POST", "/api/control/research/draft", {"draft": {"question": "자료", "detailed_design": {}}, "expected_revision": 1})
    assert clear.status == 200 and meaningful_count(app.request("GET", "/api/control/research/draft").body["draft"]["detailed_design"]) == 0


@pytest.mark.parametrize("state", ["UNKNOWN", "USER_DECLARED_NONE", "NOT_APPLICABLE", "SPECIFIED"])
def test_di17_resolution_states_are_distinct(state):
    d = normalize_design({"fields": {"purpose": intent("없음", state=state, reason="문헌 조사")}})
    assert d["fields"]["purpose"]["state"] == state


@pytest.mark.parametrize("bad", [
    {"approved": True}, {"sample_count": 0}, {"sample_count": -1}, {"sample_count": 3.5},
    {"fields": {"purpose": intent("내용", state="VERIFIED")}},
    {"fields": {"purpose": intent("내용", origin="source_metadata")}},
    {"fields": {"purpose": intent("", state="NOT_APPLICABLE")}},
    {"variables": [variable("a"), variable("b")]},
    {"variables": [variable("a", details={"approved": intent("true")})]},
])
def test_di23_43_no_forged_authority_or_invalid_scientific_value(bad):
    with pytest.raises(ValidationError):
        normalize_design(bad)


def test_di09_blank_cards_and_ids_stable():
    raw = {"variables": [variable(""), variable("온도", index=2), variable("물의 양", role="fixed", index=3, active=False)]}
    d = normalize_design(raw)
    assert [v["id"] for v in d["variables"]] == ["variable-2", "variable-3"]
    reordered = deepcopy(d)
    reordered["variables"].reverse()
    assert semantic_design(d) == semantic_design(reordered)
    assert meaningful_count(d) == 1


@pytest.mark.parametrize("approach", ["AUTO", "EXISTING", "LITERATURE"])
def test_di12_13_14_optional_variables_and_no_forced_causal_roles(approach):
    r = resolve_design("자료를 살펴본다", {"approach": approach})
    assert not r["issues"] and r["design"]["variables"] == []


def test_di16_sample_and_repeated_measurement_not_rows():
    d = normalize_design({"sample_count": 2, "fields": {"repetition": intent("같은 대상 5회"), "independent_unit": intent("별개 시료")}})
    assert d["sample_count"] == 2
    assert "observed_sample_count" not in d and "alpha" not in d
    assert normalize_design({}).get("sample_count") is None


def test_di20_phase_specific_control_conflict():
    raw = {"approach": "PHYSICAL", "variables": [variable("온도", "manipulated"), variable("온도", "fixed", index=2)]}
    r = resolve_design("측정 계획", raw)
    assert any("겹칩니다" in i["message"] for i in r["issues"])
    for v, stage in zip(raw["variables"], ["준비", "측정"]):
        v["details"] = {"stage": intent(stage)}
    assert not any("겹칩니다" in i["message"] for i in resolve_design("측정 계획", raw)["issues"])


@pytest.mark.parametrize("field,value", [("period", "1990~2000, 2001~2010"), ("question_focus", "1990~2000년과 2001~2010년의 전 지구 연간 기온 편차 평균 비교")])
def test_di21_targeted_scope_conflict(field, value):
    r = resolve_design(QUESTION, {"fields": {field: intent(value)}})
    assert len(r["issues"]) == 1 and r["issues"][0]["field"] == field


def test_di21_structured_period_conflict():
    d = {"comparisons": [{"id": "period-0001", "text": "A", "start": 1990, "end": 2000}, {"id": "period-0002", "text": "B", "start": 2001, "end": 2010}]}
    assert resolve_design(QUESTION, d)["issues"][0]["field"] == "comparisons"


def test_di27_29_30_31_local_proposal_records_span_and_revision(app):
    text = "연구 목적: 기간 비교\n예상과 다른 결과도 허용\n연구 대상: 공개 기록"
    result = organize(text, 7)
    assert result["paid_calls"] == result["network_calls"] == 0
    for p in result["proposals"]:
        assert text[slice(*p["source_span"])] == p["value"] and p["source_revision"] == 7
    assert "method" not in [p["field"] for p in result["proposals"]]
    assert not app.store.ledger()["requests"]


def test_di11_19_32_39_real_profile_plan_tool_and_intent_summary(app):
    design = {"approach": "EXISTING", "fields": {"purpose": intent("관측 평균 비교"), "method": intent("two_period_comparison"),
        "interpretation_limits": intent("인과관계로 해석하지 않는다"), "independent_unit": intent("연간 전 지구 기록")},
        "hypotheses": [{"id": "hypothesis-01", "text": "두 기간에 차이가 없을 것으로 예상한다"}],
        "variables": [variable("같은 자료 기준", "fixed", details={"maintain": intent("동일한 원본 사용"), "check": intent("해시 비교")})],
        "comparisons": [{"id": "period-01", "text": "앞", "start": 1981, "end": 2000}, {"id": "period-02", "text": "뒤", "start": 2001, "end": 2020}]}
    rid, snap, runtime, provider, result = execute(app, design)
    state = app.read._state
    assert conclusion_card(state, rid)["current"]
    s = summary(state, rid)
    assert s["trace"][0]["arguments"]["parameters"]["periods"] == [[1981, 2000], [2001, 2020]]
    assert s["control_compliance"] == "UNKNOWN" and s["trace"][0]["observed_sample_count"] is None
    assert "0.405" in result["calculation"] and "차" in result["calculation"]
    assert "차이가 없을" not in result["calculation"]
    assert len(provider.calls) == 1 and state.runtime_step(rid, "research_design:1")["output"]["design"]["hypotheses"]
    from probe.agent_context import compile_context
    contract, _ = state.contract(s["trace"][0]["contract_id"])
    assert compile_context(state, contract).active_state["research_design"]["hash"] == s["hash"]


@pytest.mark.parametrize("details", [{"unit": intent("kelvin")}, {"baseline": intent("[1961,1990]")}, {"quantity_kind": intent("absolute_temperature")}])
def test_di22_source_meaning_not_overwritten(app, details):
    rid, snap, runtime, provider = detailed(app, {"variables": [variable("기온 편차", "outcome", details=details)]})
    with pytest.raises(ControlError, match="RESEARCH_DESIGN_ACTION_BLOCKED"):
        asyncio.run(execute_profile(runtime, rid, snap, source_text=SOURCE.read_text(encoding="utf-8")))
    assert not provider.calls and not summary(app.read._state, rid)["trace"]


@pytest.mark.parametrize("key", ["inclusion", "exclusion", "missing", "outlier", "forbidden"])
def test_di25_requested_unqualified_selection_does_not_silently_run(app, key):
    rid, snap, runtime, provider = detailed(app, {"fields": {key: intent("원하는 값만 고르기")}})
    with pytest.raises(Exception, match="RESEARCH_DESIGN_ACTION_BLOCKED"):
        asyncio.run(execute_profile(runtime, rid, snap, source_text=SOURCE.read_text(encoding="utf-8")))
    assert not app.store.db.execute("SELECT 1 FROM experiments WHERE research_id=? AND status='VERIFIED'", (rid,)).fetchone()


@pytest.mark.parametrize("method", ["causal", "forecast", "independent_t_test"])
def test_di26_41_unsupported_method_saved_and_does_not_claim_execution(app, method):
    rid, snap, runtime, provider = detailed(app, {"fields": {"method": intent(method)}})
    assert current_design(app.read._state, rid)["issues"][0]["status"] == "METHOD_UNSUPPORTED"
    with pytest.raises(ControlError):
        asyncio.run(execute_profile(runtime, rid, snap, source_text=SOURCE.read_text(encoding="utf-8")))
    assert not provider.calls and not summary(app.read._state, rid)["trace"]


def test_di36_37_amendment_invalidates_proof_but_cosmetic_collapse_does_not(app):
    design = {"fields": {"purpose": intent("관측 비교")}}
    rid, snap, runtime, provider, result = execute(app, design)
    state = app.read._state
    version = state.state_version(rid)
    same = amend_design(state, rid, dict(design, editor_open=True), expected_version=version)
    assert not same["changed"] and state.state_version(rid) == version and conclusion_card(state, rid)["current"]
    changed = {"fields": {"purpose": intent("관측 비교"), "interpretation_limits": intent("지정 기간에만 해당")}}
    assert amend_design(state, rid, changed, expected_version=version)["changed"]
    assert not conclusion_card(state, rid)["current"]
    row = state._one("SELECT payload_json FROM staged_mutations WHERE mutation_id=?", (result["mutation_id"],))
    assert not verify_design(state, StagedResult.model_validate_json(row[0]))[0]["passed"]
    with pytest.raises(ControlError, match="RESEARCH_DESIGN_STALE"):
        amend_design(state, rid, design, expected_version=version)


def test_di42_pdf_export_and_tamper_are_artifact_bound(app):
    rid, _, _, _, _ = execute(app, {"fields": {"purpose": intent("원래 목적"), "interpretation_limits": intent("관측 비교만")}})
    state = app.read._state
    state.stop_research(rid, "QUALIFIED_PROCEDURE_COMPLETED")
    export_final_report(state, rid)
    assert friendly_report(state, rid)["research_design"]["available"]
    pdf = app.request("GET", f"/api/control/research/{rid}/report.pdf")
    assert pdf.status == 200 and pdf.body.startswith(b"%PDF")
    from pypdf import PdfReader
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.body)).pages)
    assert "이번 연구의 조건" in text and "원래 목적" in text and "실제로 사용한 자료" in text
    validate_report_snapshot(state, rid)
    file = (__import__("probe.report_publication", fromlist=["report_root"]).report_root(state, rid) / 'research_design.json')
    file.write_bytes(b"{}")
    with pytest.raises(Exception):
        validate_report_snapshot(state, rid)


def test_di44_cross_draft_source_and_secret_rejected(app, monkeypatch):
    from probe.input_upload import Attachments
    manager = Attachments(app.store, app.workspace)
    data = b"x,y\n1,2\n2,3\n"
    item = manager.begin({"filename": "data.csv", "draft_id": "draft-owner-1", "size_bytes": len(data)})
    item = manager.receive(item["attachment_id"], io.BytesIO(data), len(data))
    raw = {"variables": [variable("x", binding={"attachment_id": item["attachment_id"], "sha256": sha256(data).hexdigest(), "column": "x"})]}
    response = app.request("POST", "/api/control/research/draft", {"draft": {"draft_id": "draft-other-1", "attachments": [item["attachment_id"]], "detailed_design": raw}, "expected_revision": 0})
    assert response.status == 409
    response = app.request("POST", "/api/control/research/design-columns", {"draft_id": "draft-other-1", "attachment_id": item["attachment_id"]})
    assert response.status == 409
    monkeypatch.setenv("PROBE_API_KEY", "sk-" + "secretcanary"*5)
    response = app.request("POST", "/api/control/research/design-review", {"question": "sk-" + "secretcanary"*5})
    assert response.status == 409 and "secretcanary" not in str(response.body)


def test_di46_long_design_bounded_no_eager_data_or_calls(app):
    d = {"fields": {k: intent("한글 조건 "*50) for k in catalog()["help"]}}
    result = app.request("POST", "/api/control/research/design-review", {"question": "자료", "detailed_design": d})
    assert result.status == 200 and not app.store.ledger()["requests"]
    assert result.body["paid_calls"] == 0

def generic_tools(app, design):
    from probe.research_design import initialize_design
    from probe.mock import MockManager, MockExperimentCoordinator
    from probe.schemas import Constraints, ToolRequest, new_id
    from probe.real_tools import ToolRegistry, DataImportTool, DataProfileTool, StatsTool
    state = app.read._state
    rid = state.create_research("두 기록의 관련성을 살펴본다")
    state.workspace.prepare(rid)
    initialize_design(state, rid, {"question": "두 기록의 관련성을 살펴본다", "detailed_design": design})
    parent = MockManager().create_contract(rid, "두 항목의 대칭적 관련성")
    parent_task = state.issue_contract(parent)
    contract = MockExperimentCoordinator().create_contract(parent, parent_task).model_copy(update={
        "allowed_tools": ["data.import", "data.profile", "stats.run"], "constraints": Constraints(max_tool_calls=10)})
    task = state.issue_contract(contract)
    tools = ToolRegistry(state)
    for tool in (DataImportTool(state), DataProfileTool(state, contract.contract_id), StatsTool(state, contract.contract_id)):
        tools.register(tool)
    def dispatch(name, args):
        key = new_id("TREQ")
        request = ToolRequest(request_id=key, research_id=rid, task_id=task, actor_id=contract.assigned_role,
                              idempotency_key=key, tool_name=name, args=args)
        return request, tools.dispatch(contract.contract_id, request)
    _, imported = dispatch("data.import", {"source_path": str(app.workspace / "inputs/data.csv"), "research_id": rid})
    did = imported.result["dataset_id"]
    dispatch("data.profile", {"dataset_id": did})
    return state, rid, contract, dispatch, did


def test_di13_32_40_generic_real_tool_enforces_method_without_climate(app):
    state, rid, contract, dispatch, did = generic_tools(app, {"fields": {"method": intent("pearson_correlation")}})
    request, result = dispatch("stats.run", {"dataset_id": did, "method": "pearson_correlation",
                                           "variables": {"x": "temperature", "y": "growth"}, "parameters": {}})
    assert result.ok and summary(state, rid)["trace"][0]["tool"] == "stats.run"
    assert not any("기후" in k for k in summary(state, rid)["처음 정한 조건"])
    with pytest.raises(Exception, match="RESEARCH_DESIGN_ACTION_BLOCKED"):
        dispatch("stats.run", {"dataset_id": did, "method": "spearman_correlation", "variables": {"x": "temperature", "y": "growth"}, "parameters": {}})


def test_di18_35_bound_control_deviation_and_source_staleness(app):
    state, rid, contract, dispatch, did = generic_tools(app, {"fields": {"purpose": intent("대상 관측")}})
    record = state.dataset_record(did, rid)
    design = {"variables": [variable("같게 유지할 값", "fixed", binding={"dataset_id": did, "sha256": record["sha256"], "column": "temperature"},
        details={"fixed_value": intent("25"), "maintain": intent("같은 조건"), "check": intent("기록값 확인")})]}
    amend_design(state, rid, design, expected_version=state.state_version(rid))
    from probe.mock import MockManager
    from probe.schemas import Constraints, ToolRequest, new_id
    from probe.real_tools import ToolRegistry, StatsTool
    current = MockManager().create_contract(rid, "수정된 조건의 비교").model_copy(update={
        "assigned_role": "analysis_planner_worker", "allowed_tools": ["stats.run"], "constraints": Constraints(max_tool_calls=3)})
    task = state.issue_contract(current)
    tools = ToolRegistry(state);tools.register(StatsTool(state, current.contract_id))
    key = new_id("TREQ")
    request = ToolRequest(request_id=key, research_id=rid, task_id=task, actor_id=current.assigned_role, idempotency_key=key, tool_name="stats.run",
        args={"dataset_id": did, "method": "pearson_correlation", "variables": {"x": "temperature", "y": "growth"}, "parameters": {}})
    assert tools.dispatch(current.contract_id, request).ok
    assert summary(state, rid)["trace"][0]["control_observations"][0]["status"] == "DEVIATION"
    path = state.workspace.path(rid, record["stored_path"])
    path.write_bytes(path.read_bytes().replace(b"temperature", b"renamed_col"))
    assert check_tool(state, current, request)[0]["field"].startswith("variables:")


def test_di38_f3p_cannot_fix_wrong_columns_by_changing_intent(app):
    state, rid, contract, dispatch, did = generic_tools(app, {"fields": {"method": intent("pearson_correlation")}})
    frozen = deepcopy(current_design(state, rid))
    with pytest.raises(Exception):
        dispatch("stats.run", {"dataset_id": did, "method": "spearman_correlation", "variables": {"x": "growth", "y": "temperature"}, "parameters": {}})
    assert current_design(state, rid) == frozen and not summary(state, rid)["trace"]


def test_di36_change_after_verify_cannot_commit_and_recalculation_recovers(app):
    from probe.recovery import FaultInjector, InjectedCrash
    rid, snap, runtime, provider = detailed(app, {"fields": {"purpose": intent("첫 조건")}})
    state = app.read._state
    runtime.faults = FaultInjector(lambda p: (_ for _ in ()).throw(InjectedCrash()) if p == "profile_after_verify" else None)
    with pytest.raises(InjectedCrash):
        asyncio.run(execute_profile(runtime, rid, snap, source_text=SOURCE.read_text(encoding="utf-8")))
    row = state._one("SELECT mutation_id FROM staged_mutations WHERE research_id=?", (rid,))
    amend_design(state, rid, {"fields": {"purpose": intent("변경 조건")}}, expected_version=state.state_version(rid))
    with pytest.raises(Exception):
        state.commit(row[0])


def test_di33_incomplete_design_is_independent_from_connection_smoke(app, monkeypatch):
    configure(app)
    calls = []
    async def smoke(profile, body):
        calls.append((profile, body));return {"execution": "MOCK", "paid_calls": 0}
    monkeypatch.setattr(app, "check_model", smoke)
    app.request("POST", "/api/control/research/draft", {"draft": {"detailed_design": {"variables": [variable("")]}}, "expected_revision": 0})
    response = app.request("POST", "/api/control/models/m/check", {"mode": "text", "consent": True, "budget_cap_usd": "0.10"})
    assert response.status == 200 and len(calls) == 1


def test_di32_detail_period_refines_underspecified_prose_and_runs(app):
    design = {"comparisons": [{"id": "comparison-a", "text": "A", "start": 1981, "end": 2000},
                             {"id": "comparison-b", "text": "B", "start": 2001, "end": 2020}]}
    rid, snap, runtime, provider, result = execute(app, design, "전 지구 연간 기온 편차 평균을 비교해 주세요.")
    assert result["selection"]["A"] == list(range(1981, 2001)) and result["selection"]["B"] == list(range(2001, 2021))
    assert current_design(app.read._state, rid)["original_question"] == snap["question"]
    assert result["question"] != snap["question"] and len(provider.calls) == 1


def test_omitted_detail_does_not_clear_same_draft(app):
    raw = {"fields": {"purpose": intent("보존할 목적")}}
    app.request("POST", "/api/control/research/draft", {"draft": {"draft_id": "draft-owner-1", "detailed_design": raw}, "expected_revision": 0})
    saved = app.request("POST", "/api/control/research/draft", {"draft": {"draft_id": "draft-owner-1", "question": "바뀐 설명"}, "expected_revision": 1})
    assert saved.status == 200
    assert app.request("GET", "/api/control/research/draft").body["draft"]["detailed_design"]["fields"]["purpose"]["value"] == "보존할 목적"


def test_planning_context_preserves_hypothesis_and_procedure_as_intent(app):
    from probe.research_design import design_context
    raw = {"hypotheses": [{"id": "hypothesis-1", "text": "차이가 없을 것으로 예상"}],
           "procedure": [{"id": "procedure-1", "text": "같은 원본의 기간을 비교"}]}
    rid, snap, runtime, provider = detailed(app, raw)
    context = design_context(app.read._state, rid, "manager")
    assert context["intent_only"] and context["hypotheses"] == raw["hypotheses"] and context["procedure"] == raw["procedure"]


def test_time_axis_unit_is_distinct_from_observed_value(app):
    rid, snap, runtime, provider, result = execute(app, {"variables": [variable("연도", "time", details={"unit": intent("년")})]})
    assert result["selection"]["A"] == list(range(1981, 2001))
    assert conclusion_card(app.read._state, rid)["current"]


def test_material_design_amend_recalculates_with_new_bound_contract(app):
    design = {"fields": {"purpose": intent("첫 조건")}}
    rid, snap, runtime, provider, result = execute(app, design)
    state = app.read._state
    amend_design(state, rid, {"fields": {"purpose": intent("수정한 목적"), "interpretation_limits": intent("관측된 평균 차이만 설명")}},
                 expected_version=state.state_version(rid))
    assert not conclusion_card(state, rid)["current"]
    runtime.provider = FakeProvider([])
    recalculated = asyncio.run(execute_profile(runtime, rid, snap, replay=True))
    assert not runtime.provider.calls and conclusion_card(state, rid)["current"]
    assert recalculated["analysis_revision"] == 2
    assert summary(state, rid)["revision"] == 2 and summary(state, rid)["trace"]


def test_refined_period_amend_uses_original_prose(app):
    design = {"comparisons": [{"id": "comparison-a", "text": "A", "start": 1981, "end": 2000},
                             {"id": "comparison-b", "text": "B", "start": 2001, "end": 2020}]}
    rid, snap, runtime, provider, result = execute(app, design, "전 지구 연간 기온 편차 평균을 비교해 주세요.")
    state = app.read._state
    design["comparisons"][1]["start"] = 2011
    amend_design(state, rid, design, expected_version=state.state_version(rid))
    assert not current_design(state, rid)["issues"]
    recalculated = asyncio.run(execute_profile(runtime, rid, snap, replay=True))
    assert recalculated["selection"]["B"] == list(range(2011, 2021)) and conclusion_card(state, rid)["current"]


def test_design_release_replay_hash_secret_and_stale_gate(app, tmp_path, monkeypatch):
    from probe.release import export_release, ReleaseExportError
    from probe.qualified_replay import replay
    from probe.storage import sha256_file
    rid, snap, runtime, provider, result = execute(app, {"fields": {"purpose": intent("평균 비교")}})
    state = app.read._state
    state.stop_research(rid, "QUALIFIED_PROCEDURE_COMPLETED")
    export_final_report(state, rid)
    canary = "sk-" + "designexportcanary" * 4
    monkeypatch.setenv("PROBE_DESIGN_EXPORT_API_KEY", canary)
    output = tmp_path / "release"
    export_release(state, rid, output)
    document = json.loads((output / "manifests/release_manifest.json").read_text(encoding="utf-8"))
    for record in document["files"]:
        path = output / record["path"]
        assert sha256_file(path) == record["sha256"] and canary.encode("utf-8") not in path.read_bytes()
    manifest = output / "research_output/replay_manifest.json"
    assert replay(manifest)["paid_calls"] == 0
    design_file = manifest.parent / "research_design.json"
    design_file.write_bytes(b"{}")
    with pytest.raises(ValueError, match="REPLAY_DESIGN_HASH_MISMATCH"):
        replay(manifest)
    amend_design(state, rid, {"fields": {"purpose": intent("새 조건")}}, expected_version=state.state_version(rid))
    with pytest.raises(ReleaseExportError):
        export_release(state, rid, tmp_path / "stale")
