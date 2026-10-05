"""기존 실행기와 고정 정답 자료의 선택적 오프라인 쌍 비교."""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from time import perf_counter, process_time
from unittest.mock import patch
from uuid import UUID, uuid4

from f3p_eval import prepare, run_case, CONFIG as F3P_CONFIG, ROOT
from probe.database import to_json
from probe.literature import LiteralSentenceReviewer, extract_abstract_evidence
from probe.reliability import conclusions_from_snapshot, compatible, count, error_correlation
from probe.research_slice import digest
from probe.research_slice_schemas import ResearchSliceConfig, StructuredConclusion
from probe.research_schemas import HypothesisProposal
from probe.scholarly import NormalizedSource, source_from_row
from probe.storage import sha256_file

MANIFEST = ROOT / "qa/fixtures/reliability_lab_fixtures.json"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((to_json(value) + "\n").encode("utf-8", errors="strict"))


def policy(p="P2", v="V2"):
    return ResearchSliceConfig(claim_evidence_provenance=p != "P0", revision_invalidation=p == "P2",
                               verifier_dependency_catalog=v != "V0", dependency_metadata=v == "V2",
                               reliability_lab=True)


def fixed_ids():
    # 쌍 비교마다 ID를 초기화해 계약·계획·데이터를 맞추고 디렉터리는 분리한다.
    
    counter = iter(range(1, 100000))
    return patch("probe.schemas.uuid4", side_effect=lambda: UUID(int=next(counter)))


def source(state, rid, suffix, *, contradiction=False, hypothesis=None):
    text = "Temperature and growth show no association in this sample." if contradiction else "Temperature and growth show a positive association in this sample."
    candidate = NormalizedSource(title="Temperature and growth study " + suffix, authors=["Offline Fixture"], abstract=text,
                                 url="https://example.org/" + suffix, provider="fake")
    sid, _ = state.upsert_source(rid, candidate)
    state._db.execute("UPDATE sources SET status='RELEVANT' WHERE source_id=?", (sid,))
    actual = source_from_row(state._one("SELECT * FROM sources WHERE source_id=?", (sid,)))
    item = extract_abstract_evidence(sid, actual, hypothesis)
    eid = state.verify_literature_evidence(rid, item, LiteralSentenceReviewer())
    claim = next((c for c in state.research_slice.current(rid) if c.created_from == eid), None)
    return sid, claim


