"""실제 작업대 서버와 명시적 모의 전송으로 새 연구의 화면 경로를 검사한다."""
import asyncio
import json
import os
from pathlib import Path
import sys
import httpx
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"src"),str(ROOT/"tests")]
from probe.workbench import WorkbenchAPI,OwnerSession,create_server
from probe.control_plane import Connection,ModelProfile,Defaults
from probe.control_runtime import RoutedGateway,execute
from probe.provider_checks import check_model
from test_autonomous_loop import MANAGER
from probe.database import to_json

REPORT_DRAFT = {"summary": "확인된 수치 결과는 없습니다. 측정 자료가 필요합니다.", "procedure": ["측정 조건을 정하고 원본 자료를 기록합니다."], "limitations": ["자료 부족으로 결론을 확정하지 않습니다."]}


def main():
    folder=Path(sys.argv[1])
    folder.mkdir(parents=True,exist_ok=True)
    observed=[]
    def respond(request):
        value=json.loads(request.content)
        structured=bool(value.get("response_format"))
        manager_request=not any(message.get("content")=="짧은 기능 검사" for message in value.get("messages",[]))
        observed.append({"model":value["model"],"structured":structured,
            "contract_request":manager_request,
            "reasoning":value.get("reasoning_effort"),"temperature":value.get("temperature"),
            "body_keys":sorted(value)})
        text=to_json(observed);text.encode("utf-8",errors="strict")
        (folder/"observed.json").write_text(text,encoding="utf-8")
        return httpx.Response(200,json={"id":"offline-browser","model":value["model"],
            "choices":[{"message":{"role":"assistant","content":json.dumps(REPORT_DRAFT if "ReportDraft" in json.dumps(value) else MANAGER) if manager_request else "PROBE_OK"},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":10,"completion_tokens":20}})
    factory=lambda *_:httpx.AsyncClient(transport=httpx.MockTransport(respond))
    class OfflineAPI(WorkbenchAPI):
        async def check_model(self,profile_id,body):
            return await check_model(self,profile_id,body,client_factory=factory)
        def command(self,rid,action,body):
            result=super().command(rid,action,body)
            if action=="start" and self.store.run(rid)["status"]=="STARTING":
                def provider(store,research_id,snapshot):
                    return RoutedGateway(store,self.credentials,research_id,snapshot,client_factory=factory)
                asyncio.run(execute(self.database,self.workspace,rid,provider_factory=provider))
            return result
    api=OfflineAPI(folder/"state.sqlite",folder/"workspace",launch=False)
    local=Connection(connection_id="local",display_name="로컬 검사",adapter_id="openai_compatible",
        base_url="http://127.0.0.1:1234/v1",endpoint_class="loopback",destination_approved=True)
    api.store.put("connection","local",local)
    for identity in ["m","m2"]:
        api.store.put("model",identity,ModelProfile(profile_id=identity,connection_id="local",display_name="검사 모델 "+identity,
            model_id="manual-"+identity,protocol="chat",capability_status="supported",local_api_unmetered=True))
    api.store.put("defaults","global",Defaults(request_limit_usd=".10"))
    os.environ["PROBE_OFFLINE_CATALOG_KEY"]="offline-catalog-canary-for-ui-fixture"
    api.store.put("connection","catalog",Connection(connection_id="catalog",display_name="오프라인 목록 검사",
        adapter_id="openai",base_url="https://api.openai.com/v1",endpoint_class="cloud",destination_approved=True,credential_env_name="PROBE_OFFLINE_CATALOG_KEY"))
    api.request("POST","/api/control/preferences",{"explanation_prompt_dismissed":True})
    session=OwnerSession();server=create_server(api,session=session)
    print("http://"+server.RequestHandlerClass.authority+"/#bootstrap="+session.issue_bootstrap(),flush=True)
    try:server.serve_forever()
    finally:session.close();server.server_close();api.close()


if __name__=="__main__":main()
