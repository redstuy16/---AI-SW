"""고립된 QA 작업 공간에서 실제 프로세스 종료 후 의미·변환 복구를 확인한다."""
import argparse
import asyncio
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from cycle5_fixtures import GOLD_ROWS, ON, science_fixture, transform_fixture
from cycle5_lab import conclusion, save
from f3p_eval import MODELS
from htrsa.agent_runtime import AgentRuntime, RuntimeFailure
from htrsa.cycle5 import SemanticReviewRequired
from htrsa.database import initialize
from htrsa.preflight import _source_fingerprint
from htrsa.providers.fake import FakeProvider
from htrsa.service import StateConflictError, StateService
from htrsa.storage import Workspace
from htrsa.verification_repair import repair_transformation

ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = ["TRANSFORM_BEFORE_COMMIT", "TRANSFORM_AFTER_COMMIT", "AFTER_SEMANTIC_REVIEW", "AFTER_SCIENTIFIC_STAGE", "AFTER_SCIENTIFIC_COMMIT", "STALE_BEFORE_COMMIT"]


def probe(phase, folder, boundary):
    if phase == "crash":
        if boundary.startswith("TRANSFORM_"):
            rows = [{**r, "value": str(float(r["value"]) + 32)} for r in GOLD_ROWS]
            db, state, rid, lineage = transform_fixture(folder, rows)
            setup = {"research_id": rid, "lineage": lineage.record_id, "revision": 1, "parent_hash": lineage.parent_hash, "source_fingerprint": _source_fingerprint()}
            save(folder / "setup.json", setup)
            def terminate(point):
                if point == boundary.removeprefix("TRANSFORM_"): os._exit(71)
            repair_transformation(state, rid, lineage.record_id, 1, actor_role="owner", fault=terminate)
        else:
            db, state, runtime, prepared, oracle = science_fixture(folder)
            rid = prepared["research_id"]
            reply = runtime.provider.replies[0]({})
            save(folder / "setup.json", {"research_id": rid, "worker_reply": reply, "oracle": oracle, "source_fingerprint": _source_fingerprint()})
            if boundary == "AFTER_SEMANTIC_REVIEW": os._exit(71)
            stage, commit = state.stage, state.commit
            def staged(payload):
                mid = stage(payload)
                if boundary == "AFTER_SCIENTIFIC_STAGE": os._exit(71)
                return mid
            def committed(mid):
                if boundary == "STALE_BEFORE_COMMIT":
                    record = state.cycle5.records(rid, "source")[0]
                    state.cycle5.propose_source(record.model_copy(update={"revision": 3, "review_status": "NEEDS_REVIEW", "reviewed_by": None}), actor_role="analysis_planner_worker")
                    os._exit(71)
                version = commit(mid)
                if boundary == "AFTER_SCIENTIFIC_COMMIT": os._exit(71)
                return version
            state.stage, state.commit = staged, committed
            asyncio.run(runtime.resume(rid))
        raise AssertionError("장애 경계에 도달하지 않았다")
    setup = json.loads((folder / "setup.json").read_text(encoding="utf-8", errors="strict"))
    rid = setup["research_id"]
    db = initialize(folder / "state.sqlite")
    state = StateService(db, Workspace(folder / "workspace"))
    try:
        if boundary.startswith("TRANSFORM_"):
            result = repair_transformation(state, rid, setup["lineage"], 1, actor_role="owner")
            lineage = state.cycle5.records(rid, "lineage")[0]
            artifact = state.file_artifact(lineage.child_id, rid)
            with state.workspace.path(rid, artifact["relative_path"]).open(encoding="utf-8", newline="") as stream:
                correct = conclusion(list(csv.DictReader(stream))) == conclusion(GOLD_ROWS)
            commits = db.execute("SELECT COUNT(*) FROM planning_events WHERE event_type='CYCLE5_TRANSFORM_REPAIRED'").fetchone()[0]
            passed = result["verification"]["passed"] and correct and commits == 1 and lineage.parent_hash == setup["parent_hash"] and artifact["status"] == "PENDING"
            row = {"passed": passed, "repair_commits": commits, "status": result["status"], "correct_against_external_fixture": correct, "scientific_commit": False}
        else:
            provider = FakeProvider([setup["worker_reply"]])
            runtime = AgentRuntime(state, provider, MODELS, verified_analysis_skills_enabled=True, verification_repair_enabled=True)
            runtime.research_slice_config = ON
            error, result = None, None
            try: result = asyncio.run(runtime.resume(rid))
            except (RuntimeFailure, StateConflictError, ValueError, SemanticReviewRequired) as exc: error = type(exc).__name__
            counts = {"commits": db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0],
                      "analysis_executions": db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name='analysis.skill'").fetchone()[0],
                      "verified_evidence": db.execute("SELECT COUNT(*) FROM evidence WHERE status='VERIFIED'").fetchone()[0]}
            correct = False
            if counts["commits"]:
                artifact = db.execute("SELECT relative_path FROM artifacts WHERE artifact_type='SKILL_RESULT' AND status='VERIFIED'").fetchone()
                stats = json.loads(state.workspace.path(rid, artifact[0]).read_text(encoding="utf-8", errors="strict"))["result"]
                correct = abs(stats["metrics"]["estimate"] - setup["oracle"]) < 1e-10
            passed = counts == {"commits": 0 if boundary == "STALE_BEFORE_COMMIT" else 1, "analysis_executions": 1, "verified_evidence": 0 if boundary == "STALE_BEFORE_COMMIT" else 1} and (bool(error) if boundary == "STALE_BEFORE_COMMIT" else correct and error is None)
            row = {"passed": passed, "counts": counts, "resume_model_calls": len(provider.calls), "halted": error,
                   "correct_against_external_fixture": correct, "stale_blocked": bool(error) if boundary == "STALE_BEFORE_COMMIT" else None}
        return {"boundary": boundary, "new_process": True, "source_unchanged": setup["source_fingerprint"] == _source_fingerprint(), **row}
    finally: db.close()


def run_all():
    root = ROOT / "build" / ("cycle5-recovery-" + uuid4().hex)
    rows = []
    for boundary in BOUNDARIES:
        folder = root / boundary
        first = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--phase", "crash", "--folder", str(folder), "--boundary", boundary], capture_output=True, timeout=60)
        if first.returncode != 71: raise RuntimeError(first.stderr.decode("utf-8", errors="replace")[-1800:])
        second = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--phase", "resume", "--folder", str(folder), "--boundary", boundary], capture_output=True, timeout=60)
        if second.returncode: raise RuntimeError(second.stderr.decode("utf-8", errors="replace")[-1800:])
        rows.append(json.loads(second.stdout.decode("utf-8", errors="strict")))
    result = {"all_passed": all(r["passed"] and r["source_unchanged"] for r in rows), "boundaries": rows,
              "source_fingerprint": _source_fingerprint(), "api_calls": 0, "live_efficacy": "NOT_VALIDATED", "external_exactly_once": "NOT_CLAIMED"}
    save(ROOT / "qa/results/cycle5_recovery_results.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--phase", choices=["crash", "resume"])
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--boundary", choices=BOUNDARIES)
    args = parser.parse_args()
    result = run_all() if args.all else probe(args.phase, args.folder, args.boundary)
    from htrsa.database import to_json
    print(to_json(result))
    if not result.get("all_passed", result.get("passed", False)): raise SystemExit(1)
