"""선택적 API 검사의 증거를 기존 설정·감사·비용 원장에 기록한다."""
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import re
import time
from typing import Literal
from uuid import uuid4

from pydantic import Field

from .control_plane import Connection, ControlError, ModelProfile, admitted_cost, money, process_alive
from .productization import credential_version, revision
from .providers.base import ModelProviderError
from .providers.native import REGISTRY, compact
from .providers.normalized import GenerationRequest, GenerationMessage, NormalizedTool, NormalizedToolResult
from .schemas import StrictModel, utc_now

VERSION = "1.0.0"
CASES = {"discovery": "L1_MODEL_DISCOVERY", "text": "L2_LIVE_TEXT_SMOKE",
         "structured": "L3_LIVE_STRUCTURED_OUTPUT_SMOKE", "tools": "L4_LIVE_TOOL_CALL_SMOKE",
         "integration": "L5_LIVE_HTRSA_INTEGRATION_SMOKE", "stream": "LIVE_STREAM_SMOKE"}
CAPS = {"model_discovery": None, "text": "text", "structured_output": "structured_output",
        "json_schema_output": "structured_output", "tool_calling": "tool_calling",
        "parallel_tool_calls": "parallel_tools", "streaming": "streaming",
        "usage_reporting": "usage_reporting", "reasoning_control": "reasoning"}
TOKENS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
STOP = ["BUDGET_BLOCKED", "SECRET_EXPOSURE", "DISPATCHED_UNRESOLVED", "MODEL_MISMATCH",
        "CAPABILITY_MISMATCH", "TRACE_CORRUPTED", "COST_BOUND_EXCEEDED"]
TEXT = "검사 전용 문장입니다. HTRSA_OK만 응답하세요."


class ProbeOutput(StrictModel):
    status: Literal["ok"]


class LiveTestPlan(StrictModel):
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    live_test_budget_cap: Decimal = Field(gt=0, le=Decimal("0.25"), decimal_places=6, allow_inf_nan=False)
    live: bool = False
    consent: bool = False
    cases: list[Literal["discovery", "text", "structured", "tools", "stream", "integration"]] = Field(default_factory=lambda: ["discovery", "text"], min_length=1, max_length=6)
    max_output_tokens: int = Field(default=128, ge=32, le=256)
    reasoning_level: Literal["AUTO", "DISABLED", "LOW", "MEDIUM", "HIGH", "EXTRA_HIGH", "MAX"] | None = None
    research_context_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,100}$")


def digest(value):
    data = value if isinstance(value, bytes) else compact(value).encode("utf-8", errors="strict")
    return sha256(data).hexdigest()


def clean(app, value):
    from .workbench import redact
    refs = [c.get("credential_env_name") for c in app.store.configs("connection")]
    protected = app.credentials.active_secrets(refs)
    def walk(item):
        if isinstance(item, str):
            item = redact(item)
            for secret in protected:
                item = item.replace(secret, "[비밀 제거됨]")
            return item
        if isinstance(item, dict):
            return {walk(str(k)): walk(v) for k, v in item.items() if str(k).lower() not in {"api_key", "authorization", "password", "secret_value"}}
        if isinstance(item, (tuple, list)):
            return [walk(v) for v in item]
        return item
    return walk(value)


def save_record(app, record):
    public = clean(app, record)
    public_text = compact(public)
    public_text.encode("utf-8", errors="strict")
    if public != record:
        raise ControlError("SECRET_IN_TEST_RECORD")
    app.store.put("live_api_session", record["api_test_session_id"], public,
                  revision(app.store, "live_api_session", record["api_test_session_id"]))


def verify_manifest(app, record):
    original = app.store.db.execute("SELECT payload FROM control_audit WHERE research_id=? AND kind='LIVE_API_MANIFEST' ORDER BY seq LIMIT 1",
                                    (record["api_test_session_id"],)).fetchone()
    if not original or json.loads(original[0])["manifest_sha256"] != digest(record["manifest"]) or record["manifest_sha256"] != digest(record["manifest"]):
        raise ControlError("TRACE_CORRUPTED")


def attempts(app, session_id):
    rows = app.store.db.execute("SELECT payload FROM control_audit WHERE research_id=? AND kind='LIVE_API_ATTEMPT' ORDER BY seq", (session_id,))
    latest = {}
    for row in rows:
        item = json.loads(row[0])
        latest[item["attempt_id"]] = item
    return list(latest.values())


def write_attempt(app, sid, item):
    public = clean(app, item)
    if public != item:
        public.update(status="DISPATCHED_UNRESOLVED" if "dispatched_at" in item else "BLOCKED",
                      error={"category": "SECRET_IN_TEST_RECORD", "retryable": False})
        app.store.audit(sid, "LIVE_API_ATTEMPT", public)
        raise ControlError("SECRET_IN_TEST_RECORD")
    compact(public).encode("utf-8", errors="strict")
    app.store.audit(sid, "LIVE_API_ATTEMPT", public)


def budget(app, sid, cap, *, bound=None, decision="UNKNOWN"):
    ledger = app.store.ledger(sid)
    defaults = app.store.defaults()
    return {"budget_scope_id": sid, "hard_limit_usd": str(cap), "spent_usd": ledger["spent"],
            "active_reservations_usd": ledger["reserved"], "unsettled_exposure_usd": ledger["unresolved"],
            "new_reservation_usd": bound, "decision": decision,
            "app_monthly_limit_usd": str(defaults.monthly_limit_usd),
            "app_request_limit_usd": str(defaults.request_limit_usd),
            "app_monthly_exposure_usd": ledger["monthly_exposure"], "authoritative_gateway": "RoutedGateway/ControlStore.reserve"}


def runtime_snapshot(app, plan):
    if plan.research_context_id:
        from .product_policy import effective_snapshot
        raw = effective_snapshot(app.store, plan.research_context_id)
        return {"source": "SAVED_RESEARCH_CONTEXT", "research_id": plan.research_context_id,
                "performance_profile": raw.get("performance_profile", "CUSTOM"),
                "adaptive_budget": raw.get("adaptive_budget", False),
                "skills_enabled": raw.get("verified_analysis_skills", False),
                "f3p_enabled": raw.get("verification_repair", False),
                "ridge_arithmetic_enabled": raw.get("ridge_arithmetic_check", False),
                "research_budget_cap": raw.get("run_limit_usd"), "time_limit_sec": raw.get("max_elapsed_sec"),
                "scope": "저장 연구 설정의 읽기 전용 참조; 검사에서 연구 기능을 실행하지 않음"}
    return {"source": "FIXED_SAFE_SMOKE", "performance_profile": "CUSTOM", "adaptive_budget": False,
            "skills_enabled": False, "f3p_enabled": False, "ridge_arithmetic_enabled": False,
            "research_budget_cap": "NOT_APPLICABLE", "time_limit_sec": 30,
            "scope": "고정 검사 입력 · 연구 생성/정본 반영 없음"}


