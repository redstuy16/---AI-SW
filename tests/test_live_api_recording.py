"""검사 계측의 예산·복구·설정·비밀·내보내기 경계를 모의 HTTP로 확인한다."""
import asyncio
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest

from probe.control_plane import ControlError, money
from probe.live_api_test import (LiveTestPlan, attempts, capabilities, clean, digest, export_session,
    generation, recover_sessions, run_session, scan, summarize, verify_manifest, verify_package)
from probe.productization import revision
from probe.workbench import WorkbenchAPI
from test_multi_provider import app, setup_app, document


def plan(**changes):
    return {"idempotency_key": "live-record-test-key", "live_test_budget_cap": ".10",
            "live": True, "consent": True, **changes}


def setup(app, monkeypatch):
    return setup_app(app, "openai", monkeypatch)[1].profile_id


def factory(calls, fault=None):
    def response(req):
        calls.append(req)
        if req.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "manual-id"}]})
        payload = json.loads(req.content)
        if fault in {401, 403, 404, 429, 503}:
            return httpx.Response(fault, headers={"x-request-id": "req-failure"},
                                  json={"error": {"code": "invalid_parameter", "message": "개인 오류 메시지"}})
        if fault == "timeout": raise httpx.ReadTimeout("개인 오류 메시지")
        if fault == "network": raise httpx.ConnectError("개인 오류 메시지")
        if fault == "malformed": return httpx.Response(200, content=b'{"status":')
        is_tool = payload.get("tool_choice") == "required"
        value = document("responses", tools=is_tool)
        value["model"] = "manual-id"
        value["usage"]["total_tokens"] = 52
        if not is_tool and "text" not in payload:
            value["output"][0]["content"][0]["text"] = "PROBE_OK"
        if fault == "wrong_model": value["model"] = "unselected-provider-model"
        if fault == "missing_model": value.pop("model")
        if fault == "missing_usage": value.pop("usage")
        if fault == "missing_categories":
            value["usage"].pop("output_tokens_details")
            value["usage"].pop("total_tokens")
        if fault == "usage_bound": value["usage"]["output_tokens"] = 500
        if fault == "incomplete": value["status"] = "incomplete"
        if fault == "refusal":
            value["output"] = [{"type": "message", "content": [{"type": "refusal", "refusal": "미공개 거부 원문"}]}]
        if fault == "secret":
            value["output"][0]["content"][0]["text"] = "secret-provider-canary-123456789"
        if fault == "hidden":
            value["output"].insert(0, {"type": "reasoning", "summary": [{"type": "summary_text", "text": "미공개 추론 원문"}]})
        if fault == "bad_schema" and "text" in payload:
            value["output"][0]["content"][0]["text"] = '{"status":"wrong","extra":1}'
        if fault == "bad_tool" and is_tool: value["output"][0]["arguments"] = '{"key":"private-file"}'
        if fault == "cap_between":
            value["usage"].update(input_tokens=4000, output_tokens=128, total_tokens=4128)
        return httpx.Response(200, headers={"x-request-id": "req-safe"}, json=value)
    return lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(response))


def run(app, identity, calls, request=None, fault=None):
    return asyncio.run(run_session(app, identity, request or plan(), client_factory=factory(calls, fault)))


def test_default_is_offline_and_requires_explicit_cap():
    value = LiveTestPlan.model_validate({"idempotency_key": "offline-plan-key", "live_test_budget_cap": ".10"})
    assert value.live is value.consent is False
    assert value.cases == ["discovery", "text"]
    with pytest.raises(ValueError):
        LiveTestPlan.model_validate({"idempotency_key": "offline-plan-key"})


