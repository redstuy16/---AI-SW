"""기존 실행기와 QA 도구의 별도 F3-P 합성 오류 평가."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter, process_time
from unittest.mock import patch

import numpy as np

from probe.agent_runtime import AgentRuntime, RuntimeFailure
from probe.analysis_skills import SkillPlan
from probe.database import initialize
from probe.final_report import export_final_report
from probe.providers.fake import FakeProvider
from probe.release import export_release
from probe.schemas import VerificationCheck, Verdict
from probe.service import StateService
from probe.storage import Workspace


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "qa/fixtures/f3p_eval_config.json"
MODELS = {"manager": "fake-manager", "experiment_coordinator": "fake-coordinator",
          "analysis_planner_worker": "fake-worker"}


def prepare(folder: Path, *, legacy: bool = False, seed: int = 3911, mutate: bool = False, slice_config=None):
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    x = rng.normal(10, 3, 29)
    y = 0.8 * x + rng.normal(0, 1, 29)
    content = "unit,a,b\n" + "".join(f"u{i},{a:.12g},{b:.12g}\n" for i, (a, b) in enumerate(zip(x, y)))
    source = folder / "evaluation.csv"
    source.write_bytes(content.encode("utf-8", errors="strict"))
    # 정답 기준값은 실행 완료 후 평가기만 사용한다.
    reference = float(np.corrcoef(x, y)[0, 1])
    db = initialize(folder / "state.sqlite")
    state = StateService(db, Workspace(folder / "workspace"))
    tools = ["stats.run", "visualization.render", "evidence.record"] if legacy else ["analysis.skill", "evidence.record"]
    def coordinator(call):
        active = json.loads(call["input_text"])["active_state"]
        return {"decision_type": "DELEGATE", "objective": "Evaluate the fixed observed pair",
                "rationale": "Documented independent units", "assigned_role": "analysis_planner_worker",
                "input_refs": [{"type": "dataset", "id": active["datasets"][0]["dataset_id"]},
                               {"type": "artifact", "id": active["profile_artifacts"][0]["artifact_id"]}],
                "allowed_tools": tools, "max_tool_calls": len(tools), "max_retries": 0,
                "max_runtime_sec": 120, "max_cost_usd": 0.5}
    def worker(_call):
        dataset = state.dataset_record(prepared["dataset_id"], prepared["research_id"])
        skill = SkillPlan(
            skill_id="tabular_association_v1", skill_version="1.0.0", dataset_id=dataset["dataset_id"],
            dataset_sha256=dataset["sha256"], variables={"x": "a", "y": "b"}, observation_id="unit",
            observation_unit="measurement", units={"a": "a-units", "b": "b-units"}, sampling="iid",
            assumption_evidence="predeclared separately sampled observation units",
            method="pearson_correlation", exclusion_policy="complete_case", seed=seed)
        return {"dataset_id": dataset["dataset_id"], "selected_variables": ["a", "b"],
                "method": "pearson_correlation", "justification": "Documented sampling",
                "requested_tools": tools, "skill_plan": None if legacy else skill.model_dump(mode="json")}
    def repair(call):
        decision = {"action": "REPAIR", "rationale": "Repeat trusted execution of unchanged frozen inputs"}
        if mutate:
            request = json.loads(json.loads(call["input_text"])["active_state"]["recent_failure"])
            proposed = request["frozen_plan"]
            proposed["method"] = "spearman_correlation"
            proposed["skill_plan"]["method"] = "spearman_correlation"
            decision["proposed_plan"] = proposed
        return decision
    provider = FakeProvider([
        {"decision_type": "INITIAL_PLAN", "research_question": "Association in the fixed sample?",
         "rationale": "Evaluate declared units", "coordinator_role": "experiment_coordinator", "objective": "Evaluate pair"},
        coordinator, worker, repair, repair])
    runtime = AgentRuntime(state, provider, MODELS, verified_analysis_skills_enabled=not legacy,
                           verification_repair_enabled=True)
    if slice_config is not None:
        runtime.research_slice_config = slice_config
    prepared = asyncio.run(runtime.prepare("Association in the fixed sample?", source))
    return db, state, runtime, prepared, reference


def run_case(case: dict, folder: Path, seed: int, *, slice_config=None) -> dict:
    case_id = case["id"]
    wall, cpu = perf_counter(), process_time()
    db, state, runtime, prepared, oracle = prepare(
        folder, legacy=case_id == "successful_legacy_repair", seed=seed,
        mutate=case_id == "semantic_mutation", slice_config=slice_config)
    stage = state.stage
    evaluate = state._evaluate
    counter = {"stages": 0, "verifications": 0}
    def injected_stage(payload):
        counter["stages"] += 1
        if counter["stages"] == 1 and case_id not in {"faulty_checker", "shared_wrong", "healthy_control"}:
            payload.agent_result.output = deepcopy(payload.agent_result.output)
            if "metrics" in payload.agent_result.output:
                payload.agent_result.output["metrics"]["estimate"] = -0.2
            else:
                payload.agent_result.output["estimate"] = -0.2
        if case_id == "compound_defect":
            payload.scientific.numeric_provenance["metrics.estimate"].dataset_sha256 = "f" * 64
        return stage(payload)
    def injected_evaluate(row):
        counter["verifications"] += 1
        result = evaluate(row)
        if case_id in {"faulty_checker", "unsupported_repair"}:
            name = "RESULT_FIELD_MATCH" if case_id == "faulty_checker" else "UNSUPPORTED_REPAIR_SCOPE"
            result.checks.append(VerificationCheck(check_id=name, passed=False, message="evaluation checker fixture"))
            result.errors.append(name)
            result.verdict = Verdict.FAIL
        return result
    state.stage, state._evaluate = injected_stage, injected_evaluate
    if case_id == "insufficient_budget":
        with db:
            db.execute("UPDATE research_budgets SET target_usd=0.001,soft_limit_usd=0.003,hard_limit_usd=0.005")
    import probe.real_tools as real_tools
    execute = real_tools.execute_skill
    def shared_wrong(*args):
        result = execute(*args)
        if case_id == "shared_wrong":
            result.metrics["estimate"] = -0.2
        return result
    result, status = None, None
    try:
        with patch.object(real_tools, "execute_skill", shared_wrong):
            result = asyncio.run(runtime.resume(prepared["research_id"]))
        status = result["verdict"]
    except RuntimeFailure as exc:
        status = exc.code
    committed = db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0]
    final_correct = False
    if result is not None:
        estimate = result["result"].get("metrics", {}).get("estimate", result["result"].get("estimate"))
        final_correct = bool(np.isclose(estimate, oracle, rtol=1e-10, atol=1e-10))
    repair_attempts = db.execute(
        "SELECT COUNT(*) FROM contracts WHERE json_extract(contract_json,'$.task_type')='analysis_repair'").fetchone()[0]
    analysis_executions = db.execute(
        "SELECT COUNT(*) FROM tool_calls WHERE tool_name IN ('analysis.skill','stats.run')").fetchone()[0]
    release = None
    if status == "PASS":
        state.stop_research(prepared["research_id"], "BUDGET_EXHAUSTED")
        export_final_report(state, prepared["research_id"])
        release = export_release(state, prepared["research_id"], folder / "release")
    observed = {"id": case_id, "expected": case["expected"], "observed": status,
                "fixture_correct": status == case["expected"] and (final_correct if status == "PASS" else committed == 0),
                "initially_correct": case["initially_correct"], "final_correct": final_correct,
                "committed": committed, "repair_attempts": repair_attempts,
                "analysis_executions": analysis_executions, "verification_executions": counter["verifications"],
                "api_calls": 0, "api_tokens": None, "api_cost_usd": None,
                "fake_provider_calls": len(runtime.provider.calls),
                "local_execution_ms": round((process_time() - cpu) * 1000, 3),
                "wall_clock_ms": round((perf_counter() - wall) * 1000, 3),
                "release_file_count": len(release["files"]) if release else None}
    db.close()
    return observed


def run_offline(output_dir: Path | None = None) -> dict:
    config = json.loads(CONFIG.read_text(encoding="utf-8", errors="strict"))
    output_dir = output_dir or ROOT / "build/f3p-evaluation"
    output_dir.mkdir(parents=True, exist_ok=True)
    # 평가마다 새 작업 ID와 디렉터리를 사용한다.
    from uuid import uuid4
    folder = output_dir / uuid4().hex
    results = [run_case(case, folder / case["id"], config["dataset_seed"]) for case in config["cases"]]
    completed = [item for item in results if item["committed"]]
    blocked = [item for item in results if not item["committed"]]
    report = {
        "config_sha256": sha256(CONFIG.read_bytes()).hexdigest(), "logical_cases": len(results),
        "wrong_to_right": sum(not item["initially_correct"] and item["final_correct"] for item in completed),
        "right_to_wrong": sum(item["initially_correct"] and not item["final_correct"] for item in completed),
        "correctly_blocked": sum(item["fixture_correct"] for item in blocked),
        "incorrectly_blocked": sum(item["expected"] == "PASS" for item in blocked),
        "incomplete_due_to_budget": sum(item["observed"] == "REPAIR_NOT_STARTED_BUDGET" for item in results),
        "checker_faults_detected": sum(item["observed"] == "CHECKER_QUALIFICATION_FAILED" for item in results),
        "contract_mutations_blocked": sum(item["observed"] == "REPAIR_CONTRACT_MUTATION_BLOCKED" for item in results),
        "unresolved_disagreements": sum(item["observed"] == "REPAIR_UNRESOLVED" for item in results),
        "repair_success_rate": sum(item["final_correct"] for item in results) /
                               max(1, sum(item["repair_attempts"] > 0 for item in results)),
        "repair_incompletion_rate": sum(item["observed"] == "REPAIR_INCOMPLETE" for item in results) / len(results),
        "all_fixtures_passed": all(item["fixture_correct"] for item in results),
        "live_efficacy": "NOT_VALIDATED", "api_cost_usd": None, "api_tokens": None,
        "note": "Synthetic offline fault/repair fixtures with FakeProvider; no live performance inference.",
        "cases": results}
    content = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    data = content.encode("utf-8", errors="strict")
    (ROOT / "qa/results/f3p_eval_results.json").write_bytes(data)
    return report
