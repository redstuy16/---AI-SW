"""새 프로세스에서 F3-P 충돌·복구를 검사한다."""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from f3p_eval import MODELS, ROOT, prepare
from probe.agent_runtime import AgentRuntime
from probe.database import initialize
from probe.providers.fake import FakeProvider
from probe.recovery import FaultInjector, InjectedCrash
from probe.service import StateService
from probe.storage import Workspace


BOUNDARIES = ["AFTER_FAILURE_EVIDENCE", "AFTER_REPAIR_DECISION", "AFTER_REPAIRED_EXECUTION",
              "DURING_REVALIDATION", "BEFORE_REPAIR_COMMIT", "AFTER_REPAIR_COMMIT"]


def save(path: Path, value: dict) -> None:
    path.write_bytes((json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8", errors="strict"))


def probe(phase: str, folder: Path, boundary: str) -> dict:
    if phase == "crash":
        db, state, runtime, prepared, _ = prepare(folder, seed=4913)
        stage = state.stage
        count = [0]
        def defect(payload):
            count[0] += 1
            if count[0] == 1:
                payload.agent_result.output = deepcopy(payload.agent_result.output)
                payload.agent_result.output["metrics"]["estimate"] = -0.2
            return stage(payload)
        state.stage = defect
        def trigger(point):
            if point == boundary:
                raise InjectedCrash(point)
        runtime.repair_faults = FaultInjector(trigger)
        try:
            asyncio.run(runtime.resume(prepared["research_id"]))
            raise AssertionError("requested boundary not reached")
        except InjectedCrash:
            pass
        result = {"research_id": prepared["research_id"], "boundary": boundary,
                  "model_calls_before_crash": len(runtime.provider.calls)}
        save(folder / "probe.json", result)
    else:
        record = json.loads((folder / "probe.json").read_text(encoding="utf-8", errors="strict"))
        db = initialize(folder / "state.sqlite")
        state = StateService(db, Workspace(folder / "workspace"))
        provider = FakeProvider([{"action": "REPAIR", "rationale": "Reexecute frozen plan"}] * 2)
        runtime = AgentRuntime(state, provider, MODELS, verified_analysis_skills_enabled=True,
                               verification_repair_enabled=True)
        outcome = asyncio.run(runtime.resume(record["research_id"]))
        counts = {"commits": db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0],
                  "verified_evidence": db.execute("SELECT COUNT(*) FROM evidence WHERE status='VERIFIED'").fetchone()[0],
                  "analysis_executions": db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name='analysis.skill'").fetchone()[0]}
        result = {"boundary": boundary, "new_process": True,
                  "passed": counts == {"commits": 1, "verified_evidence": 1, "analysis_executions": 2},
                  "counts": counts, "model_calls_after_resume": len(provider.calls),
                  "outcome": outcome.get("verdict", "ALREADY_COMMITTED")}
        save(folder / "resume.json", result)
    db.close()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["crash", "resume"])
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--boundary", choices=BOUNDARIES)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if args.all:
        folder = ROOT / "build/f3p-recovery" / uuid4().hex
        results = []
        for boundary in BOUNDARIES:
            target = folder / boundary
            for phase in ("crash", "resume"):
                process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--phase", phase,
                                          "--folder", str(target), "--boundary", boundary],
                                         capture_output=True, text=True, timeout=60, check=False)
                if process.returncode:
                    raise RuntimeError(f"{boundary}/{phase}: {process.stderr[-3000:]}")
            results.append(json.loads((target / "resume.json").read_text(encoding="utf-8")))
        report = {"all_passed": all(item["passed"] for item in results), "boundaries": results,
                  "external_exactly_once": "NOT_CLAIMED"}
        save(ROOT / "qa/results/f3p_recovery_results.json", report)
        print(json.dumps(report))
    else:
        if not (args.phase and args.folder and args.boundary):
            parser.error("phase, folder and boundary are required")
        print(json.dumps(probe(args.phase, args.folder, args.boundary)))


if __name__ == "__main__":
    main()
