"""제공사 모의 응답으로 프로토콜과 기존 보안·정산 경계를 확인한다."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from htrsa.control_plane import Connection, ControlError, ModelProfile, PriceRecord, RoutingProfile
from htrsa.control_runtime import RoutedGateway
from htrsa.provider_checks import ProbeOutput, check_model
from htrsa.providers.native import DEFINITIONS, REGISTRY, parse_usage, normalized_error
from htrsa.providers.normalized import (CAPABILITIES, CapabilityEvidence, CapabilityStatus as CS,
    GenerationError, GenerationMessage, GenerationRequest, NormalizedTool, NormalizedToolResult,
    ReasoningPolicy as RP, merge_evidence)
from htrsa.providers.streams import decode_stream
from htrsa.schemas import utc_now
from htrsa.workbench import WorkbenchAPI


IDS = list(DEFINITIONS)


def config(identity, **updates):
    conn = Connection(connection_id="c",display_name=identity,adapter_id=identity,
        **({"base_url":"http://127.0.0.1:1234/v1","endpoint_class":"loopback"} if identity == "openai_compatible" else {}),destination_approved=True)
    profile = ModelProfile(profile_id="m",connection_id="c",model_id="manual-id",capability_status="supported",
        capabilities={name:CapabilityEvidence(status=CS.SUPPORTED,source="USER_DECLARED") for name in CAPABILITIES},
        reasoning_levels=list(RP),**updates)
    req = GenerationRequest(request_id="req",research_id="r",role="manager",model_profile_id="m",input_text="안녕",data_egress_policy="selected")
    return conn,profile,req


def document(protocol, *, tools=False, secret=None):
    text = secret or '{"status":"ok"}'
    if protocol == "responses":
        out = [{"type":"function_call","call_id":"call-1","name":"get_test_value","arguments":'{"key":"probe"}'}] if tools else [{"type":"message","content":[{"type":"output_text","text":text}]}]
        return {"id":"response-1","model":"resolved-id","status":"completed","output":out,"usage":{"input_tokens":40,"output_tokens":12,"output_tokens_details":{"reasoning_tokens":4}}}
    if protocol == "messages":
        content = [{"type":"thinking","thinking":"숨겨진 추론","signature":"opaque-signature"},{"type":"tool_use","id":"call-1","name":"get_test_value","input":{"key":"probe"}}] if tools else [{"type":"text","text":text}]
        return {"id":"response-1","model":"resolved-id","content":content,"stop_reason":"tool_use" if tools else "end_turn","usage":{"input_tokens":40,"output_tokens":12}}
    if protocol == "interactions":
        steps = [{"type":"thought","signature":"opaque-signature"},{"type":"function_call","id":"call-1","name":"get_test_value","arguments":{"key":"probe"}}] if tools else [{"type":"model_output","content":[{"type":"text","text":text}]}]
        return {"id":"response-1","model":"resolved-id","status":"requires_action" if tools else "completed","steps":steps,"usage":{"total_input_tokens":40,"total_output_tokens":8,"total_thought_tokens":4,"total_tokens":52}}
    if protocol == "generate_content":
        parts = [{"thoughtSignature":"opaque-signature","functionCall":{"id":"call-1","name":"get_test_value","args":{"key":"probe"}}}] if tools else [{"text":text}]
        return {"responseId":"response-1","modelVersion":"resolved-id","candidates":[{"content":{"parts":parts},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":40,"candidatesTokenCount":8,"thoughtsTokenCount":4,"totalTokenCount":52}}
    message = {"content":None,"reasoning_content":"숨겨진 추론","tool_calls":[{"id":"call-1","type":"function","function":{"name":"get_test_value","arguments":'{"key":"probe"}'}}]} if tools else {"content":text}
    return {"id":"response-1","model":"resolved-id","choices":[{"message":message,"finish_reason":"tool_calls" if tools else "stop"}],"usage":{"prompt_tokens":40,"completion_tokens":12,"completion_tokens_details":{"reasoning_tokens":4}}}


@pytest.mark.parametrize("identity",IDS)
def test_native_request_and_response(identity):
    c,p,r=config(identity)
    adapter=REGISTRY.get(identity)
    url,payload,meta=adapter.serialize(r,p,c)
    assert url.startswith(c.base_url+"/") and meta["adapter"]==identity
    assert "manual-id" in json.dumps(payload)
    assert meta["reasoning"]["effective_reasoning"] is None
    result=adapter.normalize(document(meta["protocol"]),p,meta["protocol"])
    assert result.structured_output=={"status":"ok"}
    assert result.usage.input_tokens==40 and result.usage.output_tokens==12
    assert result.usage.total_tokens==52
    assert result.usage.cached_input_tokens is None


@pytest.mark.parametrize("identity",IDS)
def test_tool_continuity_is_private_and_roundtrips(identity):
    c,p,r=config(identity)
    adapter=REGISTRY.get(identity)
    protocol=adapter.protocol(p)
    result=adapter.normalize(document(protocol,tools=True),p,protocol)
    assert result.status=="REQUIRES_TOOL" and result.tool_calls[0].arguments=={"key":"probe"}
    assert "숨겨진 추론" not in result.model_dump_json() and "opaque-signature" not in result.model_dump_json()
    r.tools=[NormalizedTool(tool_name="get_test_value",parameters={"type":"object"})]
    r.messages=[result.assistant_message(),GenerationMessage(role="tool",tool_results=[NormalizedToolResult(call_id="call-1",tool_name="get_test_value",result={"value":"ok"})])]
    _,payload,_=adapter.serialize(r,p,c)
    assert "call-1" in json.dumps(payload)
    if protocol in {"messages","interactions","chat"}: assert "opaque-signature" in json.dumps(payload) or "숨겨진 추론" in json.dumps(payload,ensure_ascii=False)
    other=REGISTRY.get("anthropic" if identity!="anthropic" else "openai")
    with pytest.raises(GenerationError): other._messages(r,"messages")


@pytest.mark.parametrize("identity",IDS)
@pytest.mark.parametrize("level",list(RP))
def test_reasoning_levels_are_explicit_native_mappings(identity,level):
    c,p,r=config(identity)
    r.reasoning_policy=level
    allowed={"google_gemini":{RP.AUTO,RP.LOW,RP.MEDIUM,RP.HIGH},"deepseek":{RP.AUTO,RP.DISABLED,RP.LOW,RP.HIGH,RP.MAX},"mistral":{RP.AUTO,RP.HIGH},"xai":{RP.AUTO,RP.LOW,RP.MEDIUM,RP.HIGH,RP.EXTRA_HIGH}}.get(identity,set(RP))
    if level not in allowed:
        with pytest.raises(GenerationError,match="UNSUPPORTED_CAPABILITY"): REGISTRY.get(identity).serialize(r,p,c)
    else:
        _,payload,meta=REGISTRY.get(identity).serialize(r,p,c)
        assert meta["reasoning"]["requested_reasoning"]==level.value
        if level!=RP.AUTO: assert meta["reasoning"]["effective_reasoning"]


@pytest.mark.parametrize("identity",IDS)
def test_unknown_capability_blocks_optional_parameters(identity):
    c,p,r=config(identity)
    p.capabilities={}
    r.temperature=0.5
    with pytest.raises(GenerationError,match="UNSUPPORTED_CAPABILITY"): REGISTRY.get(identity).serialize(r,p,c)
    r.temperature=None
    r.reasoning_policy=RP.HIGH
    with pytest.raises(GenerationError): REGISTRY.get(identity).serialize(r,p,c)


@pytest.mark.parametrize("identity",IDS)
def test_structured_native_or_application_validation(identity):
    c,p,r=config(identity)
    r.structured_output_schema=ProbeOutput.model_json_schema()
    adapter=REGISTRY.get(identity)
    if identity=="deepseek":
        with pytest.raises(GenerationError): adapter.serialize(r,p,c)
        p.capabilities.pop("structured_output")
    _,payload,meta=adapter.serialize(r,p,c)
    assert meta["validation_result"]=="NOT_VALIDATED"
    assert meta["provider_mode_used"] in {"NATIVE_JSON_SCHEMA","NATIVE_JSON_MODE"}
    p.capabilities={}
    _,payload,meta=adapter.serialize(r,p,c)
    assert meta["provider_mode_used"]=="APPLICATION_JSON_VALIDATION"
    assert "status" in json.dumps(payload)


@pytest.mark.parametrize("identity",IDS)
def test_usage_missing_unknown_and_malformed_response(identity):
    c,p,r=config(identity)
    adapter=REGISTRY.get(identity)
    result=adapter.normalize(document(adapter.protocol(p)),p,adapter.protocol(p))
    raw=document(adapter.protocol(p)); raw.pop("usage",None)
    assert adapter.normalize(raw,p,adapter.protocol(p)).usage.input_tokens is None
    with pytest.raises(GenerationError,match="MALFORMED_RESPONSE"): adapter.normalize({},p,adapter.protocol(p))
    assert result.raw_response_ref is None


def test_anthropic_cache_usage_and_no_reasoning_double_count():
    u=parse_usage("anthropic","messages",{"usage":{"input_tokens":10,"cache_read_input_tokens":20,"cache_creation_input_tokens":30,"output_tokens":40,"output_tokens_details":{"thinking_tokens":15}}})
    assert (u.input_tokens,u.output_tokens,u.total_tokens,u.reasoning_tokens)==(60,40,100,15)
    u=parse_usage("openai","responses",{"usage":{"input_tokens":True,"output_tokens":10}})
    assert u.input_tokens is None


@pytest.mark.parametrize("status,code",[(400,"INVALID_PARAMETER"),(401,"AUTHENTICATION_ERROR"),(403,"AUTHORIZATION_ERROR"),(404,"MODEL_NOT_FOUND"),(429,"RATE_LIMIT"),(500,"PROVIDER_UNAVAILABLE"),(503,"PROVIDER_UNAVAILABLE")])
def test_errors_do_not_echo_remote_messages(status,code):
    assert normalized_error(status,{"error":{"message":"sk-secret-canary"}})==code


@pytest.mark.parametrize("identity",IDS)
def test_native_origins_fixed_and_fallback_disabled(identity):
    if identity!="openai_compatible":
        with pytest.raises((ValidationError,ControlError)): Connection(connection_id="c",display_name="x",adapter_id=identity,base_url="https://wrong.example/v1")
    with pytest.raises(ValidationError): RoutingProfile(profile_id="r",display_name="x",routing={},fallback_policy="AUTO")
    with pytest.raises(ValidationError): ModelProfile(profile_id="m",connection_id="c",model_id="x",max_retries=1)


def test_failed_live_evidence_cannot_be_replaced_by_assumptions():
    old={"tool_calling":CapabilityEvidence(status=CS.UNSUPPORTED,source="LIVE_CAPABILITY_TEST")}
    incoming={"tool_calling":CapabilityEvidence(status=CS.SUPPORTED,source="PROVIDER_METADATA")}
    assert merge_evidence(old,incoming)["tool_calling"]==old["tool_calling"]


@pytest.fixture
def app(tmp_path):
    instance=WorkbenchAPI(tmp_path/"state.sqlite",tmp_path/"workspace",launch=False,credential_file=tmp_path/"not-created.env")
    yield instance
    instance.close()


def setup_app(app,identity,monkeypatch,**updates):
    c,p,r=config(identity,**updates)
    if c.credential_env_name: monkeypatch.setenv(c.credential_env_name,"secret-provider-canary-123456789")
    if identity=="openai_compatible": p.local_api_unmetered=True
    else: p.price=PriceRecord(input_per_million=1,output_per_million=2,source="모의 가격",checked_at=utc_now(),revision="p1",owner_verified=True)
    if identity=="deepseek": p.capabilities.pop("structured_output")
    app.store.put("connection","c",c); app.store.put("model","m",p)
    snapshot={"models":{"manager":p.model_dump(mode="json")},"connections":{"c":c.model_dump(mode="json")},"egress":"selected", "run_limit_usd":"1", **app.store.defaults().model_dump(mode="json"),"depth_limits":{"attempts":4}}
    return c,p,r,snapshot


@pytest.mark.parametrize("identity",IDS)
def test_gateway_paid_settlement_and_replay(app,monkeypatch,identity):
    c,p,r,s=setup_app(app,identity,monkeypatch)
    requests=[]
    def handler(req):
        requests.append(req)
        return httpx.Response(200,json=document(REGISTRY.get(identity).protocol(p)))
    gateway=RoutedGateway(app.store,app.credentials,"r",s,client_factory=lambda *_:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    first=asyncio.run(gateway.run_structured(role="manager",instructions="응답",input_text="입력",output_type=ProbeOutput,model="manual-id"))
    second=asyncio.run(gateway.run_structured(role="manager",instructions="응답",input_text="입력",output_type=ProbeOutput,model="manual-id"))
    assert first.output.status==second.output.status=="ok" and len(requests)==1 and second.request_count==0
    assert app.store.db.execute("SELECT status FROM spend_ledger").fetchone()[0]=="SETTLED"
    assert "resolved-id" in app.store.db.execute("SELECT payload FROM control_audit ORDER BY seq DESC LIMIT 1").fetchone()[0]


@pytest.mark.parametrize("failure",["timeout","usage","secret","cache-price","truncated"])
def test_ambiguous_dispatch_stays_unresolved(app,monkeypatch,failure):
    c,p,r,s=setup_app(app,"openai",monkeypatch)
    calls=[]
    def handler(req):
        calls.append(req)
        if failure=="timeout": raise httpx.ReadTimeout("비밀은 오류에 기록하지 않음")
        raw=document("responses",secret="secret-provider-canary-123456789" if failure=="secret" else None)
        if failure=="usage": raw.pop("usage")
        if failure=="cache-price": raw["usage"]["input_tokens_details"]={"cached_tokens":20}
        if failure=="truncated": return httpx.Response(200,content=b'{"partial":')
        return httpx.Response(200,json=raw)
    gateway=RoutedGateway(app.store,app.credentials,"r",s,client_factory=lambda *_:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises((ControlError,GenerationError)): asyncio.run(gateway.generate(r))
    assert app.store.db.execute("SELECT status FROM spend_ledger").fetchone()[0]=="UNRESOLVED"
    with pytest.raises(ControlError): asyncio.run(gateway.generate(r))
    assert len(calls)==1
    assert not app.store.db.execute("SELECT * FROM control_model_cache").fetchall()


@pytest.mark.parametrize("identity",IDS)
def test_model_discovery_and_manual_fallback(identity):
    c,p,r=config(identity)
    raw={"name":"models/text-id","supportedGenerationMethods":["generateContent"]} if identity=="google_gemini" else {"id":"text-id"}
    page={"models":[raw,{"name":"models/embed","supportedGenerationMethods":["embedContent"]}]} if identity=="google_gemini" else {"data":[raw]}
    adapter=REGISTRY.get(identity)
    models=asyncio.run(adapter.list_models(c,None,reservation_id="reservation",client_factory=lambda *_:httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=page)))))
    assert len(models)==1
    assert models[0].model_id.endswith("text-id")
    assert "tool_calling" not in models[0].capabilities


def test_pagination_and_missing_cursor():
    c,p,r=config("anthropic")
    calls=[]
    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200,json={"data":[{"id":"one" if len(calls)==1 else "two"}],"has_more":len(calls)==1,"last_id":"one"})
    models=asyncio.run(REGISTRY.get("anthropic").list_models(c,None,reservation_id="r",client_factory=lambda *_:httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    assert [m.model_id for m in models]==["one","two"] and "after_id=one" in calls[-1]
    with pytest.raises(GenerationError): asyncio.run(REGISTRY.get("anthropic").list_models(c,None,reservation_id="r",client_factory=lambda *_:httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={"data":[],"has_more":True})))) )


@pytest.mark.parametrize("mode",["text","inference","structured","tools"])
def test_explicit_settings_checks_use_same_ledger(app,monkeypatch,mode):
    c,p,r,s=setup_app(app,"openai_compatible",monkeypatch)
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(200,json=document("chat",tools=mode=="tools" and len(calls)==1))
    factory=lambda *_:httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ControlError): asyncio.run(check_model(app,"m",{"mode":mode},client_factory=factory))
    assert len(calls)==0
    result=asyncio.run(check_model(app,"m",{"mode":mode,"consent":True},client_factory=factory))
    assert result["research_quality"]=="NOT_VALIDATED" and len(calls)==(2 if mode=="tools" else 1)
    assert all(row[0]=="SETTLED" for row in app.store.db.execute("SELECT status FROM spend_ledger"))


@pytest.mark.parametrize("protocol",["responses","messages","chat","generate_content","interactions"])
def test_streaming_text_and_partial_failure(protocol):
    raw=document(protocol)
    if protocol=="responses": parts=[{"type":"response.output_text.delta","delta":"안녕"},{"type":"response.completed","response":raw}]
    elif protocol=="messages": parts=[{"type":"message_start","message":raw},{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}},{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"안녕"}},{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":12}},{"type":"message_stop"}]
    elif protocol=="chat": parts=[{"id":"r","choices":[{"index":0,"delta":{"content":"안녕"}}]},{"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":raw["usage"]}]
    elif protocol=="generate_content": parts=[{"candidates":[{"content":{"parts":[{"text":"안녕"}]},"finishReason":"STOP"}],"usageMetadata":raw["usageMetadata"]}]
    else: parts=[{"event_type":"interaction.created","interaction":{"id":"r"}},{"event_type":"step.start","index":0,"step":{"type":"model_output","content":[]}},{"event_type":"step.delta","index":0,"delta":{"type":"text","text":"안녕"}},{"event_type":"interaction.completed","interaction":{"status":"completed","usage":raw["usage"]}}]
    wire=lambda ps:("\n\n".join("data: "+json.dumps(x,ensure_ascii=False) for x in ps)+"\n\n").encode("utf-8")
    doc,events=decode_stream(wire(parts),protocol)
    assert any(e.event=="text_delta" and e.data["text"]=="안녕" for e in events)
    if len(parts)>1:
        with pytest.raises(GenerationError,match="INCOMPLETE_RESPONSE"): decode_stream(wire(parts[:-1]),protocol)


def test_generic_local_auth_is_not_cloud_key_by_default():
    c,p,r=config("openai_compatible")
    assert c.credential_env_name is None
    assert "Authorization" not in REGISTRY.get(c.adapter_id).headers(c,"cloud-key")


@pytest.mark.parametrize("identity",IDS)
def test_discovery_escaped_secrets_never_saved(identity):
    c,p,r=config(identity)
    secret="secret-canary-unique-value"
    raw={"models":[{"name":"models/"+secret}]} if identity=="google_gemini" else {"data":[{"id":secret}]}
    wire=json.dumps(raw).replace("secret","\\u0073ecret").encode()
    with pytest.raises(GenerationError,match="SECRET_IN_PROVIDER_RESPONSE"):
        asyncio.run(REGISTRY.get(identity).list_models(c,None,reservation_id="r",secrets=[secret],client_factory=lambda *_:httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=wire)))))


@pytest.mark.parametrize("identity",IDS)
def test_dispatch_requires_reservation_and_egress(identity):
    c,p,r=config(identity)
    with pytest.raises(GenerationError,match="BUDGET_RESERVATION_REQUIRED"):
        asyncio.run(REGISTRY.get(identity)._http("GET",c.base_url+"/models",c,None))
    r.data_egress_policy="none"
    with pytest.raises(GenerationError,match="AUTHORIZATION_ERROR"):
        asyncio.run(REGISTRY.get(identity).create_response(r,p,c,None))


def test_model_api_cannot_fabricate_live_capability_evidence(app):
    c,p,r=config("openai_compatible")
    app.store.put("connection","c",c)
    raw=p.model_dump(mode="json")
    raw["capabilities"]={"text":{"status":"SUPPORTED","source":"LIVE_CAPABILITY_TEST"}}
    response = app.request("POST","/api/control/models",{"value":raw})
    assert response.status==409 and response.body["error"]=="CAPABILITY_SOURCE_FORBIDDEN"
    assert not app.store.configs("model")


def test_default_configuration_has_no_extra_live_calls(app):
    settings=app.request("GET","/api/control/settings").body
    assert len(settings["providers"])==7 and settings["routing_profiles"]==[]
    assert not app.store.db.execute("SELECT * FROM spend_ledger").fetchall()
    c,p,r=config("openai")
    assert p.reasoning_policy==RP.AUTO and p.temperature is None and p.max_retries==0


def test_gemini_stable_interactions_format():
    c,p,r=config("google_gemini")
    r.structured_output_schema=ProbeOutput.model_json_schema()
    url,payload,_=REGISTRY.get("google_gemini").serialize(r,p,c)
    assert url.endswith("/v1/interactions")
    assert payload["response_format"]["type"]=="text" and payload["response_format"]["mime_type"]=="application/json"
    p.protocol="generate_content"
    url,payload,_=REGISTRY.get("google_gemini").serialize(r,p,c)
    assert url.endswith(":generateContent") and payload["generationConfig"]["responseMimeType"]=="application/json"


def test_secret_cannot_enter_run_snapshot_or_owner_config(app,monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY","active-secret-canary-123456789")
    result=app.request("POST","/api/control/connections",{"value":{"connection_id":"secret","display_name":"active-secret-canary-123456789"}})
    assert result.status==409 and result.body["error"]=="SECRET_IN_CONFIG"
    assert app.store.configs("connection")==[]
    with pytest.raises(ControlError,match="SECRET_IN_CONFIG"): app.create({"question":"active-secret-canary-123456789"})


def test_present_nonterminal_status_never_means_completed():
    c,p,r=config("openai")
    raw=document("responses");raw["status"]="in_progress"
    assert REGISTRY.get("openai").normalize(raw,p,"responses").status=="INCOMPLETE"


def test_release_export_preserves_nonsecret_provider_trace(tmp_path,monkeypatch):
    from htrsa.demo import run_demo_a
    from htrsa.database import initialize,to_json
    from htrsa.control_plane import ControlStore
    from htrsa.release import export_release, _secret_free
    from htrsa.service import StateService
    from htrsa.storage import Workspace,sha256_file
    db_path=tmp_path/"demo.sqlite";workspace=tmp_path/"workspace"
    manifest=run_demo_a(db_path,workspace);rid=manifest["research_id"]
    db=initialize(db_path)
    try:
        store=ControlStore(db)
        store.db.execute("INSERT INTO control_runs(research_id,title,status,snapshot,created_at) VALUES(?,?,?,?,?)",(rid,"추적 검사","COMPLETED",to_json({"models":{},"adapter_versions":{"manager":"1.0.0"},"fallback_policy":"NONE"}),utc_now().isoformat()))
        store.audit(rid,"NORMALIZED_RESPONSE_SETTLED",{"resolved_model_id":"actual-model","reservation_id":"private-financial-link"})
        output=tmp_path/"release";export_release(StateService(db,Workspace(workspace)),rid,output)
        raw=(output/"model_configuration.json").read_text(encoding="utf-8")
        assert "actual-model" in raw and "private-financial-link" not in raw
        release=json.loads((output/"manifests/release_manifest.json").read_text(encoding="utf-8"))
        item=next(i for i in release["files"] if i["path"]=="model_configuration.json")
        assert sha256_file(output/item["path"])==item["sha256"]
    finally: db.close()
    monkeypatch.setenv("GEMINI_API_KEY","gemini-secret-value-canary")
    assert not _secret_free("a.json",b'{"v":"gemini-secret-value-canary"}')
    assert not _secret_free("a.json",b'{"v":"\\u0067emini-secret-value-canary"}')
