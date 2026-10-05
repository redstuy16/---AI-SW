"""실제 로컬 앱에 모의 키 저장소·오류만 연결하는 튜토리얼 검사 서버."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from probe.api import APIResponse
from probe.control_plane import Credentials, ControlError
from probe.resource_policy import preferences
from probe.workbench import WorkbenchAPI, OwnerSession, create_server
from model_activation_browser_fixture import MemoryStore


class TutorialAPI(WorkbenchAPI):
    fault = None
    checks = 0

    async def check_model(self, profile_id, body):
        self.checks += 1
        raise ControlError(self.fault or "QA_LIVE_CALL_BLOCKED")

    def command(self, rid, action, body):
        if action in {"start", "resume"}:
            raise ControlError("QA_LIVE_CALL_BLOCKED")
        return super().command(rid, action, body)

    def _request(self, method, path, body=None):
        if path == "/qa/fault" and method == "POST":
            code = body.get("code")
            allowed = {None, "PROVIDER_AUTH", "PROVIDER_PERMISSION", "PROVIDER_SPEND_LIMIT", "PROVIDER_RATE_LIMIT",
                       "MODEL_NOT_FOUND", "SECRET_READBACK_FAILED"}
            if code not in allowed:
                raise ControlError("QA_FAULT_INVALID")
            self.fault = code
            return APIResponse(200, {"execution": "MOCK_ERROR"})
        if path == "/qa/state" and method == "GET":
            return APIResponse(200, {
                "researches": self.store.db.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0],
                "agents": self.store.db.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0],
                "requests": len(self.store.ledger()["requests"]),
                "checks": self.checks,
                "keys": len(self.credentials.os_store.values),
                "preferences": preferences(self.store)})
        if path == "/api/control/connections/register" and self.fault == "SECRET_READBACK_FAILED":
            return APIResponse(409, {"error": "SECRET_READBACK_FAILED"})
        return super()._request(method, path, body)


def main():
    folder = Path(sys.argv[1])
    folder.mkdir(parents=True, exist_ok=True)
    api = TutorialAPI(folder / "state.sqlite", folder / "workspace", launch=False)
    api.credentials = Credentials(folder / "repository", api.workspace, folder / "private/unused.env")
    api.credentials.os_store = MemoryStore()
    api.credentials.os_store.values.clear()
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
