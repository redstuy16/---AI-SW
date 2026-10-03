"""기존 실제 분석 기록과 의미 검토를 별도 DEMO 공간에 만든다."""
import asyncio
import json
from pathlib import Path
from uuid import uuid4
from cycle5_fixtures import science_fixture
from htrsa.final_report import export_final_report
from htrsa.preflight import _source_fingerprint

ROOT = Path(__file__).resolve().parents[1]


def main():
    folder = ROOT / "build" / ("cycle5-visual-" + uuid4().hex)
    db, state, runtime, prepared, _ = science_fixture(folder)
    rid = prepared["research_id"]
    try:
        record = next(s for s in state.cycle5.records(rid, "source") if s.record_id == "SM-a")
        state.cycle5.propose_source(record.model_copy(update={"revision": 3, "quantity_name": '<img src="https://evil.example/meaning" onerror="window.__cycle5_xss=1">', "review_status": "NEEDS_REVIEW", "reviewed_by": None}), actor_role="analysis_planner_worker")
        state.cycle5.review_source(rid, "SM-a", 3, actor_role="owner")
        asyncio.run(runtime.resume(rid))
        state.stop_research(rid, "BUDGET_EXHAUSTED")
        export_final_report(state, rid)
        legacy = state.create_research("Cycle 5 기본 OFF 표시 검사")
        state.workspace.prepare(legacy)
    finally: db.close()
    metadata = {"database": str(folder / "state.sqlite"), "workspace": str(folder / "workspace"),
                "research_id": rid, "legacy_id": legacy, "source_fingerprint": _source_fingerprint()}
    content = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8", errors="strict")
    (ROOT / "build/cycle5_visual_fixture.json").write_bytes(content)
    print("build/cycle5_visual_fixture.json")


if __name__ == "__main__": main()
