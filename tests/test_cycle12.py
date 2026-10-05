from probe.report_publication import report_root
"""Cycle 12의 선택값·의미·수정본을 실제 저장 경계에서 검사한다."""
import asyncio
from copy import deepcopy
import json

import pytest

from probe.climate_profile import MEANING, parse_source, selected_rows
from probe.period_comparison import compare_periods
from probe.qualified_profiles import conclusion_card
from probe.qualified_workflow import execute_profile, update_source
from test_beginner_v4 import SOURCE, QUESTION, prepare, run_profile, revise
from test_workbench import app

CSV_TEXT = (SOURCE.parent / "gistemp.csv").read_text(encoding="utf-8", errors="strict")


def paired(app, *, question=QUESTION, text=None, alternate=CSV_TEXT):
    rid, snapshot, runtime, provider = prepare(app, question)
    execute = asyncio.run(execute_profile(runtime, rid, snapshot,
        source_text=text if text is not None else SOURCE.read_text(encoding="utf-8"), secondary_text=alternate))
    return rid, snapshot, runtime, provider, execute


def csv_revise(text, year, amount):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(str(year) + ","):
            cells = line.split(",")
            cells[13] = str(amount)
            lines[i] = ",".join(cells)
    return "\n".join(lines) + "\n"


def test_c12_01_real_tools_keyed_pair_and_frozen_policy(app):
    rid, snapshot, runtime, provider, result = paired(app)
    card = conclusion_card(app.read._state, rid)
    assert card["current"] and card["representation"]["status"] == "MATCHED_SELECTED_KEYS"
    assert len(provider.calls) == 1 and result["execution"] == "fake"
    assert app.store.db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?", (rid,)).fetchone()[0] == 1
    assert not snapshot["verification_repair"] and not snapshot["ridge_arithmetic_check"] and not snapshot["verified_analysis_skills"]


@pytest.mark.parametrize("kind", ["wrong_value", "wrong_scale", "mean_preserving_swap"])
def test_c12_02_03_12_recomputed_capture_hash_cannot_hide_key_conflict(app, kind):
    from probe.control_plane import ControlError
    text = SOURCE.read_text(encoding="utf-8")
    if kind == "wrong_value":
        text = revise(text, 2005, 10)
    elif kind == "wrong_scale":
        text = revise(text, 2005, 100 * 68 - 68)
    else:
        lines = text.splitlines()
        i, j = [next(n for n, line in enumerate(lines) if line.startswith(str(year) + " ")) for year in (2004, 2005)]
        a, b = lines[i].split(), lines[j].split()
        a[13], b[13] = b[13], a[13]
        lines[i], lines[j] = " ".join(a), " ".join(b)
        text = "\n".join(lines) + "\n"
    rid, snapshot, runtime, provider = prepare(app)
    with pytest.raises(ControlError, match="SOURCE_CONFLICT"):
        asyncio.run(execute_profile(runtime, rid, snapshot, source_text=text, secondary_text=CSV_TEXT))
    audit = app.read._state.runtime_step(rid, "qualified_analysis:1:representation")["output"]
    assert audit["differing_keys"] and not provider.calls
    assert len(list(app.read._state.workspace.path(rid, "inputs/profile_sources").glob("*"))) == 2
    assert not app.store.db.execute("SELECT 1 FROM state_events WHERE research_id=?", (rid,)).fetchone()


@pytest.mark.parametrize("field,bad", [("quantity_kind", "absolute_temperature"), ("baseline", [1961, 1990]),
    ("value_column", "D-N"), ("temporal_resolution", "monthly"), ("product", "other"), ("spatial_scope", "local"),
    ("unit", "kelvin"), ("storage_scale", "1")])
def test_c12_04_05_06_applicable_meaning_fields_are_checked(field, bad):
    metadata = dict(MEANING, **{field: bad})
    with pytest.raises(ValueError):
        parse_source(SOURCE.read_text(encoding="utf-8"), metadata)


