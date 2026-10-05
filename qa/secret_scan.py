"""출시·추적·Agent·도구·대시보드의 비밀 검사용 값 유출을 검사한다."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from probe.dashboard import DashboardReadAPI
from probe.database import initialize
from probe.release import export_release_from_paths


def main() -> None:
    validation = json.loads((ROOT / "qa/results" / "artifact_validation.json").read_text(encoding="utf-8"))
    demo = json.loads((ROOT / "qa/results" / "demo_repeatability.json").read_text(encoding="utf-8"))
    database = Path(validation["clean_database"])
    workspace = database.parent / "workspace"
    research_id = demo["runs"]["A"][0]["research_id"]
    canary = "sk-qa-canary-0123456789abcde"
    os.environ["OPENAI_API_KEY"] = canary
    os.environ["PROBE_MANAGER_MODEL"] = "qa-canary-model"
    output = ROOT / "build" / "qa-day1" / f"secret-scan-{uuid4().hex[:12]}"
    exported = export_release_from_paths(database, workspace, research_id, output)
    findings = []
    for path in output.rglob("*"):
        if path.is_file() and (canary.encode() in path.read_bytes() or re.search(r"(?:^|\.)env$|secret|credential", path.name, re.I)):
            findings.append(f"release:{path.relative_to(output).as_posix()}")
    db = initialize(database)
    try:
        for table in ("agent_runs", "tool_calls", "runtime_events"):
            for row in db.execute(f"SELECT * FROM {table}"):
                if canary in json.dumps(dict(row), ensure_ascii=False):
                    findings.append(f"database:{table}")
    finally:
        db.close()
    dashboard = DashboardReadAPI(database, workspace, mode="DEMO")
    try:
        for route in ("", "/artifacts", "/report", "/usage", "/timeline"):
            body = json.dumps(dashboard.get(f"/api/research/{research_id}{route}"), ensure_ascii=False)
            if canary in body:
                findings.append(f"dashboard:{route or 'overview'}")
        environment = json.dumps(dashboard.get("/api/environment"), ensure_ascii=False)
        if canary in environment:
            findings.append("dashboard:environment")
    finally:
        dashboard.close()
    result = {"passed": not findings, "findings": findings,
              "release_files": len(exported["files"]),
              "surfaces": ["release", "agent_runs", "tool_calls", "runtime_events", "dashboard"],
              "canary_present_in_environment": True}
    serialized = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    serialized.encode("utf-8", errors="strict")
    (ROOT / "qa/results" / "secret_scan.json").write_text(serialized, encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    if findings:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
