"""실제 실행의 정상·비밀 값·변조·오래된 상태 내보내기를 검사한다."""
from __future__ import annotations

import json
import argparse
import os
from pathlib import Path
import shutil
from uuid import uuid4

from f3p_eval import ROOT
from probe.dashboard import DashboardReadAPI
from probe.database import initialize, to_json
from probe.release import export_release, ReleaseExportError
from probe.service import StateService
from probe.storage import Workspace, sha256_file


def run(lab_root=None):
    lab = max((Path(lab_root) if lab_root else ROOT / "build/research-slice-lab-verified").iterdir(), key=lambda p: p.stat().st_mtime_ns)
    evaluation = json.loads((lab / "evaluation_results.json").read_text(encoding="utf-8", errors="strict"))
    if not evaluation["all_passed"] or not evaluation["source_unchanged"]:
        raise ValueError("COMPLETED_CURRENT_LAB_REQUIRED")
    root = ROOT / "build/research-slice-export" / uuid4().hex
    root.mkdir(parents=True)
    canary = "sk-slice-env-canary-0123456789abcdef"
    results = []
    for name in ("healthy_control", "single_repairable", "successful_legacy_repair"):
        source = lab / "V2" / name
        db = initialize(source / "state.sqlite")
        state = StateService(db, Workspace(source / "workspace"))
        rid = db.execute("SELECT research_id FROM research_runs").fetchone()[0]
        output = root / name / "clean"
        clean = export_release(state, rid, output)
        manifest = json.loads((output / "manifests/release_manifest.json").read_text(encoding="utf-8"))
        hashes = [f["path"] for f in clean["files"] if sha256_file(output / f["path"]) != f["sha256"]]
        extension = json.loads((output / "research_output/research_slice.json").read_text(encoding="utf-8"))
        assert manifest["research_slice"]["extension_schema_version"] == "1"
        assert extension["claims"] and extension["obligation_history"] and extension["analysis_precommits"]
        original_env = {key: os.environ.get(key) for key in ("OPENAI_API_KEY", "PROBE_MANAGER_MODEL")}
        try:
            os.environ["OPENAI_API_KEY"], os.environ["PROBE_MANAGER_MODEL"] = canary, "slice-canary-model"
            canary_out = root / name / "canary"
            secret_export = export_release(state, rid, canary_out)
            leaks = [str(p.relative_to(canary_out)) for p in canary_out.rglob("*") if p.is_file() and canary.encode() in p.read_bytes()]
            api = DashboardReadAPI(source / "state.sqlite", source / "workspace")
            try:
                for route in ("research-slice", "report", "usage", "timeline"):
                    if canary in to_json(api.request(f"/api/research/{rid}/{route}").body): leaks.append("api:" + route)
            finally:
                api.close()
            for table in ("agent_runs", "tool_calls", "runtime_events", "claim_revisions", "claim_bindings", "verifier_obligations"):
                if any(canary in to_json(dict(r)) for r in db.execute(f"SELECT * FROM {table}")): leaks.append("db:" + table)
        finally:
            for key, value in original_env.items():
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value
        faults = {}
        for fault in ("tamper", "stale"):
            target = root / name / fault
            target.mkdir(parents=True)
            copied_db = initialize(target / "state.sqlite")
            db.backup(copied_db)
            shutil.copytree(source / "workspace", target / "workspace")
            copied = StateService(copied_db, Workspace(target / "workspace"))
            claim = copied.research_slice.current(rid)[0]
            if fault == "tamper":
                b = copied.research_slice.bindings(claim)[0]
                artifact = copied.file_artifact(b.target_id, rid)
                copied.workspace.path(rid, artifact["relative_path"]).write_bytes(b"{}")
            else:
                copied.invalidate_claim_parent(rid, "dataset", claim.scope["dataset_id"], "export stale-state fixture")
            try:
                export_release(copied, rid, target / "rejected-release")
                faults[fault] = False
            except ReleaseExportError:
                faults[fault] = True
            copied_db.close()
        results.append({"case": name, "manifest": clean["manifest"], "files": len(clean["files"]),
                        "hash_mismatches": hashes, "canary_leaks": leaks, "blocked_faults": faults,
                        "passed": not hashes and not leaks and all(faults.values())})
        db.close()
    report = {"cases": results, "all_passed": all(r["passed"] for r in results), "live_api_calls": 0}
    (ROOT / "qa/results/research_slice_export_results.json").write_bytes((to_json(report) + "\n").encode("utf-8", errors="strict"))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lab-root", type=Path)
    report = run(parser.parse_args().lab_root)
    print(to_json(report))
    if not report["all_passed"]:
        raise SystemExit(1)