def provenance_case(case, folder, p, manifest):
    wall, cpu = perf_counter(), process_time()
    cfg = policy(p)
    db, state, runtime, prepared, reference = prepare(folder, seed=manifest["seed"], slice_config=cfg)
    rid, case_id = prepared["research_id"], case["id"]
    actual_contracts = []
    stage = state.stage
    def defect(payload):
        mid = stage(payload)
        actual_contracts.append(digest(state.contract(payload.agent_result.contract_id)[0]))
        if p != "P0" and case_id in {"wrong_numeric_locator", "wrong_unit", "missing_required_claim"}:
            value = json.loads(db.execute("SELECT payload_json FROM slice_staging WHERE mutation_id=?", (mid,)).fetchone()[0])
            if case_id == "wrong_numeric_locator": value["claim"]["numeric_slots"][0]["locator"] = "/result/never_present"
            elif case_id == "wrong_unit": value["claim"]["numeric_slots"][0]["unit"] = "kilograms"
            else: value["claim"]["numeric_slots"] = []
            db.execute("UPDATE slice_staging SET payload_json=? WHERE mutation_id=?", (to_json(value), mid))
        return mid
    state.stage = defect
    observed, fixture_correct, current = None, True, None
    try:
        asyncio.run(runtime.resume(rid))
        current = next(iter(state.research_slice.current(rid)), None)
        if case_id in {"url_without_support", "abstract_as_full_text", "multiple_supports", "contradiction"}:
            hypothesis = None
            if case_id == "contradiction":
                hypothesis = state.create_hypothesis(rid, HypothesisProposal(statement="Temperature is associated with growth.", rationale="Offline fixed proposition", score={k: 0.5 for k in ("plausibility", "testability", "data_availability", "information_value", "cost")}), created_by="fixture")
            sid, current = source(state, rid, "A", hypothesis=hypothesis)
            if p != "P0":
                claim, bindings = state.research_slice._next_revision(current, state.research_slice.bindings(current))
                if case_id == "url_without_support": claim.text = "This URL proves a causal treatment effect."
                elif case_id == "abstract_as_full_text": bindings[0].locator["access_level"] = "FULL_TEXT"
                else:
                    _, second = source(state, rid, "B", contradiction=case_id == "contradiction", hypothesis=hypothesis)
                    b = state.research_slice.bindings(second)[0].model_copy(deep=True)
                    b.claim_id, b.claim_revision, b.scope = claim.claim_id, claim.revision, claim.scope
                    b.binding_id += "-additional"
                    bindings.append(b)
                current = state.publish_claim(claim, bindings)
                if case_id == "multiple_supports":
                    state.invalidate_source(rid, sid, "one support withdrawn")
                    current = state.revalidate_claim(rid, current.claim_id)
        elif case_id == "parent_revision":
            state.invalidate_dataset(rid, prepared["dataset_id"], "new dataset snapshot")
        elif case_id == "stale_pass":
            if current:
                aid = state.research_slice.bindings(current)[0].target_id
                state.invalidate_claim_parent(rid, "artifact", aid, "result invalidation")
                db.execute("UPDATE artifacts SET status='INVALIDATED' WHERE artifact_id=?", (aid,))
        elif case_id == "legitimate_rounding" and current:
            claim, bindings = state.research_slice._next_revision(current, state.research_slice.bindings(current))
            slot = next(s for s in claim.numeric_slots if s.name == "metrics.estimate")
            slot.value, slot.display_precision, slot.tolerance_policy = round(slot.value, 3), 3, "ROUND_HALF_EVEN"
            current = state.publish_claim(claim, bindings)
        if p == "P0":
            observed = "UNEXAMINED_METADATA"
        else:
            snapshot = state.research_slice.snapshot(rid)
            current_id = current.claim_id
            observed = next(c["effective_support_state"] for c in snapshot["claims"] if c["current"] and c["claim_id"] == current_id)
    except ValueError:
        observed = "BLOCKED"
    fixture_correct = observed == ("UNEXAMINED_METADATA" if p == "P0" else case["allowed"])
    snapshot = state.research_slice.snapshot(rid)
    claims = [c for c in snapshot["claims"] if c["current"]] if cfg.claim_evidence_provenance else []
    # 필수 항목은 생성된 주장과 독립적인 선언 파일에서 가져온다.
    valid_numeric = set()
    for c in claims:
        if c["effective_support_state"] == "SUPPORTED":
            valid_numeric.update(s["name"] for s in c["numeric_slots"] if s["name"] in manifest["required_numeric_names"])
    accepted = [c for c in claims if c["effective_support_state"] in {"SUPPORTED", "CONFLICTED", "NOT_SUPPORTED"}]
    bindings = [b for b in snapshot["bindings"] if any(c["claim_id"] == b["claim_id"] and c["revision"] == b["claim_revision"] for c in claims)] if cfg.claim_evidence_provenance else []
    valid_bindings = 0
    for c in state.research_slice.current(rid):
        for binding in state.research_slice.bindings(c):
            if binding.status != "ACTIVE":
                continue
            try:
                state.research_slice.validate(c, [binding])
                valid_bindings += 1
            except Exception:
                pass
    frozen_plan = state.runtime_step(rid, next(r[0] for r in db.execute("SELECT step_key FROM runtime_steps WHERE research_id=? AND step_key LIKE 'worker_plan:%' LIMIT 1", (rid,))))
    row = {"fixture": case_id, "arm": p, "gold_obligation": case["gold"], "expected": case["allowed"], "observed": observed,
           "fixture_correct": fixture_correct, "contract_hashes": actual_contracts, "plan_hash": digest(frozen_plan["output"]),
           "dataset_hash": db.execute("SELECT sha256 FROM datasets LIMIT 1").fetchone()[0], "seed": manifest["seed"],
           "required_claims": 1, "emitted_current_claims": len(claims), "accepted_current_claims": len(accepted),
           "required_numeric_slots": len(manifest["required_numeric_names"]), "validated_numeric_slots": len(valid_numeric),
           "current_bindings": len(bindings), "active_bindings": sum(b["status"] == "ACTIVE" for b in bindings),
           "valid_current_bindings": valid_bindings,
           "contradictions_required": int(case_id == "contradiction"), "contradictions_recalled": int(observed == "CONFLICTED"),
           "unsupported_leakage": int(p != "P0" and case["allowed"] == "BLOCKED" and observed != "BLOCKED"),
           "stale_leakage": int(p != "P0" and case_id in {"parent_revision", "stale_pass"} and observed == "SUPPORTED"),
           "commits": db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0],
           "api_calls": 0, "api_tokens": None, "api_cost_usd": None, "wall_ms": (perf_counter() - wall) * 1000,
           "local_compute_ms": (process_time() - cpu) * 1000}
    db.close()
    return row


