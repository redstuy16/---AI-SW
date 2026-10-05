"""기존 Reliability Lab에서 외부 정답·결론 일치·복구·검토 비율을 분리해 기록한다."""
import argparse
from copy import deepcopy
import csv
import json
import math
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from cycle5_fixtures import GOLD_ROWS, transform_fixture
from probe.database import to_json
from probe.preflight import _source_fingerprint
from probe.reliability import truth_aware_metrics
from probe.storage import sha256_file
from probe.verification_repair import repair_transformation

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "qa/fixtures/cycle5_gold_fixtures.json"


def save(path, value):
    content = (to_json(value) + "\n").encode("utf-8", errors="strict")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def conclusion(rows):
    try:
        values = [{"year": r["year"], "value": float(r["value"]), "other": r["other"]} for r in rows]
        if any(not math.isfinite(r["value"]) for r in values): return None
        return to_json(sorted(values, key=lambda r: r["year"]))
    except (KeyError, TypeError, ValueError): return None


def corrupted(case):
    rows = deepcopy(GOLD_ROWS)
    if case in {"healthy_absolute", "wrong_offset"}:
        for row in rows: row["value"] = str(float(row["value"]) + 32)
    elif case == "wrong_scale":
        for row in rows: row["value"] = str(float(row["value"]) * 2)
    elif case == "year_swap": rows[0]["value"], rows[2]["value"] = rows[2]["value"], rows[0]["value"]
    elif case == "duplicate": rows.append(deepcopy(rows[0]))
    elif case == "missing": rows.pop()
    elif case == "fabricated": rows[-1]["year"] = "2099"
    elif case == "other_column": rows[0]["other"] = "fabricated"
    elif case == "missing_value": rows[0]["value"] = ""
    return rows


def evaluate(output_dir=None):
    started, fingerprint = perf_counter(), _source_fingerprint()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8", errors="strict"))
    folder = Path(output_dir) if output_dir else ROOT / "build" / ("cycle5-lab-" + uuid4().hex)
    raw, gated, runs = [], [], []
    for case in manifest["cases"]:
        for repeat in range(manifest["repeats"]):
            quantity = "absolute_temperature" if case == "healthy_absolute" else "temperature_difference" if case == "healthy_difference" else "temperature_anomaly"
            supplied = corrupted(case)
            gold = manifest["absolute_gold"] if quantity == "absolute_temperature" else manifest["anomaly_and_difference_gold"]
            observed, expected = conclusion(supplied), conclusion(gold)
            db, state, rid, lineage = transform_fixture(folder / case / str(repeat), supplied, quantity=quantity)
            try:
                check = state.cycle5.verify_lineage(lineage)
                eligible = case in manifest["repair_cases"]
                source = {"fixture_id": case, "gold_ref": "cycle5_gold_fixtures.json#" + ("absolute_gold" if quantity == "absolute_temperature" else "anomaly_and_difference_gold"),
                          "gold_origin": "EXTERNAL_FIXTURE", "gold_conclusion": expected, "observed_conclusion": observed,
                          "outcome": "EMITTED", "completed": observed is not None, "review_required": False,
                          "fault_eligible": eligible, "recovered": False}
                raw.append(source)
                corrected = {**source, "observed_conclusion": observed if check["passed"] else None,
                             "outcome": "ACCEPTED" if check["passed"] else "NEEDS_REVIEW",
                             "completed": check["passed"], "review_required": not check["passed"]}
                repair = None
                if eligible:
                    repair = repair_transformation(state, rid, lineage.record_id, 1, actor_role="owner")
                    path = state.workspace.path(rid, state.file_artifact(lineage.child_id, rid)["relative_path"])
                    with path.open(encoding="utf-8", newline="") as stream:
                        repaired_observation = conclusion(list(csv.DictReader(stream)))
                    corrected.update(observed_conclusion=repaired_observation, outcome="REPAIRED", completed=True, review_required=False, recovered=True)
                gated.append(corrected)
                runs.append({"case": case, "repeat": repeat, "research_id": rid, "parent_hash": lineage.parent_hash,
                             "child_hash": lineage.child_hash, "check": check, "raw_correct_against_external_gold": observed == expected,
                             "expected_detection": not case.startswith("healthy_"), "detected": not check["passed"],
                             "repair": repair, "repaired_correct_against_external_gold": corrected["observed_conclusion"] == expected})
            finally: db.close()
    result = {"source_fingerprint": fingerprint, "source_unchanged": fingerprint == _source_fingerprint(),
              "manifest_sha256": sha256_file(MANIFEST), "scope": manifest["scope"], "runs": runs,
              "raw_metrics": truth_aware_metrics(raw), "gated_metrics": truth_aware_metrics(gated),
              "always_wrong_always_same": truth_aware_metrics([r for r in raw if r["fixture_id"] == "wrong_offset"]),
              "detection": {"correct": sum(r["detected"] == r["expected_detection"] for r in runs), "total": len(runs)},
              "faults_detected": sum(r["detected"] for r in runs if r["expected_detection"]),
              "fault_count": sum(r["expected_detection"] for r in runs),
              "healthy_false_positives": sum(r["detected"] for r in runs if not r["expected_detection"]),
              "repairs_correct": sum(r["repaired_correct_against_external_gold"] for r in runs if r["repair"]),
              "repair_count": sum(r["repair"] is not None for r in runs),
              "api_calls": 0, "api_cost_usd": None, "wall_sec": perf_counter() - started, "live_efficacy": "NOT_VALIDATED"}
    result["all_passed"] = result["source_unchanged"] and result["detection"]["correct"] == len(runs) and result["repairs_correct"] == result["repair_count"]
    save(folder / "evaluation_results.json", result)
    save(ROOT / "qa/results/cycle5_lab_results.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if not args.enable: parser.error("기본 OFF입니다. --enable을 지정하세요")
    result = evaluate(args.output_dir)
    print(to_json({k: result[k] for k in ("all_passed", "detection", "faults_detected", "healthy_false_positives", "repairs_correct", "repair_count", "always_wrong_always_same", "wall_sec")}))
    if not result["all_passed"]: raise SystemExit(1)


if __name__ == "__main__": main()
