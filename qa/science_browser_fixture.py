"""실제 Workbench 실행기와 결정적 과학 모델로 브라우저 검사를 준비한다."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from probe.api import APIResponse
from probe.control_runtime import execute
from probe.control_plane import Credentials
from probe.database import to_json
from probe.providers.fake import FakeProvider
from probe.report_pdf import render_pdf
from probe.report_ux import friendly_report
from probe.secret_store import WindowsCredentialStore
from probe.resource_policy import save_preferences
from probe.workbench import WorkbenchAPI, OwnerSession, create_server
from test_autonomous_loop import CSV
from test_workbench import configure


class OfflineCredentials:
    """브라우저 모의 검증은 실제 자격 증명 저장소를 조회하지 않는다."""

    def get(self, name):
        return None

    def active_secrets(self, references=()):
        return ()

    def metadata(self, name):
        return {"configured": False, "active_source": "not_required",
                "storage_backend": "NOT_REQUIRED", "saved": False,
                "last_changed_at": None, "provider_revoked": False}


def completion(report_type="principle"):
    return {"action": "COMPLETE", "rationale": "원리와 적용 조건을 충분히 정리했습니다.", "report_draft": {
        "report_type": report_type, "summary": "빛의 조건과 광합성 원리를 설명합니다." if report_type == "principle" else "현재 자료의 온도와 생장 관계를 확인했습니다.",
        "explanation": "빛에너지는 광합성 과정에 사용됩니다. 조건과 변인을 분리해 비교합니다.",
        "variables": [{"name": "빛의 세기" if report_type == "principle" else "온도", "role": "독립변인", "unit": "상대 세기" if report_type == "principle" else "°C"},
            {"name": "기포 변화" if report_type == "principle" else "생장", "role": "종속변인", "unit": "개수" if report_type == "principle" else "기록 단위"},
            {"name": "수온과 용기", "role": "통제변인", "control": "같은 수온과 용기 조건을 유지합니다."}],
        "procedure": ["비교할 조건을 정합니다.", "같은 측정 기준으로 원자료를 기록합니다.", "오차와 해석 범위를 함께 확인합니다."],
        "measurement": "단위·반복·통제 조건을 함께 기록합니다.", "limitations": ["이 설명을 실제 수행한 실험 결과로 취급하지 않습니다."], "figure_refs": []}}


def analyze(call):
    bundle = json.loads(call["input_text"])
    context = json.loads(bundle["active_state"]["contract"]["objective"].split("\n", 1)[1])
    return {"action": "ANALYZE", "rationale": "입력 자료의 두 수치 열을 검증합니다.", "analysis_plan": {
        "dataset_id": context["dataset"]["dataset_id"], "selected_variables": ["temperature", "growth"],
        "method": "pearson_correlation", "justification": "실제 자료의 온도와 생장 기록을 사용합니다.",
        "requested_tools": ["stats.run", "visualization.render", "evidence.record"]}}


def main():
    folder = Path(sys.argv[1]).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    providers, scenarios, network_attempts, credential_attempts = {}, {}, [], []

    def gateway(store, rid, snapshot):
        question = snapshot["question"]
        scenario = scenarios.get(rid) or ("refine" if "설계 보완" in question else "analysis" if snapshot.get("source_relative") else "principle")
        replies = [completion()]
        if scenario == "refine":
            replies = [{"action": "REFINE_DESIGN", "rationale": "통제 조건을 구체화합니다.", "design_updates": {"control": "같은 수온과 용기를 유지합니다."}}, completion()]
        elif scenario == "analysis":
            replies = [analyze, completion("analysis")]
        provider = FakeProvider(replies)
        providers[rid] = provider
        return provider

    class OfflineAPI(WorkbenchAPI):
        @contextmanager
        def request_context(self):
            # 병렬 서버의 요청별 연결에서도 모의 모델과 검사 경로를 보존한다.
            api = OfflineAPI(self.database, self.workspace, mode=self.mode,
                             launch=self.launch, initialize_schema=False)
            api.credentials, api.children = self.credentials, self.children
            try:
                yield api
            finally:
                api.close()

        def command(self, rid, action, body):
            result = super().command(rid, action, body)
            if action in {"start", "resume"} and self.store.run(rid)["status"] in {"STARTING", "RESUMING"}:
                asyncio.run(execute(self.database, self.workspace, rid, provider_factory=gateway))
            return result

        def _request(self, method, path, body=None):
            if method == "GET" and path == "/qa/observed":
                guard = {"credential_lookup_attempts": len(credential_attempts),
                         "external_http_or_search_attempts": len(network_attempts), "paid_calls": 0}
                encoded = to_json(guard).encode("utf-8", errors="strict")
                (folder / "offline-guard.json").write_bytes(encoded)
                return APIResponse(200, {"calls": {rid: len(provider.calls) for rid, provider in providers.items()},
                    "types": {rid: [v["output_type"] for v in provider.calls] for rid, provider in providers.items()},
                    "searches": len(network_attempts), "external_attempts": network_attempts,
                    "credential_lookup_attempts": len(credential_attempts), "paid_calls": 0,
                    "runs": [dict(r) for r in self.store.db.execute("SELECT research_id,status,error FROM control_runs")],
                    "tool_calls": [dict(r) for r in self.store.db.execute("SELECT json_extract(request_json,'$.research_id') AS research_id,tool_name FROM tool_calls")]})
            return super()._request(method, path, body)

    def blocked_credential(*args, **kwargs):
        credential_attempts.append("unexpected-credential-read")
        raise AssertionError("오프라인 검사에서 실제 자격 증명 조회는 허용하지 않습니다.")
    Credentials.get = blocked_credential
    Credentials.active_secrets = blocked_credential
    Credentials._values = blocked_credential
    Credentials.metadata = blocked_credential
    WindowsCredentialStore.read = blocked_credential

    import probe.search_policy as search_policy
    async def blocked_search(*args, **kwargs):
        network_attempts.append("unexpected-search")
        raise AssertionError("오프라인 검사에서 검색은 허용하지 않습니다.")
    search_policy.run_search = blocked_search
    import httpx
    def blocked_http(*args, **kwargs):
        network_attempts.append("unexpected-http")
        raise AssertionError("오프라인 검사에서 외부 HTTP는 허용하지 않습니다.")
    httpx.Client.send = blocked_http
    httpx.AsyncClient.send = blocked_http

    api = OfflineAPI(folder / "state.sqlite", folder / "workspace", launch=False)
    api.credentials = OfflineCredentials()
    configure(api)
    (api.workspace / "inputs/data.csv").write_bytes(CSV.read_bytes())
    save_preferences(api.store, {"tutorial_completed": True, "tutorial_do_not_ask": True, "explanation_prompt_dismissed": True})
    ids = {}
    for scenario, question in [("principle", "빛의 세기와 광합성의 원리를 설명해 주세요."), ("refine", "빛의 세기와 광합성 탐구의 설계 보완"), ("analysis", "온도와 생장의 관계를 입력 자료로 확인합니다.")]:
        body = {"title": question, "question": question, "settings_version": 2, "execution_mode": "SCIENCE_AUTO",
            "model_profile_id": "m", "egress": "selected", "search_policy": "DISABLED", "ai_report_enabled": True,
            "run_limit_usd": ".10", "audience": "teacher", "science_field": "biology"}
        if scenario == "analysis": body["source_relative"] = "data.csv"
        created = api.create(body)
        rid = created["research_id"]
        scenarios[rid], ids[scenario] = scenario, rid
        api.command(rid, "start", {"expected_version": 0, "idempotency_key": "science-browser-" + scenario})
        run = api.store.run(rid)
        if run["status"] != "COMPLETED":
            raise AssertionError(to_json({"scenario": scenario, "status": run["status"], "error": run.get("error")}))
        output = render_pdf(api.read._state, rid)
        (folder / (scenario + ".pdf")).write_bytes(output["data"])
        (folder / (scenario + "-view.json")).write_bytes(to_json(friendly_report(api.read._state, rid)).encode("utf-8", errors="strict"))
    (folder / "fixture.json").write_bytes(to_json(ids).encode("utf-8", errors="strict"))
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
