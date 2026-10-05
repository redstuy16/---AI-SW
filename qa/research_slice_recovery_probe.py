"""기존 F3-P 자료와 실행기의 새 프로세스 충돌 검사."""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter, process_time
from uuid import uuid4

from f3p_eval import prepare, MODELS, ROOT
from f3p_recovery_probe import BOUNDARIES as REPAIR_BOUNDARIES
from probe.agent_runtime import AgentRuntime
from probe.database import initialize, to_json
from probe.final_report import export_final_report
from probe.providers.fake import FakeProvider
from probe.recovery import FaultInjector, InjectedCrash
from probe.research_slice import ResearchSlice
from probe.research_slice_schemas import ResearchSliceConfig
from probe.release import export_release
from probe.service import StateService
from probe.storage import Workspace, sha256_file

ON = ResearchSliceConfig(claim_evidence_provenance=True, verifier_dependency_catalog=True)
BOUNDARIES = REPAIR_BOUNDARIES + ["AFTER_CLAIM_STAGE", "DURING_CLAIM_COMMIT", "AFTER_PARENT_INVALIDATION", "DURING_CLAIM_REVALIDATION", "AFTER_EXPORT"]


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((to_json(data) + "\n").encode("utf-8", errors="strict"))


def probe(phase, folder, boundary, policy="V2"):
    started_wall, started_cpu = perf_counter(), process_time()
    if phase == "crash":
        config = ON.model_copy(update={"verifier_dependency_catalog": policy != "V0", "dependency_metadata": policy == "V2"})
        db, state, runtime, prepared, _ = prepare(folder, seed=4913, slice_config=config)
        rid = prepared["research_id"]
        save(folder / "probe.json", {"research_id": rid, "boundary": boundary, "config": config.model_dump(mode="json")})
        original_stage = state.stage
        stages = [0]
        def stage(payload):
            stages[0] += 1
            if boundary in REPAIR_BOUNDARIES and stages[0] == 1:
                payload.agent_result.output = deepcopy(payload.agent_result.output)
                payload.agent_result.output["metrics"]["estimate"] = -0.2
            mid = original_stage(payload)
            if boundary == "AFTER_CLAIM_STAGE":
                raise InjectedCrash(boundary)
            return mid
        state.stage = stage
        def trigger(point):
            if point == boundary:
                raise InjectedCrash(point)
        runtime.repair_faults = FaultInjector(trigger)
        original_insert = ResearchSlice._insert
        def broken_insert(self, claim, bindings):
            original_insert(self, claim, bindings)
            raise InjectedCrash(boundary)
        try:
            if boundary == "DURING_CLAIM_COMMIT":
                ResearchSlice._insert = broken_insert
            asyncio.run(runtime.resume(rid))
            if boundary in {"AFTER_PARENT_INVALIDATION", "DURING_CLAIM_REVALIDATION"}:
                claim = state.research_slice.current(rid)[0]
                state.invalidate_claim_parent(rid, "dataset", claim.scope["dataset_id"], "offline revision fixture")
                if boundary == "DURING_CLAIM_REVALIDATION":
                    ResearchSlice._insert = broken_insert
                    state.revalidate_claim(rid, claim.claim_id)
                raise InjectedCrash(boundary)
            if boundary == "AFTER_EXPORT":
                state.stop_research(rid, "BUDGET_EXHAUSTED")
                export_final_report(state, rid)
                export_release(state, rid, folder / "first-release")
                raise InjectedCrash(boundary)
            raise AssertionError("boundary not reached")
        except InjectedCrash:
            pass
        finally:
            ResearchSlice._insert = original_insert
            # 중단된 연결을 닫으면 미반영 트랜잭션이 되돌려진다.
            db.close()
        return {"boundary": boundary, "crash_reached": True, "wall_ms": (perf_counter() - started_wall) * 1000,
                "local_compute_ms": (process_time() - started_cpu) * 1000}
    record = json.loads((folder / "probe.json").read_text(encoding="utf-8", errors="strict"))
    rid = record["research_id"]
    db = initialize(folder / "state.sqlite")
    state = StateService(db, Workspace(folder / "workspace"))
    runtime = AgentRuntime(state, FakeProvider([{"action": "REPAIR", "rationale": "Reexecute frozen plan"}] * 2), MODELS,
                           verified_analysis_skills_enabled=True, verification_repair_enabled=True,
                           claim_evidence_provenance_enabled=True, verifier_dependency_catalog_enabled=True)
    runtime.research_slice_config = ResearchSliceConfig.model_validate(record["config"])
    if boundary == "DURING_CLAIM_REVALIDATION":
        claim = state.research_slice.current(rid)[0]
        state.revalidate_claim(rid, claim.claim_id)
    elif boundary != "AFTER_EXPORT":
        asyncio.run(runtime.resume(rid))
    snapshot = state.research_slice.snapshot(rid)
    current = [c for c in snapshot["claims"] if c["current"]]
    expected_state = "NEEDS_REVALIDATION" if boundary == "AFTER_PARENT_INVALIDATION" else "INCONCLUSIVE"
    # 같은 무효화 요청의 재실행으로 수정본을 중복 생성하지 않는다.
    before = len(snapshot["claims"])
    if boundary == "AFTER_PARENT_INVALIDATION":
        state.invalidate_claim_parent(rid, "dataset", current[0]["scope"]["dataset_id"], "offline revision fixture")
    snapshot_after = state.research_slice.snapshot(rid)
    commits = db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?", (rid,)).fetchone()[0]
    counts = {"commits": commits, "current_claims": len(current), "claim_revisions": len(snapshot_after["claims"]),
              "analysis_executions": db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name='analysis.skill'").fetchone()[0]}
    exported = None
    if boundary == "AFTER_EXPORT":
        export_final_report(state, rid)
        exported = export_release(state, rid, folder / "resumed-release")
        assert all(sha256_file(folder / "resumed-release" / f["path"]) == f["sha256"] for f in exported["files"])
    passed = (commits == 1 and len(current) == 1 and current[0]["effective_support_state"] == expected_state
              and before == len(snapshot_after["claims"]) and snapshot["config"] == record["config"]
              and counts["analysis_executions"] == (2 if boundary in REPAIR_BOUNDARIES else 1))
    if boundary in REPAIR_BOUNDARIES:
        passed = passed and len(snapshot["staged_material_claims"]) == 2 and snapshot["staged_material_claims"][0]["status"] == "ROLLED_BACK"
    result = {"boundary": boundary, "new_process": True, "passed": passed, "counts": counts,
              "support_state": current[0]["effective_support_state"], "export_files": len(exported["files"]) if exported else None}
    result.update(wall_ms=(perf_counter() - started_wall) * 1000, local_compute_ms=(process_time() - started_cpu) * 1000)
    db.close()
    save(folder / "resume.json", result)
    return result


def run_all(output_dir=None, policy="V2"):
    folder = (output_dir or ROOT / "build/research-slice-recovery") / uuid4().hex
    results = []
    for boundary in BOUNDARIES:
        target = folder / boundary
        phases, started = [], perf_counter()
        for phase in ("crash", "resume"):
            process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--phase", phase,
                                      "--folder", str(target), "--boundary", boundary, "--policy", policy], capture_output=True, timeout=90)
            if process.returncode:
                raise RuntimeError(process.stderr.decode("utf-8", errors="replace")[-5000:])
            phases.append(json.loads(process.stdout.decode("utf-8", errors="strict")))
        result = json.loads((target / "resume.json").read_text(encoding="utf-8"))
        result["total_wall_ms"] = (perf_counter() - started) * 1000
        result["total_local_compute_ms"] = sum(p["local_compute_ms"] for p in phases)
        results.append(result)
    report = {"boundaries": results, "all_passed": all(r["passed"] for r in results), "external_exactly_once": "NOT_CLAIMED"}
    save(ROOT / ("qa/results/research_slice_recovery_results.json" if policy == "V2" else f"qa/results/research_slice_recovery_{policy}.json"), report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--phase", choices=["crash", "resume"])
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--boundary", choices=BOUNDARIES)
    parser.add_argument("--policy", choices=["V0", "V1", "V2"], default="V2")
    args = parser.parse_args()
    if args.all:
        print(to_json(run_all(policy=args.policy)))
    elif args.phase and args.folder and args.boundary:
        print(to_json(probe(args.phase, args.folder, args.boundary, args.policy)))
    else:
        parser.error("--all or phase/folder/boundary required")


if __name__ == "__main__":
    main()