def identity(profile, connection, adapter):
    return {"provider": connection.adapter_id, "adapter_id": adapter.adapter_id, "adapter_version": adapter.version,
            "protocol": adapter.protocol(profile, connection), "endpoint_class": connection.endpoint_class,
            "base_url_identity": connection.base_url, "connection_id": connection.connection_id,
            "model_profile_id": profile.profile_id, "routing_profile_id_if_used": "NOT_APPLICABLE",
            "requested_model_id": profile.model_id, "requested_alias": profile.model_alias,
            "resolved_model_id": "UNKNOWN", "provider_reported_model": "UNKNOWN",
            "provider_model_revision_if_available": "UNKNOWN"}


def make_request(plan, profile, sid, mode, aid):
    request = GenerationRequest(request_id=aid, research_id=sid, role="manager", model_profile_id=profile.profile_id,
        system_instructions="아래 고정 검사만 수행하세요. 연구 도구를 사용하지 마세요.",
        input_text=TEXT, max_output_tokens=profile.output_limit, timeout=min(30, profile.timeout_sec),
        reasoning_policy=plan.reasoning_level or profile.reasoning_policy, temperature=profile.temperature,
        top_p=profile.top_p, stream=mode == "stream", store_preference=False, data_egress_policy="selected",
        probe_capability="text", metadata={"api_test_session_id": sid})
    if mode in {"structured", "integration"}:
        request.structured_output_schema = ProbeOutput.model_json_schema()
        request.native_schema_strict = True
        request.input_text = 'JSON으로 {"status":"ok"}만 응답하세요.'
        request.probe_capability = "structured_output"
    if mode == "tools":
        request.tools = [NormalizedTool(tool_name="get_test_value", description="검사 전용 고정 값; 외부 접근 없음",
            parameters={"type": "object", "properties": {"key": {"type": "string", "enum": ["probe"]}},
                        "required": ["key"], "additionalProperties": False})]
        request.tool_choice = "required"
        request.explicit_parallel_tool_control = True
        request.input_text = "get_test_value를 key=probe로 한 번 호출하고 결과 value를 그대로 응답하세요."
        request.probe_capability = "tool_calling"
    return request


def preflight(app, profile_id, plan, sid):
    checks, context, selected = [], None, None
    try:
        if len(set(plan.cases)) != len(plan.cases): raise ControlError("DUPLICATE_TEST_CASE")
        if "integration" in plan.cases and "text" not in plan.cases: raise ControlError("LOWER_LEVEL_REQUIRED")
        profile = ModelProfile.model_validate(app.store.config("model", profile_id))
        connection = Connection.model_validate(app.store.config("connection", profile.connection_id))
        adapter = REGISTRY.get(connection.adapter_id)
        context = identity(profile, connection, adapter)
        meta = app.credentials.metadata(connection.credential_env_name)
        context["credential_metadata"] = meta
        context["credential_version"] = credential_version(app, connection)
        context["profile_revision"] = revision(app.store, "model", profile_id)
        context["connection_revision"] = revision(app.store, "connection", connection.connection_id)
        checks.append({"check": "PROFILE_PROTOCOL", "status": "PASS"})
        if not connection.enabled or not connection.destination_approved: raise ControlError("EGRESS_BLOCKED")
        if "discovery" in plan.cases and not connection.discovery_unmetered:
            raise ControlError("DISCOVERY_COST_NOT_BOUNDED_MANUAL_ID_ALLOWED")
        profile = profile.model_copy(update={"output_limit": min(plan.max_output_tokens, profile.output_limit), "stream": False, "store_preference": False})
        runtime = runtime_snapshot(app, plan)
        for mode in plan.cases:
            if mode == "discovery": continue
            cap = {"structured": "structured_output", "integration": "structured_output", "tools": "tool_calling", "stream": "streaming"}.get(mode)
            if cap and (not profile.capabilities.get(cap) or profile.capabilities[cap].status != "SUPPORTED"):
                raise ControlError("CAPABILITY_NOT_VALIDATED")
            req = make_request(plan, profile, sid, mode, "preflight")
            _, payload, _ = adapter.serialize(req, profile, connection)
            local_unmetered = profile.local_api_unmetered and connection.endpoint_class == "loopback"
            bound = admitted_cost(profile, len(compact(payload).encode("utf-8", errors="strict")) + 4096) if not local_unmetered else Decimal(0)
            if bound > plan.live_test_budget_cap: raise ControlError("BUDGET_BLOCKED")
            if bound > app.store.defaults().request_limit_usd: raise ControlError("REQUEST_BUDGET_BLOCKED")
            if any(s in compact(payload) for s in app.credentials.active_secrets([connection.credential_env_name])):
                raise ControlError("SECRET_IN_MODEL_INPUT")
            checks.append({"check": mode + "_SERIALIZATION_PRICE_BOUND", "status": "PASS", "estimated_max_usd": str(bound)})
        if connection.endpoint_class == "cloud" and not meta["configured"]: raise ControlError("CREDENTIAL_UNCONFIGURED")
        checks.append({"check": "OFFLINE_PREFLIGHT", "status": "PASS"})
        selected = (profile, connection, adapter, runtime)
        return checks, context, selected, None
    except ModelProviderError as exc:
        checks.append({"check": "OFFLINE_PREFLIGHT", "status": "BLOCK", "error_code": exc.code})
        return checks, context, selected, exc.code