def perturbation_case(folder, variant, manifest, *, evidence_change=False):
    db, state, runtime, prepared, reference = prepare(folder, seed=manifest["seed"], slice_config=policy())
    supplied = [{"id": "source-A", "span": "Fixed observed measurements."}, {"id": "source-B", "span": "Observational sampling is not causal."}]
    run = runtime.provider.run_structured
    delivered = []
    async def perturbed(**kwargs):
        bundle = json.loads(kwargs["input_text"])
        evidence = list(reversed(supplied)) if variant == "source_order" else supplied
        bundle["active_state"]["lab_evidence"] = evidence
        if variant == "irrelevant_distractor": bundle["active_state"]["irrelevant_distractor"] = "Unrelated notebook color: blue."
        if variant == "prompt_paraphrase": kwargs["instructions"] += "\nEvaluate the same fixed question with the unchanged evidence."
        kwargs["input_text"] = json.dumps(bundle, sort_keys=variant != "response_format", indent=2 if variant == "response_format" else None)
        delivered.append(sorted(digest(x) for x in json.loads(kwargs["input_text"])["active_state"]["lab_evidence"]))
        response = await run(**kwargs)
        if variant == "response_format": response = replace(response, output=json.loads(json.dumps(response.output, sort_keys=True)))
        return response
    runtime.provider.run_structured = perturbed
    result = asyncio.run(runtime.resume(prepared["research_id"]))
    rid = prepared["research_id"]
    before = conclusions_from_snapshot(state.research_slice.snapshot(rid), "fixed-question")[0]
    if evidence_change:
        claim = state.research_slice.current(rid)[0]
        binding = state.research_slice.bindings(claim)[0]
        if variant == "dataset_revision": state.invalidate_dataset(rid, prepared["dataset_id"], "revision")
        elif variant == "source_withdrawal":
            sid, _ = source(state, rid, "withdrawn")
            state.invalidate_source(rid, sid, "withdrawal")
            current = next(c for c in state.research_slice.current(rid) if c.claim_type == "literature_summary")
            observed = current.support_state
        elif variant == "artifact_hash":
            record = state.file_artifact(binding.target_id, rid)
            state.workspace.path(rid, record["relative_path"]).write_bytes(b"{}")
        else: state.invalidate_experiment(rid, binding.locator["experiment_id"], variant)
    after = conclusions_from_snapshot(state.research_slice.snapshot(rid), "fixed-question")
    if evidence_change:
        passed = any(c.validity_status == "NEEDS_REVALIDATION" for c in after)
    else:
        passed = abs(before.estimate - reference) < 1e-10 and all(d == sorted(digest(x) for x in supplied) for d in delivered)
    row = {"variant": variant, "evidence_change": evidence_change, "passed": passed,
           "actually_delivered_evidence_hashes": delivered, "structured_conclusion": before.model_dump(mode="json"),
           "after": [c.model_dump(mode="json") for c in after], "correct_against_held_out_estimate": abs(before.estimate - reference) < 1e-10,
           "contract_hashes": [digest(json.loads(r[0])) for r in db.execute("SELECT contract_json FROM contracts WHERE json_extract(contract_json,'$.assigned_role')='analysis_planner_worker'")],
           "commits": db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0]}
    db.close()
    return row


