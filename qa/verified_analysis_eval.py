"""개발 테스트와 분리한 오프라인 검증 사례."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter

from probe.analysis_skills import SkillApplicabilityError, SkillPlan, execute_skill


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "qa/fixtures" / "verified_analysis_eval_config.json"
OUTPUT = ROOT / "qa/results" / "verified_analysis_eval_results.json"


def _plan(kind: str, **updates) -> SkillPlan:
    common = {"dataset_id": "D-eval", "dataset_sha256": sha256(b"evaluation fixture v1").hexdigest(),
              "skill_version": "1.0.0",
              "observation_id": "row", "observation_unit": "measurement", "seed": 1907,
              "assumption_evidence": "predeclared independent observational units"}
    if kind == "A":
        values = {"skill_id": "tabular_association_v1", "variables": {"x": "a", "y": "b"},
                  "units": {"a": "unit-a", "b": "unit-b"}, "sampling": "iid",
                  "method": "pearson_correlation", "exclusion_policy": "complete_case"}
    elif kind == "B":
        values = {"skill_id": "tabular_regression_evaluation_v1",
                  "variables": {"feature_1": "a", "target": "b"},
                  "units": {"a": "unit-a", "b": "unit-b"}, "sampling": "iid",
                  "method": "ridge_holdout", "exclusion_policy": "train_only_feature_imputation"}
    else:
        values = {"skill_id": "timeseries_backtest_v1", "variables": {"target": "b"},
                  "units": {"b": "unit-b"}, "sampling": "temporal",
                  "assumption_evidence": "regular daily readings with one known observation per day",
                  "method": "ridge_rolling_origin", "exclusion_policy": "reject_missing",
                  "timestamp_column": "row", "lags": [1, 3]}
    return SkillPlan(**(common | values | updates))


def _case(case_id: str):
    kind = case_id[0]
    number = int(case_id[1:])
    if kind == "A":
        rows = [{"row": str(i), "a": str((i * 7) % 23), "b": str(((i * 7) % 23) * 0.6 + i % 4)}
                for i in range(1, 29)]
        plan = _plan(kind)
        if number == 2:
            plan = _plan(kind, method="spearman_correlation")
            for i, row in enumerate(rows):
                row["b"] = str((i * 11) % 29)
        if number == 3:
            plan = _plan(kind, sampling="grouped")
        if number == 4:
            for row in rows:
                row["a"] = "8"
        return plan, ["row", "a", "b"], rows
    if kind == "B":
        rows = [{"row": f"person-{i}", "a": str(i * 0.5 + i % 4),
                 "b": str(3.0 + i * 1.3 + i % 5)} for i in range(30)]
        plan = _plan(kind)
        if number == 2:
            for i, row in enumerate(rows):
                row["b"] = str((i * 13) % 17)
        if number == 3:
            plan = _plan(kind, sampling="temporal")
        if number == 4:
            rows[1]["row"] = rows[0]["row"]
        return plan, ["row", "a", "b"], rows
    start = datetime(2022, 3, 1, tzinfo=timezone.utc)
    rows = [{"row": (start + timedelta(days=i)).isoformat(),
             "b": str(50 + i * 0.4 + (i % 7) * 0.8)} for i in range(22)]
    plan = _plan(kind)
    if number == 2:
        for i, row in enumerate(rows):
            row["b"] = str(50 + (i * 7) % 11)
    if number == 3:
        rows[8]["row"] = (start + timedelta(days=8, hours=1)).isoformat()
    if number == 4:
        rows = rows[:7]
    return plan, ["row", "b"], rows


def run_offline() -> dict:
    config = json.loads(CONFIG.read_text(encoding="utf-8", errors="strict"))
    results = []
    for case in config["cases"]:
        plan, headers, rows = _case(case["id"])
        started = perf_counter()
        try:
            output = execute_skill(plan, headers, rows)
            status = "succeeded" if output.execution == "succeeded" else output.execution
            reason = None
            metrics = output.metrics
        except SkillApplicabilityError as exc:
            status, reason, metrics = exc.applicability, exc.code, {}
        results.append({"id": case["id"], "skill": case["skill"], "expected": case["expected"],
                        "observed": status, "correct": status == case["expected"],
                        "reason": reason, "metrics": metrics,
                        "local_compute_ms": round((perf_counter() - started) * 1000, 3)})
    supported = [item for item in results if item["expected"] == "succeeded"]
    abstentions = [item for item in results if item["expected"] != "succeeded"]
    report = {"config_sha256": sha256(CONFIG.read_bytes()).hexdigest(),
              "logical_cases": len(results), "supported_correct_completion": sum(item["correct"] for item in supported),
              "supported_total": len(supported),
              "correct_abstention_or_rejection": sum(item["correct"] for item in abstentions),
              "abstention_total": len(abstentions),
              "unsafe_acceptance": sum(item["observed"] == "succeeded" for item in abstentions),
              "unnecessary_rejection": sum(item["observed"] != "succeeded" for item in supported),
              "live_agent_ablation": "NOT_VALIDATED", "api_cost_usd": None,
              "cases": results}
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    serialized.encode("utf-8", errors="strict")
    OUTPUT.write_text(serialized, encoding="utf-8")
    return report
