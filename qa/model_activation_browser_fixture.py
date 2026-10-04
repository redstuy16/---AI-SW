"""모델 활성화 화면을 실제 서버와 모의 키 저장소로 검사한다."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from htrsa.control_plane import Connection, Credentials, ControlError
from htrsa.resource_policy import UIPreferences
from htrsa.workbench import WorkbenchAPI, OwnerSession, create_server


class MemoryStore:
    available = True

    def __init__(self):
        self.values = {"QA_OPENAI_KEY": "qa-model-activation-initial-canary"}

    def read(self, name):
        return self.values.get(name), None

    def write(self, name, value):
        if value is None:
            self.values.pop(name, None)
        else:
            self.values[name] = value


class OfflineAPI(WorkbenchAPI):
    async def check_model(self, *args, **kwargs):
        raise ControlError("QA_LIVE_CALL_BLOCKED")

    def command(self, rid, action, body):
        if action in {"start", "resume"}:
            raise ControlError("QA_LIVE_CALL_BLOCKED")
        return super().command(rid, action, body)


def main():
    folder = Path(sys.argv[1])
    folder.mkdir(parents=True, exist_ok=True)
    api = OfflineAPI(folder / "state.sqlite", folder / "workspace", launch=False)
    api.credentials = Credentials(folder / "repository", api.workspace, folder / "private/unused.env")
    api.credentials.os_store = MemoryStore()
    api.store.put("connection", "qa-openai", Connection(
        connection_id="qa-openai", display_name="OpenAI 검사 연결", adapter_id="openai",
        credential_env_name="QA_OPENAI_KEY", destination_approved=False))
    api.store.put("ui_preferences", "owner", UIPreferences(tutorial_completed=True,
        explanation_prompt_dismissed=True))
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