def new_attempt(app, record, case, number=1, logical=None):
    sid = record["api_test_session_id"]
    item = {"api_test_session_id": sid, "logical_request_id": logical or str(uuid4()),
        "attempt_id": str(uuid4()), "attempt_number": number, "case_id": CASES[case], "mode": case,
        "status": "STARTED", "started_at": utc_now().isoformat(), "completed_at": None,
        "retry_reason": "NOT_APPLICABLE", "retry_policy": "NO_AUTOMATIC_RETRY", "retry_authorized": False,
        "execution": record["execution"], "identity": record["identity"],
        "runtime_snapshot": {**record.get("runtime_snapshot", {}), "smoke_features": {"performance_profile": "CUSTOM", "adaptive_budget": False, "skills_enabled": False, "f3p_enabled": False, "ridge_arithmetic_enabled": False}}, "budget_before": budget(app, sid, record["live_test_budget_cap"]),
        "usage": {k: {"value": None, "source": "UNKNOWN"} for k in TOKENS},
        "latency": {k: "UNKNOWN" for k in ["request_latency_ms", "queue_ms", "connection_ms", "first_token_ms", "generation_ms", "tool_wait_ms"]},
        "structured_output": {"status": "NOT_TESTED", "repair_attempt_count": 0},
        "tool_calls": [], "stream": {"status": "NOT_TESTED"},
        "cost": {"status": "UNKNOWN", "currency": "USD", "observed_cost_usd": None},
        "requested_parameters": {}, "effective_parameters": {}, "error": None}
    write_attempt(app, sid, item)
    return item


def record_response(item, result):
    item["response"] = {"response_id": result.response_id, "provider_request_id": result.provider_request_id,
        "status": result.status, "finish_reason": result.finish_reason, "output_sha256": digest(result.output_text),
        "output_character_count": len(result.output_text), "tool_call_count": len(result.tool_calls),
        "refusal_state": result.refusal or "NONE", "raw_response_ref": result.raw_response_ref,
        "hidden_reasoning_stored": False, "private_continuity_present": result._provider_content is not None}
    item["identity"] = {**item["identity"], "resolved_model_id": result.model_id,
        "provider_reported_model": result.provider_metadata.get("provider_reported_model", "UNKNOWN"),
        "provider_model_revision_if_available": result.model_revision or "UNKNOWN",
        "provider_identity_source": result.provider_metadata.get("provider_identity_source", "UNKNOWN")}
    item["usage"] = {k: {"value": getattr(result.usage, k), "source": result.provider_metadata.get("usage_sources", {}).get(k, "UNKNOWN")} for k in TOKENS}
    item["usage_raw_ref"] = result.usage.provider_usage_raw_ref
    item["reasoning"] = {"requested_effort": result.reasoning_metadata.get("requested_reasoning"),
        "reported_reasoning_tokens": result.usage.reasoning_tokens, "tokens_included_in_output": True,
        "protocol_state_present": result._provider_content is not None, "hidden_reasoning_stored": False}
    item["stream"] = {"enabled": item["requested_parameters"].get("stream", False),
        "status": "COMPLETED" if result.status == "COMPLETED" else "INCOMPLETE",
        "event_count": len(result.provider_metadata.get("stream_events", [])),
        "first_event_at": "UNKNOWN", "final_event_at": "UNKNOWN",
        "usage_event_observed": any(e["event"] == "usage" for e in result.provider_metadata.get("stream_events", [])),
        "clean_completion": result.status == "COMPLETED", "delivery": result.provider_metadata.get("stream_delivery", "NOT_APPLICABLE")} if item["requested_parameters"].get("stream") else {"status": "NOT_TESTED"}


