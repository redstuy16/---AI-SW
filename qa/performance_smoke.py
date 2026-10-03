"""연구 상태를 바꾸지 않고 제한된 조회·재시도 시간을 측정한다."""
from __future__ import annotations

import asyncio
import argparse
import json
from pathlib import Path
import sys
from time import perf_counter

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from htrsa.dashboard import (project_evidence, project_experiments, project_overview,
                             project_tree, project_usage)
from htrsa.database import initialize
from htrsa.scholarly import ScholarlyError, ScholarlyHTTPClient
from htrsa.service import StateService
from htrsa.storage import Workspace


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verified-analysis-offline", action="store_true")
    parser.add_argument("--f3p-offline", action="store_true")
    args = parser.parse_args()
    if args.f3p_offline:
        from f3p_eval import run_offline
        report = run_offline()
        print(json.dumps(report, ensure_ascii=False))
        if not report["all_fixtures_passed"]:
            raise SystemExit(1)
        return
    if args.verified_analysis_offline:
        from verified_analysis_eval import run_offline
        print(json.dumps(run_offline(), ensure_ascii=False))
        return
    release = json.loads((ROOT / "qa/results" / "artifact_validation.json").read_text(encoding="utf-8"))
    repeats = json.loads((ROOT / "qa/results" / "demo_repeatability.json").read_text(encoding="utf-8"))
    database = Path(release["clean_database"])
    workspace = database.parent / "workspace"
    research_id = repeats["runs"]["A"][0]["research_id"]
    db = initialize(database)
    state = StateService(db, Workspace(workspace))
    projections = {}
    for name, project in (("overview", project_overview), ("tree", project_tree),
                          ("evidence", project_evidence), ("experiments", project_experiments),
                          ("usage", project_usage)):
        queries = [0]
        db.set_trace_callback(lambda _sql: queries.__setitem__(0, queries[0] + 1))
        started = perf_counter()
        for _ in range(5):
            project(state, research_id)
        elapsed = perf_counter() - started
        db.set_trace_callback(None)
        projections[name] = {"mean_ms": round(1000 * elapsed / 5, 3),
                             "queries_per_call": queries[0] / 5}
    db.close()
    calls = [0]
    def handler(_request):
        calls[0] += 1
        return httpx.Response(429, headers={"Retry-After": "0"}, json={})
    client = ScholarlyHTTPClient(retries=2, transport=httpx.MockTransport(handler))
    started = perf_counter()
    try:
        asyncio.run(client.get_json("https://api.openalex.org/works"))
        raise AssertionError("rate limit was not reported")
    except ScholarlyError as exc:
        assert exc.status == "RATE_LIMITED"
    retry_ms = round(1000 * (perf_counter() - started), 3)
    result = {"projection": projections,
              "demo_a_mean_sec": round(sum(item["duration_sec"] for item in repeats["runs"]["A"]) / 5, 3),
              "demo_b_mean_sec": round(sum(item["duration_sec"] for item in repeats["runs"]["B"]) / 5, 3),
              "rate_limit_retry": {"calls": calls[0], "elapsed_ms": retry_ms,
                                   "bounded": calls[0] == 3},
              "note": "Observation only; no pre-QA performance baseline was available."}
    serialized = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    serialized.encode("utf-8", errors="strict")
    (ROOT / "qa/results" / "performance_smoke.json").write_text(serialized, encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