def test_default_only_discovery_and_one_generation(app, monkeypatch):
    identity = setup(app, monkeypatch)
    connection = app.store.config("connection", "c")
    connection["discovery_unmetered"] = True
    app.store.put("connection", "c", connection, revision(app.store, "connection", "c"))
    calls = []
    record = run(app, identity, calls)
    value = summarize(app, record)
    assert record["status"] == "OFFLINE_SIMULATION_PASS"
    assert [r.method for r in calls] == ["GET", "POST"]
    assert value["totals"]["live_generation_request_count"] == 0
    assert value["totals"]["offline_simulated_generation_request_count"] == 1
    assert value["connection_checks"]["structured_output"] == "NOT_TESTED"
    assert value["connection_checks"]["tool_calling"] == "NOT_TESTED"
    assert record["skill_live_efficacy"] == record["f3p_live_efficacy"] == "NOT_VALIDATED"
    assert all(a["execution"] == "MOCK_HTTP" for a in value["attempts"])
    assert all("secret-provider-canary" not in json.dumps(a) for a in value["attempts"])


@pytest.mark.parametrize("problem,code", [
    ("consent", "PAID_TEST_CONSENT_REQUIRED"), ("price", "PRICE_REQUIRED"),
    ("tiny_budget", "BUDGET_BLOCKED"), ("key", "CREDENTIAL_UNCONFIGURED"),
    ("egress", "EGRESS_BLOCKED"), ("discovery", "DISCOVERY_COST_NOT_BOUNDED_MANUAL_ID_ALLOWED"),
    ("capability", "CAPABILITY_NOT_VALIDATED"), ("reasoning", "UNSUPPORTED_CAPABILITY")])
def test_preflight_blocks_without_http_or_reservation(app, monkeypatch, problem, code):
    identity, calls = setup(app, monkeypatch), []
    body = plan(cases=["text"])
    if problem == "consent": body["consent"] = False
    if problem == "tiny_budget": body["live_test_budget_cap"] = ".000001"
    if problem == "key": monkeypatch.delenv("OPENAI_API_KEY")
    if problem == "discovery": body["cases"] = ["discovery", "text"]
    if problem in {"price", "capability", "reasoning"}:
        p = app.store.config("model", identity)
        if problem == "price": p["price"] = None
        if problem == "capability":
            p["capabilities"]["structured_output"]["status"] = "UNKNOWN"
            body["cases"] = ["structured"]
        if problem == "reasoning":
            p["reasoning_levels"] = ["LOW"]; body["reasoning_level"] = "MAX"
        app.store.put("model", identity, p, revision(app.store, "model", identity))
    if problem == "egress":
        c = app.store.config("connection", "c"); c["destination_approved"] = False
        app.store.put("connection", "c", c, revision(app.store, "connection", "c"))
    record = run(app, identity, calls, body)
    assert record["stop_reason"] == code
    assert not calls and not app.store.ledger(record["api_test_session_id"])["requests"]


@pytest.mark.parametrize("fault,code", [(401, "AUTHENTICATION_ERROR"), (403, "AUTHORIZATION_ERROR"), (404, "MODEL_NOT_FOUND"),
    (429, "RATE_LIMIT"), (503, "PROVIDER_UNAVAILABLE"), ("timeout", "TIMEOUT"), ("network", "NETWORK_ERROR"),
    ("malformed", "MALFORMED_RESPONSE"), ("missing_usage", "USAGE_MISSING"), ("usage_bound", "USAGE_BOUND_EXCEEDED"),
    ("incomplete", "INCOMPLETE_RESPONSE"), ("refusal", "REFUSAL"), ("wrong_model", "MODEL_MISMATCH"),
    ("missing_model", "MODEL_MISMATCH"), ("secret", "SECRET_IN_PROVIDER_RESPONSE")])