def test_c12_07_conversion_direction_and_baseline_recalculated_from_data():
    from probe.climate_profile import transform_rows, independent_comparison
    rows = parse_source(SOURCE.read_text(encoding="utf-8"), MEANING)
    periods = [[1981, 2000], [2001, 2020]]
    means, difference = independent_comparison(rows, periods)
    converted, converted_difference = independent_comparison(transform_rows(rows, {"unit": "degF_difference"}), periods)
    assert float(converted[0]) == pytest.approx(float(means[0]) * 1.8)
    assert float(converted_difference) == pytest.approx(float(difference) * 1.8)
    shifted, shifted_difference = independent_comparison(transform_rows(rows, {"unit": "degC", "baseline_period": [1961, 1990]}), periods)
    assert shifted[0] != means[0] and float(shifted_difference) == pytest.approx(float(difference))


def test_c12_08_source_row_permutation_preserves_mean_dependencies(app):
    rid, snapshot, runtime, provider, result = paired(app)
    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    indices = [i for i, line in enumerate(lines) if line.startswith(("2004 ", "2005 "))]
    lines[indices[0]], lines[indices[1]] = lines[indices[1]], lines[indices[0]]
    changed = update_source(app.read._state, rid, "\n".join(lines), secondary_text=CSV_TEXT)
    assert not changed["affected"] and conclusion_card(app.read._state, rid)["current"]
    assert len(provider.calls) == 1


@pytest.mark.parametrize("method,expected", [("unweighted_mean", True), ("two_period_comparison", True), ("ordered_timeseries", False)])
def test_c12_08_09_method_aware_group_order(method, expected):
    from probe.period_comparison import selection_equivalent
    original = {"groups": {"A": ["a", "b"], "B": ["c"]}, "direction": "B-A", "weights": None}
    changed = deepcopy(original)
    changed["groups"]["A"].reverse()
    assert selection_equivalent(original, changed, method=method) is expected
    changed["direction"] = "A-B"
    assert not selection_equivalent(original, changed, method=method)


@pytest.mark.parametrize("value,code", [(None,"MISSING"),("","MISSING"),("not numeric","INVALID"),(True,"INVALID"),("NaN","NONFINITE"),("Infinity","NONFINITE")])
def test_c12_10_bad_selected_values_are_typed(value, code):
    from probe.period_comparison import selected_values, SelectedDataError
    with pytest.raises(SelectedDataError, match="SELECTED_VALUE_" + code):
        selected_values([{"year":2000,"value":value}], [2000])


def test_c12_11_unselected_missing_value_does_not_block():
    result = compare_periods([{"year":1999,"value":None},{"year":2000,"value":"1"},{"year":2001,"value":"2"}],
                            {"year":"year","value":"value"}, [[2000,2000],[2001,2001]])
    assert result["n"] == 2 and result["difference"] == 1


@pytest.mark.parametrize("change", ["duplicate", "missing", "substitution", "reassignment", "weights"])
def test_c12_12_scope_membership_is_not_silently_changed(change):
    from probe.period_comparison import selection_equivalent
    original = {"groups":{"A":["a","b"],"B":["c","d"]},"weights":None}
    candidate = deepcopy(original)
    if change == "duplicate":candidate["groups"]["A"] = ["a","a"]
    elif change == "missing":candidate["groups"]["A"] = ["a"]
    elif change == "substitution":candidate["groups"]["A"] = ["a","e"]
    elif change == "weights":candidate["weights"] = [1,2]
    else:candidate["groups"]["A"][1],candidate["groups"]["B"][1] = "d","b"
    assert not selection_equivalent(original,candidate,method="unweighted_mean")


def test_c12_13_optional_absence_is_recorded_without_false_check_pass(app):
    rid, snapshot, runtime, provider, result = run_profile(app)
    card = conclusion_card(app.read._state,rid)
    assert card["current"] and card["representation"]["status"] == "NOT_PRESENT_OPTIONAL"
    assert not card["representation"]["performed"] and not card["representation"]["required"]
    assert any("대조는 수행하지" in limit for limit in card["unconfirmed"])


