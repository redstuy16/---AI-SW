"""소유자가 직접 요청한 제공사 검사. 연구의 도구 권한은 부여하지 않는다."""
from .control_plane import Connection, ControlError, ModelProfile
from .control_runtime import RoutedGateway
from .providers.native import REGISTRY
from .providers.normalized import (CapabilityEvidence, CapabilityStatus, GenerationMessage,
    GenerationRequest, NormalizedTool, NormalizedToolResult)
from .providers.base import ModelProviderError
from .schemas import StrictModel, new_id, utc_now


class ProbeOutput(StrictModel):
    status: str


async def check_model(app, profile_id, body, *, client_factory=None, scope=None):
    profile = ModelProfile.model_validate(app.store.config("model", profile_id))
    connection = Connection.model_validate(app.store.config("connection", profile.connection_id))
    adapter = REGISTRY.get(connection.adapter_id)
    mode = body.get("mode")
    if mode == "syntax":
        return {"status":"LOCAL_CONFIG_VALID", "model_id":profile.model_id, "inference_executed":False,
                "adapter":adapter.validate_profile(profile, connection)}
    if body.get("consent") is not True: raise ControlError("PAID_TEST_CONSENT_REQUIRED")
    if not connection.enabled or not connection.destination_approved: raise ControlError("EGRESS_BLOCKED")
    key = app.credentials.get(connection.credential_env_name)
    if connection.endpoint_class == "cloud" and connection.auth_strategy != "none" and not key: raise ControlError("CREDENTIAL_UNCONFIGURED")
    references = [c.get("credential_env_name") for c in app.store.configs("connection")]
    protected = app.credentials.active_secrets(references)
    if mode == "discovery":
        if not (connection.discovery_unmetered or connection.endpoint_class == "loopback" and profile.local_api_unmetered):
            raise ControlError("DISCOVERY_COST_NOT_BOUNDED_MANUAL_ID_ALLOWED")
        defaults = app.store.defaults()
        rid = scope["rid"] if scope else new_id("DISCOVERY")
        reservation = app.store.reserve(rid=rid, connection=connection.connection_id, model=profile.model_id,
            role="settings", purpose="model_discovery", bound=0, run_limit=scope["limit"] if scope else defaults.request_limit_usd,
            request_limit=defaults.request_limit_usd, monthly_limit=defaults.monthly_limit_usd, attempts=6 if scope else 1, revision="owner-metadata-unmetered")
        app.store.transition(reservation, "DISPATCHED")
        try:
            models = await adapter.list_models(connection, key, reservation_id=reservation, client_factory=client_factory, secrets=protected)
            app.store.transition(reservation, "SETTLED", settled=0)
            selected = next((m for m in models if m.model_id.removeprefix("models/") == profile.model_id.removeprefix("models/")), None)
            if selected:
                from .providers.normalized import merge_evidence
                profile.capabilities = merge_evidence(profile.capabilities, selected.capabilities)
                profile.reasoning_levels = selected.reasoning_levels or profile.reasoning_levels
                profile.provider_settings.update(selected.provider_settings)
                profile.max_input_tokens = selected.max_input_tokens
                profile.max_output_tokens = selected.max_output_tokens
                revision = next(x["revision"] for x in app.store.configs("model") if x["profile_id"] == profile_id)
                app.store.put("model", profile_id, profile, revision)
            app.store.audit(rid, "MODEL_DISCOVERY_COMPLETED", {"connection_id":connection.connection_id,"count":len(models),"callable_status":"UNKNOWN"})
            previous = next((x["revision"] for x in app.store.configs("catalog_discovery") if x["connection_id"] == connection.connection_id), 0)
            app.store.put("catalog_discovery", connection.connection_id, {"connection_id": connection.connection_id,
                "models": [m.model_dump(mode="json") for m in models], "checked_at": utc_now().isoformat()}, previous)
            return {"status":"DISCOVERY_OK" if models else "DISCOVERY_EMPTY", "model_ids":[m.model_id for m in models],
                "models":[m.model_dump(mode="json") for m in models], "manual_model_id_allowed":True,"research_quality":"NOT_VALIDATED"}
        except BaseException:
            app.store.transition(reservation, "UNRESOLVED")
            raise
    capability = {"inference":"text", "text":"text", "structured":"structured_output", "tools":"tool_calling", "stream":"streaming", "reasoning":"reasoning"}.get(mode)
    if capability is None: raise ControlError("UNKNOWN_CHECK_MODE")
    # 검사별 한도와 실패 기록을 독립적으로 보존한다.
    profile = profile.model_copy(update={"output_limit":min(128, profile.output_limit), "task_output_limits": {}})
    if capability in profile.capabilities and profile.capabilities[capability].status == CapabilityStatus.UNSUPPORTED:
        if body.get("retest") is not True: raise ControlError("CAPABILITY_RETEST_REQUIRED")
        profile.capabilities = dict(profile.capabilities)
        profile.capabilities.pop(capability)
    defaults = app.store.defaults()
    if body.get("budget_cap_usd") is not None and scope is None:
        from decimal import Decimal
        try:
            cap = Decimal(str(body["budget_cap_usd"]))
            if not cap.is_finite() or not 0 < cap <= defaults.request_limit_usd:
                raise ValueError()
        except (ValueError, ArithmeticError):
            raise ControlError("BUDGET_CAP_REQUIRED") from None
        scope = {"rid":new_id("SMOKE"), "limit":str(cap)}
    snapshot = {"models":{"manager":profile.model_dump(mode="json")},"connections":{connection.connection_id:connection.model_dump(mode="json")},
        "egress":"selected","run_limit_usd":scope["limit"] if scope else str(defaults.request_limit_usd), **defaults.model_dump(mode="json"), "depth_limits":{"attempts":6 if scope else 2 if mode == "tools" else 1}}
    gateway = RoutedGateway(app.store, app.credentials, scope["rid"] if scope else new_id("SMOKE"), snapshot, purpose="settings_smoke", client_factory=client_factory)
    request = GenerationRequest(request_id=new_id("PROBE"), research_id=gateway.rid, role="manager", model_profile_id=profile.profile_id,
        system_instructions='JSON 형식으로 {"status":"ok"}를 응답하라.', input_text="짧은 기능 검사", max_output_tokens=profile.output_limit,
        structured_output_schema=ProbeOutput.model_json_schema() if mode in {"inference","structured"} else None,
        probe_capability=capability, stream=mode == "stream", data_egress_policy="selected",
        reasoning_policy=(scope.get("reasoning_policy", profile.reasoning_policy) if scope else profile.reasoning_policy) if mode == "reasoning" else "AUTO")
    if mode == "reasoning" and request.reasoning_policy.value == "AUTO": raise ControlError("EXPLICIT_REASONING_LEVEL_REQUIRED")
    if mode == "tools":
        request.tools = [NormalizedTool(tool_name="get_test_value", description="검사 전용 고정 값 읽기", parameters={"type":"object","properties":{"key":{"type":"string"}},"required":["key"],"additionalProperties":False})]
        request.tool_choice = "required"
    try:
        observed_usage = []
        result, output, _ = await gateway.generate(request, output_type=ProbeOutput if mode in {"inference","structured"} else None)
        observed_usage.append(result.usage.model_dump(mode="json"))
        if mode == "tools":
            if result.status != "REQUIRES_TOOL" or len(result.tool_calls) != 1: raise ControlError("TOOL_PROBE_FAILED")
            call = result.tool_calls[0]
            if call.tool_name != "get_test_value" or set(call.arguments) != {"key"} or call.arguments["key"] != "probe": raise ControlError("TOOL_PROBE_FAILED")
            request = request.model_copy(update={"request_id":new_id("PROBE"), "messages":[GenerationMessage(role="user",text="get_test_value의 key=probe를 호출하라."),result.assistant_message(),
                GenerationMessage(role="tool",tool_results=[NormalizedToolResult(call_id=call.call_id,tool_name=call.tool_name,result={"value":"ok"})])], "tool_choice":"none"})
            result, _, _ = await gateway.generate(request)
            observed_usage.append(result.usage.model_dump(mode="json"))
        if result.status != "COMPLETED": raise ControlError("INCOMPLETE_RESPONSE")
        if mode in {"text", "tools", "reasoning"} and not result.output_text.strip(): raise ControlError("PROBE_OUTPUT_INVALID")
        if output is not None and output.status != "ok": raise ControlError("PROBE_OUTPUT_INVALID")
        status, code = CapabilityStatus.SUPPORTED, None
    except ModelProviderError as exc:
        status, code = CapabilityStatus.UNSUPPORTED if exc.code in {"INVALID_PARAMETER","UNSUPPORTED_CAPABILITY"} else CapabilityStatus.UNKNOWN, exc.code
    stored = ModelProfile.model_validate(app.store.config("model", profile_id))
    stored.capabilities[capability] = CapabilityEvidence(status=status, source="LIVE_CAPABILITY_TEST", checked_at=utc_now().isoformat(), details=code or "명시적으로 요청한 실제 기능 검사 통과")
    if status == CapabilityStatus.SUPPORTED:
        stored.capabilities["usage_reporting"] = CapabilityEvidence(status=status, source="LIVE_CAPABILITY_TEST", checked_at=utc_now().isoformat(), details="원장의 사용량·상한·정산 검사 통과")
    if status == CapabilityStatus.SUPPORTED and mode in {"inference","structured"}:
        stored.capability_status = "supported"
        stored.capability_source = "실제 응답의 Pydantic 검사 통과; 다른 기능은 독립적으로 확인"
        stored.capability_checked_at = utc_now().isoformat()
    revision = next(x["revision"] for x in app.store.configs("model") if x["profile_id"] == profile_id)
    app.store.put("model", profile_id, stored, revision)
    if code: raise ControlError(code)
    return {"status":"LIVE_CAPABILITY_TEST_PASSED","capability":capability,"evidence":stored.capabilities[capability].model_dump(mode="json"),
        "research_quality":"NOT_VALIDATED","ledger_reservations":gateway.dispatch_count,"usage":observed_usage}