def test_fault_denominator_no_retry_and_redacted_errors(app, monkeypatch, fault, code):
    identity, calls = setup(app, monkeypatch), []
    record = run(app, identity, calls, plan(cases=["text"]), fault)
    value = summarize(app, record)
    assert record["status"] == "TEST_SESSION_STOPPED" and record["stop_reason"] == code
    assert len(calls) == 1 and len(value["attempts"]) == 1
    assert value["totals"]["retry_count"] == 0
    assert value["totals"]["failed_generation_requests"] + value["totals"]["ambiguous_generation_requests"] == 1
    item = value["attempts"][0]
    if isinstance(fault, int): assert item["error"]["http_status"] == fault
    if fault == "missing_usage": assert item["usage"]["input_tokens"]["source"] == "UNKNOWN"
    assert "개인 오류 메시지" not in json.dumps(value, ensure_ascii=False)
    assert "secret-provider-canary" not in json.dumps(value)
    assert sum(r["settled"] if r["settled"] is not None else r["reserved"] for r in value["ledger"]["requests"]) <= 100000


def test_requested_effective_usage_cost_and_identity(app, monkeypatch):
    identity, calls = setup(app, monkeypatch), []
    profile = app.store.config("model", identity)
    profile["price"]["input_per_million"] = ".1234567"
    app.store.put("model", identity, profile, revision(app.store, "model", identity))
    record = run(app, identity, calls, plan(cases=["text"], reasoning_level="LOW"), "missing_categories")
    item = summarize(app, record)["attempts"][0]
    assert item["requested_parameters"]["reasoning_policy"] == "LOW"
    native = item["effective_parameters"]["native_parameters"]
    assert native["reasoning"] == {"effort": "low"} and native["max_output_tokens"] == 128 and native["store"] is False
    assert item["effective_parameters"]["provider_applied_settings"] == "UNKNOWN"
    assert item["usage"]["reasoning_tokens"] == {"value": None, "source": "UNKNOWN"}
    assert item["usage"]["cached_input_tokens"] == {"value": None, "source": "UNKNOWN"}
    assert item["usage"]["total_tokens"] == {"value": 52, "source": "ESTIMATED"}
    assert item["cost"]["status"] == "ESTIMATED" and item["cost"]["provider_invoice"] == "UNKNOWN"
    assert item["budget_before"]["decision"] == "ALLOW"
    assert item["budget_before"]["new_reservation_usd"] == item["cost"]["reserved_usd"]
    ledger_row = app.store.ledger(record["api_test_session_id"])["requests"][0]
    assert item["cost"]["reserved_usd"] == money(ledger_row["reserved"])
    assert item["cost"]["observed_cost_usd"] == money(ledger_row["settled"])
    assert item["cost"]["released_reservation_usd"] == money(ledger_row["reserved"] - ledger_row["settled"])
    assert item["identity"]["provider_model_revision_if_available"] == "UNKNOWN"
    assert item["response"]["provider_request_id"] == "req-safe"
    assert item["latency"]["first_token_ms"] == "UNKNOWN" and item["latency"]["request_latency_ms"] > 0
    assert len({a["attempt_id"] for a in attempts(app, record["api_test_session_id"])}) == 1


def test_structured_schema_is_strict_and_independent(app, monkeypatch):
    identity, calls = setup(app, monkeypatch), []
    record = run(app, identity, calls, plan(cases=["structured"]))
    value = summarize(app, record)
    schema = json.loads(calls[0].content)["text"]["format"]
    assert schema["strict"] and schema["schema"]["additionalProperties"] is False
    item = value["attempts"][0]
    assert item["structured_output"]["application_schema_validated"]
    assert item["structured_output"]["provider_schema_enforced"] == "UNKNOWN"
    assert value["connection_checks"]["text_inference"] == "NOT_TESTED"
    assert value["connection_checks"]["structured_output"] == "PASS"


def test_invalid_schema_retains_failure_without_repair(app, monkeypatch):
    identity, calls = setup(app, monkeypatch), []
    record = run(app, identity, calls, plan(cases=["structured"]), "bad_schema")
    item = attempts(app, record["api_test_session_id"])[0]
    assert record["stop_reason"] == "STRUCTURED_OUTPUT_INVALID"
    assert item["structured_output"]["status"] == "FAIL" and item["structured_output"]["repair_attempt_count"] == 0
    assert len(calls) == 1