def test_c12_14_required_absent_cannot_complete(app,monkeypatch):
    from probe.climate_profile import SOURCE_POLICY
    from probe.control_plane import ControlError
    monkeypatch.setitem(SOURCE_POLICY,"secondary_required",True)
    rid,snapshot,runtime,provider=prepare(app)
    with pytest.raises(ControlError,match="CHECK_PENDING"):
        asyncio.run(execute_profile(runtime,rid,snapshot,source_text=SOURCE.read_text(encoding="utf-8")))
    assert not provider.calls and not app.store.db.execute("SELECT 1 FROM state_events WHERE research_id=?",(rid,)).fetchone()
    assert app.read._state.runtime_step(rid,"qualified_authority:1")["output"]["plan"]["source_policy"]["secondary_required"]


def test_c12_15_missing_meaning_is_review_not_default_success(app):
    rid,snapshot,runtime,provider,result=run_profile(app)
    changed=update_source(app.read._state,rid,SOURCE.read_text(encoding="utf-8"),semantics={})
    assert changed["status"]=="CLARIFICATION_REQUIRED" and not conclusion_card(app.read._state,rid)["current"]


def test_c12_16_21_typed_selection_wrong_against_question_cannot_commit(app):
    from probe.control_plane import ControlError
    rid,snapshot,runtime,provider=prepare(app)
    def wrong(payload):
        value=payload.model_copy(deep=True)
        value.agent_result.provenance["qualified_scope"]["periods"]=[[1986,1995],[2011,2020]]
        return value
    with pytest.raises(ControlError,match="VERIFICATION_FAILED"):
        asyncio.run(execute_profile(runtime,rid,snapshot,source_text=SOURCE.read_text(encoding="utf-8"),fault=wrong))
    assert not app.store.db.execute("SELECT 1 FROM state_events WHERE research_id=?",(rid,)).fetchone()


def test_c12_17_owner_amendment_changes_selection_history_and_pdf(app):
    import io
    from pypdf import PdfReader
    from probe.final_report import export_final_report
    from probe.report_pdf import render_pdf
    from probe.qualified_replay import replay
    rid,snapshot,runtime,provider,result=paired(app)
    state=app.read._state
    state.stop_research(rid,"QUALIFIED_PROCEDURE_COMPLETED")
    export_final_report(state,rid)
    new_question=QUESTION.replace("1981~2000","1986~1995").replace("2001~2020","2011~2020")
    response=app.request("POST",f"/api/control/research/{rid}/amend-question",{"question":new_question,"expected_version":state.state_version(rid)})
    assert response.status==200 and not conclusion_card(state,rid)["current"]
    assert state._one("SELECT goal FROM research_runs WHERE research_id=?",(rid,))[0]==QUESTION
    recalculated=asyncio.run(execute_profile(runtime,rid,snapshot,replay=True))
    card=conclusion_card(state,rid)
    assert card["current"] and card["question"]==new_question and recalculated["question_revision"]==2
    assert len(provider.calls)==1 and card["changes"]["groups"]["A"]["removed"]
    assert card["analysis_history"][1]["historical"] and card["changes"]["values"]
    export_final_report(state,rid)
    assert replay((report_root(state, rid) / "replay_manifest.json"))["representation_status"]=="MATCHED_SELECTED_KEYS"
    pdf=render_pdf(state,rid)["data"]
    text="".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "이전 질문" in text and "현재 질문" in text and "독립 관측" in text


@pytest.mark.parametrize("field",["owner_approved","qualification","secondary_required"])
def test_c12_19_worker_authority_fields_rejected_at_commit_boundary(app,field):
    from probe.control_plane import ControlError
    rid,snapshot,runtime,provider=prepare(app)
    def forged(payload):
        value=payload.model_copy(deep=True)
        value.agent_result.provenance[field]=False if field=="secondary_required" else True
        return value
    with pytest.raises(ControlError,match="VERIFICATION_FAILED"):
        asyncio.run(execute_profile(runtime,rid,snapshot,source_text=SOURCE.read_text(encoding="utf-8"),fault=forged))
    assert not app.store.db.execute("SELECT 1 FROM state_events WHERE research_id=?",(rid,)).fetchone()