async def generation(app, record, profile, connection, plan, mode, *, client_factory=None, request=None, number=1, logical=None):
    sid = record["api_test_session_id"]
    verify_manifest(app, record)
    item = new_attempt(app, record, mode, number, logical)
    request = request or make_request(plan, profile, sid, mode, item["attempt_id"])
    request = request.model_copy(update={"request_id": item["attempt_id"]})
    defaults = app.store.defaults()
    snapshot = {"models": {"manager": profile.model_dump(mode="json")}, "connections": {connection.connection_id: connection.model_dump(mode="json")},
        "egress": "selected", "run_limit_usd": str(plan.live_test_budget_cap), **defaults.model_dump(mode="json"),
        "depth_limits": {"attempts": record["manifest"]["maximum_dispatches"]}}
    began = time.perf_counter()
    def observe(phase, data):
        if phase == "prepared":
            payload = data["payload"]
            item["requested_parameters"] = request.model_dump(mode="json", include={"reasoning_policy", "max_output_tokens", "temperature", "top_p",
                "tool_choice", "parallel_tool_calls", "stream", "timeout", "store_preference", "native_schema_strict"})
            effective = {k: v for k, v in payload.items() if k not in {"input", "instructions", "messages", "system", "tools", "contents", "systemInstruction"}}
            item["effective_parameters"] = {"native_parameters": effective, "mapping": data["mapping"], "serialized_payload_sha256": digest(payload),
                "application_timeout_sec": data["profile"].timeout_sec, "provider_applied_settings": "UNKNOWN",
                "omitted_parameters": {k: ("OMITTED_UNSUPPORTED" if profile.capabilities.get(k) and profile.capabilities[k].status == "UNSUPPORTED" else "PROVIDER_DEFAULT")
                                       for k in ("temperature", "top_p", "tool_choice", "parallel_tool_calls") if k not in effective},
                "reasoning_level": data["mapping"]["reasoning"]["effective_reasoning"] or "PROVIDER_DEFAULT"}
            item["input"] = {"template_id": "HTRSA_SAFE_API_PROBE", "template_version": VERSION,
                "prompt_sha256": digest(request.model_dump(mode="json", include={"input_text", "system_instructions", "messages"})),
                "prompt_character_count": len(request.input_text) + len(request.system_instructions),
                "input_token_count_or_estimate": data["input_bound"], "token_count_source": "ESTIMATED_UTF8_BYTES_PLUS_4096",
                "storage_policy": "FULL_SAFE_TEST_TEXT" if not request.messages else "HASH_ONLY",
                "safe_test_text": request.input_text if not request.messages else None}
            item["budget_before"] = budget(app, sid, plan.live_test_budget_cap, bound=data["bound"])
            price = profile.price
            item["cost"].update(pricing_revision=price.revision if price else "local-unmetered",
                pricing_source=price.source if price else "USER_DECLARED_LOCAL", pricing_checked_at=price.checked_at.isoformat() if price else None,
                estimated_max_usd=data["bound"], reserved_usd=None)
        elif phase == "reserved":
            item["reservation_id"] = data["reservation_id"]
            row = app.store.db.execute("SELECT reserved FROM spend_ledger WHERE id=?", (data["reservation_id"],)).fetchone()
            item["budget_before"]["decision"] = "ALLOW"
            item["cost"]["reserved_usd"] = money(row["reserved"])
            item["budget_before"]["new_reservation_usd"] = money(row["reserved"])
        elif phase == "dispatched":
            item["dispatched_at"] = utc_now().isoformat()
            item["status"] = "DISPATCHED"
        elif phase == "response": record_response(item, data["result"])
        elif phase == "settled":
            row = app.store.db.execute("SELECT reserved,settled FROM spend_ledger WHERE id=?", (item["reservation_id"],)).fetchone()
            item["cost"].update(status="ESTIMATED", observed_cost_usd=money(row["settled"]),
                unrounded_app_estimate_usd=data["cost"], provider_invoice="UNKNOWN",
                released_reservation_usd=money(row["reserved"] - row["settled"]))
        elif phase == "failed":
            item["error"] = {"category": data["code"], "http_status": data["http_status"],
                "provider_error_code": data["provider_error_code"], "provider_request_id": data["provider_request_id"],
                "retryable": False, "user_action": "설정·원장·제공사 상태를 확인한 뒤 새 세션을 직접 시작하세요."}
        write_attempt(app, sid, item)
    from .control_runtime import RoutedGateway
    gateway = RoutedGateway(app.store, app.credentials, sid, snapshot, purpose="settings_smoke", client_factory=client_factory, observer=observe)
    result = None
    try:
        result, _, count = await gateway.generate(request)
        if count != 1: raise ControlError("TRACE_CORRUPTED")
        reported = result.provider_metadata.get("provider_reported_model")
        if not reported or reported == "UNKNOWN" or not (reported == profile.model_id or re.fullmatch(re.escape(profile.model_id) + r"-\d{4}-\d{2}-\d{2}", reported)):
            raise ControlError("MODEL_MISMATCH")
        expected = "REQUIRES_TOOL" if mode == "tools" and number == 1 else "COMPLETED"
        if result.status != expected: raise ControlError("REFUSAL" if result.status == "REFUSED" else "INCOMPLETE_RESPONSE")
        if mode in {"structured", "integration"}:
            item["structured_output"] = {"schema_id": "HTRSA_PROBE_STATUS", "schema_version": VERSION,
                "provider_mode_used": result.provider_metadata["provider_mode_used"],
                "provider_schema_enforced": "UNKNOWN", "native_strict_requested": True,
                "application_schema_validated": False, "validation_error": None, "repair_attempt_count": 0}
            try:
                ProbeOutput.model_validate_json(result.output_text)
                item["structured_output"].update(status="PASS", application_schema_validated=True)
            except ValueError:
                item["structured_output"].update(status="FAIL", validation_error="SCHEMA_MISMATCH")
                raise ControlError("STRUCTURED_OUTPUT_INVALID") from None
        elif mode == "tools" and number == 1:
            if len(result.tool_calls) != 1: raise ControlError("TOOL_PROBE_FAILED")
            call = result.tool_calls[0]
            tool = {"tool_schema_id": "HTRSA_FIXED_TEST_VALUE", "tool_schema_version": VERSION,
                "normalized_call_id": call.call_id, "provider_call_id": call.call_id, "name": call.tool_name,
                "argument_sha256": digest(call.arguments), "permission_check": "DENY",
                "contract_check": "FAIL", "result_sha256": None, "started_at": utc_now().isoformat(),
                "completed_at": None, "error": None, "model_final_response_received": False}
            item["tool_calls"] = [tool]
            if call.tool_name != "get_test_value" or call.arguments != {"key": "probe"}:
                tool["error"] = "TOOL_CONTRACT_DENIED"
                raise ControlError("TOOL_PROBE_FAILED")
            tool.update(permission_check="ALLOW_FIXED_TEST_TOOL_ONLY", contract_check="PASS",
                        result_sha256=digest({"value": "ok"}), completed_at=utc_now().isoformat())
        elif not result.output_text.strip(): raise ControlError("PROBE_OUTPUT_INVALID")
        item["status"] = "PASS"
    except ModelProviderError as exc:
        item["status"] = "FAIL" if "dispatched_at" in item else "BLOCKED"
        item["error"] = item["error"] or {"category": exc.code, "http_status": None, "provider_error_code": None,
            "provider_request_id": None, "retryable": False, "user_action": "설정 또는 예산을 확인하세요."}
        if "dispatched_at" not in item: item["budget_before"]["decision"] = "BLOCK"
    finally:
        ledger = app.store.ledger(sid)
        if request.stream and "response" not in item and "dispatched_at" in item:
            item["stream"] = {"enabled": True, "status": "INCOMPLETE", "clean_completion": False,
                              "event_count": "UNKNOWN", "first_event_at": "UNKNOWN", "final_event_at": "UNKNOWN",
                              "usage_event_observed": "UNKNOWN"}
        row = next((r for r in ledger["requests"] if r["id"] == item.get("reservation_id")), None)
        if row and row["status"] == "UNRESOLVED": item["status"] = "DISPATCHED_UNRESOLVED"
        item["cost"]["unsettled_exposure_usd"] = money(row["reserved"]) if row and row["status"] == "UNRESOLVED" else "0.000000"
        item["budget_after"] = budget(app, sid, plan.live_test_budget_cap)
        item["completed_at"] = utc_now().isoformat()
        item["latency"]["request_latency_ms"] = (time.perf_counter() - began) * 1000
        write_attempt(app, sid, item)
    return item, result


