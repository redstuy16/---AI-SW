"""유료 실행을 차단하고 상세 설계의 실제 도구 결과를 준비하는 검사 서버."""
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "qa")]

from tutorial_browser_fixture import TutorialAPI, MemoryStore
from probe.workbench import OwnerSession, create_server
from probe.control_plane import Credentials
from probe.final_report import export_final_report
from probe.resource_policy import save_preferences
from test_research_design import execute


def main():
    folder = Path(sys.argv[1])
    folder.mkdir(parents=True, exist_ok=True)
    api = TutorialAPI(folder / "state.sqlite", folder / "workspace", launch=False)
    api.credentials = Credentials(folder / "repository", api.workspace, folder / "private/unused.env")
    api.credentials.os_store = MemoryStore()
    design = {"approach": "EXISTING", "fields": {"purpose": {"value": "연간 기온 편차 비교"},
        "interpretation_limits": {"value": "지정 기간의 관측 비교만"}},
        "variables": [{"id": "control-source-01", "name": "같은 자료 기준", "role": "fixed",
                       "details": {"maintain": {"value": "동일 원본"}, "check": {"value": "해시 확인"}}}]}
    rid, snapshot, runtime, provider, result = execute(api, design)
    api.read._state.stop_research(rid, "QUALIFIED_PROCEDURE_COMPLETED")
    api.store.db.execute("UPDATE control_runs SET status='COMPLETED' WHERE research_id=?", (rid,))
    export_final_report(api.read._state, rid)
    save_preferences(api.store, {"tutorial_completed": True, "tutorial_do_not_ask": True, "explanation_prompt_dismissed": True})
    data = json.dumps({"research_id": rid, "fake_model_calls": len(provider.calls), "paid_calls": 0}, ensure_ascii=False).encode("utf-8", errors="strict")
    (folder / "execution.json").write_bytes(data)
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