def test_c12_19_worker_cannot_change_required_policy_via_owner_route(app):
    rid,snapshot,runtime,provider,result=run_profile(app)
    body={"question":QUESTION,"expected_version":app.read._state.state_version(rid),"secondary_required":False,"reviewed_by":"owner"}
    result=app.request("POST",f"/api/control/research/{rid}/amend-question",body)
    assert result.status==409 and result.body["error"]=="PROFILE_OWNER_AMENDMENT_REQUIRED"


def test_c12_20_shared_wrong_source_roots_remain_explicit_limit(app):
    text=revise(SOURCE.read_text(encoding="utf-8"),2005,10)
    alternate=csv_revise(CSV_TEXT,2005,"0.78")
    rid,snapshot,runtime,provider,result=paired(app,text=text,alternate=alternate)
    card=conclusion_card(app.read._state,rid)
    assert card["current"] and card["representation"]["performed"]
    assert "공통 원자료" in card["representation"]["limitation"]
    assert card["live_efficacy"]=="NOT_VALIDATED"


def test_c12_22_checker_change_and_latest_capture_tamper_are_stale(app,monkeypatch):
    rid,snapshot,runtime,provider,result=run_profile(app)
    state=app.read._state
    monkeypatch.setattr("probe.climate_profile.CHECKER_VERSION","next-version")
    assert not conclusion_card(state,rid)["current"]
    monkeypatch.undo()
    update_source(state,rid,revise(SOURCE.read_text(encoding="utf-8"),1900,10))
    from probe.qualified_workflow import latest_source
    path=state.workspace.path(rid,latest_source(state,rid)["source_relative"])
    path.write_bytes(path.read_bytes()+b"tampered")
    assert not conclusion_card(state,rid)["current"]


def test_c12_23_unused_change_reuses_numbers_and_unrelated_research_does_not_stale(app):
    rid,snapshot,runtime,provider,result=run_profile(app)
    state=app.read._state
    old=result["reported_values"]
    state.create_research("별도 연구")
    update_source(state,rid,revise(SOURCE.read_text(encoding="utf-8"),1900,10))
    assert conclusion_card(state,rid)["current"] and conclusion_card(state,rid)["record"]["reported_values"]==old
    assert len(provider.calls)==1


def test_c12_24_context_change_between_verify_and_commit_blocks(app):
    from probe.recovery import FaultInjector
    from probe.service import StateConflictError
    rid,snapshot,runtime,provider=prepare(app)
    state=app.read._state
    runtime.faults=FaultInjector(lambda point: state.set_research_question(rid,QUESTION.replace("1981~2000","1986~1995")) if point=="profile_after_verify" else None)
    with pytest.raises(StateConflictError,match="PROFILE_QUESTION"):
        asyncio.run(execute_profile(runtime,rid,snapshot,source_text=SOURCE.read_text(encoding="utf-8")))
    assert not state._db.execute("SELECT 1 FROM state_events WHERE research_id=?",(rid,)).fetchone()


def test_c12_26_budget_boundary_after_verify_cannot_commit_partial(app):
    from probe.control_plane import ControlError
    from probe.recovery import FaultInjector
    rid,snapshot,runtime,provider=prepare(app)
    def boundary():raise ControlError("RUN_BUDGET_BLOCKED")
    runtime.faults=FaultInjector(lambda point:setattr(runtime,"control_boundary",boundary) if point=="profile_after_verify" else None)
    with pytest.raises(ControlError,match="BUDGET_BLOCKED"):
        asyncio.run(execute_profile(runtime,rid,snapshot,source_text=SOURCE.read_text(encoding="utf-8")))
    assert not app.store.db.execute("SELECT 1 FROM state_events WHERE research_id=?",(rid,)).fetchone()


