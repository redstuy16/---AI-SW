"""실제 API 호출 없이 연구별 비용 변화를 재현하는 로컬 서버."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "qa"), str(ROOT / "tests")]

from htrsa.api import APIResponse
from htrsa.resource_policy import save_preferences
from htrsa.workbench import OwnerSession, create_server
from tutorial_browser_fixture import TutorialAPI, MemoryStore
from htrsa.control_plane import Credentials
from test_workbench import CSV, configure, create
from test_research_cost_display import reserve, settle


def main():
    folder = Path(sys.argv[1])
    folder.mkdir(parents=True, exist_ok=True)

    class CostAPI(TutorialAPI):
        pending = None

        def _request(self, method, path, body=None):
            if method == "POST" and path == "/qa/cost":
                self.store.transition(self.pending, "DISPATCHED")
                self.store.transition(self.pending, "SETTLED", settled=".003")
                return APIResponse(200, {"execution": "MOCK_LEDGER_ONLY"})
            return super()._request(method, path, body)

    api = CostAPI(folder / "state.sqlite", folder / "workspace", launch=False)
    api.credentials = Credentials(folder / "repository", api.workspace, folder / "private/unused.env")
    api.credentials.os_store = MemoryStore()
    configure(api)
    (api.workspace / "inputs/data.csv").write_bytes(CSV.read_bytes())
    first, second = create(api), create(api)
    api.request("POST", f"/api/control/research/{first}/rename", {"title": "비용 추적 연구"})
    api.request("POST", f"/api/control/research/{second}/rename", {"title": "별도 연구"})
    settle(api, first, amount=".004")
    settle(api, first, amount=".002", role="search")
    settle(api, second, amount=".015")
    api.pending = reserve(api, first)
    save_preferences(api.store, {"tutorial_completed": True, "tutorial_do_not_ask": True,
                                "explanation_prompt_dismissed": True})
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
