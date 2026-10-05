"""F3-P 내보내기와 비밀 검사용 값의 유출 차단을 검증한다."""
from __future__ import annotations

from hashlib import sha256
import argparse
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from probe.dashboard import DashboardReadAPI
from probe.database import initialize
from probe.release import export_release
from probe.service import StateService
from probe.storage import Workspace


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true", help="export with the actual environment after QA gates")
    parser.add_argument("--evaluation-dir", type=Path, default=ROOT / "build/f3p-evaluation")
    args = parser.parse_args()
    canary = "sk-f3p-env-canary-0123456789abcdef"
    if not args.clean:
        os.environ["OPENAI_API_KEY"] = canary
        os.environ["PROBE_MANAGER_MODEL"] = "f3p-canary-model"
    cases = []
    manifests = list(args.evaluation_dir.rglob("release_manifest.json"))
    for name in ("single_repairable", "successful_legacy_repair"):
        selected = max((path for path in manifests if path.parents[2].name == name),
                       key=lambda path: path.stat().st_mtime_ns)
        folder = selected.parents[2]
        source = json.loads(selected.read_text(encoding="utf-8", errors="strict"))
        db = initialize(folder / "state.sqlite")
        state = StateService(db, Workspace(folder / "workspace"))
        rid = source["research_id"]
        mode = "clean" if args.clean else "canary"
        release = export_release(state, rid, folder / f"release-{mode}-{uuid4().hex[:8]}")
        missing, mismatch, secrets, absolute, unresolved = [], [], [], [], []
        for item in release["files"]:
            path = Path(release["output"]) / item["path"]
            if not path.is_file():
                missing.append(item["path"])
                continue
            data = path.read_bytes()
            if sha256(data).hexdigest() != item["sha256"]:
                mismatch.append(item["path"])
            if canary.encode() in data or re.search(rb"OPENAI_API_KEY\s*=|sk-[A-Za-z0-9_-]{12,}", data):
                secrets.append(item["path"])
            try:
                text = data.decode("utf-8", errors="strict")
            except UnicodeError:
                continue
            if re.search(r"[A-Za-z]:[\\/]", text):
                absolute.append(item["path"])
            if re.search(r"\{\{(?:NUM|SRC|EVIDENCE):", text):
                unresolved.append(item["path"])
        db_leaks = [table for table in ("runtime_steps", "runtime_events", "tool_calls", "agent_runs")
                    if any(canary in json.dumps(dict(row)) for row in db.execute(f"SELECT * FROM {table}"))]
        db.close()
        dashboard = DashboardReadAPI(folder / "state.sqlite", folder / "workspace", mode="DEMO")
        try:
            dashboard_leaks = [route for route in ("", "/artifacts", "/report", "/usage", "/timeline")
                               if canary in json.dumps(dashboard.get(f"/api/research/{rid}{route}"))]
            if canary in json.dumps(dashboard.get("/api/environment")):
                dashboard_leaks.append("environment")
        finally:
            dashboard.close()
        cases.append({"case": name, "manifest": release["manifest"], "file_count": len(release["files"]),
                      "hash_mismatches": mismatch, "missing_artifacts": missing, "secrets": secrets,
                      "absolute_paths": absolute, "unresolved_refs": unresolved,
                      "db_canary_leaks": db_leaks, "dashboard_canary_leaks": dashboard_leaks,
                      "passed": not any((mismatch, missing, secrets, absolute, unresolved, db_leaks, dashboard_leaks))})
    result = {"all_passed": all(item["passed"] for item in cases),
              "canary_present_in_environment": not args.clean, "live_api_calls": 0, "cases": cases}
    report_name = "f3p_clean_export_validation.json" if args.clean else "f3p_export_validation.json"
    (ROOT / "qa/results" / report_name).write_bytes(
        (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8", errors="strict"))
    print(json.dumps(result, ensure_ascii=False))
    if not result["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