@pytest.mark.parametrize("snapshot_change",[{"search_policy":"DISABLED"},{"egress":"none"},{"public_search_query":"sk-qaSecretCanary1234567890"},{"search_attempt_limit":0}])
def test_c12_28_secondary_cannot_bypass_search_egress_or_secret_policy(app,monkeypatch,snapshot_change):
    from probe.qualified_workflow import fetch_secondary
    rid,snapshot,runtime,provider=prepare(app)
    snapshot.update(search_policy="ALLOWED",search_required=True,public_search_consent=True,public_search_query="NASA annual GISTEMP")
    snapshot.update(snapshot_change)
    async def forbidden(*args):raise AssertionError("금지한 전송")
    monkeypatch.setattr("probe.qualified_workflow._fetch_text",forbidden)
    assert asyncio.run(fetch_secondary(app.read._state,rid,snapshot)) is None
    assert app.store.ledger()["requests"]==[]


def test_c12_28_registered_opaque_secret_blocks_secondary_before_request(app,monkeypatch):
    from probe.qualified_workflow import fetch_secondary
    from probe.control_plane import Credentials
    rid,snapshot,runtime,provider=prepare(app)
    secret="qa-opaque-registered-credential-1234567890"
    snapshot.update(search_policy="ALLOWED",search_required=True,public_search_consent=True,public_search_query="NASA "+secret)
    monkeypatch.setattr(Credentials,"active_secrets",lambda *args:(secret,))
    async def forbidden(*args):raise AssertionError("등록한 비밀값으로 자료 대조를 요청했습니다")
    monkeypatch.setattr("probe.qualified_workflow._fetch_text",forbidden)
    assert asyncio.run(fetch_secondary(app.read._state,rid,snapshot)) is None
    assert app.store.ledger()["requests"]==[]


def test_c12_29_inspector_is_lazy_and_currentness_is_not_pdf_fabrication(app,monkeypatch):
    from probe.final_report import export_final_report
    rid,snapshot,runtime,provider,result=paired(app)
    state=app.read._state
    basic=app.request("GET",f"/api/control/research/{rid}/source-inspection").body
    expanded=app.request("GET",f"/api/control/research/{rid}/source-inspection?rows=1").body
    assert not basic["rows_loaded"] and expanded["rows_loaded"] and len(expanded["rows"])==40
    assert not basic["primary"]["capture"]["http_observed"] and basic["primary"]["retrieved_at"] is None
    assert basic["paid_calls"]==basic["network_calls"]==0
    state.stop_research(rid,"QUALIFIED_PROCEDURE_COMPLETED");export_final_report(state,rid)
    update_source(state,rid,revise(SOURCE.read_text(encoding="utf-8"),2005,10),secondary_text=CSV_TEXT)
    assert app.request("GET",f"/api/control/research/{rid}/report.pdf").status!=200


@pytest.mark.parametrize("defect",["missing","corrupt"])
def test_c12_30_secondary_replay_failures_never_refetch(app,monkeypatch,defect):
    from probe.final_report import export_final_report
    from probe.qualified_replay import replay
    rid,snapshot,runtime,provider,result=paired(app)
    state=app.read._state
    state.stop_research(rid,"QUALIFIED_PROCEDURE_COMPLETED");export_final_report(state,rid)
    manifest=(report_root(state, rid) / "replay_manifest.json")
    binding=json.loads(manifest.read_text(encoding="utf-8"))["binding"]
    alternate=manifest.parent/"qualified_sources"/binding["secondary"]["source_relative"].rsplit("/",1)[-1]
    if defect=="missing":alternate.unlink()
    else:alternate.write_bytes(alternate.read_bytes()+b"tamper")
    async def forbidden(*args):raise AssertionError("네트워크를 읽었습니다")
    monkeypatch.setattr("probe.qualified_workflow._fetch_text",forbidden)
    with pytest.raises(ValueError,match="SECONDARY"):
        replay(manifest)


