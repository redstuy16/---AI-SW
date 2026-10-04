"""공개 문헌 검색 경로만 실제 Crossref로 확인한다. AI와 과금 요청은 실행하지 않는다."""
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from htrsa.workbench import WorkbenchAPI
from htrsa.scholarly import CrossrefProvider, ScholarlyHTTPClient
from htrsa.search_policy import run_search
from htrsa.schemas import utc_now
from test_workbench import configure
from test_research_report_flow import request


async def main():
    folder = ROOT / "build/research-report/live-search" / uuid4().hex
    api = WorkbenchAPI(folder / "state.sqlite", folder / "workspace", launch=False)
    configure(api)
    created = api.create(request(search_attempt_limit=2))
    rid, snapshot = created["research_id"], created["snapshot"]
    client = ScholarlyHTTPClient(timeout=12, retries=0)
    provider = CrossrefProvider(client, mailto="")
    result = {"execution": "LIVE_CROSSREF_SEARCH_ONLY", "started_at": utc_now().isoformat(),
              "research_id": rid, "model_calls": 0, "paid_calls": 0, "live_agent_efficacy": "NOT_VALIDATED"}
    try:
        await run_search(api.read._state, api.store, api.credentials, rid, snapshot, provider=provider,
                         price=0, price_source="https://www.crossref.org/services/metadata-retrieval/",
                         queries=["temperature CO2 release carbonated beverage"])
        result["status"] = "COMPLETED"
    except Exception as error:
        result.update(status="FAILED", error_code=getattr(error, "code", error.__class__.__name__),
                      provider_status=client.last_status, http_status=client.last_status_code)
    result.update(completed_at=utc_now().isoformat(),
                  searches=[json.loads(row[0]) for row in api.store.db.execute(
                      "SELECT payload FROM control_audit WHERE research_id=? AND kind='SEARCH_COMPLETED'", (rid,))],
                  sources=[dict(row) for row in api.store.db.execute(
                      "SELECT title,url,status,CASE WHEN abstract IS NULL THEN 0 ELSE length(abstract) END abstract_length FROM sources WHERE research_id=?", (rid,))],
                  evidence=dict(api.store.db.execute("SELECT status,COUNT(*) FROM evidence WHERE research_id=? GROUP BY status", (rid,)).fetchall()),
                  ledger=api.store.ledger(rid))
    data = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8", errors="strict")
    (folder / "result.json").write_bytes(data)
    print(json.dumps({"status": result["status"], "error": result.get("error_code"),
                      "http_status": result.get("http_status"), "searches": len(result["searches"]),
                      "sources": len(result["sources"]), "evidence": result["evidence"],
                      "spent": result["ledger"]["spent"], "model_calls": 0, "output": str(folder / "result.json")}, ensure_ascii=False))
    api.close()


if __name__ == "__main__":
    asyncio.run(main())