def test_fixed_tool_roundtrip_has_two_requests_and_one_logical_request(app, monkeypatch):
    identity, calls = setup(app, monkeypatch), []
    record = run(app, identity, calls, plan(cases=["tools"]))
    value = summarize(app, record)
    assert len(calls) == 2
    assert json.loads(calls[0].content)["parallel_tool_calls"] is False
    assert len({a["logical_request_id"] for a in value["attempts"]}) == 1
    assert len({a["attempt_id"] for a in value["attempts"]}) == 2
    assert value["attempts"][0]["tool_calls"][0]["model_final_response_received"]
    assert value["connection_checks"]["tool_calling"] == "PASS"
    assert value["totals"]["retry_count"] == 0


@pytest.mark.parametrize("fault,cap,code", [("bad_tool", ".10", "TOOL_PROBE_FAILED"), ("cap_between", ".006", "BUDGET_BLOCKED")])
def test_tool_rejects_permissions_or_second_request_budget(app, monkeypatch, fault, cap, code):
    identity, calls = setup(app, monkeypatch), []
    record = run(app, identity, calls, plan(cases=["tools"], live_test_budget_cap=cap), fault)
    assert record["stop_reason"] == code and len(calls) == 1
    assert summarize(app, record)["connection_checks"]["tool_calling"] == "FAIL"
    assert Decimal(record["ledger"]["spent"]) <= Decimal(cap)


def test_same_key_replays_and_restart_gets_new_session(app, monkeypatch):
    identity, calls = setup(app, monkeypatch), []
    original = run(app, identity, calls, plan(cases=["text"]))
    replay = run(app, identity, calls, plan(cases=["text"]))
    assert replay["replayed"] and len(calls) == 1
    with pytest.raises(ControlError, match="IDEMPOTENCY_CONFLICT"):
        run(app, identity, calls, plan(cases=["text"], live_test_budget_cap=".09"))
    again = run(app, identity, calls, plan(cases=["text"], idempotency_key="new-live-record-key"))
    assert original["api_test_session_id"] != again["api_test_session_id"] and len(calls) == 2


def test_manifest_tamper_blocks_replay_and_export(app, monkeypatch):
    identity, calls = setup(app, monkeypatch), []
    record = run(app, identity, calls, plan(cases=["text"]))
    record["manifest"]["cases"][0]["expected_outcome"] = "ANY_RESULT"
    app.store.put("live_api_session", record["api_test_session_id"], record, revision(app.store, "live_api_session", record["api_test_session_id"]))
    with pytest.raises(ControlError, match="TRACE_CORRUPTED"):
        run(app, identity, calls, plan(cases=["text"]))
    with pytest.raises(ControlError, match="TRACE_CORRUPTED"):
        export_session(app, record["api_test_session_id"])
    assert len(calls) == 1


def test_export_has_required_sections_hashes_unknowns_and_no_secret(app, monkeypatch):
    identity, calls = setup(app, monkeypatch), []
    record = run(app, identity, calls, plan(cases=["text"]), "hidden")
    package = export_session(app, record["api_test_session_id"])
    root = Path(package["relative_path"])
    assert verify_package(root)["verified_files"] == 9
    report = (root / "report_ko.md").read_text(encoding="utf-8")
    assert report.count("\n## ") == 16 and "UNKNOWN" in report
    joined = "".join(p.read_text(encoding="utf-8") for p in root.iterdir())
    assert "미공개 추론 원문" not in joined and "secret-provider-canary" not in joined
    assert scan(app, root)["exposure_count"] == 0
    with (root / "attempts.jsonl").open("a", encoding="utf-8") as f: f.write("{}\n")
    with pytest.raises(ControlError, match="EXPORT_TAMPER"): verify_package(root)


