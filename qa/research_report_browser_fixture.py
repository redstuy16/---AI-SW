"""새 조사 경로의 Chrome 검사와 별도 프로세스 복구에 쓰는 고정 제공사."""
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from htrsa.workbench import WorkbenchAPI, OwnerSession, create_server
from htrsa.api import APIResponse
from htrsa.database import to_json
from htrsa.report_pdf import render_pdf
from htrsa.research_report import rewrite_report, report_record
from htrsa.service import StateService
from test_research_report_flow import rig, run, request


class Patch:
    def setattr(self, target, name, value=None):
        if value is None:
            import importlib
            target, attribute = target.rsplit('.', 1)
            target, name, value = importlib.import_module(target), attribute, name
        setattr(target, name, value)


def main():
    folder = Path(sys.argv[1]).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    calls = []
    prior_model_calls, prior_searches = [], []
    class OfflineAPI(WorkbenchAPI):
        def command(self, rid, action, body):
            result = super().command(rid, action, body)
            if action in {"start", "resume"} and self.store.run(rid)["status"] in {"STARTING", "RESUMING"}:
                asyncio.run(__import__("htrsa.control_runtime", fromlist=["execute"]).execute(self.database, self.workspace, rid, provider_factory=gateway))
                save()
            return result
        def _request(self, method, path, body=None):
            if method == "GET" and path == "/qa/observed":
                counts = {name: self.store.db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                          for name in ("research_runs", "state_events", "agent_runs", "spend_ledger")}
                return APIResponse(200, {"model_calls": prior_model_calls + model_calls, "searches": len(prior_searches) + len(searches), "paid_calls": 0, "counts": counts})
            if "--board" in sys.argv and method == "POST" and path == "/qa/state":
                active = body["status"] == "RUNNING"
                self.store.db.execute("UPDATE control_runs SET status=?,version=version+1,pid=? WHERE research_id=?", ("RUNNING" if active else "COMPLETED", os.getpid() if active else None, rid))
                self.store.db.execute("UPDATE runtime_steps SET status=? WHERE research_id=? AND step_key='report-draft:1'", ("RUNNING" if active else "COMPLETED", rid))
                self.store.db.execute("UPDATE contracts SET status=? WHERE research_id=? AND contract_id=?", ("RUNNING" if active else report_contract_status, rid, report_contract))
                return APIResponse(200, {"fixture": True, "paid_calls": 0})
            if method == "POST" and path.endswith("/rewrite-report"):
                result = asyncio.run(rewrite_report(self, path.split("/")[4], body, provider_factory=gateway))
                save()
                return APIResponse(200, result)
            return super()._request(method, path, body)
    api = OfflineAPI(folder / "state.sqlite", folder / "workspace", launch=False)
    from htrsa import search_policy
    base_search = search_policy.run_search
    gateway, model_calls, searches = rig(api, Patch(), search="empty" if "--empty" in sys.argv else "ok")
    if '--fulltext' in sys.argv:
        search_policy.run_search = base_search
        from test_free_search_recovery import free_rig
        gateway, model_calls, searches = free_rig(api, Patch())
    if "--many" in sys.argv:
        import httpx
        from decimal import Decimal
        from htrsa.scholarly import CrossrefProvider, ScholarlyHTTPClient
        from test_research_report_flow import ABSTRACT
        def many_sources(req):
            searches.append(str(req.url))
            return httpx.Response(200, json={"message": {"items": [
                {"DOI": "10.5555/co2/" + str(i), "URL": "https://doi.org/10.5555/co2/" + str(i),
                 "title": ["Temperature and CO2 release in carbonated beverages · " + "서로 다른 측정 조건과 출처를 비교하는 문헌 제목 " * 8 + str(i)],
                 "abstract": ABSTRACT} for i in range(5)]}})
        provider = CrossrefProvider(ScholarlyHTTPClient(retries=0, transport=httpx.MockTransport(many_sources)))
        async def large_search(*args, **kwargs):
            return await base_search(*args, provider=provider, price=Decimal(0), price_source="offline-fixed", **kwargs)
        search_policy.run_search = large_search
    def save():
        value = {"model_calls": prior_model_calls + model_calls, "searches": len(prior_searches) + len(searches), "paid_calls": 0,
                 "runs": [dict(r) for r in api.store.db.execute("SELECT research_id,status,error FROM control_runs")]}
        (folder / "observed.json").write_bytes((to_json(value) + "\n").encode("utf-8", errors="strict"))
    if "--crash" in sys.argv:
        old = StateService.finish_runtime_step
        def crash(state, rid, key, output, *args, **kwargs):
            value = old(state, rid, key, output, *args, **kwargs)
            if key == "report-draft:1":
                save()
                os._exit(79)
            return value
        StateService.finish_runtime_step = crash
        run(api, gateway)
        raise AssertionError("종료 지점에 도달하지 못했습니다.")
    if "--resume" in sys.argv:
        rid = api.store.db.execute("SELECT research_id FROM control_runs LIMIT 1").fetchone()[0]
        api.command(rid, "resume", {"expected_version": api.store.run(rid)["version"], "idempotency_key": "fresh-resume-once"})
        result = {"status": api.store.run(rid)["status"], "report": report_record(api.read._state, rid),
                  "model_calls": model_calls, "searches": len(searches), "paid_calls": 0}
        (folder / "resume.json").write_bytes((to_json(result) + "\n").encode("utf-8", errors="strict"))
        api.close()
        return
    rid = run(api, gateway, **({'fulltext_enabled': True} if '--fulltext' in sys.argv else {}))
    blocked = run(api, gateway, ai_report_enabled=False, public_search_query="", public_search_consent=False)
    from htrsa.resource_policy import save_preferences
    save_preferences(api.store, {"tutorial_completed": True, "tutorial_do_not_ask": True, "explanation_prompt_dismissed": True})
    board = {}
    if "--board" in sys.argv:
        for index in range(60):
            api.create(request(title=f"목록 검사 {index:02d} · " + "연구 조건과 자료 범위를 비교하는 아주 긴 연구 제목 " * 5))
        paused = api.create(request(title="일시정지 검사 연구"))["research_id"]
        api.store.db.execute("UPDATE control_runs SET status='PAUSED' WHERE research_id=?", (paused,))
        stale = run(api, gateway)
        api.store.db.execute("UPDATE research_runs SET state_version=state_version+1 WHERE research_id=?", (stale,))
        report_contract, report_contract_status = api.store.db.execute("SELECT c.contract_id,c.status FROM runtime_steps s JOIN contracts c ON c.contract_id=s.contract_id WHERE s.research_id=? AND s.step_key='report-draft:1'", (rid,)).fetchone()
        board = {"paused_id": paused, "stale_id": stale, "draft_count": 60}
        api.store.db.execute("INSERT INTO runtime_events(research_id,event_type,created_at,details_json) VALUES(?,?,?,?)",
                             (rid, "UI_FIXTURE", "2026-10-05T00:00:00+00:00", to_json({"chain_of_thought": "private-ui-v8-canary", "messages": ["private-ui-v8-canary"]})))
    save()
    (folder / "fixture.json").write_bytes(to_json({"research_id": rid, "blocked_id": blocked, **board}).encode("utf-8", errors="strict"))
    pdf = render_pdf(api.read._state, rid)
    (folder / "report.pdf").write_bytes(pdf["data"])
    if '--weak' in sys.argv:
        prior_model_calls.extend(model_calls)
        prior_searches.extend(searches)
        search_policy.run_search = base_search
        gateway, model_calls, searches = rig(api, Patch(), search='no_abstract')
    session = OwnerSession()
    server = create_server(api, session=session)
    print("http://" + server.RequestHandlerClass.authority + "/#bootstrap=" + session.issue_bootstrap(), flush=True)
    try:
        server.serve_forever()
    finally:
        session.close()
        server.server_close()
        api.close()


if __name__ == "__main__":
    main()