def test_c12_31_astronomy_uses_generic_selected_values_without_climate_fields():
    import csv
    from pathlib import Path
    from probe.period_comparison import selected_values, compare_keyed, require_meaning
    from probe.qualified_profiles import registry
    root=Path(__file__).resolve().parents[1]/"qa/cycle12/public"
    records=list(csv.DictReader((root/"trappist1_default.csv").read_text(encoding="utf-8").splitlines()))
    alternate=json.loads((root/"trappist1_default.json").read_text(encoding="utf-8"))
    keys=[row["pl_name"] for row in records]
    values=selected_values(records,keys,key_column="pl_name",value_column="pl_orbper")
    assert len(values)==7 and not compare_keyed(records,alternate,keys,key_column="pl_name",value_column="pl_orbper")
    meaning={"product":"NASA Exoplanet Archive PS","quantity_kind":"orbital_period_duration","unit":"day",
             "target":"TRAPPIST-1","default_flag":1,"value_column":"pl_orbper"}
    require_meaning(meaning,dict(meaning))
    with pytest.raises(ValueError,match="MEANING_CONFLICT"):
        require_meaning(meaning,dict(meaning,quantity_kind="transit_epoch"))
    assert "baseline" not in meaning
    assert registry().choose("TRAPPIST-1 행성의 공전 주기 평균 비교")["status"]=="GENERAL"


def test_c12_32_beginner_start_has_no_new_required_witness_fields(app):
    from test_workbench import configure
    configure(app)
    snapshot=app.prepare({"beginner_mode":True,"question":QUESTION,"egress":"selected"})
    assert app.preflight(snapshot)["ready"] and snapshot["research_profile_mode"]=="AUTO"
    assert "secondary_required" not in snapshot
    created=app.create({"beginner_mode":True,"question":QUESTION,"egress":"selected"})
    assert not app.read._state.cycle5.enabled(created["research_id"])


def test_c12_24_source_update_and_invalidation_roll_back_together(app,monkeypatch):
    from probe.qualified_workflow import latest_source
    rid,snapshot,runtime,provider,result=run_profile(app)
    state=app.read._state
    version=state.state_version(rid)
    original=state._invalidate_qualified_result
    def fail(*args):
        original(*args)
        raise RuntimeError("C12 결함 주입 중단")
    monkeypatch.setattr(state,"_invalidate_qualified_result",fail)
    with pytest.raises(RuntimeError,match="C12"):
        update_source(state,rid,revise(SOURCE.read_text(encoding="utf-8"),2005,10))
    assert latest_source(state,rid) is None and state.state_version(rid)==version
    assert conclusion_card(state,rid)["current"]
    assert state._one("SELECT status FROM experiments WHERE research_id=?",(rid,))[0]=="VERIFIED"


def test_c12_22_27_stale_result_cannot_be_returned_as_resumed_current(app):
    from probe.control_plane import ControlError
    rid,snapshot,runtime,provider,result=run_profile(app)
    update_source(app.read._state,rid,revise(SOURCE.read_text(encoding="utf-8"),2005,10))
    with pytest.raises(ControlError,match="REVALIDATION_REQUIRED"):
        asyncio.run(execute_profile(runtime,rid,snapshot))
    assert len(provider.calls)==1


def test_c12_25_compound_fault_repair_rechecks_every_obligation(tmp_path):
    import importlib.util
    from pathlib import Path
    path=Path(__file__).resolve().parents[1]/"qa/f3p_eval.py"
    specification=importlib.util.spec_from_file_location("c12_f3p_evaluator",path)
    module=importlib.util.module_from_spec(specification);specification.loader.exec_module(module)
    record=module.run_case({"id":"compound_defect","expected":"REPAIR_INCOMPLETE","initially_correct":False},tmp_path/"compound",3911)
    assert record["observed"]=="REPAIR_INCOMPLETE" and record["committed"]==0
    from probe.verification_repair import MAX_REPAIR_ATTEMPTS
    assert record["repair_attempts"]==MAX_REPAIR_ATTEMPTS and record["verification_executions"]>=2


