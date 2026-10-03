"""기존 동작과 검증 경계를 확인하는 회귀 테스트."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from htrsa.agent_runtime import AgentRuntime, RuntimeFailure
from htrsa.agent_schemas import AnalysisPlan
from htrsa.analysis_skills import execute_skill
from htrsa.database import initialize, to_json
from htrsa.final_report import export_final_report
from htrsa.providers.fake import FakeProvider
from htrsa.recovery import FaultInjector, InjectedCrash
from htrsa.release import export_release, ReleaseExportError
from htrsa.schemas import VerificationCheck, Verdict
from htrsa.service import StateService, StateConflictError
from htrsa.storage import Workspace, ArtifactIntegrityError, DatasetIntegrityError
from htrsa.verification_repair import (
    VerificationFailureEvidence, frozen_plan_matches, qualify_association,
    ridge_arithmetic_check, REPAIR_POLICY_VERSION)
from test_verified_analysis_skills import (
    association_plan, regression_plan, backtest_plan, tabular_rows, MODELS)


def prepare_case(folder, *, skill=True, decision=None, kind="association", ridge=False):
    source = folder / "pair.csv"
    content = "id,x,y\n" + "".join(f"{row['id']},{row['x']},{row['y']}\n" for row in tabular_rows(23))
    source.write_bytes(content.encode("utf-8", errors="strict"))
    db = initialize(folder / "state.sqlite")
    state = StateService(db, Workspace(folder / "workspace"))

    def coordinator(call):
        active = json.loads(call["input_text"])["active_state"]
        return dict(decision_type="DELEGATE", objective="Evaluate the fixed pair",
                    rationale="Prespecified independent observations", assigned_role="analysis_planner_worker",
                    input_refs=[{"type": "dataset", "id": active["datasets"][0]["dataset_id"]},
                                {"type": "artifact", "id": active["profile_artifacts"][0]["artifact_id"]}],
                    allowed_tools=["analysis.skill", "evidence.record"] if skill else
                                  ["stats.run", "visualization.render", "evidence.record"],
                    max_tool_calls=2 if skill else 3, max_retries=0, max_runtime_sec=120, max_cost_usd=0.5)

    def worker(call):
        dataset = state.dataset_record(prepared["dataset_id"], prepared_id[0])
        factory = association_plan if kind == "association" else regression_plan
        plan = factory(dataset_id=dataset["dataset_id"], dataset_sha256=dataset["sha256"])
        return dict(dataset_id=dataset["dataset_id"], selected_variables=["x", "y"],
                    method=plan.method, justification="Documented IID observations",
                    requested_tools=["analysis.skill", "evidence.record"] if skill else
                                    ["stats.run", "visualization.render", "evidence.record"],
                    skill_plan=plan.model_dump(mode="json") if skill else None)

    manager = dict(decision_type="INITIAL_PLAN", research_question="Association in this sample?",
                   rationale="Use the fixed dataset", coordinator_role="experiment_coordinator", objective="Analyze pair")
    repair = decision or {"action": "REPAIR", "rationale": "Reexecute the frozen plan"}
    provider = FakeProvider([manager, coordinator, worker, repair, repair])
    agent = AgentRuntime(state, provider, MODELS, verified_analysis_skills_enabled=skill,
                         verification_repair_enabled=True, ridge_arithmetic_check_enabled=ridge)
    prepared_id = []
    prepared = asyncio.run(agent.prepare("Association in this sample?", source))
    prepared_id.append(prepared["research_id"])
    return db, state, agent, prepared


def corrupt_first_output(state):
    original = state.stage
    count = [0]
    def stage(payload):
        count[0] += 1
        if count[0] == 1:
            if "metrics" in payload.agent_result.output:
                payload.agent_result.output = deepcopy(payload.agent_result.output)
                payload.agent_result.output["metrics"]["estimate"] = 0.125
            else:
                payload.agent_result.output = {**payload.agent_result.output, "estimate": 0.125}
        return original(payload)
    state.stage = stage
    return count


@pytest.mark.parametrize("skill", [False, True])
def test_repair_success_preserves_trace_and_export(tmp_path, skill):
    db, state, agent, prepared = prepare_case(tmp_path, skill=skill)
    corrupt_first_output(state)
    result = asyncio.run(agent.resume(prepared["research_id"]))
    assert result["verdict"] == "PASS"
    attempts = db.execute("SELECT * FROM staged_mutations ORDER BY rowid").fetchall()
    assert [row["status"] for row in attempts] == ["ROLLED_BACK", "COMMITTED"]
    assert attempts[0]["contract_id"] != attempts[1]["contract_id"]
    original = json.loads(attempts[0]["payload_json"])
    final = json.loads(attempts[1]["payload_json"])
    assert original["scientific"]["stats_artifact_id"] != final["scientific"]["stats_artifact_id"]
    failure = VerificationFailureEvidence.model_validate(
        state.runtime_step(prepared["research_id"], f"repair_failure:{attempts[0]['contract_id']}")["output"])
    checks = json.loads(attempts[1]["verification_json"])["checks"]
    assert set(failure.required_post_repair_checks) <= {item["check_id"] for item in checks}
    assert all(item["passed"] for item in checks)
    assert final["agent_result"]["provenance"]["analysis_revision"] == 1
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM evidence WHERE status='VERIFIED'").fetchone()[0] == 1
    state.file_artifact(failure.failure_id, prepared["research_id"])
    assert asyncio.run(agent.resume(prepared["research_id"]))["already_completed"]
    state.stop_research(prepared["research_id"], "BUDGET_EXHAUSTED")
    export_final_report(state, prepared["research_id"])
    release = export_release(state, prepared["research_id"], tmp_path / "release")
    assert any(item["path"] == "repair_artifacts/repair_trace.json" for item in release["files"])
    for item in release["files"]:
        data = (tmp_path / "release" / item["path"]).read_bytes()
        assert sha256(data).hexdigest() == item["sha256"]
        assert b"C:\\" not in data
    trace = json.loads((tmp_path / "release/repair_artifacts/repair_trace.json").read_text(encoding="utf-8"))
    assert trace["procedure_approved_for_reuse"] is False
    assert len(trace["attempts"]) == 2
    db.close()


@pytest.mark.parametrize("boundary", [
    "AFTER_FAILURE_EVIDENCE", "AFTER_REPAIR_DECISION", "AFTER_REPAIRED_EXECUTION",
    "DURING_REVALIDATION", "BEFORE_REPAIR_COMMIT", "AFTER_REPAIR_COMMIT"])
def test_repair_crash_reopen_resume_is_idempotent(tmp_path, boundary):
    db, state, agent, prepared = prepare_case(tmp_path)
    corrupt_first_output(state)
    def trigger(point):
        if point == boundary:
            raise InjectedCrash(point)
    agent.repair_faults = FaultInjector(trigger)
    with pytest.raises(InjectedCrash):
        asyncio.run(agent.resume(prepared["research_id"]))
    remaining = list(agent.provider.replies)
    db.close()
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    resumed = AgentRuntime(state, FakeProvider(remaining), MODELS,
                           verified_analysis_skills_enabled=True, verification_repair_enabled=True)
    result = asyncio.run(resumed.resume(prepared["research_id"]))
    assert result.get("verdict", "PASS") == "PASS"
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM evidence WHERE status='VERIFIED'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name='analysis.skill'").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM runtime_steps WHERE step_key LIKE 'repair_decision:%'").fetchone()[0] == 1
    db.close()


def test_repair_budget_preflight_does_not_execute_proposal_or_revision(tmp_path):
    db, state, agent, prepared = prepare_case(tmp_path)
    corrupt_first_output(state)
    with db:
        db.execute("UPDATE research_budgets SET target_usd=0.001,soft_limit_usd=0.003,hard_limit_usd=0.005")
    with pytest.raises(RuntimeFailure, match="REPAIR_NOT_STARTED_BUDGET"):
        asyncio.run(agent.resume(prepared["research_id"]))
    assert len(agent.provider.calls) == 3
    assert db.execute("SELECT COUNT(*) FROM contracts WHERE json_extract(contract_json,'$.task_type')='analysis_repair'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    db.close()


def test_compound_defect_requires_all_checks_and_bounds_repair(tmp_path):
    db, state, agent, prepared = prepare_case(tmp_path)
    stage = state.stage
    counter = [0]
    def compound(payload):
        counter[0] += 1
        if counter[0] == 1:
            payload.agent_result.output = deepcopy(payload.agent_result.output)
            payload.agent_result.output["metrics"]["estimate"] = 0.125
        payload.scientific.numeric_provenance["metrics.estimate"].dataset_sha256 = "b" * 64
        return stage(payload)
    state.stage = compound
    with pytest.raises(RuntimeFailure, match="REPAIR_INCOMPLETE"):
        asyncio.run(agent.resume(prepared["research_id"]))
    rows = db.execute("SELECT verification_json FROM staged_mutations ORDER BY rowid").fetchall()
    assert len(rows) == 3
    revised = json.loads(rows[1]["verification_json"])
    assert next(item["passed"] for item in revised["checks"] if item["check_id"] == "F3P_INDEPENDENT_ASSOCIATION")
    assert "NUMERIC_PROVENANCE" in revised["errors"]
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    db.close()


def test_faulty_checker_does_not_corrupt_correct_analysis(tmp_path):
    db, state, agent, prepared = prepare_case(tmp_path)
    evaluate = state._evaluate
    def faulty(row):
        result = evaluate(row)
        result.checks.append(VerificationCheck(check_id="RESULT_FIELD_MATCH", passed=False,
                                               message="deliberately faulty checker"))
        result.errors.append("RESULT_FIELD_MATCH")
        result.verdict = Verdict.FAIL
        return result
    state._evaluate = faulty
    with pytest.raises(RuntimeFailure, match="CHECKER_QUALIFICATION_FAILED"):
        asyncio.run(agent.resume(prepared["research_id"]))
    assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name='analysis.skill'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    db.close()


def test_shared_wrong_result_is_blocked_independently(tmp_path, monkeypatch):
    import htrsa.real_tools as real_tools
    execute = real_tools.execute_skill
    def wrong(*args):
        result = execute(*args)
        result.metrics["estimate"] = 0.125
        return result
    monkeypatch.setattr(real_tools, "execute_skill", wrong)
    db, state, agent, prepared = prepare_case(tmp_path)
    with pytest.raises(RuntimeFailure, match="REPAIR_INCOMPLETE"):
        asyncio.run(agent.resume(prepared["research_id"]))
    for row in db.execute("SELECT verification_json FROM staged_mutations"):
        verification = json.loads(row[0])
        assert "F3P_INDEPENDENT_ASSOCIATION" in verification["errors"]
        assert next(item["passed"] for item in verification["checks"] if item["check_id"] == "RESULT_FIELD_MATCH")
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    db.close()


def test_semantic_plan_mutation_rejected(tmp_path):
    def change(call):
        # 고정 계획만 공개하며 평가 정답은 전달하지 않는다.
        request = json.loads(json.loads(call["input_text"])["active_state"]["recent_failure"])
        proposal = request["frozen_plan"]
        proposal["method"] = "spearman_correlation"
        proposal["skill_plan"]["method"] = "spearman_correlation"
        return {"action": "REPAIR", "rationale": "Attempt a forbidden method change", "proposed_plan": proposal}
    db, state, agent, prepared = prepare_case(tmp_path, decision=change)
    corrupt_first_output(state)
    with pytest.raises(RuntimeFailure, match="REPAIR_CONTRACT_MUTATION_BLOCKED"):
        asyncio.run(agent.resume(prepared["research_id"]))
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    db.close()


def test_flags_versions_and_frozen_fields(tmp_path):
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    off = AgentRuntime(state, FakeProvider([]), MODELS, verification_repair_enabled=False,
                       verified_analysis_skills_enabled=False)
    assert off._repair_config() == {"enabled": False, "version": None, "ridge_arithmetic_check": False}
    rid = state.create_research("legacy")
    off._check_repair_config(rid)
    on = AgentRuntime(state, FakeProvider([]), MODELS, verification_repair_enabled=True)
    with pytest.raises(StateConflictError, match="CONFIG_MISSING"):
        on._check_repair_config(rid)
    state.finish_runtime_step(rid, "verification_repair_config",
                              {"enabled": True, "version": "0.2", "ridge_arithmetic_check": False})
    with pytest.raises(StateConflictError, match="VERSION_OR_CONFIG_MISMATCH"):
        on._check_repair_config(rid)
    base = AnalysisPlan(dataset_id="D-test", selected_variables=["x", "y"],
                        method="ridge_holdout", justification="fixed", requested_tools=["analysis.skill"],
                        skill_plan=regression_plan())
    changed = base.model_copy(deep=True)
    changed.skill_plan.holdout_fraction = 0.3
    assert not frozen_plan_matches(base, changed)
    assert frozen_plan_matches(base, None)
    db.close()


@pytest.mark.parametrize("scale", [1e-4, 1.0, 1e6])
def test_experimental_ridge_arithmetic_detects_coefficient_and_train_state_faults(scale):
    rows = [{**row, "y": str(float(row["y"]) * scale)} for row in tabular_rows(31)]
    skill = regression_plan()
    plan = AnalysisPlan(dataset_id=skill.dataset_id, selected_variables=["x", "y"],
                        method=skill.method, justification="fixed", requested_tools=["analysis.skill"],
                        skill_plan=skill)
    result = execute_skill(skill, ["id", "x", "y"], rows).model_dump(mode="json")
    assert ridge_arithmetic_check(plan, result, rows).status == "passed"
    altered = deepcopy(result)
    altered["diagnostics"]["fitted_training_only"]["coefficients"][0] += scale
    assert ridge_arithmetic_check(plan, altered, rows).status == "failed"
    altered = deepcopy(result)
    altered["diagnostics"]["fitted_training_only"]["imputer_means"][0] += 1
    assert ridge_arithmetic_check(plan, altered, rows).status == "failed"
    altered = deepcopy(result)
    altered["diagnostics"]["fitted_training_only"]["alpha"] = 2
    assert ridge_arithmetic_check(plan, altered, rows).status == "failed"


@pytest.mark.parametrize("fault", ["artifact", "dataset", "failure_evidence", "stale_report", "unresolved_ref", "secret_canary"])
def test_repaired_export_keeps_integrity_and_secret_gates(tmp_path, fault):
    db, state, agent, prepared = prepare_case(tmp_path)
    corrupt_first_output(state)
    result = asyncio.run(agent.resume(prepared["research_id"]))
    rid = prepared["research_id"]
    state.stop_research(rid, "BUDGET_EXHAUSTED")
    export_final_report(state, rid)
    if fault in {"artifact", "failure_evidence"}:
        kind = "SKILL_RESULT" if fault == "artifact" else "F3P_FAILURE_EVIDENCE"
        row = db.execute("SELECT relative_path FROM artifacts WHERE artifact_type=? ORDER BY rowid DESC", (kind,)).fetchone()
        path = state.workspace.path(rid, row[0])
        path.write_bytes(b"tampered")
    elif fault == "dataset":
        row = state.dataset_record(prepared["dataset_id"], rid)
        state.workspace.path(rid, row["stored_path"]).write_bytes(b"modified dataset")
    elif fault == "stale_report":
        with db:
            db.execute("UPDATE research_runs SET state_version=state_version+1 WHERE research_id=?", (rid,))
    elif fault == "unresolved_ref":
        state.workspace.path(rid, "research_output/final_report.md").write_bytes(b"{{NUM:unresolved}}")
    else:
        state.workspace.path(rid, "research_output/credentials.env").write_bytes(b"OPENAI_API_KEY=sk-f3p-canary-0123456789")
    with pytest.raises((ReleaseExportError, ArtifactIntegrityError)):
        export_release(state, rid, tmp_path / "bad-release")
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 1
    db.close()


@pytest.mark.parametrize("fault", ["stale_state", "postverify_artifact", "postverify_dataset"])
def test_repair_commit_rechecks_tamper_and_stale_state(tmp_path, fault):
    db, state, agent, prepared = prepare_case(tmp_path)
    corrupt_first_output(state)
    def trigger(point):
        if point != "BEFORE_REPAIR_COMMIT":
            return
        if fault == "stale_state":
            with db:
                db.execute("UPDATE research_runs SET state_version=state_version+1")
        elif fault == "postverify_artifact":
            row = db.execute("SELECT relative_path FROM artifacts WHERE artifact_type='SKILL_RESULT' ORDER BY rowid DESC").fetchone()
            state.workspace.path(prepared["research_id"], row[0]).write_bytes(b"tampered")
        else:
            dataset = state.dataset_record(prepared["dataset_id"], prepared["research_id"])
            state.workspace.path(prepared["research_id"], dataset["stored_path"]).write_bytes(b"tampered")
    agent.repair_faults = FaultInjector(trigger)
    with pytest.raises(StateConflictError):
        asyncio.run(agent.resume(prepared["research_id"]))
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    db.close()


@pytest.mark.parametrize("enabled", [False, True])
def test_ridge_check_optin_is_part_of_full_verifier(tmp_path, enabled):
    db, state, agent, prepared = prepare_case(tmp_path, kind="regression", ridge=enabled)
    result = asyncio.run(agent.resume(prepared["research_id"]))
    verification = json.loads(db.execute(
        "SELECT verification_json FROM staged_mutations WHERE mutation_id=?", (result["mutation_id"],)).fetchone()[0])
    ids = {item["check_id"] for item in verification["checks"]}
    assert ("F3P_RIDGE_ARITHMETIC" in ids) == enabled
    assert all(item["passed"] for item in verification["checks"])
    assert agent._repair_config()["ridge_arithmetic_check"] == enabled
    db.close()


def test_semantic_split_lag_units_and_permission_changes_are_frozen():
    for skill in (association_plan(), regression_plan(), backtest_plan()):
        base = AnalysisPlan(dataset_id=skill.dataset_id, selected_variables=["x", "y"],
                            method=skill.method, justification="frozen", requested_tools=["analysis.skill"],
                            skill_plan=skill)
        changes = {"units": {"x": "different"}, "observation_unit": "different",
                   "sampling": "grouped", "seed": 99}
        if skill.skill_id == "tabular_regression_evaluation_v1":
            changes["holdout_fraction"] = 0.3
        if skill.skill_id == "timeseries_backtest_v1":
            changes["lags"] = [1, 3]
        for field, value in changes.items():
            proposed = base.model_copy(deep=True)
            setattr(proposed.skill_plan, field, value)
            assert not frozen_plan_matches(base, proposed)
        proposed = base.model_copy(update={"requested_tools": ["analysis.skill", "shell.exec"]})
        assert not frozen_plan_matches(base, proposed)


def test_ridge_nonfinite_and_unsupported_states_abstain():
    skill = regression_plan()
    plan = AnalysisPlan(dataset_id=skill.dataset_id, selected_variables=["x", "y"],
                        method=skill.method, justification="fixed", requested_tools=["analysis.skill"],
                        skill_plan=skill)
    result = execute_skill(skill, ["id", "x", "y"], tabular_rows()).model_dump(mode="json")
    result["diagnostics"]["fitted_training_only"]["coefficients"][0] = float("nan")
    assert ridge_arithmetic_check(plan, result, tabular_rows()).status == "needs_reference_check"
    other = plan.model_copy(update={"skill_plan": association_plan(), "method": "pearson_correlation"})
    assert ridge_arithmetic_check(other, {}, []).status == "not_applicable"


def test_reopened_state_service_enforces_recorded_f3p_policy(tmp_path):
    db, state, agent, prepared = prepare_case(tmp_path)
    corrupt_first_output(state)
    def crash(point):
        if point == "AFTER_REPAIRED_EXECUTION":
            raise InjectedCrash(point)
    agent.repair_faults = FaultInjector(crash)
    with pytest.raises(InjectedCrash):
        asyncio.run(agent.resume(prepared["research_id"]))
    pending = db.execute("SELECT mutation_id,contract_id FROM staged_mutations WHERE status='PENDING'").fetchone()
    reopened = StateService(db, Workspace(tmp_path / "workspace"))
    verification = reopened.verify(pending["mutation_id"])
    assert verification.verdict.value == "PASS"
    assert "F3P_INDEPENDENT_ASSOCIATION" in {item.check_id for item in verification.checks}
    with db:
        db.execute("DELETE FROM runtime_steps WHERE step_key=?", (f"worker_plan:{pending['contract_id']}",))
    verification = reopened.verify(pending["mutation_id"])
    assert "F3P_FROZEN_PLAN_AVAILABLE" in verification.errors
    from htrsa.service import VerificationRequiredError
    with pytest.raises(VerificationRequiredError):
        reopened.commit(pending["mutation_id"])
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    db.close()


def test_known_price_reserves_proposal_plus_full_recovery(tmp_path):
    from htrsa.agent_policy import Price, PricingRegistry
    db, state, agent, prepared = prepare_case(tmp_path)
    corrupt_first_output(state)
    agent.pricing = PricingRegistry({("fake", model): Price(1, 1, 1, 0.2) for model in MODELS.values()})
    agent.budget.pricing = agent.pricing
    with db:
        db.execute("UPDATE research_budgets SET target_usd=0.1,soft_limit_usd=0.2,hard_limit_usd=0.205")
    with pytest.raises(RuntimeFailure, match="REPAIR_NOT_STARTED_BUDGET"):
        asyncio.run(agent.resume(prepared["research_id"]))
    assert len(agent.provider.calls) == 3
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    db.close()


@pytest.mark.parametrize("crash", [False, True])
def test_autonomous_loop_repair_keeps_critic_and_followup_flow(tmp_path, crash):
    from htrsa.autonomous_loop import AutonomousResearchLoop
    from test_autonomous_loop import CSV, MODELS as LOOP_MODELS, fake_replies
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    replies = fake_replies()
    replies.insert(4, {"action": "REPAIR", "rationale": "Reexecute the fixed Pearson plan"})
    provider = FakeProvider(replies)
    loop = AutonomousResearchLoop(state, provider, LOOP_MODELS,
                                   verification_repair_enabled=True, verified_analysis_skills_enabled=False)
    corrupt_first_output(state)
    if crash:
        def trigger(point):
            if point == "AFTER_REPAIR_COMMIT":
                raise InjectedCrash(point)
        loop.repair_faults = FaultInjector(trigger)
        with pytest.raises(InjectedCrash):
            asyncio.run(loop.run("Analyze temperature and growth without causality.", CSV))
        rid = db.execute("SELECT research_id FROM research_runs").fetchone()[0]
        remaining = list(provider.replies)
        db.close()
        db = initialize(tmp_path / "state.sqlite")
        state = StateService(db, Workspace(tmp_path / "workspace"))
        loop = AutonomousResearchLoop(state, FakeProvider(remaining), LOOP_MODELS,
                                       verification_repair_enabled=True, verified_analysis_skills_enabled=False)
        result = asyncio.run(loop.resume(rid))
    else:
        result = asyncio.run(loop.run("Analyze temperature and growth without causality.", CSV))
    assert result["stop_reason"] == "GOAL_ANSWERED"
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM critic_reviews").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM evidence WHERE status='VERIFIED'").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name='stats.run'").fetchone()[0] == 3
    db.close()


def test_known_proposal_cost_is_not_reserved_twice(tmp_path):
    from htrsa.agent_policy import Price, PricingRegistry
    db, state, agent, prepared = prepare_case(tmp_path)
    corrupt_first_output(state)
    agent.pricing = PricingRegistry({("fake", model): Price(10000, 10000, 10000, 0.2) for model in MODELS.values()})
    agent.budget.pricing = agent.pricing
    with db:
        db.execute("UPDATE research_budgets SET target_usd=0.1,soft_limit_usd=0.3,hard_limit_usd=0.43")
    result = asyncio.run(agent.resume(prepared["research_id"]))
    assert result["verdict"] == "PASS"
    assert state.budget(prepared["research_id"])["spent_usd"] == pytest.approx(0.4)
    db.close()
