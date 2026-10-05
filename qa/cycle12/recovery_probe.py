"""필수 두 형식의 저장·검증·반영 경계에서 프로세스를 종료하고 재개한다."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from probe.climate_profile import SOURCE_POLICY
from probe.qualified_workflow import execute_profile
from probe.qualified_profiles import conclusion_card
from probe.recovery import FaultInjector, InjectedCrash
from probe.workbench import WorkbenchAPI
from probe.autonomous_loop import AutonomousResearchLoop
from probe.providers.fake import FakeProvider
from probe.control_plane import ROLES
from test_beginner_v4 import prepare, SOURCE


def save(path, value):
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8", errors="strict")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["crash", "resume"], required=True)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--boundary", required=True, choices=["profile_after_verify", "profile_after_commit"])
    args = parser.parse_args()
    args.folder.mkdir(parents=True, exist_ok=True)
    SOURCE_POLICY["secondary_required"] = True
    app = WorkbenchAPI(args.folder / "state.sqlite", args.folder / "workspace", launch=False)
    try:
        if args.phase == "crash":
            rid, snapshot, runtime, provider = prepare(app)
            save(args.folder / "request.json", {"research_id": rid, "snapshot": snapshot})
            runtime.faults = FaultInjector(lambda point: (_ for _ in ()).throw(InjectedCrash()) if point == args.boundary else None)
        else:
            saved = json.loads((args.folder / "request.json").read_text(encoding="utf-8"))
            rid, snapshot = saved["research_id"], saved["snapshot"]
            provider = FakeProvider([])
            runtime = AutonomousResearchLoop(app.read._state, provider, models={role: "manual-id" for role in ROLES})
        try:
            asyncio.run(execute_profile(runtime, rid, snapshot,
                source_text=SOURCE.read_text(encoding="utf-8") if args.phase == "crash" else None,
                secondary_text=(SOURCE.parent / "gistemp.csv").read_text(encoding="utf-8") if args.phase == "crash" else None))
        except InjectedCrash:
            os._exit(79)
        card = conclusion_card(app.read._state, rid)
        count = app.store.db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?", (rid,)).fetchone()[0]
        record = {"boundary": args.boundary, "fresh_process": True, "commit_count": count,
                  "resume_model_calls": len(provider.calls), "current": card["current"],
                  "required": card["representation"]["required"], "representation": card["representation"]["status"],
                  "passed": count == 1 and not provider.calls and card["current"] and card["representation"]["required"],
                  "execution": "FRESH_PROCESS_OFFLINE_FAKE_AGENT_REAL_TOOLS", "paid_calls": 0}
        save(args.folder / "resume.json", record)
        if not record["passed"]:
            raise SystemExit(1)
    finally:
        app.close()


if __name__ == "__main__":
    main()