def test_c12_oracle_csv_read_is_rejected_by_actual_qualified_tool_permissions(app,tmp_path):
    from probe.schemas import ToolRequest,new_id
    rid,snapshot,runtime,provider=prepare(app)
    app.read._state.configure_budget(rid,0.025,0.075,0.1)
    contract,task=runtime._role_contract(rid,"experiment_coordinator","승인된 입력만 읽습니다.","QualifiedProfileImport",allowed_tools=["data.import"],max_tool_calls=1)
    oracle=tmp_path/"evaluator_only"/"oracle.csv"
    oracle.parent.mkdir();oracle.write_bytes("answer\n999\n".encode("utf-8",errors="strict"))
    registry=runtime._registry(contract.contract_id)
    request=ToolRequest(request_id=new_id("TC"),tool_name="data.import",research_id=rid,task_id=task,
        actor_id=contract.assigned_role,args={"source_path":str(oracle),"research_id":rid},idempotency_key="c12-oracle-denied")
    result=registry.dispatch(contract.contract_id,request)
    assert not result.ok and "SOURCE_NOT_AUTHORIZED" in result.error
    assert not app.store.db.execute("SELECT 1 FROM datasets WHERE research_id=?",(rid,)).fetchone()


def test_c12_source_secret_canary_is_blocked_before_archive(app):
    from probe.control_plane import ControlError
    rid,snapshot,runtime,provider=prepare(app)
    text=SOURCE.read_text(encoding="utf-8")+"\nsk-c12SecretCanary1234567890\n"
    with pytest.raises(ControlError,match="SOURCE_SECRET_BLOCKED"):
        asyncio.run(execute_profile(runtime,rid,snapshot,source_text=text))
    assert not provider.calls and not app.store.db.execute("SELECT 1 FROM state_events WHERE research_id=?",(rid,)).fetchone()


