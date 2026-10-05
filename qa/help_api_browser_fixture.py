"""공식 단가 적용과 모의 제공사 응답을 실제 앱 경로로 검사한다."""
from pathlib import Path
import asyncio
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "qa"), str(ROOT / "tests")]

import httpx
from probe.api import APIResponse
from probe.control_plane import Connection, Credentials, Defaults, ModelProfile
from probe.product_policy import resolve_catalog_profile
from probe.provider_checks import check_model
from probe.resource_policy import save_preferences
from probe.workbench import WorkbenchAPI, OwnerSession, create_server
from probe.control_runtime import RoutedGateway, execute
from tutorial_browser_fixture import TutorialAPI, MemoryStore
from test_multi_provider import document
from test_autonomous_loop import MANAGER


class PriceAPI(TutorialAPI):
    provider_calls = 0
    profile_id = None
    allow_research = False

    def command(self, rid, action, body):
        if not self.allow_research:
            return super().command(rid, action, body)
        result = WorkbenchAPI.command(self, rid, action, body)
        if action == "start" and self.store.run(rid)["status"] == "STARTING":
            def respond(request):
                self.provider_calls += 1
                return httpx.Response(200, json=document("responses", secret=json.dumps(MANAGER)))
            factory = lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(respond))
            asyncio.run(execute(self.database, self.workspace, rid,
                provider_factory=lambda store, research_id, snapshot: RoutedGateway(
                    store, self.credentials, research_id, snapshot, client_factory=factory)))
        return result

    async def check_model(self, profile_id, body):
        self.checks += 1

        def respond(request):
            self.provider_calls += 1
            return httpx.Response(200, json=document("responses"))

        return await check_model(self, profile_id, body,
            client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(respond)))

    def _request(self, method, path, body=None):
        if method == "POST" and path == "/qa/reset-price":
            raw = self.store.config("model", self.profile_id)
            revision = next(m["revision"] for m in self.store.configs("model") if m["profile_id"] == self.profile_id)
            self.store.put("model", self.profile_id, ModelProfile.model_validate(raw).model_copy(update={"price": None}), revision)
            return APIResponse(200, {"execution": "MOCK_PROVIDER"})
        response = super()._request(method, path, body)
        if method == "GET" and path == "/qa/state":
            response.body.update(provider_calls=self.provider_calls, spent=self.store.ledger()["spent"],
                price=self.store.config("model", self.profile_id)["price"], paid_calls=0, execution="MOCK_PROVIDER",
                runs=[dict(row) for row in self.store.db.execute("SELECT status,error FROM control_runs")])
        return response


def main():
    folder = Path(sys.argv[1])
    folder.mkdir(parents=True, exist_ok=True)
    api = PriceAPI(folder / "state.sqlite", folder / "workspace", launch=False)
    api.allow_research = "--research" in sys.argv[2:]
    api.credentials = Credentials(folder / "repository", api.workspace, folder / "private/unused.env")
    api.credentials.os_store = MemoryStore()
    api.store.put("connection", "qa-openai", Connection(connection_id="qa-openai",
        display_name="OpenAI 검사 연결", adapter_id="openai", credential_env_name="QA_OPENAI_KEY",
        destination_approved=True))
    api.store.put("defaults", "global", Defaults(request_limit_usd=".10"))
    api.profile_id = resolve_catalog_profile(api.store,
        {"provider": "openai", "model_id": "gpt-6-luna", "connection_id": "qa-openai"})
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