async def discovery(app, record, profile, connection, adapter, plan, client_factory):
    sid = record["api_test_session_id"]
    item = new_attempt(app, record, "discovery")
    start = time.perf_counter()
    try:
        defaults = app.store.defaults()
        rsv = app.store.reserve(rid=sid, connection=connection.connection_id, model=profile.model_id, role="settings",
            purpose="model_discovery", bound=0, run_limit=plan.live_test_budget_cap, monthly_limit=defaults.monthly_limit_usd,
            request_limit=defaults.request_limit_usd, attempts=record["manifest"]["maximum_dispatches"], revision="owner-metadata-unmetered")
        item.update(reservation_id=rsv, status="DISPATCHED", dispatched_at=utc_now().isoformat())
        item["budget_before"] = budget(app, sid, plan.live_test_budget_cap, bound="0", decision="ALLOW")
        item["cost"] = {"status": "ESTIMATED", "currency": "USD", "estimated_max_usd": "0", "reserved_usd": "0",
                        "observed_cost_usd": None, "pricing_source": "OWNER_UNMETERED_METADATA_DECLARATION"}
        write_attempt(app, sid, item)
        app.store.transition(rsv, "DISPATCHED")
        models = await adapter.list_models(connection, app.credentials.get(connection.credential_env_name),
            reservation_id=rsv, client_factory=client_factory, secrets=app.credentials.active_secrets([connection.credential_env_name]))
        app.store.transition(rsv, "SETTLED", settled=0)
        selected = next((m for m in models if m.model_id == profile.model_id), None)
        item["status"] = "PASS" if selected else "FAIL"
        item["discovery"] = {"listed_count": len(models), "selected_model_listed": selected is not None,
            "inference_executed": False, "selected_metadata": selected.model_dump(mode="json") if selected else None}
        item["cost"].update(observed_cost_usd="0", released_reservation_usd="0")
        if not selected: item["error"] = {"category": "MODEL_NOT_FOUND"}
    except ModelProviderError as exc:
        item.update(status="DISPATCHED_UNRESOLVED" if "reservation_id" in item else "BLOCKED",
                    error={"category": exc.code, "http_status": getattr(exc, "http_status", None), "retryable": False})
        if "reservation_id" in item: app.store.recover_ledger(sid)
    finally:
        item["completed_at"] = utc_now().isoformat()
        item["latency"]["request_latency_ms"] = (time.perf_counter() - start) * 1000
        item["budget_after"] = budget(app, sid, plan.live_test_budget_cap)
        write_attempt(app, sid, item)
    return item


def connection_checks(record, items):
    result = {"credentials_configured": bool((record.get("identity") or {}).get("credential_metadata", {}).get("configured"))}
    names = {"discovery": "model_discovery", "text": "text_inference", "structured": "structured_output",
             "tools": "tool_calling", "stream": "streaming", "integration": "htrsa_gateway_integration"}
    for mode, name in names.items():
        rows = [a for a in items if a["mode"] == mode]
        result[name] = ("PASS" if all(a["status"] == "PASS" for a in rows) and (mode != "tools" or len(rows) == 2) else "FAIL") if rows else "NOT_TESTED"
    gen = [a for a in items if a["mode"] != "discovery" and "dispatched_at" in a]
    result["usage_reporting"] = "PASS" if gen and all(a["usage"]["input_tokens"]["value"] is not None and a["usage"]["output_tokens"]["value"] is not None for a in gen) else "FAIL" if gen else "NOT_TESTED"
    explicit = [a for a in gen if a["requested_parameters"].get("reasoning_policy") != "AUTO"]
    result["reasoning_control"] = "PASS" if explicit and all(a["status"] == "PASS" for a in explicit) else "FAIL" if explicit else "NOT_TESTED"
    return result


def capabilities(record, items):
    found = {k: {"status": "NOT_TESTED", "source": "UNKNOWN", "checked_at": None} for k in CAPS}
    for a in items:
        if a["mode"] == "discovery" and a["status"] == "PASS":
            found["model_discovery"] = {"status": "SUPPORTED", "source": "LIVE_TEST" if record["execution"] == "REAL_HTTP" else "STATIC_ADAPTER_RULE",
                                       "checked_at": a["completed_at"], "execution": record["execution"]}
            meta = a.get("discovery", {}).get("selected_metadata") or {}
            for name, mapped in CAPS.items():
                if mapped in meta.get("capabilities", {}): found[name] = meta["capabilities"][mapped]
        elif a["status"] == "PASS":
            for name in ({"text": ["text"], "structured": ["structured_output", "json_schema_output"],
                          "tools": ["tool_calling"], "stream": ["streaming"], "integration": ["structured_output"]}.get(a["mode"], [])):
                found[name] = {"status": "SUPPORTED", "source": "LIVE_TEST" if record["execution"] == "REAL_HTTP" else "STATIC_ADAPTER_RULE",
                               "checked_at": a["completed_at"], "execution": record["execution"]}
    checks = connection_checks(record, items)
    for name in ("usage_reporting", "reasoning_control"):
        if checks[name] == "PASS":
            found[name] = {"status": "SUPPORTED", "source": "LIVE_TEST" if record["execution"] == "REAL_HTTP" else "STATIC_ADAPTER_RULE",
                           "checked_at": record["completed_at"], "execution": record["execution"]}
    if checks["tool_calling"] != "PASS" and any(a["mode"] == "tools" for a in items): found["tool_calling"]["status"] = "UNKNOWN"
    return found


