"""기존 동작과 검증 경계를 확인하는 회귀 테스트."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError
from scipy import stats

from probe.analysis_skills import SkillApplicabilityError, SkillPlan, SkillRequest, execute_skill
from probe.agent_runtime import AgentRuntime
from probe.agent_policy import validate_contract
from probe.database import initialize
from probe.final_report import export_final_report
from probe.providers.fake import FakeProvider
from probe.recovery import FaultInjector, InjectedCrash
from probe.real_tools import VerifiedAnalysisSkillTool
from probe.release import export_release, ReleaseExportError
from probe.schemas import ContextRef, RefType, ResearchContract
from probe.service import ContractViolationError, StateConflictError, StateService
from probe.storage import Workspace


HASH = "a" * 64
MODELS = {"manager": "fake-manager", "experiment_coordinator": "fake-coordinator",
          "analysis_planner_worker": "fake-worker"}


def association_plan(**changes):
    value = dict(skill_id="tabular_association_v1", skill_version="1.0.0", dataset_id="D-test", dataset_sha256=HASH,
                 variables={"x": "x", "y": "y"}, observation_id="id", observation_unit="person",
                 units={"x": "kg", "y": "cm"}, sampling="iid",
                 assumption_evidence="randomly sampled distinct people", method="pearson_correlation",
                 exclusion_policy="complete_case", seed=7)
    return SkillPlan(**(value | changes))


def regression_plan(**changes):
    value = dict(skill_id="tabular_regression_evaluation_v1", skill_version="1.0.0", dataset_id="D-test",
                 dataset_sha256=HASH, variables={"feature_1": "x", "target": "y"},
                 observation_id="id", observation_unit="person", units={"x": "kg", "y": "cm"},
                 sampling="iid", assumption_evidence="randomly sampled distinct people",
                 method="ridge_holdout", exclusion_policy="train_only_feature_imputation", seed=7)
    return SkillPlan(**(value | changes))


def backtest_plan(**changes):
    value = dict(skill_id="timeseries_backtest_v1", skill_version="1.0.0", dataset_id="D-test", dataset_sha256=HASH,
                 variables={"target": "y"}, observation_id="time", observation_unit="day",
                 units={"y": "units"}, sampling="temporal",
                 assumption_evidence="daily regularly sampled measurements", method="ridge_rolling_origin",
                 exclusion_policy="reject_missing", seed=7, timestamp_column="time", lags=[1, 2])
    return SkillPlan(**(value | changes))


def tabular_rows(n=20):
    return [{"id": str(i), "x": str(i), "y": str(2 * i + (i % 3))} for i in range(n)]


def series_rows(n=16):
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    return [{"time": (start + timedelta(days=i)).isoformat(), "y": str(i + (i % 3))}
            for i in range(n)]


def test_association_matches_scipy_and_counts_exclusions():
    rows = tabular_rows(10) + [{"id": "10", "x": "", "y": "5"},
                               {"id": "11", "x": "NaN", "y": "4"}]
    result = execute_skill(association_plan(), ["id", "x", "y"], rows)
    expected = stats.pearsonr([float(row["x"]) for row in rows[:10]],
                              [float(row["y"]) for row in rows[:10]])
    assert result.metrics["estimate"] == pytest.approx(expected.statistic)
    assert result.metrics["p_value"] == pytest.approx(expected.pvalue)
    assert result.counts == {"total": 12, "missing_excluded": 1, "nonfinite_excluded": 1, "used": 10}
    assert result.uncertainty["status"] == "estimated"


def test_association_review_and_undefined_cases():
    with pytest.raises(SkillApplicabilityError, match="INDEPENDENCE_UNDOCUMENTED"):
        execute_skill(association_plan(sampling="grouped"), ["id", "x", "y"], tabular_rows())
    with pytest.raises(SkillApplicabilityError, match="CONSTANT_INPUT"):
        execute_skill(association_plan(), ["id", "x", "y"],
                      [{**row, "y": "1"} for row in tabular_rows()])
    with pytest.raises(SkillApplicabilityError, match="DUPLICATE_OBSERVATION_ID"):
        execute_skill(association_plan(), ["id", "x", "y"],
                      [{**row, "id": "same"} for row in tabular_rows()])
    result = execute_skill(association_plan(method="spearman_correlation"), ["id", "x", "y"], tabular_rows())
    assert result.uncertainty["status"] == "not_estimated"


def test_regression_reference_baseline_and_training_only_sentinel():
    rows = tabular_rows()
    rows[3]["x"] = ""
    plan = regression_plan()
    first = execute_skill(plan, ["id", "x", "y"], rows)
    permutation = np.random.default_rng(7).permutation(20)
    test_n = 5
    target = np.asarray([float(row["y"]) for row in rows])
    baseline = np.mean(target[permutation[test_n:]])
    reference_mae = np.mean(np.abs(target[permutation[:test_n]] - baseline))
    assert first.metrics["baseline"]["mae"] == pytest.approx(reference_mae)
    altered = [dict(row) for row in rows]
    for index in permutation[:test_n]:
        altered[index]["y"] = str(float(altered[index]["y"]) + 1000)
        altered[index]["x"] = "99999"
    second = execute_skill(plan, ["id", "x", "y"], altered)
    assert first.diagnostics["fitted_training_only"] == second.diagnostics["fitted_training_only"]
    assert first.metrics != second.metrics


def test_regression_rejects_leakage_and_infeasible_split():
    with pytest.raises(ValidationError):
        regression_plan(variables={"feature_1": "y", "target": "y"})
    with pytest.raises(SkillApplicabilityError, match="IID_UNDOCUMENTED"):
        execute_skill(regression_plan(sampling="temporal"), ["id", "x", "y"], tabular_rows())
    with pytest.raises(SkillApplicabilityError, match="INFEASIBLE_HOLDOUT"):
        execute_skill(regression_plan(), ["id", "x", "y"], tabular_rows(5))
    with pytest.raises(SkillApplicabilityError, match="DUPLICATE_OBSERVATION_ID"):
        execute_skill(regression_plan(), ["id", "x", "y"],
                      [{**row, "id": "same"} for row in tabular_rows()])
    with pytest.raises(SkillApplicabilityError, match="REPEATED_ENTITY"):
        execute_skill(regression_plan(entity_column="entity"), ["id", "x", "y", "entity"],
                      [{**row, "entity": str(int(row["id"]) // 2)} for row in tabular_rows()])


def test_timeseries_cutoffs_and_future_perturbation():
    rows = series_rows()
    plan = backtest_plan()
    first = execute_skill(plan, ["time", "y"], rows)
    assert first.trace and all(item["training_label_cutoff"] == item["origin"]
                               for item in first.trace)
    assert first.uncertainty["status"] == "not_estimated"
    changed = [dict(row) for row in rows]
    changed[-1]["y"] = "9999"
    second = execute_skill(plan, ["time", "y"], changed)
    assert first.trace[:-1] == second.trace[:-1]
    assert first.metrics != second.metrics


def test_timeseries_rejects_leaking_lags_and_time_structure():
    with pytest.raises(ValidationError):
        backtest_plan(lags=[0, 1])
    with pytest.raises(SkillApplicabilityError, match="IRREGULAR_OR_UNORDERED_TIME"):
        rows = series_rows()
        rows[4]["time"] = "2020-01-05T01:00:00+00:00"
        execute_skill(backtest_plan(), ["time", "y"], rows)
    with pytest.raises(SkillApplicabilityError, match="DUPLICATE_TIMESTAMP"):
        rows = series_rows()
        rows[4]["time"] = rows[3]["time"]
        execute_skill(backtest_plan(), ["time", "y"], rows)
    with pytest.raises(SkillApplicabilityError, match="INSUFFICIENT_HISTORY"):
        execute_skill(backtest_plan(), ["time", "y"], series_rows(7))


def test_request_rejects_bad_version_and_fingerprint():
    plan = association_plan()
    with pytest.raises(ValidationError):
        association_plan(skill_version="2.0.0")
    with pytest.raises(ValidationError):
        SkillPlan.model_validate({key: value for key, value in plan.model_dump().items()
                                  if key != "skill_version"})
    with pytest.raises(ValidationError):
        SkillRequest(research_id="R", task_id="T", contract_id="C", plan_ref="C", plan_version="1",
                     plan_hash="bad", plan=plan)
    with pytest.raises(ValidationError):
        SkillRequest(research_id="R", task_id="T", contract_id="C", plan_ref="C",
                     plan_hash=plan.fingerprint(), plan=plan)


@pytest.mark.parametrize("crash_boundary", [None, "AFTER_STAGE", "AFTER_VERIFY", "AFTER_COMMIT"])
def test_enabled_agent_skill_verification_resume_report_and_release(tmp_path, crash_boundary):
    source = tmp_path / "skill.csv"
    source.write_text("id,x,y\n" + "".join(f"{row['id']},{row['x']},{row['y']}\n"
                                             for row in tabular_rows()), encoding="utf-8")
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))

    def coordinator(call):
        active = json.loads(call["input_text"])["active_state"]
        return {"decision_type": "DELEGATE", "objective": "Analyze the declared IID pair",
                "rationale": "Use the fixed association Skill", "assigned_role": "analysis_planner_worker",
                "input_refs": [{"type": "dataset", "id": active["datasets"][0]["dataset_id"]},
                               {"type": "artifact", "id": active["profile_artifacts"][0]["artifact_id"]}],
                "allowed_tools": ["analysis.skill", "evidence.record"], "max_tool_calls": 2,
                "max_retries": 0, "max_runtime_sec": 120, "max_cost_usd": 0.5}

    def worker(call):
        active = json.loads(call["input_text"])["active_state"]
        dataset = active["skill_dataset_hashes"][0]
        plan = association_plan(dataset_id=dataset["dataset_id"], dataset_sha256=dataset["sha256"])
        return {"dataset_id": dataset["dataset_id"], "selected_variables": ["x", "y"],
                "method": "pearson_correlation", "justification": "Declared IID sampling",
                "requested_tools": ["analysis.skill", "evidence.record"],
                "skill_plan": plan.model_dump(mode="json")}

    manager = {"decision_type": "INITIAL_PLAN", "research_question": "Pair association?",
               "rationale": "Use the declared dataset", "coordinator_role": "experiment_coordinator",
               "objective": "Analyze the pair"}
    agent = AgentRuntime(state, FakeProvider([manager, coordinator, worker]), MODELS,
                         verified_analysis_skills_enabled=True)
    prepared = asyncio.run(agent.prepare("Pair association?", source))
    if crash_boundary:
        contract, task_id = state.contract(prepared["active_contract_ids"][1])
        def crash(point):
            if point == crash_boundary:
                raise InjectedCrash(point)
        with pytest.raises(InjectedCrash):
            asyncio.run(agent._run_worker(contract, task_id, prepared, faults=FaultInjector(crash)))
    result = asyncio.run(agent.resume(prepared["research_id"]))
    assert result["verdict"] == "PASS"
    assert result["result"]["applicability"] == "applicable"
    assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name='analysis.skill'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name='stats.run'").fetchone()[0] == 0
    assert {row[0] for row in db.execute("SELECT artifact_type FROM artifacts WHERE artifact_type LIKE 'SKILL_%'")} == {"SKILL_PLAN", "SKILL_RESULT"}
    replay = asyncio.run(agent.resume(result["research_id"]))
    assert replay["already_completed"]
    assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name='analysis.skill'").fetchone()[0] == 1
    state.stop_research(result["research_id"], "BUDGET_EXHAUSTED")
    exported_report = export_final_report(state, result["research_id"])
    assert "pearson_correlation" in Path(exported_report["report"]).read_text(encoding="utf-8")
    release = export_release(state, result["research_id"], tmp_path / "release")
    assert release["files"] and exported_report["report"]
    assert len([item for item in release["files"] if item["path"].startswith("analysis_artifacts/")]) == 3
    assert all(not item["path"].startswith("C:") for item in release["files"])
    skill_artifact = db.execute("SELECT relative_path FROM artifacts WHERE artifact_type='SKILL_RESULT'").fetchone()[0]
    (tmp_path / "workspace" / result["research_id"] / skill_artifact).write_text("tampered", encoding="utf-8")
    with pytest.raises(ReleaseExportError):
        export_release(state, result["research_id"], tmp_path / "release-tampered")
    db.close()


def test_default_off_and_resume_config_guard(tmp_path):
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    agent = AgentRuntime(state, FakeProvider([]), MODELS, verified_analysis_skills_enabled=False)
    assert "analysis.skill" not in agent._registry("unused")._tools
    rid = state.create_research("config")
    forbidden = ResearchContract(contract_id="C-disabled", research_id=rid, task_type="analysis",
                                 issued_by="experiment_coordinator", assigned_role="analysis_planner_worker",
                                 objective="must remain off", allowed_tools=["analysis.skill"],
                                 output_schema_id="AnalysisPlan")
    with pytest.raises(ContractViolationError, match="disabled"):
        validate_contract(state, forbidden, expected_research_id=rid, remaining_usd=1.0)
    state.finish_runtime_step(rid, "verified_analysis_skills_config", agent._skill_config())
    other = AgentRuntime(state, FakeProvider([]), MODELS, verified_analysis_skills_enabled=True)
    with pytest.raises(StateConflictError, match="RECOVERY_SKILL_VERSION_OR_CONFIG_MISMATCH"):
        other._check_skill_config(rid)
    db.close()


@pytest.mark.parametrize("kind", ["regression", "timeseries"])
def test_other_skill_paths_commit_and_export(kind, tmp_path):
    if kind == "regression":
        rows, headers, select = tabular_rows(), ["id", "x", "y"], ["x", "y"]
        factory = regression_plan
    else:
        rows, headers, select = series_rows(), ["time", "y"], ["time", "y"]
        factory = backtest_plan
    source = tmp_path / "input.csv"
    source.write_text(",".join(headers) + "\n" + "".join(
        ",".join(row[name] for name in headers) + "\n" for row in rows), encoding="utf-8")
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    manager = {"decision_type": "INITIAL_PLAN", "research_question": "Evaluate fixed method",
               "rationale": "Use the dataset", "coordinator_role": "experiment_coordinator",
               "objective": "Evaluate fixed method"}

    def coordinator(call):
        active = json.loads(call["input_text"])["active_state"]
        return {"decision_type": "DELEGATE", "objective": "Evaluate fixed method",
                "rationale": "Use Skill", "assigned_role": "analysis_planner_worker",
                "input_refs": [{"type": "dataset", "id": active["datasets"][0]["dataset_id"]},
                               {"type": "artifact", "id": active["profile_artifacts"][0]["artifact_id"]}],
                "allowed_tools": ["analysis.skill", "evidence.record"], "max_tool_calls": 2,
                "max_retries": 0, "max_runtime_sec": 120, "max_cost_usd": 0.5}

    def worker(call):
        active = json.loads(call["input_text"])["active_state"]
        dataset = active["skill_dataset_hashes"][0]
        plan = factory(dataset_id=dataset["dataset_id"], dataset_sha256=dataset["sha256"])
        return {"dataset_id": dataset["dataset_id"], "selected_variables": select,
                "method": plan.method, "justification": "Documented structure",
                "requested_tools": ["analysis.skill", "evidence.record"],
                "skill_plan": plan.model_dump(mode="json")}

    agent = AgentRuntime(state, FakeProvider([manager, coordinator, worker]), MODELS,
                         verified_analysis_skills_enabled=True)
    outcome = asyncio.run(agent.run("Evaluate fixed method", source))
    assert outcome["verdict"] == "PASS"
    assert outcome["result"]["metrics"]["baseline"]["mae"] >= 0
    assert outcome["result"]["uncertainty"]["status"] == "not_estimated"
    state.stop_research(outcome["research_id"], "BUDGET_EXHAUSTED")
    export_final_report(state, outcome["research_id"])
    release = export_release(state, outcome["research_id"], tmp_path / "release")
    assert release["files"]
    assert len([item for item in release["files"] if item["path"].startswith("analysis_artifacts/")]) == 3
    db.close()
