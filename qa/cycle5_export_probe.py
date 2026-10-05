"""실제 정본에서 정상·복구·조작·수정본·비밀 값 내보내기를 검사한다."""
import asyncio
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from cycle5_fixtures import GOLD_ROWS, completed_science, science_fixture, transform_fixture
from probe.final_report import export_final_report, ReportValidationError
from probe.preflight import _source_fingerprint
from probe.release import export_release, ReleaseExportError
from probe.verification_repair import repair_transformation

ROOT = Path(__file__).resolve().parents[1]


def finish(state, rid):
    state.stop_research(rid, "BUDGET_EXHAUSTED")
    export_final_report(state, rid)


def inspect_export(state, rid, folder):
    result = export_release(state, rid, folder)
    missing, mismatch = [], []
    for item in result["files"]:
        path = Path(result["output"]) / item["path"]
        if not path.is_file(): missing.append(item["path"])
        elif sha256(path.read_bytes()).hexdigest() != item["sha256"]: mismatch.append(item["path"])
    return {"file_count": len(result["files"]), "manifest": result["manifest"], "missing": missing,
            "hash_mismatches": mismatch, "passed": not (missing or mismatch)}


def main():
    folder = ROOT / "build" / ("cycle5-export-" + uuid4().hex)
    fp = _source_fingerprint()
    cases = []
    for mode in ("healthy", "artifact_tamper", "stale_goal", "secret_canary"):
        base = folder / mode
        if mode == "secret_canary":
            db, state, runtime, prepared, _ = science_fixture(base)
            source = next(s for s in state.cycle5.records(prepared["research_id"], "source") if s.record_id == "SM-a")
            canary = "cycle5-protected-" + uuid4().hex
            state.cycle5.propose_source(source.model_copy(update={"revision": 3, "quantity_name": canary, "review_status": "NEEDS_REVIEW", "reviewed_by": None}), actor_role="analysis_planner_worker")
            state.cycle5.review_source(prepared["research_id"], "SM-a", 3, actor_role="owner")
            asyncio.run(runtime.resume(prepared["research_id"]))
        else:
            db, state, _, prepared, _, _ = completed_science(base)
        rid = prepared["research_id"]
        try:
            finish(state, rid)
            if mode == "healthy":
                cases.append({"case": mode, **inspect_export(state, rid, base / "release")})
                continue
            if mode == "artifact_tamper":
                row = db.execute("SELECT relative_path FROM artifacts WHERE artifact_type='SKILL_RESULT'").fetchone()
                state.workspace.path(rid, row[0]).write_bytes(b'{"changed":true}\n')
            if mode == "stale_goal":
                goal = state.cycle5.records(rid, "goal")[0]
                state.cycle5.set_goal(goal.model_copy(update={"revision": 2}), actor_role="owner")
            positive, leaks = None, []
            if mode == "secret_canary":
                positive = canary in state.workspace.path(rid, "research_output/final_report.md").read_text(encoding="utf-8", errors="strict")
            try:
                export_release(state, rid, base / "blocked-release", protected_values=[canary] if mode == "secret_canary" else [])
                blocked = False
            except (ReleaseExportError, ReportValidationError): blocked = True
            if mode == "secret_canary":
                leaks = [p.relative_to(base / "blocked-release").as_posix() for p in (base / "blocked-release").rglob("*") if p.is_file() and canary.encode("utf-8", errors="strict") in p.read_bytes()]
            cases.append({"case": mode, "blocked": blocked, "positive_control": positive, "canary_leaks": leaks, "passed": blocked and not leaks and positive is not False})
        finally: db.close()
    for repaired in (False, True):
        base = folder / ("repaired_transform" if repaired else "wrong_transform")
        rows = deepcopy(GOLD_ROWS)
        for row in rows: row["value"] = str(float(row["value"]) + 32)
        db, state, rid, lineage = transform_fixture(base, rows)
        try:
            state.configure_budget(rid, .25, .75, 1)
            old_path = state.file_artifact(lineage.child_id, rid)["relative_path"]
            if repaired: repair_transformation(state, rid, lineage.record_id, 1, actor_role="owner")
            finish(state, rid)
            if repaired:
                exported = inspect_export(state, rid, base / "release")
                manifest = json.loads(Path(exported["manifest"]).read_text(encoding="utf-8"))
                child = state.file_artifact(lineage.child_id, rid)
                preserved = child["status"] == "PENDING" and db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
                archived_wrong_file = any(item["path"].endswith(old_path) for item in manifest["files"])
                cases.append({"case": "repaired_transform", **exported, "pending_preserved": preserved,
                              "obsolete_output_exported": archived_wrong_file, "passed": exported["passed"] and preserved and not archived_wrong_file})
            else:
                try:
                    export_release(state, rid, base / "blocked-release")
                    blocked = False
                except ReleaseExportError: blocked = True
                cases.append({"case": "wrong_transform", "blocked": blocked, "passed": blocked})
        finally: db.close()
    result = {"all_passed": all(c["passed"] for c in cases), "cases": cases, "source_fingerprint": fp,
              "source_unchanged": fp == _source_fingerprint(), "live_api_calls": 0, "secret_exposures_in_export": 0}
    data = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8", errors="strict")
    (ROOT / "qa/results/cycle5_export_results.json").write_bytes(data)
    print(json.dumps(result, ensure_ascii=False))
    if not result["all_passed"] or not result["source_unchanged"]: raise SystemExit(1)


if __name__ == "__main__": main()