async def run_session(app, profile_id, body, *, client_factory=None):
    plan = LiveTestPlan.model_validate(body)
    key = digest({"profile_id": profile_id, "idempotency_key": plan.idempotency_key})
    fingerprint = digest(plan.model_dump(mode="json"))
    try:
        claim = app.store.config("live_api_request", key)
    except ControlError:
        claim = None
    if claim and claim["request_fingerprint"] != fingerprint:
        raise ControlError("IDEMPOTENCY_CONFLICT")
    previous = next((r for r in app.store.configs("live_api_session") if r.get("request_key") == key), None)
    if previous:
        if previous["request_fingerprint"] != fingerprint: raise ControlError("IDEMPOTENCY_CONFLICT")
        verify_manifest(app, previous)
        return {**previous, "replayed": True, "automatic_retry": False}
    if claim:
        raise ControlError("TEST_SETUP_NEEDS_RECONCILIATION")
    sid = str(uuid4())
    started_at, began = utc_now().isoformat(), time.perf_counter()
    from .preflight import _source_fingerprint
    checks, context, selected, block = preflight(app, profile_id, plan, sid)
    preflight_latency_ms = (time.perf_counter() - began) * 1000
    order = [mode for mode in CASES if mode in plan.cases]
    max_dispatches = sum(2 if mode == "tools" else 1 for mode in order)
    case_manifest = [{"case_id": CASES[m], "purpose": m, "provider": (context or {}).get("provider", "UNKNOWN"),
        "model_profile_id": profile_id, "reasoning_level": plan.reasoning_level or (selected[0].reasoning_policy.value if selected else "UNKNOWN"),
        "maximum_output_tokens": selected[0].output_limit if selected else plan.max_output_tokens,
        "structured_output": m in {"structured", "integration"}, "tool_calling": m == "tools",
        "expected_outcome": "PASS_WITH_VALIDATED_SCHEMA" if m in {"structured", "integration"} else "PASS",
        "maximum_attempts": 2 if m == "tools" else 1, "maximum_retries": 0,
        "maximum_cost_bound_usd": str(plan.live_test_budget_cap), "stop_conditions": STOP} for m in order]
    manifest = {"test_plan_version": VERSION, "test_harness_version": VERSION, "api_test_session_id": sid,
        "created_at": utc_now().isoformat(), "live_test_budget_cap": str(plan.live_test_budget_cap),
        "maximum_dispatches": max_dispatches, "maximum_generation_requests": max_dispatches - int("discovery" in order),
        "cases": case_manifest, "automatic_retry": False}
    record = {"api_test_session_id": sid, "request_key": key, "request_fingerprint": fingerprint,
        "started_at": started_at, "completed_at": None, "owner_pid": os.getpid(), "preflight_latency_ms": preflight_latency_ms,
        "app_revision": _source_fingerprint(), "git_commit_or_build_revision": "UNKNOWN_NO_GIT_REPOSITORY",
        "config_revision": {"model": (context or {}).get("profile_revision", 0), "connection": (context or {}).get("connection_revision", 0),
                            "defaults": revision(app.store, "defaults", "global")},
        "platform": platform.platform(), "python_version": platform.python_version(), "manifest": manifest,
        "manifest_sha256": digest(manifest), "identity": context, "preflight": checks,
        "live_test_budget_cap": str(plan.live_test_budget_cap), "requested_plan": plan.model_dump(mode="json"),
        "execution": "MOCK_HTTP" if client_factory else "REAL_HTTP" if plan.live else "OFFLINE_PREFLIGHT",
        "status": "RUNNING", "stop_reason": None, "automatic_retry": False,
        "skill_live_efficacy": "NOT_VALIDATED", "f3p_live_efficacy": "NOT_VALIDATED", "research_quality": "NOT_VALIDATED",
        "project_live_llm_gate": "NOT_VALIDATED_UNLESS_EXISTING_PROJECT_STRUCTURED_SMOKE_EXECUTED"}
    if selected: record["runtime_snapshot"] = selected[3]
    app.store.put("live_api_request", key, {"api_test_session_id": sid, "request_fingerprint": fingerprint})
    # 명세와 상한은 전송 전에 기존 감사 원장에 고정한다.
    app.store.audit(sid, "LIVE_API_MANIFEST", clean(app, {"manifest": manifest, "manifest_sha256": record["manifest_sha256"]}))
    save_record(app, record)
    try:
        if block:
            record.update(status="TEST_SESSION_STOPPED", stop_reason=block)
        elif not plan.live:
            record["status"] = "OFFLINE_PREFLIGHT_PASS"
        elif not plan.consent:
            record.update(status="TEST_SESSION_STOPPED", stop_reason="PAID_TEST_CONSENT_REQUIRED")
        else:
            profile, connection, adapter, _ = selected
            for mode in order:
                verify_manifest(app, record)
                if revision(app.store, "model", profile_id) != record["config_revision"]["model"]:
                    raise ControlError("CONFIG_STALE")
                if credential_version(app, connection) != record["identity"]["credential_version"]:
                    raise ControlError("CREDENTIAL_CHANGED")
                if mode == "discovery":
                    item = await discovery(app, record, profile, connection, adapter, plan, client_factory)
                else:
                    item, result = await generation(app, record, profile, connection, plan, mode, client_factory=client_factory)
                    if mode == "tools" and item["status"] == "PASS":
                        call = result.tool_calls[0]
                        request = make_request(plan, profile, sid, mode, "continuation").model_copy(update={
                            "messages": [GenerationMessage(role="user", text="검사 결과를 반환하세요."), result.assistant_message(),
                                GenerationMessage(role="tool", tool_results=[NormalizedToolResult(call_id=call.call_id, tool_name=call.tool_name, result={"value": "ok"})])],
                            "tool_choice": "none"})
                        final, _ = await generation(app, record, profile, connection, plan, mode, client_factory=client_factory,
                                                   request=request, number=2, logical=item["logical_request_id"])
                        item["tool_calls"][0]["model_final_response_received"] = final["status"] == "PASS"
                        write_attempt(app, sid, item)
                        item = final
                if item["status"] != "PASS":
                    record.update(status="TEST_SESSION_STOPPED", stop_reason=(item.get("error") or {}).get("category", item["status"]))
                    break
            else: record["status"] = "LIVE_SELECTED_TESTS_PASS" if client_factory is None else "OFFLINE_SIMULATION_PASS"
    except ModelProviderError as exc:
        record.update(status="TEST_SESSION_STOPPED", stop_reason=exc.code)
    finally:
        record.update(completed_at=utc_now().isoformat(), session_wall_ms=(time.perf_counter() - began) * 1000,
                      connection_checks=connection_checks(record, attempts(app, sid)), ledger=app.store.ledger(sid))
        save_record(app, record)
    return record


def recover_sessions(app):
    for stored in app.store.configs("live_api_session"):
        if stored["status"] != "RUNNING" or process_alive(stored.get("owner_pid", -1)): continue
        app.store.recover_ledger(stored["api_test_session_id"])
        for item in attempts(app, stored["api_test_session_id"]):
            if item["status"] in {"STARTED", "DISPATCHED"}:
                item.update(status="DISPATCHED_UNRESOLVED" if item.get("reservation_id") else "INTERRUPTED_BEFORE_DISPATCH",
                    completed_at=utc_now().isoformat(), error={"category": "PROCESS_INTERRUPTED"}, automatic_retry=False)
                write_attempt(app, stored["api_test_session_id"], item)
        stored.pop("revision", None)
        stored.update(status="TEST_SESSION_STOPPED", stop_reason="PROCESS_INTERRUPTED",
                      completed_at=utc_now().isoformat(), ledger=app.store.ledger(stored["api_test_session_id"]))
        save_record(app, stored)


