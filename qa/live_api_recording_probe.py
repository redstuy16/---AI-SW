"""모의 HTTP의 정상·실패 사례를 실제 원장과 증거 패키지로 실행한다."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from htrsa.control_plane import Connection, ModelProfile, PriceRecord
from htrsa.live_api_test import run_session, export_session, summarize, verify_package, scan
from htrsa.providers.normalized import CAPABILITIES, CapabilityEvidence, ReasoningPolicy
from htrsa.schemas import utc_now
from htrsa.workbench import WorkbenchAPI

CANARY = "offline-live-recording-canary-" + uuid4().hex
BASE = ROOT / "build" / "live-api-recording-probe" / uuid4().hex
BASE.mkdir(parents=True)
checks = []


def responder(calls, fault):
    def respond(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "qa-fixed-model"}]})
        payload = json.loads(request.content)
        if fault in {401, 429, 503}: return httpx.Response(fault, json={"error": {"message": "비공개 메시지"}})
        if fault == "timeout": raise httpx.ReadTimeout("비공개 메시지")
        if fault == "malformed": return httpx.Response(200, content=b'{"unfinished":')
        tools = payload.get("tool_choice") == "required"
        output = [{"type": "function_call", "call_id": "safe-call", "name": "get_test_value", "arguments": '{"key":"probe"}'}] if tools else [
            {"type": "message", "content": [{"type": "output_text", "text": '{"status":"ok"}' if "text" in payload else "HTRSA_OK"}]}]
        value = {"id": "safe-response", "model": "qa-fixed-model", "status": "completed", "output": output,
            "usage": {"input_tokens": 40, "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens": 12, "output_tokens_details": {"reasoning_tokens": 4}, "total_tokens": 52}}
        if fault == "missing_usage": value.pop("usage")
        if fault == "wrong_model": value["model"] = "unselected-model"
        if fault == "incomplete": value["status"] = "incomplete"
        if fault == "secret": value["output"][0]["content"][0]["text"] = CANARY
        if fault == "schema": value["output"][0]["content"][0]["text"] = '{"status":"wrong"}'
        if fault == "tool": value["output"][0]["arguments"] = '{"key":"private-file"}'
        if payload.get("stream"):
            events = [{"type": "response.output_text.delta", "delta": "HTRSA_OK"},
                      {"type": "response.completed", "response": value}]
            content = ("\n\n".join("data: " + json.dumps(e) for e in events) + "\n\n").encode("utf-8")
            return httpx.Response(200, content=content, headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=value, headers={"x-request-id": "safe-request"})
    return lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(respond))


async def scenario(name, modes, fault=None, cap=".10"):
    folder = BASE / name
    app = WorkbenchAPI(folder / "state.sqlite", folder / "workspace", launch=False, credential_file=folder / "absent.env")
    try:
        connection = Connection(connection_id="qa-openai", display_name="모의 OpenAI", destination_approved=True, discovery_unmetered=True)
        profile = ModelProfile(profile_id="qa-profile", connection_id=connection.connection_id, model_id="qa-fixed-model",
            price=PriceRecord(input_per_million=1, output_per_million=2, cached_input_per_million=0.1,
                checked_at=utc_now(), source="오프라인 검사용 모의 단가", revision="mock-price-v1", owner_verified=True),
            capabilities={k: CapabilityEvidence(status="SUPPORTED", source="USER_DECLARED") for k in CAPABILITIES},
            reasoning_levels=list(ReasoningPolicy))
        app.store.put("connection", connection.connection_id, connection)
        app.store.put("model", profile.profile_id, profile)
        calls = []
        record = await run_session(app, profile.profile_id, {"idempotency_key": str(uuid4()), "live_test_budget_cap": cap,
            "live": True, "consent": True, "cases": modes}, client_factory=responder(calls, fault))
        package = export_session(app, record["api_test_session_id"])
        result = summarize(app, record)
        expected = "OFFLINE_SIMULATION_PASS" if fault is None and cap == ".10" else "TEST_SESSION_STOPPED"
        assert record["status"] == expected
        assert result["totals"]["live_generation_request_count"] == 0
        assert verify_package(package["relative_path"])["status"] == "PASS"
        assert scan(app, Path(package["relative_path"]), (CANARY,))["exposure_count"] == 0
        checks.append({"case": name, "passed": True, "status": record["status"], "stop_reason": record.get("stop_reason"),
            "session_id": record["api_test_session_id"], "package": package["relative_path"],
            "mock_http_requests": len(calls), "mock_generation_requests": sum(r.method == "POST" for r in calls),
            "live_generation_requests": 0, "totals": result["totals"]})
    finally: app.close()


async def main():
    old = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = CANARY
    try:
        await scenario("all-selected", ["discovery", "text", "structured", "tools", "integration", "stream"])
        for name, mode, fault in [("auth", "text", 401), ("rate", "text", 429), ("unavailable", "text", 503),
            ("timeout", "text", "timeout"), ("malformed", "text", "malformed"), ("missing-usage", "text", "missing_usage"),
            ("model-mismatch", "text", "wrong_model"), ("incomplete", "text", "incomplete"), ("secret", "text", "secret"),
            ("schema-mismatch", "structured", "schema"), ("tool-denied", "tools", "tool")]:
            await scenario(name, [mode], fault)
        await scenario("budget-before-dispatch", ["text"], cap=".000001")
        result = {"passed": all(c["passed"] for c in checks), "execution": "MOCK_HTTP_ONLY", "cases": checks,
            "case_count": len(checks), "live_paid_requests": 0, "secret_exposures": 0,
            "live_agent_performance": "NOT_VALIDATED", "source_note": "모의 모델·모의 단가의 경계 검사이며 제공사 성능·청구 검증이 아니다."}
        text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        text.encode("utf-8", errors="strict")
        assert CANARY not in text
        (ROOT / "qa/results" / "live_api_recording_offline_results.json").write_text(text, encoding="utf-8", errors="strict")
        print(json.dumps({"passed": result["passed"], "cases": len(checks), "live_paid_requests": 0, "secret_exposures": 0}))
    finally:
        if old is None: os.environ.pop("OPENAI_API_KEY", None)
        else: os.environ["OPENAI_API_KEY"] = old


if __name__ == "__main__": asyncio.run(main())

