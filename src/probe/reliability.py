"""단일 종합 점수 없이 오프라인 비교와 원시 건수를 기록한다."""
from __future__ import annotations

import math
import numpy as np
from .research_slice_schemas import StructuredConclusion


def conclusions_from_snapshot(snapshot: dict, question_id: str) -> list[StructuredConclusion]:
    records = []
    for claim in snapshot["claims"]:
        if not claim["current"]:
            continue
        slots = {s["name"]: s["value"] for s in claim["numeric_slots"]}
        estimate = slots.get("metrics.estimate", slots.get("estimate", slots.get("metrics.candidate_minus_baseline_mae")))
        low, high = slots.get("uncertainty.bounds.0"), slots.get("uncertainty.bounds.1")
        status = claim["effective_support_state"]
        records.append(StructuredConclusion(question_id=question_id, estimand_id=claim["estimand_id"],
            scope=claim["scope"], support_status=status,
            direction="UNKNOWN" if estimate is None else "positive" if estimate > 0 else "negative" if estimate < 0 else "zero",
            estimate=estimate, interval=[low, high] if low is not None and high is not None else None,
            material_claim_ids=[claim["claim_id"]],
            validity_status="NEEDS_REVALIDATION" if status == "NEEDS_REVALIDATION" else "INCOMPLETE" if status in {"INCONCLUSIVE", "NEEDS_REVIEW"} else "VALID",
            limitations=claim["scope"].get("limitations", [])))
    return records


def compatible(a: StructuredConclusion, b: StructuredConclusion) -> bool:
    if (a.question_id, a.estimand_id, a.scope, a.support_status, a.direction, a.validity_status, a.limitations) != (b.question_id, b.estimand_id, b.scope, b.support_status, b.direction, b.validity_status, b.limitations):
        return False
    if a.estimate is None or b.estimate is None:
        if a.estimate != b.estimate:
            return False
    elif not math.isclose(a.estimate, b.estimate, rel_tol=0, abs_tol=1e-10):
        return False
    if a.interval is None or b.interval is None:
        return a.interval == b.interval
    return len(a.interval) == len(b.interval) and all(math.isclose(x, y, rel_tol=0, abs_tol=1e-10) for x, y in zip(a.interval, b.interval))


def error_correlation(a: list[int], b: list[int]) -> dict:
    if len(a) != len(b) or not a or any(x not in {0, 1} for x in a + b):
        raise ValueError("COMPARABLE_BINARY_GOLD_INDICATORS_REQUIRED")
    if np.var(a) == 0 or np.var(b) == 0:
        return {"status": "NOT_ESTIMABLE", "pairs": len(a), "correlation": None}
    return {"status": "OFFLINE_DIAGNOSTIC", "pairs": len(a), "correlation": float(np.corrcoef(a, b)[0, 1])}


def count(numerator, denominator):
    return {"numerator": int(numerator), "denominator": int(denominator)}


def truth_aware_metrics(observations: list[dict]) -> dict:
    """외부 고정 정답과 반복 결과를 분리한다. 내부 검증 PASS는 정답 입력으로 받지 않는다."""
    required = {"fixture_id", "gold_ref", "gold_origin", "gold_conclusion", "observed_conclusion",
                "outcome", "completed", "review_required", "fault_eligible", "recovered"}
    if any(not required <= row.keys() or row["gold_origin"] != "EXTERNAL_FIXTURE" or not row["gold_ref"]
           or row["gold_conclusion"] is None
           or any(type(row[k]) is not bool for k in ("completed", "review_required", "fault_eligible", "recovered")) for row in observations):
        raise ValueError("EXTERNAL_GROUND_TRUTH_REQUIRED")
    groups = {}
    for row in observations:
        key = row["fixture_id"]
        groups.setdefault(key, []).append(row)
    pairs = []
    for rows in groups.values():
        if any(r["gold_ref"] != rows[0]["gold_ref"] or r["gold_conclusion"] != rows[0]["gold_conclusion"] for r in rows):
            raise ValueError("COMPARABLE_EXTERNAL_TRUTH_REQUIRED")
        pairs.extend((rows[0], row) for row in rows[1:])
    def correct(row):
        return row["observed_conclusion"] is not None and row["observed_conclusion"] == row["gold_conclusion"]
    def metric(n, d):
        return {**count(n, d), "rate": n / d if d else None, "status": "OFFLINE_FIXTURE" if d else "NOT_ESTIMABLE"}
    emitted_pairs = [(a, b) for a, b in pairs if a["observed_conclusion"] is not None and b["observed_conclusion"] is not None]
    conditional = [(a, b) for a, b in pairs if correct(a)]
    faults = [r for r in observations if r["fault_eligible"]]
    accepted = [r for r in observations if r["completed"] and not r["review_required"]]
    return {"correctness": metric(sum(correct(r) for r in observations), len(observations)),
            "conclusion_agreement": metric(sum(a["observed_conclusion"] == b["observed_conclusion"] for a, b in emitted_pairs), len(emitted_pairs)),
            "conditional_robustness": metric(sum(correct(b) and a["observed_conclusion"] == b["observed_conclusion"] for a, b in conditional), len(conditional)),
            "outcome_consistency": metric(sum(a["outcome"] == b["outcome"] for a, b in pairs), len(pairs)),
            "recoverability": metric(sum(r["recovered"] and r["completed"] and correct(r) for r in faults), len(faults)),
            "completion_review_tradeoff": {"completion": metric(sum(r["completed"] for r in observations), len(observations)),
                "review": metric(sum(r["review_required"] for r in observations), len(observations)),
                "accepted_correctness": metric(sum(correct(r) for r in accepted), len(accepted))},
            "ground_truth": "EXTERNAL_FIXTURE", "live_efficacy": "NOT_VALIDATED"}