def summarize(app, record):
    items = attempts(app, record["api_test_session_id"])
    gen = [a for a in items if a["mode"] != "discovery" and "dispatched_at" in a]
    usage = {}
    for name in TOKENS:
        values = [a["usage"][name]["value"] for a in gen]
        usage[name] = {"value": sum(values) if values and all(v is not None for v in values) else None,
                       "source": "UNKNOWN" if not values or any(v is None for v in values) else "ESTIMATED" if any(a["usage"][name]["source"] == "ESTIMATED" for a in gen) else "PROVIDER_REPORTED"}
    current = False
    ident = record.get("identity")
    if ident:
        try:
            connection = Connection.model_validate(app.store.config("connection", ident["connection_id"]))
            current = (revision(app.store, "model", ident["model_profile_id"]) == record["config_revision"]["model"]
                       and revision(app.store, "connection", ident["connection_id"]) == record["config_revision"]["connection"]
                       and credential_version(app, connection) == ident["credential_version"])
        except ModelProviderError:
            pass
    return {**record, "current_config": current, "connection_evidence_freshness": "CURRENT" if current else "STALE_OR_UNCONFIGURED",
        "attempts": items, "capabilities": capabilities(record, items),
        "connection_checks": connection_checks(record, items),
        "totals": {"live_generation_request_count": len(gen) if record["execution"] == "REAL_HTTP" else 0,
                   "offline_simulated_generation_request_count": len(gen) if record["execution"] == "MOCK_HTTP" else 0,
                   "successful_generation_requests": sum(a["status"] == "PASS" for a in gen),
                   "failed_generation_requests": sum(a["status"] == "FAIL" for a in gen),
                   "ambiguous_generation_requests": sum(a["status"] == "DISPATCHED_UNRESOLVED" for a in gen),
                   "retry_count": 0, "usage": usage,
                   "estimated_total_cost_usd": "UNKNOWN" if Decimal(app.store.ledger(record["api_test_session_id"])["unresolved"]) else app.store.ledger(record["api_test_session_id"])["spent"],
                   "estimated_settled_cost_usd": app.store.ledger(record["api_test_session_id"])["spent"],
                   "unsettled_exposure_usd": app.store.ledger(record["api_test_session_id"])["unresolved"],
                   "provider_metered_total_cost_usd": "UNKNOWN", "session_wall_ms": record.get("session_wall_ms", "UNKNOWN")},
        "gate_policy": "새 검사 결과는 해당 연결·모델·선택 설정에 한정한다. 기존 SDK 구조화 smoke·Docker·Search·출시 게이트를 변경하지 않는다."}


def scan(app, folder, protected=()):
    from .local_auth import contains_auth_material
    refs = [c.get("credential_env_name") for c in app.store.configs("connection")]
    values = tuple(app.credentials.active_secrets(refs)) + tuple(protected)
    texts = [p.read_bytes() for p in folder.rglob("*") if p.is_file() and not p.is_symlink()]
    public_rows = []
    for table in ("control_configs", "control_audit", "control_model_cache"):
        for row in app.store.db.execute("SELECT * FROM " + table):
            public_rows.append(compact(dict(row)).encode("utf-8", errors="strict"))
    hits = sum(any(v and v.encode("utf-8", errors="strict") in data for v in values)
               or contains_auth_material(data) or bool(re.search(rb"sk-[A-Za-z0-9_-]{12,}", data)) for data in texts + public_rows)
    return {"status": "PASS" if hits == 0 else "SECRET_EXPOSURE", "exposure_count": hits, "scanned_files": len(texts),
        "scanned_sqlite_public_rows": len(public_rows), "real_secret_values_printed": False,
        "scope": ["session_package", "SQLite_control_configs", "SQLite_control_audit", "SQLite_control_model_cache"],
        "not_scanned": ["OS_SECRET_STORE_PRIVATE_VALUES", "unrelated_screenshots", "unrelated_diagnostics", "browser_storage"],
        "screenshots": "NOT_CREATED", "raw_response_files": "NOT_CREATED_HASH_ONLY"}


