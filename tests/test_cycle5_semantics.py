"""Cycle 5의 의미·계보·질문·복구 경계를 실제 DB와 고정 입력으로 검사한다."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "qa"))
from cycle5_fixtures import GOLD_ROWS, completed_science, science_fixture, transform_fixture
from probe.cycle5 import GoalWitness, SourceSemanticRecord, SemanticReviewRequired
from probe.database import initialize, to_json
from probe.final_report import export_final_report, ReportValidationError
from probe.release import export_release, ReleaseExportError
from probe.reliability import truth_aware_metrics
from probe.service import StateConflictError, StateService
from probe.storage import Workspace
from probe.verification_repair import frozen_plan_matches, repair_transformation


def test_autonomous_pause_review_resume_uses_existing_cursor(tmp_path):
    from test_autonomous_loop import CSV, MANAGER, MODELS, initial_coordinator, shortlist, worker
    from cycle5_fixtures import ON, semantic
    from probe.autonomous_loop import AutonomousResearchLoop
    from probe.cycle5 import GoalScope
    from probe.providers.fake import FakeProvider
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    rid = state.create_research("Analyze association.")
    state.workspace.prepare(rid)
    state.cycle5.enable(rid, actor_role="owner")
    state.configure_budget(rid, .25, .75, 1)
    def status(call):
        active = json.loads(call["input_text"])["active_state"]
        return {"hypothesis_id": active["active_hypotheses"][0]["hypothesis_id"], "new_status": "INCONCLUSIVE",
                "rationale": "고정 표본의 연관성만 확인함", "evidence_refs": [{"type": "evidence", "id": item["evidence_id"]} for item in active["verified_evidence"]]}
    provider = FakeProvider([MANAGER, shortlist(), initial_coordinator, worker("pearson_correlation"),
        {"verdict": "ACCEPT_WITH_LIMITATION", "issues": [], "alternative_explanations": [], "confounders": [], "requested_followups": [], "conclusion_strength": "MODERATE"}, status,
        {"stop": True, "reason": "INSUFFICIENT_DATA", "rationale": "검증된 근거 하나로 결론을 확정하지 않음"}])
    runtime = AutonomousResearchLoop(state, provider, MODELS)
    runtime.research_slice_config = ON
    state.finish_runtime_step(rid, "verified_analysis_skills_config", runtime._skill_config())
    state.finish_runtime_step(rid, "verification_repair_config", runtime._repair_config())
    state.finish_runtime_step(rid, "source", {"csv_source": str(CSV), "goal": "Analyze association."})
    runtime._save_cursor(rid, "START")
    try:
        with pytest.raises(SemanticReviewRequired):
            asyncio.run(runtime._run_with_stops(rid, CSV, "Analyze association."))
        assert len(provider.calls) == 3
        did = db.execute("SELECT dataset_id FROM datasets WHERE research_id=?", (rid,)).fetchone()[0]
        for col in ("temperature", "growth"):
            semantic(state, rid, "SM-" + col, did, col, unit="declared-unit")
        scope = GoalScope(intent="association", estimand="corr(temperature,growth)", analysis_method="pearson_correlation",
            quantity={col: "declared_other" for col in ("temperature", "growth")}, baseline={col: None for col in ("temperature", "growth")},
            units={col: "declared-unit" for col in ("temperature", "growth")}, population_scope="고정 표본",
            spatial_scope="합성 표본", temporal_scope="고정 관측 기간", limitations=["인과 해석 불가"])
        state.cycle5.set_goal(GoalWitness(research_id=rid, original_question="Analyze association.", scope=scope), actor_role="owner")
        reply = provider.replies[0]
        provider.replies[0] = lambda call: {**reply(call), "semantic_scope": scope.model_dump(mode="json")}
        result = asyncio.run(runtime.resume(rid))
        assert result["stop_reason"] == "INSUFFICIENT_DATA"
        assert db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?", (rid,)).fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.parametrize("fault", ["offset", "scale", "year_swap", "duplicate", "missing", "fabricated", "empty_key", "missing_value", "nan", "infinity", "other_column"])
def test_row_level_transform_faults_rejected(tmp_path, fault):
    rows = deepcopy(GOLD_ROWS)
    if fault == "offset":
        for r in rows: r["value"] = str(float(r["value"]) + 32)
    if fault == "scale":
        for r in rows: r["value"] = str(float(r["value"]) * 2)
    if fault == "year_swap": rows[0]["value"], rows[2]["value"] = rows[2]["value"], rows[0]["value"]
    if fault == "duplicate": rows.append(deepcopy(rows[0]))
    if fault == "missing": rows.pop()
    if fault == "fabricated": rows[-1]["year"] = "2099"
    if fault == "empty_key": rows[0]["year"] = ""
    if fault == "missing_value": rows[0]["value"] = ""
    if fault == "nan": rows[0]["value"] = "NaN"
    if fault == "infinity": rows[0]["value"] = "Infinity"
    if fault == "other_column": rows[0]["other"] = "changed"
    db, state, rid, lineage = transform_fixture(tmp_path, rows)
    try:
        assert not state.cycle5.verify_lineage(lineage)["passed"]
        assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    finally: db.close()


@pytest.mark.parametrize("quantity,offset", [("temperature_anomaly", 0), ("temperature_difference", 0), ("absolute_temperature", 32)])
def test_quantity_separate_from_unit_and_correct_conversion(tmp_path, quantity, offset):
    rows = [{**r, "value": str(float(r["value"]) + offset)} for r in GOLD_ROWS]
    db, state, rid, lineage = transform_fixture(tmp_path, rows, quantity=quantity)
    try:
        assert state.cycle5.verify_lineage(lineage)["passed"]
        assert {r.quantity_kind for r in state.cycle5.records(rid, "source")} == {quantity}
    finally: db.close()


def test_row_reorder_is_valid_but_mean_preserving_swap_is_not(tmp_path):
    db, state, rid, lineage = transform_fixture(tmp_path, list(reversed(GOLD_ROWS)))
    try: assert state.cycle5.verify_lineage(lineage)["passed"]
    finally: db.close()


@pytest.mark.parametrize("fault", ["offset", "scale", "baseline", "quantity", "unit", "review", "stale"])
def test_declared_transform_cannot_change_meaning(tmp_path, fault):
    db, state, rid, lineage = transform_fixture(tmp_path)
    try:
        if fault in {"offset", "scale"}:
            candidate = lineage.model_copy(update={"offset": 32 if fault == "offset" else 0, "scale": 2 if fault == "scale" else 1.8, "revision": 2})
            with pytest.raises(ValueError): state.cycle5.declare_lineage(candidate, actor_role="owner")
        else:
            child = next(r for r in state.cycle5.records(rid, "source") if r.record_id == "SM-child")
            changes = {"revision": 3, "review_status": "NEEDS_REVIEW", "reviewed_by": None}
            changes.update({"baseline": "다른 기준"} if fault == "baseline" else {"quantity_kind": "absolute_temperature"} if fault == "quantity" else {"unit": "degC"} if fault == "unit" else {})
            state.cycle5.propose_source(child.model_copy(update=changes), actor_role="analysis_planner_worker")
            if fault != "review": state.cycle5.review_source(rid, child.record_id, 3, actor_role="owner")
            assert not state.cycle5.verify_lineage(lineage)["passed"]
            with pytest.raises(ValueError): repair_transformation(state, rid, lineage.record_id, 1, actor_role="owner")
    finally: db.close()


def test_storage_scale_is_applied_once(tmp_path):
    parents = [{"year": r["year"], "value": str(i * 100), "other": r["other"]} for i, r in enumerate(GOLD_ROWS, 1)]
    db, state, rid, lineage = transform_fixture(tmp_path, parent_rows=parents, parent_scale=.01)
    try: assert state.cycle5.verify_lineage(lineage)["passed"]
    finally: db.close()


@pytest.mark.parametrize("boundary", ["BEFORE_COMMIT", "AFTER_COMMIT"])
def test_transform_repair_restart_is_atomic_and_idempotent(tmp_path, boundary):
    rows = [{**r, "value": str(float(r["value"]) + 32)} for r in GOLD_ROWS]
    db, state, rid, lineage = transform_fixture(tmp_path, rows)
    parent_hash = lineage.parent_hash
    def crash(point):
        if point == boundary: raise RuntimeError("고정 장애 주입")
    with pytest.raises(RuntimeError): repair_transformation(state, rid, lineage.record_id, 1, actor_role="owner", fault=crash)
    db.close()
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    try:
        result = repair_transformation(state, rid, lineage.record_id, 1, actor_role="owner")
        assert result["status"] == ("REPAIRED" if boundary == "BEFORE_COMMIT" else "ALREADY_REPAIRED")
        assert result["verification"]["passed"]
        assert state.dataset_record(lineage.parent_id, rid)["sha256"] == parent_hash
        assert state.file_artifact(lineage.child_id, rid)["status"] == "PENDING"
        assert db.execute("SELECT COUNT(*) FROM planning_events WHERE event_type='CYCLE5_TRANSFORM_REPAIRED'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    finally: db.close()


def test_dataset_repair_is_forbidden(tmp_path):
    rows = [{**r, "value": "33.8"} for r in GOLD_ROWS]
    db, state, rid, lineage = transform_fixture(tmp_path, rows, child_kind="dataset")
    try:
        with pytest.raises(ValueError, match="NEW_ANALYSIS_PLAN"): repair_transformation(state, rid, lineage.record_id, 1, actor_role="owner")
    finally: db.close()


@pytest.mark.parametrize("actor", ["analysis_planner_worker", "experiment_coordinator", "manager"])
def test_worker_or_agent_cannot_approve(tmp_path, actor):
    db, state, rid, lineage = transform_fixture(tmp_path)
    try:
        with pytest.raises(ValueError): state.cycle5.review_source(rid, "SM-parent", 2, actor_role=actor)
        goal = GoalWitness(research_id=rid, original_question="original", scope={"intent": "causal", "estimand": "effect", "quantity": {"value": "temperature_anomaly"}, "baseline": {"value": "ref"}, "units": {"value": "degC"}, "population_scope": "p", "spatial_scope": "s", "temporal_scope": "t"})
        with pytest.raises(ValueError): state.cycle5.set_goal(goal, actor_role=actor)
    finally: db.close()


def test_scientific_integration_records_all_obligations(tmp_path):
    db, state, runtime, prepared, oracle, outcome = completed_science(tmp_path)
    try:
        rid = prepared["research_id"]
        assert outcome["verdict"] == "PASS"
        checks = state.cycle5.snapshot(rid)["current_checks"][0]["checks"]
        assert all(c["passed"] for c in checks)
        assert {c["check_id"] for c in checks} <= {r.check_id for r in state.research_slice.obligation_records(rid)}
        call = next(c for c in runtime.provider.calls if c["role"] == "analysis_planner_worker")
        assert json.loads(call["input_text"])["active_state"]["goal_witness"]["scope"]["intent"] == "association"
        assert abs(outcome["result"]["metrics"]["estimate"] - oracle) < 1e-10
    finally: db.close()


@pytest.mark.parametrize("fault", ["intent", "baseline", "estimand", "spatial", "time", "unit", "limitation"])
def test_worker_scope_change_requires_new_plan(tmp_path, fault):
    db, state, runtime, prepared, _ = science_fixture(tmp_path)
    worker = runtime.provider.replies[0]
    def changed(call):
        result = worker(call)
        scope = result["semantic_scope"]
        field = {"intent": "intent", "baseline": "baseline", "estimand": "estimand", "spatial": "spatial_scope", "time": "temporal_scope", "unit": "units", "limitation": "limitations"}[fault]
        scope[field] = "causal" if fault == "intent" else {"a": "changed", "b": None} if fault == "baseline" else {"a": "degC", "b": "b-units"} if fault == "unit" else [] if fault == "limitation" else "changed"
        return result
    runtime.provider.replies[0] = changed
    try:
        with pytest.raises(ValueError, match="NEW_ANALYSIS_PLAN"): asyncio.run(runtime.resume(prepared["research_id"]))
        assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    finally: db.close()


def test_stale_semantic_after_pass_blocks_commit(tmp_path):
    db, state, runtime, prepared, _ = science_fixture(tmp_path)
    commit = state.commit
    def changed(mid):
        source = next(r for r in state.cycle5.records(prepared["research_id"], "source") if r.record_id == "SM-a")
        source = source.model_copy(update={"revision": 3, "review_status": "NEEDS_REVIEW", "reviewed_by": None})
        state.cycle5.propose_source(source, actor_role="analysis_planner_worker")
        return commit(mid)
    state.commit = changed
    try:
        with pytest.raises(StateConflictError): asyncio.run(runtime.resume(prepared["research_id"]))
        assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    finally: db.close()


def test_report_export_and_semantic_revision_invalidation(tmp_path):
    db, state, runtime, prepared, _, _ = completed_science(tmp_path)
    rid = prepared["research_id"]
    try:
        state.stop_research(rid, "BUDGET_EXHAUSTED")
        paths = export_final_report(state, rid)
        assert all(c.support_state == "INCONCLUSIVE" for c in state.research_slice.current(rid))
        report = Path(paths["report"]).read_text(encoding="utf-8")
        assert "INCONCLUSIVE" in report
        assert "자료 의미와 원질문 범위" in report and "a-units" in report
        exported = export_release(state, rid, tmp_path / "release")
        assert any(f["path"].endswith("research_slice.json") for f in exported["files"])
        goal = state.cycle5.records(rid, "goal")[0]
        state.cycle5.set_goal(goal.model_copy(update={"revision": 2}), actor_role="owner")
        assert all(c.support_state == "NEEDS_REVALIDATION" for c in state.research_slice.current(rid))
        with pytest.raises((ReleaseExportError, ReportValidationError)): export_release(state, rid, tmp_path / "stale-release")
    finally: db.close()


def truth_row(**updates):
    return {"fixture_id": "always_wrong", "gold_ref": "external-fixed-values.json#temperature_difference", "gold_origin": "EXTERNAL_FIXTURE", "gold_conclusion": "1.8", "observed_conclusion": "33.8", "outcome": "accepted", "completed": True, "review_required": False, "fault_eligible": False, "recovered": False, **updates}


def test_always_wrong_always_same_is_not_correct():
    metrics = truth_aware_metrics([truth_row() for _ in range(3)])
    assert metrics["correctness"]["rate"] == 0
    assert metrics["conclusion_agreement"]["rate"] == 1
    assert metrics["outcome_consistency"]["rate"] == 1
    assert metrics["conditional_robustness"]["status"] == "NOT_ESTIMABLE"
    assert metrics["recoverability"]["rate"] is None
    assert "reliability_score" not in metrics


@pytest.mark.parametrize("origin", ["INTERNAL_PASS", "EXTERNAL_FIXTURE"])
def test_internal_pass_or_missing_gold_cannot_be_truth(origin):
    with pytest.raises(ValueError): truth_aware_metrics([truth_row(gold_origin=origin, gold_ref="" if origin == "EXTERNAL_FIXTURE" else "verifier")])


@pytest.mark.parametrize("fault", ["semantic_record", "policy", "goal_scope", "contract"])
def test_tamper_and_stale_history_do_not_reuse_pass(tmp_path, fault):
    db, state, runtime, prepared, _, _ = completed_science(tmp_path)
    rid = prepared["research_id"]
    try:
        if fault == "policy":
            db.execute("UPDATE runtime_steps SET output_json=? WHERE research_id=? AND step_key='cycle5_config'", (to_json({"enabled": False, "version": "1"}), rid))
        elif fault == "semantic_record":
            row = db.execute("SELECT step_key,output_json FROM runtime_steps WHERE research_id=? AND step_key LIKE 'cycle5:source:SM-a:%' ORDER BY rowid DESC LIMIT 1", (rid,)).fetchone()
            value = json.loads(row[1]); value["record"]["unit"] = "degC"
            from probe.research_slice import digest
            value["sha256"] = digest(value["record"])
            db.execute("UPDATE runtime_steps SET output_json=? WHERE research_id=? AND step_key=?", (to_json(value), rid, row[0]))
        elif fault == "goal_scope":
            goal = state.cycle5.records(rid, "goal")[0]
            goal.scope.estimand = "different estimand"
            state.cycle5.set_goal(goal.model_copy(update={"revision": 2}), actor_role="owner")
        else:
            step = db.execute("SELECT step_key,output_json FROM runtime_steps WHERE research_id=? AND step_key LIKE 'worker_plan:%'", (rid,)).fetchone()
            plan = json.loads(step[1]); plan["semantic_scope"]["intent"] = "causal"
            db.execute("UPDATE runtime_steps SET output_json=? WHERE research_id=? AND step_key=?", (to_json(plan), rid, step[0]))
        with pytest.raises((StateConflictError, ValueError)): asyncio.run(runtime.resume(rid))
    finally: db.close()


@pytest.mark.parametrize("fault", ["contract", "type", "verified", "disabled", "worker"])
def test_transform_repair_boundaries_fail_closed(tmp_path, fault):
    rows = [{**r, "value": str(float(r["value"]) + 32)} for r in GOLD_ROWS]
    db, state, rid, lineage = transform_fixture(tmp_path, rows)
    try:
        if fault == "contract":
            value = json.loads(db.execute("SELECT contract_json FROM contracts").fetchone()[0]); value["objective"] = "changed"
            db.execute("UPDATE contracts SET contract_json=?", (to_json(value),))
        if fault == "type": db.execute("UPDATE artifacts SET artifact_type='SKILL_RESULT' WHERE artifact_id=?", (lineage.child_id,))
        if fault == "verified": db.execute("UPDATE artifacts SET status='VERIFIED' WHERE artifact_id=?", (lineage.child_id,))
        if fault == "disabled": db.execute("UPDATE runtime_steps SET output_json=? WHERE step_key='verification_repair_config'", (to_json({"enabled": False}),))
        with pytest.raises(ValueError): repair_transformation(state, rid, lineage.record_id, 1, actor_role="analysis_planner_worker" if fault == "worker" else "owner")
        assert db.execute("SELECT COUNT(*) FROM planning_events WHERE event_type='CYCLE5_TRANSFORM_REPAIRED'").fetchone()[0] == 0
    finally: db.close()


def test_scope_is_in_f3p_frozen_fields(tmp_path):
    from probe.agent_schemas import AnalysisPlan
    db, state, _, prepared, _, _ = completed_science(tmp_path)
    try:
        value = db.execute("SELECT output_json FROM runtime_steps WHERE step_key LIKE 'worker_plan:%'").fetchone()[0]
        plan = AnalysisPlan.model_validate_json(value)
        other = plan.model_copy(deep=True); other.semantic_scope.baseline["a"] = "changed"
        assert not frozen_plan_matches(plan, other)
    finally: db.close()


def test_owner_api_opt_in_and_cross_research_are_checked(tmp_path):
    from probe.workbench import WorkbenchAPI
    db, state, rid, _ = transform_fixture(tmp_path)
    db.close()
    app = WorkbenchAPI(tmp_path / "state.sqlite", tmp_path / "workspace", mode="DEMO", launch=False)
    try:
        response = app.request("GET", f"/api/research/{rid}/cycle5")
        assert response.status == 200 and response.body["enabled"]
        record = response.body["sources"][0]
        record["research_id"] = "R-another"
        assert app.request("POST", f"/api/control/research/{rid}/cycle5", {"operation": "source", "record": record}).status == 400
        assert app.request("POST", f"/api/control/research/{rid}/cycle5", {"operation": "review", "record_id": record["record_id"], "expected_revision": 1, "approve": True}).status == 400
    finally: app.close()


def test_cycle5_keeps_existing_f3p_numeric_repair(tmp_path):
    db, state, runtime, prepared, _ = science_fixture(tmp_path)
    stage, count = state.stage, [0]
    def corrupt(payload):
        count[0] += 1
        if count[0] == 1:
            payload.agent_result.output = deepcopy(payload.agent_result.output)
            payload.agent_result.output["metrics"]["estimate"] = -.2
        return stage(payload)
    state.stage = corrupt
    try:
        result = asyncio.run(runtime.resume(prepared["research_id"]))
        assert result["verdict"] == "PASS"
        assert count[0] == 2
        assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 1
        assert all(c["passed"] for c in state.cycle5.snapshot(prepared["research_id"])["current_checks"][0]["checks"])
    finally: db.close()


@pytest.mark.parametrize("change", ["storage_scale", "question", "analysis_method"])
def test_actual_plan_and_reviewed_question_are_bound(tmp_path, change):
    db, state, runtime, prepared, _ = science_fixture(tmp_path)
    rid = prepared["research_id"]
    try:
        if change == "storage_scale":
            source = state.cycle5.records(rid, "source")[0]
            state.cycle5.propose_source(source.model_copy(update={"revision": 3, "storage_scale": .01, "review_status": "NEEDS_REVIEW", "reviewed_by": None}), actor_role="analysis_planner_worker")
            state.cycle5.review_source(rid, source.record_id, 3, actor_role="owner")
        elif change == "question": state.set_research_question(rid, "A different causal question")
        else:
            worker = runtime.provider.replies[0]
            def changed(call):
                plan = worker(call); plan["method"] = "spearman_correlation"; plan["skill_plan"]["method"] = "spearman_correlation"
                return plan
            runtime.provider.replies[0] = changed
        with pytest.raises((ValueError, StateConflictError)): asyncio.run(runtime.resume(rid))
        assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    finally: db.close()