def test_c12_http_capture_only_records_observed_headers(app,monkeypatch):
    import httpx
    from probe.qualified_workflow import _fetch_text,_source
    rid,snapshot,runtime,provider=prepare(app)
    original=httpx.AsyncClient
    seen=[]
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200,headers={"ETag":"observed-tag"},content=SOURCE.read_bytes())
    monkeypatch.setattr("probe.qualified_workflow.httpx.AsyncClient",lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    captured=asyncio.run(_fetch_text("https://data.giss.nasa.gov/gistemp/tabledata_v4/GLB.Ts+dSST.txt"))
    saved=_source(app.read._state,rid,captured)
    assert saved["capture"]["http_observed"] and saved["capture"]["headers"]["etag"]=="observed-tag"
    assert "last-modified" not in saved["capture"]["headers"] and saved["retrieved_at"]
    assert saved["documentation_status"]=="PROFILE_RECORDED_URL_HTML_NOT_CAPTURED"
    assert len(seen)==1 and "question" not in seen[0]


def test_c12_10_missing_selected_value_preserves_actual_draft_and_capture(app):
    from probe.period_comparison import SelectedDataError
    rid,snapshot,runtime,provider=prepare(app)
    lines=SOURCE.read_text(encoding="utf-8").splitlines()
    index=next(i for i,line in enumerate(lines) if line.startswith("2005 "))
    cells=lines[index].split();cells[13]="***";lines[index]=" ".join(cells)
    with pytest.raises(SelectedDataError,match="SELECTED_VALUE_MISSING"):
        asyncio.run(execute_profile(runtime,rid,snapshot,source_text="\n".join(lines)))
    assert app.store.run(rid)["status"]=="DRAFT" and app.store.run(rid)["snapshot"]["question"]==QUESTION
    assert app.read._state.runtime_step(rid,"qualified_analysis:1:source")["output"]["source_sha256"]
    assert not provider.calls and not app.store.db.execute("SELECT 1 FROM state_events WHERE research_id=?",(rid,)).fetchone()


def test_c12_05_optional_present_semantic_conflict_is_not_ignored(app):
    from probe.climate_profile import CSV_MEANING
    rid,snapshot,runtime,provider,result=paired(app)
    changed=update_source(app.read._state,rid,SOURCE.read_text(encoding="utf-8"),secondary_text=CSV_TEXT,
                          secondary_semantics=dict(CSV_MEANING,baseline=[1961,1990]))
    assert changed["status"]=="SOURCE_CONFLICT" and changed["affected"]
    assert changed["source"]["representation"]["differing_fields"]==["secondary.baseline"]
    assert not conclusion_card(app.read._state,rid)["current"]


def test_c12_14_requirement_frozen_through_source_update(app,monkeypatch):
    from probe.climate_profile import SOURCE_POLICY
    monkeypatch.setitem(SOURCE_POLICY,"secondary_required",True)
    rid,snapshot,runtime,provider,result=paired(app)
    monkeypatch.setitem(SOURCE_POLICY,"secondary_required",False)
    changed=update_source(app.read._state,rid,SOURCE.read_text(encoding="utf-8"))
    assert changed["status"]=="CHECK_PENDING" and changed["source"]["representation"]["required"]
    assert not conclusion_card(app.read._state,rid)["current"]


def test_c12_30_latest_unused_capture_missing_also_blocks_frozen_replay(app):
    from probe.final_report import export_final_report
    from probe.qualified_replay import replay
    rid,snapshot,runtime,provider,result=run_profile(app)
    state=app.read._state
    changed=update_source(state,rid,revise(SOURCE.read_text(encoding="utf-8"),1900,10))
    state.stop_research(rid,"QUALIFIED_PROCEDURE_COMPLETED");export_final_report(state,rid)
    manifest=(report_root(state, rid) / "replay_manifest.json")
    (manifest.parent/"qualified_sources"/changed["source"]["source_relative"].rsplit("/",1)[-1]).unlink()
    with pytest.raises(ValueError,match="CAPTURE_UNSAFE_OR_MISSING"):
        replay(manifest)


def test_c12_capture_timing_incompatible_is_review_not_replacement(app):
    from probe.qualified_workflow import CapturedText,_pair_source
    from probe.climate_profile import representation_check
    rid,snapshot,runtime,provider=prepare(app)
    a=CapturedText(SOURCE.read_text(encoding="utf-8"),{"http_observed":True,"retrieved_at":"2026-10-03T00:00:00+00:00"})
    b=CapturedText(CSV_TEXT,{"http_observed":True,"retrieved_at":"2026-10-03T01:00:00+00:00"})
    source=_pair_source(app.read._state,rid,a,b)
    result=representation_check(app.read._state,rid,source,{"periods":[[1981,2000],[2001,2020]],"transform":{"unit":"degC"}})
    assert not result["eligible"] and result["reason"]=="SOURCE_CAPTURE_INCOMPARABLE"
    assert source["source_sha256"]!=source["secondary"]["source_sha256"]


def test_c12_10_missing_selected_value_is_typed():
    with pytest.raises(ValueError, match="SELECTED_VALUE_MISSING"):
        compare_periods([{"year": 2000, "value": None}, {"year": 2001, "value": 1}],
                        {"year": "year", "value": "value"}, [[2000, 2000], [2001, 2001]])


def test_c12_12_selected_duplicate_is_not_deduplicated():
    with pytest.raises(ValueError, match="SELECTED_KEY_DUPLICATE"):
        selected_rows([{"year": 2000, "value": "1"}, {"year": 2000, "value": "1"}], [[2000, 2000]])


def test_c12_18_raw_question_change_invalidates_current_result(app):
    rid, snapshot, runtime, provider, result = run_profile(app)
    state = app.read._state
    state.set_research_question(rid, QUESTION.replace("1981~2000", "1986~1995"))
    assert not conclusion_card(state, rid)["current"]