def test_redaction_positive_canary_is_detected_without_printing_value(app, monkeypatch):
    identity = setup(app, monkeypatch)
    root = app.workspace / "scan"; root.mkdir()
    canary = "isolated-canary-for-live-scan"
    path = root / "canary.md"
    text = "노출 양성 대조: " + canary
    text.encode("utf-8", errors="strict"); path.write_text(text, encoding="utf-8")
    result = scan(app, root, (canary,))
    assert result["exposure_count"] == 1 and result["status"] == "SECRET_EXPOSURE"
    assert canary not in json.dumps(result)
    assert clean(app, {"nested": {"url": "https://user:secret-provider-canary-123456789@example.test"}})["nested"]["url"].find("secret-provider-canary") == -1


def test_missing_config_offline_pack_is_honest(app):
    calls = []
    record = run(app, "NOT_CONFIGURED", calls, plan(live=False, consent=False))
    package = export_session(app, record["api_test_session_id"])
    value = summarize(app, record)
    assert record["stop_reason"] == "CONFIG_MISSING" and not calls
    assert value["totals"]["live_generation_request_count"] == 0
    assert value["totals"]["usage"]["input_tokens"]["value"] is None
    assert value["connection_checks"]["text_inference"] == "NOT_TESTED"
    assert Path(package["relative_path"]).exists()


def test_free_ui_routes_preserve_scientific_state_and_never_dispatch(app, monkeypatch):
    identity = setup(app, monkeypatch)
    response = app.request("POST", "/api/control/models/" + identity + "/live-test", plan(live=False, cases=["text"]))
    assert response.status == 200 and response.body["status"] == "OFFLINE_PREFLIGHT_PASS"
    sid = response.body["api_test_session_id"]
    assert app.request("GET", "/api/control/live-api-tests").status == 200
    assert app.request("GET", "/api/control/live-api-tests/" + sid).body["totals"]["live_generation_request_count"] == 0
    assert not app.store.ledger()["requests"]
    assert app.store.db.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0


def test_gateway_reservation_observer_failure_releases_untransmitted_reserve(app, monkeypatch):
    from probe.control_runtime import RoutedGateway
    from probe.providers.normalized import GenerationRequest
    c,p,r,snapshot = setup_app(app, "openai", monkeypatch)
    calls = []
    def observe(phase, _):
        if phase == "reserved": raise ControlError("TRACE_CORRUPTED")
    gateway = RoutedGateway(app.store, app.credentials, "observer-test", snapshot, purpose="settings_smoke",
                            client_factory=factory(calls), observer=observe)
    with pytest.raises(ControlError, match="TRACE_CORRUPTED"): asyncio.run(gateway.generate(r))
    assert not calls and app.store.ledger("observer-test")["requests"][0]["status"] == "RELEASED"


def test_gateway_prepared_trace_exists_before_network(app, monkeypatch):
    identity, calls = setup(app, monkeypatch), []
    base = factory(calls)
    def check(c,p):
        rows = app.store.db.execute("SELECT payload FROM control_audit WHERE kind='LIVE_API_ATTEMPT' ORDER BY seq DESC").fetchall()
        value = json.loads(rows[0][0])
        assert value["status"] == "DISPATCHED" and value["reservation_id"]
        assert value["effective_parameters"]["serialized_payload_sha256"]
        return base(c,p)
    record = asyncio.run(run_session(app, identity, plan(cases=["text"]), client_factory=check))
    assert record["status"] == "OFFLINE_SIMULATION_PASS"