def report_ko(value, redaction):
    def show(v): return "UNKNOWN" if v is None else str(v).replace("|", "\\|").replace("\n", " ")
    def dump(v): return "\n\n~~~json\n" + json.dumps(v, ensure_ascii=False, indent=2) + "\n~~~\n"
    rows = ["| Test | Provider | Model | Reasoning | Result | Input tok | Output tok | Cost USD | Latency ms | Retry |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for a in value["attempts"]:
        rows.append("| " + " | ".join(show(v) for v in [a["case_id"], a["identity"]["provider"],
            a["identity"].get("provider_reported_model", "UNKNOWN"), a["requested_parameters"].get("reasoning_policy", "NOT_TESTED"),
            a["status"], a["usage"]["input_tokens"]["value"], a["usage"]["output_tokens"]["value"],
            a["cost"].get("observed_cost_usd"), a["latency"]["request_latency_ms"], 0]) + " |")
    sections = [
        ("검사 목적", "첫 연결의 선택 기능만 검사한다. Skills·F3-P·연구 품질의 라이브 효능은 NOT_VALIDATED이다."),
        ("환경", dump(value.get("environment", {}))),
        ("제공사와 모델", dump(value["identity"])),
        ("검사 사례", "\n".join(rows) + dump(value["manifest"])),
        ("요청 설정", dump([a["requested_parameters"] for a in value["attempts"]])),
        ("직렬화된 실제 설정", "제공사 적용 결과는 별도 응답 증거가 없으면 UNKNOWN이다." + dump([a["effective_parameters"] for a in value["attempts"]])),
        ("사용량", "미보고 범주는 UNKNOWN이다. reasoning 토큰은 output에 포함하며 총비용에 중복 합산하지 않는다." + dump(value["totals"])),
        ("비용과 예약", "앱 단가 계산은 제공사 청구서가 아니다. 예약 정책은 기존 원장을 재사용한다." + dump(value["ledger"])),
        ("지연 시간", "측정하지 않은 구간은 UNKNOWN이며 성능 향상을 주장하지 않는다." + dump([a["latency"] for a in value["attempts"]])),
        ("구조화 출력", dump([a["structured_output"] for a in value["attempts"]])),
        ("도구 왕복", "검사 전용 get_test_value만 허용한다." + dump([a["tool_calls"] for a in value["attempts"]])),
        ("재시도와 오류", "자동 재시도 0. 불확실한 과금 노출은 유지한다." + dump({"stop_reason": value.get("stop_reason"), "preflight": value["preflight"], "attempt_errors": [a["error"] for a in value["attempts"]]})),
        ("비밀 검사", dump(redaction)),
        ("게이트 변경", value["gate_policy"] + dump(value["connection_checks"])),
        ("미검사 항목", dump({k: v for k, v in value["connection_checks"].items() if v == "NOT_TESTED"})),
        ("다음 검사", "키·모델·공식 단가·전송 승인을 준비한 뒤 0.10 USD 이내의 새 세션을 직접 실행한다. 상위 검사는 별도로 선택한다.")]
    return "# 첫 Live API 검사 기록\n\n세션: " + value["api_test_session_id"] + "\n\n상태: " + value["status"] + "\n\n중단 사유: " + (value.get("stop_reason") or "없음") + "\n\n" + "\n\n".join("## " + str(i) + ". " + title + "\n\n" + text for i, (title, text) in enumerate(sections, 1)) + "\n"


def export_session(app, sid, output_root=None):
    record = app.store.config("live_api_session", sid)
    verify_manifest(app, record)
    from .preflight import environment_status, productization_status, ROOT
    root = Path(output_root) if output_root is not None else app.workspace / "qa" / "live_api_test"
    if root.is_symlink() or any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in [root, *root.parents]):
        raise ControlError("EXPORT_PATH_UNSAFE")
    root = root.resolve()
    if not (root.is_relative_to(ROOT.resolve()) or root.is_relative_to(app.workspace)):
        raise ControlError("EXPORT_PATH_UNSAFE")
    folder = root / sid
    if folder.is_symlink() or getattr(folder, "is_junction", lambda: False)():
        raise ControlError("EXPORT_PATH_UNSAFE")
    if folder.exists():
        verify_package(folder)
        return {"session_id": sid, "relative_path": str(folder), "replayed": True}
    folder.mkdir(parents=True)
    initial_scan = scan(app, folder)
    if initial_scan["exposure_count"]:
        record.update(status="TEST_SESSION_STOPPED", stop_reason="SECRET_EXPOSURE")
        save_record(app, record)
        raise ControlError("SECRET_EXPOSURE")
    value = summarize(app, record)
    env = {"platform": record["platform"], "python_version": record["python_version"],
           "app_revision": record["app_revision"], "credentials": (record.get("identity") or {}).get("credential_metadata", {"configured": False}),
           "project_environment": environment_status(), "product_platform_checks": productization_status(),
           "network_path": "EXECUTED_REAL_HTTP" if record["execution"] == "REAL_HTTP" and value["totals"]["live_generation_request_count"] else "NOT_VALIDATED",
           "docker_isolation": "NOT_VALIDATED_UNLESS_EXISTING_GATE", "browser_auth": "NOT_TESTED_IN_CLI"}
    value["environment"] = env
    documents = {"manifest.json": value["manifest"], "summary.json": {k: v for k, v in value.items() if k != "attempts"},
        "capabilities.json": value["capabilities"], "cost.json": {"ledger": value["ledger"], "totals": value["totals"],
            "attempt_costs": [a["cost"] for a in value["attempts"]]},
        "latency.json": {"session_wall_ms": value["totals"]["session_wall_ms"], "attempt_latency": [a["latency"] for a in value["attempts"]]},
        "environment.json": env}
    def write(name, text):
        text.encode("utf-8", errors="strict")
        if clean(app, text) != text: raise ControlError("SECRET_IN_TEST_RECORD")
        path = folder / name
        if path.is_symlink(): raise ControlError("EXPORT_PATH_UNSAFE")
        path.write_text(text, encoding="utf-8", errors="strict")
    for name, data in documents.items(): write(name, json.dumps(clean(app, data), ensure_ascii=False, indent=2) + "\n")
    write("attempts.jsonl", "".join(compact(clean(app, a)) + "\n" for a in value["attempts"]))
    redaction = scan(app, folder)
    write("redaction_scan.json", json.dumps(redaction, ensure_ascii=False, indent=2) + "\n")
    write("report_ko.md", report_ko(clean(app, value), redaction))
    final_scan = scan(app, folder)
    if final_scan["exposure_count"]:
        record.update(status="TEST_SESSION_STOPPED", stop_reason="SECRET_EXPOSURE")
        save_record(app, record)
        raise ControlError("SECRET_EXPOSURE")
    write("redaction_scan.json", json.dumps(final_scan, ensure_ascii=False, indent=2) + "\n")
    write("artifact_manifest.json", compact({"api_test_session_id": sid, "manifest_sha256": record["manifest_sha256"],
        "files": {p.name: digest(p.read_bytes()) for p in folder.iterdir() if p.is_file()}}) + "\n")
    verify_package(folder)
    return {"session_id": sid, "relative_path": str(folder), "file_count": len(list(folder.iterdir())), "secret_exposures": 0}


def verify_package(folder):
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        raise ControlError("EXPORT_PATH_UNSAFE")
    index = json.loads((folder / "artifact_manifest.json").read_text(encoding="utf-8", errors="strict"))
    expected = set(index["files"]) | {"artifact_manifest.json"}
    if {p.name for p in folder.iterdir()} != expected: raise ControlError("EXPORT_TAMPER")
    for name, hash_value in index["files"].items():
        p = folder / name
        if Path(name).name != name or p.is_symlink() or digest(p.read_bytes()) != hash_value: raise ControlError("EXPORT_TAMPER")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8", errors="strict"))
    if digest(manifest) != index["manifest_sha256"]: raise ControlError("EXPORT_TAMPER")
    return {"status": "PASS", "verified_files": len(index["files"])}


def main():
    parser = argparse.ArgumentParser(description="선택적 API 검사와 비밀 없는 증거 패키지")
    parser.add_argument("database")
    parser.add_argument("workspace")
    parser.add_argument("--profile", default="NOT_CONFIGURED")
    parser.add_argument("--budget-cap", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--consent", action="store_true")
    parser.add_argument("--cases", default="discovery,text")
    parser.add_argument("--reasoning", choices=["AUTO", "DISABLED", "LOW", "MEDIUM", "HIGH", "EXTRA_HIGH", "MAX"])
    parser.add_argument("--output-dir")
    parser.add_argument("--session", help="기존 세션 내보내기만 수행; HTTP 요청 없음")
    args = parser.parse_args()
    from .workbench import WorkbenchAPI
    app = WorkbenchAPI(args.database, args.workspace, launch=False)
    try:
        if args.session:
            record = app.store.config("live_api_session", args.session)
        else:
            record = asyncio.run(run_session(app, args.profile, {"idempotency_key": str(uuid4()), "live_test_budget_cap": args.budget_cap,
                "live": args.live, "consent": args.consent, "cases": args.cases.split(","), "reasoning_level": args.reasoning}))
        package = export_session(app, record["api_test_session_id"], args.output_dir)
        print(compact(clean(app, {"session_id": record["api_test_session_id"], "status": record["status"], "stop_reason": record["stop_reason"],
                                 "totals": summarize(app, record)["totals"], "package": package})))
    finally: app.close()


if __name__ == "__main__":
    main()
