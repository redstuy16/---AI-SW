"""실제 서버에 모의 API와 고정 공개 원본을 연결하는 오프라인 UI 검사."""
import asyncio
import json
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from htrsa.workbench import WorkbenchAPI, OwnerSession, create_server
from htrsa.control_plane import Connection, ModelProfile, Defaults
from htrsa.control_runtime import RoutedGateway, execute
from htrsa.provider_checks import check_model
from htrsa import qualified_workflow
from htrsa.api import APIResponse
from test_autonomous_loop import MANAGER


def main():
    folder = Path(sys.argv[1])
    folder.mkdir(parents=True, exist_ok=True)
    observed = []
    source = (ROOT / "qa/qualified_profiles/public/gistemp.txt").read_text(encoding="utf-8")
    secondary = (ROOT / "qa/qualified_profiles/public/gistemp.csv").read_text(encoding="utf-8")
    async def fixed_source(snapshot):
        return source
    qualified_workflow.fetch_source = fixed_source
    async def fixed_download(url, **kwargs):
        assert str(url).endswith(".csv"), "고정 CSV 외 다운로드"
        return secondary
    qualified_workflow._fetch_text = fixed_download
    def respond(request):
        body = json.loads(request.content)
        fixed = any(m.get("content") == "짧은 기능 검사" for m in body.get("messages", []))
        profile = "QualifiedProfileDecision" in json.dumps(body) or "ProfileDecision" in json.dumps(body)
        output = "HTRSA_OK" if fixed else json.dumps({"decision":"PROCEED","reason":"공식 연간 자료의 두 기간 비교"} if profile else MANAGER)
        observed.append({"model":body["model"],"profile":profile,"connection_check":fixed,"execution":"MOCK_HTTP"})
        text = json.dumps(observed, ensure_ascii=False)
        text.encode("utf-8", errors="strict")
        (folder / "observed.json").write_text(text, encoding="utf-8")
        return httpx.Response(200, json={"model":body["model"],"choices":[{"message":{"content":output},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":20}})
    factory = lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(respond))
    class OfflineAPI(WorkbenchAPI):
        async def check_model(self, profile_id, body):
            return await check_model(self, profile_id, body, client_factory=factory)
        def command(self, rid, action, body):
            result = super().command(rid, action, body)
            if action == "start" and self.store.run(rid)["status"] == "STARTING":
                asyncio.run(execute(self.database, self.workspace, rid, provider_factory=lambda store, research_id, snapshot:
                    RoutedGateway(store, self.credentials, research_id, snapshot, client_factory=factory)))
            return result
        def _request(self, method, path, body=None):
            nonlocal source, secondary
            if method == "POST" and path == "/qa/revise":
                lines = source.splitlines()
                for i, line in enumerate(lines):
                    if line.startswith("2005 "):
                        cells = line.split();cells[13] = str(int(cells[13]) + 10);lines[i] = " ".join(cells)
                source = "\n".join(lines) + "\n"
                lines = secondary.splitlines()
                for i, line in enumerate(lines):
                    if line.startswith("2005,"):
                        cells = line.split(",")
                        cells[13] = str(float(cells[13]) + .10)
                        lines[i] = ",".join(cells)
                secondary = "\n".join(lines) + "\n"
                return APIResponse(200, {"execution":"OFFLINE_SOURCE_REVISION"})
            return super()._request(method, path, body)
    api = OfflineAPI(folder / "state.sqlite", folder / "workspace", launch=False)
    api.store.put("connection", "local", Connection(connection_id="local", display_name="오프라인 검사용 AI", adapter_id="openai_compatible",
        base_url="http://127.0.0.1:1234/v1", endpoint_class="loopback", destination_approved=True))
    for identity in ["m", "m2"]:
        api.store.put("model", identity, ModelProfile(profile_id=identity, connection_id="local", display_name="검사 모델 " + identity,
            model_id="manual-" + identity, protocol="chat", capability_status="supported", local_api_unmetered=True))
    api.store.put("defaults", "global", Defaults(request_limit_usd=".10"))
    session = OwnerSession()
    server = create_server(api, session=session)
    print("http://" + server.RequestHandlerClass.authority + "/#bootstrap=" + session.issue_bootstrap(), flush=True)
    try:
        server.serve_forever()
    finally:
        session.close();server.server_close();api.close()


if __name__ == "__main__":
    main()