def test_process_crash_recovery_never_reissues_http(app, monkeypatch):
    identity = setup(app, monkeypatch)
    program = """import asyncio,os
from pathlib import Path
from probe.workbench import WorkbenchAPI
from probe.live_api_test import run_session
app=WorkbenchAPI(os.environ['QA_DB'],os.environ['QA_WS'],launch=False,credential_file=Path(os.environ['QA_FILE']))
def crash(*_): os._exit(71)
asyncio.run(run_session(app,'m',{'idempotency_key':'fresh-process-live-crash','live_test_budget_cap':'.10','live':True,'consent':True,'cases':['text']},client_factory=crash))
"""
    import os
    proc = subprocess.run([sys.executable, "-c", program], env={**os.environ, "QA_DB": str(app.database),
        "QA_WS": str(app.workspace), "QA_FILE": str(app.credentials.file)}, timeout=30, capture_output=True)
    assert proc.returncode == 71
    recover_sessions(app)
    record = app.store.configs("live_api_session")[0]
    assert record["status"] == "TEST_SESSION_STOPPED" and record["stop_reason"] == "PROCESS_INTERRUPTED"
    row = app.store.ledger(record["api_test_session_id"])["requests"][0]
    assert row["status"] == "UNRESOLVED"
    assert attempts(app, record["api_test_session_id"])[0]["status"] == "DISPATCHED_UNRESOLVED"
    calls = []
    replay = run(app, identity, calls, plan(cases=["text"], idempotency_key="fresh-process-live-crash"))
    assert replay["replayed"] and not calls



@pytest.mark.parametrize("interrupted", [False, True])
def test_selected_stream_records_clean_completion_or_unresolved(app, monkeypatch, interrupted):
    identity, calls = setup(app, monkeypatch), []
    def respond(req):
        calls.append(req)
        raw = document("responses"); raw["model"] = "manual-id"
        parts = [{"type": "response.output_text.delta", "delta": "PROBE_OK"}]
        if not interrupted: parts.append({"type": "response.completed", "response": raw})
        text = "\n\n".join("data: " + json.dumps(p) for p in parts) + "\n\n"
        return httpx.Response(200, content=text.encode("utf-8"), headers={"Content-Type": "text/event-stream"})
    record = asyncio.run(run_session(app, identity, plan(cases=["stream"]),
        client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(respond))))
    item = attempts(app, record["api_test_session_id"])[0]
    assert len(calls) == 1
    if interrupted:
        assert record["stop_reason"] == "INCOMPLETE_RESPONSE" and item["status"] == "DISPATCHED_UNRESOLVED"
        assert item["stream"]["status"] == "INCOMPLETE"
        assert summarize(app, record)["totals"]["estimated_total_cost_usd"] == "UNKNOWN"
    else:
        assert item["stream"]["clean_completion"] and item["stream"]["event_count"] >= 2
        assert item["stream"]["first_event_at"] == "UNKNOWN"


def test_app_monthly_budget_blocks_before_generation(app, monkeypatch):
    identity, calls = setup(app, monkeypatch), []
    defaults = app.store.defaults()
    defaults.monthly_limit_usd = Decimal(".000001")
    app.store.put("defaults", "global", defaults)
    record = run(app, identity, calls, plan(cases=["text"]))
    assert record["stop_reason"] == "BUDGET_BLOCKED" and not calls
    item = attempts(app, record["api_test_session_id"])[0]
    assert item["budget_before"]["decision"] == "BLOCK"


def test_configuration_change_marks_historical_evidence_stale(app, monkeypatch):
    identity, calls = setup(app, monkeypatch), []
    record = run(app, identity, calls, plan(cases=["text"]))
    assert summarize(app, record)["current_config"]
    profile = app.store.config("model", identity); profile["output_limit"] = 256
    app.store.put("model", identity, profile, revision(app.store, "model", identity))
    historical = summarize(app, record)
    assert historical["connection_evidence_freshness"] == "STALE_OR_UNCONFIGURED"
    assert historical["status"] == "OFFLINE_SIMULATION_PASS"


