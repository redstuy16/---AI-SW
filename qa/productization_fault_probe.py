"""모의 제공사 고장과 검사 전체 예산 차단의 관측 근거를 저장한다."""
import asyncio
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import pytest
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_productization import body, prepare, run
from probe.preflight import _source_fingerprint
from probe.productization import qualify
from probe.workbench import WorkbenchAPI


def main():
    rows = []
    folder = ROOT / "build/productization-faults" / uuid4().hex
    cases = [("healthy", None, body()), *[(str(f), f, body()) for f in
        (401, 429, 503, "timeout", "usage", "truncated", "incomplete", "empty", "secret", "bad_json", "bad_tool")],
        ("cap_before", None, body(discovery=False, max_total_usd=".00001")),
        ("cap_between", None, body(discovery=False, max_total_usd=".0107"))]
    for name, fault, request in cases:
        app = WorkbenchAPI(folder / name / "state.sqlite", folder / name / "workspace", launch=False,
                           credential_file=folder / "absent-key.env")
        with pytest.MonkeyPatch.context() as patch:
            try:
                identity, calls = prepare(app, patch), []
                def invoke():
                    if fault not in {401, 429, 503}:
                        return run(app, identity, calls, request, fault)
                    def respond(http_request):
                        calls.append(http_request)
                        if http_request.method == "GET":
                            return httpx.Response(200, json={"data": [{"id": "gpt-6.1-sol"}]})
                        return httpx.Response(fault, json={"error": {"message": "제공사 검사 오류"}})
                    return asyncio.run(qualify(app, identity, request, client_factory=lambda *_:
                        httpx.AsyncClient(transport=httpx.MockTransport(respond))))
                started = time.monotonic()
                result = invoke()
                duration = time.monotonic() - started
                observed_count = len(calls)
                replay = invoke()
                assert replay["replayed"] and len(calls) == observed_count
                assert result["status"] == ("OFFLINE_VALIDATED" if name == "healthy" else "NOT_VALIDATED")
                if fault in {401, 429, 503}:
                    assert result["error_code"] == {401: "AUTHENTICATION_ERROR", 429: "RATE_LIMIT", 503: "PROVIDER_UNAVAILABLE"}[fault]
                rows.append({"scenario": name, "status": result["status"], "error_code": result.get("error_code"),
                    "http_requests": observed_count, "generation_requests": len([c for c in calls if c.method == "POST"]),
                    "same_key_redispatch": len(calls) - observed_count, "automatic_retry": result["automatic_retry"],
                    "checks_completed": [c["mode"] for c in result["checks"]],
                    "spent_usd": result["ledger"]["spent"], "unresolved_usd": result["ledger"]["unresolved"],
                    "ledger_statuses": [r["status"] for r in result["ledger"]["requests"]],
                    "whole_suite_cap_usd": result["max_total_usd"], "offline_wall_sec": round(duration, 6)})
                assert Decimal(result["ledger"]["spent"]) + Decimal(result["ledger"]["unresolved"]) <= Decimal(result["max_total_usd"])
                assert "offline-productization-canary" not in json.dumps(rows)
            finally:
                app.close()
    record = {"passed": True, "execution": "MOCK_HTTP", "source_fingerprint": _source_fingerprint(),
              "scenarios": rows, "covered_secret_exposures": 0, "paid_live_calls": 0,
              "live_connectivity": "NOT_VALIDATED", "research_efficacy": "NOT_VALIDATED",
              "note": "토큰·단가·응답은 고정 fixture다. wall time은 오프라인 검사 시간이며 실제 모델 속도·품질 향상을 나타내지 않는다."}
    data = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8", errors="strict")
    (ROOT / "qa/results/productization_fault_results.json").write_bytes(data)
    print(json.dumps({"passed": True, "scenarios": len(rows), "redispatch": 0, "canary_exposures": 0, "paid_live_calls": 0}))


if __name__ == "__main__":
    main()