def evaluate(output_dir=None, *, recovery=True):
    wall, cpu = perf_counter(), process_time()
    from probe.preflight import _source_fingerprint
    fingerprint = _source_fingerprint()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8", errors="strict"))
    root = (output_dir or ROOT / "build/reliability-lab") / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    f3p_config = json.loads(F3P_CONFIG.read_text(encoding="utf-8"))
    cases = f3p_config["cases"] + [{"id": "healthy_control", "expected": "PASS", "initially_correct": True}]
    f3p, provenance, perturbations = [], [], []
    def progress():
        save(root / "partial_results.json", {"status": "INCOMPLETE", "expected_f3p": len(cases) * 3,
             "expected_provenance": 30, "expected_perturbations": 10,
             "f3p": f3p, "provenance": provenance, "perturbations": perturbations})
    for v in ("V0", "V1", "V2"):
        for case in cases:
            with fixed_ids(): row = run_case(case, root / v / case["id"], manifest["seed"], slice_config=policy(v=v))
            row["arm"] = v
            from probe.database import initialize
            audit_db = initialize(root / v / case["id"] / "state.sqlite")
            row["contract_hashes"] = [digest(json.loads(r[0])) for r in audit_db.execute("SELECT contract_json FROM contracts WHERE json_extract(contract_json,'$.assigned_role')='analysis_planner_worker' ORDER BY rowid")]
            row["plan_hashes"] = [digest(json.loads(r[0])) for r in audit_db.execute("SELECT output_json FROM runtime_steps WHERE step_key LIKE 'worker_plan:%' ORDER BY rowid")]
            row["dataset_hash"] = audit_db.execute("SELECT sha256 FROM datasets LIMIT 1").fetchone()[0]
            audit_db.close()
            f3p.append(row)
            progress()
    for p in ("P0", "P1", "P2"):
        for case in manifest["provenance"]:
            with fixed_ids(): row = provenance_case(case, root / p / case["id"], p, manifest)
            provenance.append(row)
            progress()
    for variant in manifest["meaning_preserving"] + manifest["evidence_changing"]:
        with fixed_ids(): row = perturbation_case(root / "perturbations" / variant, variant, manifest, evidence_change=variant in manifest["evidence_changing"])
        perturbations.append(row)
        progress()
    comparison = [r for r in perturbations if not r["evidence_change"]]
    first = StructuredConclusion.model_validate(comparison[0]["structured_conclusion"])
    for r in comparison: r["compatible_with_identical_repeat"] = compatible(first, StructuredConclusion.model_validate(r["structured_conclusion"]))
    paired_inputs = all(len({(tuple(r["contract_hashes"]), r["plan_hash"], r["dataset_hash"], r["seed"]) for r in provenance if r["fixture"] == case["id"]}) == 1 for case in manifest["provenance"])
    paired_f3p = all(len({(tuple(r["contract_hashes"]), tuple(r["plan_hashes"]), r["dataset_hash"]) for r in f3p if r["id"] == case["id"]}) == 1 for case in cases)
    metrics = {}
    for v in ("V0", "V1", "V2"):
        rows = [r for r in f3p if r["arm"] == v]
        faulty = [r for r in rows if not r["initially_correct"]]
        healthy = [r for r in rows if r["initially_correct"]]
        metrics[v] = {"false_acceptance_faulty": count(sum(r["committed"] > 0 and not r["final_correct"] for r in faulty), len(faulty)),
                      "false_rejection_healthy_analysis": count(sum(not r["committed"] for r in healthy), len(healthy)),
                      "wrong_to_right": count(sum(r["committed"] > 0 and r["final_correct"] for r in faulty), len(faulty)),
                      "right_to_wrong": count(sum(r["committed"] > 0 and not r["final_correct"] for r in healthy), len(healthy)),
                      "residual_compound_defects": count(sum(r["observed"] != "PASS" for r in rows if r["id"] == "compound_defect"), 1),
                      "repair_incomplete": count(sum(r["observed"] in {"REPAIR_INCOMPLETE", "REPAIR_NOT_STARTED_BUDGET", "REPAIR_UNRESOLVED"} for r in rows), len(rows)),
                      "revalidation_incomplete": count(sum(r["expected"] == "PASS" and r["observed"] != "PASS" for r in rows), len(rows)),
                      "duplicate_commits": count(sum(max(0, r["committed"] - 1) for r in rows), len(rows)),
                      "contract_mutation_commits": count(sum(r["committed"] for r in rows if r["id"] == "semantic_mutation"), 1),
                      "budget_violation_commits": count(sum(r["committed"] for r in rows if r["id"] == "insufficient_budget"), 1)}
    for p in ("P0", "P1", "P2"):
        rows = [r for r in provenance if r["arm"] == p]
        metrics[p] = {"claim_coverage": count(sum(min(1, r["emitted_current_claims"]) for r in rows), len(rows)),
                      "numeric_completeness": count(sum(r["validated_numeric_slots"] for r in rows), sum(r["required_numeric_slots"] for r in rows)),
                      "binding_validity": count(sum(r["valid_current_bindings"] for r in rows), sum(r["current_bindings"] for r in rows)),
                      "freshness": count(sum(r["observed"] != "SUPPORTED" for r in rows if r["fixture"] in {"parent_revision", "stale_pass"}), 2) if p != "P0" else count(0, 2),
                      "stale_claim_leakage": count(sum(r["stale_leakage"] for r in rows), 2),
                      "unsupported_claim_leakage": count(sum(r["unsupported_leakage"] for r in rows), 5),
                      "contradiction_recall": count(sum(r["contradictions_recalled"] for r in rows), 1),
                      "answer_coverage": count(sum(r["observed"] in {"SUPPORTED", "CONFLICTED", "UNEXAMINED_METADATA"} for r in rows), len(rows))}
    report = {"manifest_sha256": sha256_file(MANIFEST), "fixtures": {"f3p_healthy": len(cases), "provenance": 10, "meaning_preserving": 5, "evidence_changing": 5},
              "paired_inputs_identical": paired_inputs, "paired_f3p_inputs_identical": paired_f3p, "f3p_runs": f3p, "provenance_runs": provenance, "perturbations": perturbations,
              "metrics": metrics, "consistency": count(sum(r["compatible_with_identical_repeat"] for r in comparison), len(comparison)),
              "correctness": count(sum(r["correct_against_held_out_estimate"] for r in perturbations), len(perturbations)),
              "robustness": count(sum(r["passed"] and r["compatible_with_identical_repeat"] for r in comparison), len(comparison)),
              "error_correlation": error_correlation([int(not r["fixture_correct"]) for r in f3p if r["arm"] == "V1"], [int(not r["fixture_correct"]) for r in f3p if r["arm"] == "V2"]),
              "api_calls": 0, "api_tokens": None, "api_cost_usd": None, "wall_ms": (perf_counter() - wall) * 1000,
              "local_compute_ms": (process_time() - cpu) * 1000, "live_provenance_efficacy": "NOT_VALIDATED", "live_reliability_efficacy": "NOT_VALIDATED",
              "scope": manifest["scope"]}
    if recovery:
        from research_slice_recovery_probe import run_all
        arms = {v: run_all(policy=v) for v in ("V0", "V1", "V2")}
        report["recovery"] = {"arms": arms, "all_passed": all(r["all_passed"] for r in arms.values())}
        report["recoverability"] = count(sum(r["passed"] for a in arms.values() for r in a["boundaries"]), sum(len(a["boundaries"]) for a in arms.values()))
    report["wall_ms"] = (perf_counter() - wall) * 1000
    report["local_compute_ms"] = (process_time() - cpu) * 1000 + sum(r["total_local_compute_ms"] for a in report.get("recovery", {}).get("arms", {}).values() for r in a["boundaries"])
    report["local_compute_scope"] = "Measured parent plus measured recovery child CPU; includes failed attempts and revalidation."
    report["source_fingerprint"] = fingerprint
    report["source_unchanged"] = fingerprint == _source_fingerprint()
    report["harness_sha256"] = sha256_file(Path(__file__))
    report["all_passed"] = (report["source_unchanged"] and paired_inputs and paired_f3p and all(r["fixture_correct"] for r in f3p + provenance) and all(r["passed"] for r in perturbations)
                              and all(r["compatible_with_identical_repeat"] for r in comparison) and (report.get("recovery", {}).get("all_passed", True)))
    save(root / "evaluation_results.json", report)
    save(ROOT / "qa/results/reliability_lab_results.json", report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--enable", action="store_true", help="Explicit opt-in; never run by default")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--cycle5", action="store_true", help="Cycle 5의 고정 외부 정답·의미 변환 평가")
    args = parser.parse_args()
    if not args.enable:
        parser.error("reliability_lab is disabled; use --enable")
    if args.cycle5:
        from cycle5_lab import evaluate as evaluate_cycle5
        report = evaluate_cycle5(args.output_dir)
        print(to_json({k: report[k] for k in ("all_passed", "detection", "repairs_correct", "repair_count", "always_wrong_always_same", "wall_sec")}))
        if not report["all_passed"]: raise SystemExit(1)
        return
    report = evaluate(args.output_dir)
    print(to_json({k: report[k] for k in ("all_passed", "paired_inputs_identical", "fixtures", "metrics", "consistency", "robustness", "api_calls", "wall_ms")}))
    if not report["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