def test_integration_requires_lower_test_and_records_limited_scope(app, monkeypatch):
    identity, calls = setup(app, monkeypatch), []
    blocked = run(app, identity, calls, plan(cases=["integration"]))
    assert blocked["stop_reason"] == "LOWER_LEVEL_REQUIRED" and not calls
    result = run(app, identity, calls, plan(cases=["text", "integration"], idempotency_key="integration-new-key"))
    assert result["status"] == "OFFLINE_SIMULATION_PASS" and len(calls) == 2
    assert result["research_quality"] == "NOT_VALIDATED"
    assert app.store.db.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0


@pytest.mark.parametrize("include_search_defaults", [False, True])
@pytest.mark.parametrize("include_search_price_defaults", [False, True])
def test_default_fields_preserve_existing_cache_request_identity(app, monkeypatch, include_search_defaults, include_search_price_defaults):
    from probe.control_runtime import RoutedGateway
    from probe.providers.native import compact, REGISTRY
    from probe.providers.normalized import GenerationResult, GenerationUsage
    from hashlib import sha256
    c,p,r,snapshot = setup_app(app, "openai", monkeypatch)
    excluded = {"budget_reservation_id", "native_schema_strict", "explicit_parallel_tool_control"}
    if not include_search_defaults:
        excluded.update({"hosted_tools", "max_tool_calls", "include", "hosted_input_token_bound"})
    old_request = r.model_dump(mode="json", exclude=excluded)
    old_profile = p.model_dump(mode="json")
    if not include_search_price_defaults:
        old_profile.pop("task_output_limits", None)
        old_profile["price"].pop("web_search_per_call", None)
        old_profile["price"].pop("web_search_price_source", None)
    key = sha256(compact({"rid": "legacy-cache", "request": old_request, "profile": old_profile,
                         "adapter_version": REGISTRY.get("openai").version}).encode("utf-8")).hexdigest()
    cached = GenerationResult(model_id="manual-id", output_text="old", usage=GenerationUsage(input_tokens=1, output_tokens=1))
    app.store.db.execute("INSERT INTO control_model_cache VALUES(?,?,?,?,?,?)",
                        (key, "legacy-cache", cached.model_dump_json(), "manual-id", "historic-reservation", None))
    calls = []
    gateway = RoutedGateway(app.store, app.credentials, "legacy-cache", snapshot, purpose="settings_smoke", client_factory=factory(calls))
    result,_,count = asyncio.run(gateway.generate(r))
    assert count == 0 and result.output_text == "old" and not calls


def test_export_stops_on_existing_public_secret_row(app, monkeypatch):
    identity, calls = setup(app, monkeypatch), []
    record = run(app, identity, calls, plan(cases=["text"]))
    app.store.audit(None, "CANARY_POSITIVE_CONTROL", {"value": "secret-provider-canary-123456789"})
    with pytest.raises(ControlError, match="SECRET_EXPOSURE"):
        export_session(app, record["api_test_session_id"])
    assert app.store.config("live_api_session", record["api_test_session_id"])["stop_reason"] == "SECRET_EXPOSURE"



def test_local_auth_canary_in_provider_header_stops_and_keeps_failure_denominator(app, monkeypatch):
    from probe.local_auth import MemorySecrets
    identity = setup(app, monkeypatch)
    protected = MemorySecrets()
    canary = "local-auth-header-" + uuid4().hex
    protected.remember(canary)
    calls = []
    def response(req):
        calls.append(req)
        raw = document("responses"); raw["model"] = "manual-id"
        return httpx.Response(200, json=raw, headers={"x-request-id": canary})
    record = asyncio.run(run_session(app, identity, plan(cases=["text"]),
        client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(response))))
    result = summarize(app, record)
    assert record["status"] == "TEST_SESSION_STOPPED" and record["stop_reason"] == "SECRET_IN_TEST_RECORD"
    assert result["totals"]["ambiguous_generation_requests"] == 1
    assert canary not in json.dumps(result)
    assert len(calls) == 1
