"""자료 없이 모델 하나로 시작하는 실제 서비스 경로를 검사한다."""
import asyncio
import json
import httpx
import pytest
from htrsa.control_plane import ROLES
from htrsa.control_runtime import RoutedGateway, execute
from htrsa.provider_checks import check_model
from test_autonomous_loop import MANAGER
from test_workbench import app, configure


def test_start_01_selected_primary_without_files_or_advanced(app):
    configure(app)
    body = {"question": "공개 자료의 관계를 검토해 주세요.",
            "model_profile_id": "m", "run_limit_usd": "0.10",
            "egress": "research", "search_policy": "AUTO"}
    response = app.request("POST", "/api/control/research/preflight", body)
    assert response.status == 200, response.body
    assert response.body["ready"], response.body
    created = app.request("POST", "/api/control/research", body)
    assert created.status == 201, created.body
    snapshot = created.body["snapshot"]
    assert snapshot["routing"] == {role: "m" for role in ROLES}
    assert snapshot["attachments"] == []
    assert snapshot["source"] is None
    assert snapshot["report_format"] == "pdf"
    rid = created.body["research_id"]
    started = app.request("POST", f"/api/control/research/{rid}/start",
                          {"idempotency_key":"start-one-model", "expected_version":0})
    assert started.status == 200, started.body
    calls = []
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"id":"offline-manager", "model":"manual-id",
            "choices":[{"message":{"role":"assistant","content":json.dumps(MANAGER)},
                        "finish_reason":"stop"}],
            "usage":{"prompt_tokens":10,"completion_tokens":20}})
    def provider(store, research_id, resolved):
        return RoutedGateway(store, app.credentials, research_id, resolved,
            client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=provider))
    assert len(calls) == 1 and calls[0]["model"] == "manual-id"
    assert app.store.run(rid)["status"] == "INSUFFICIENT_DATA"
    assert app.read._state.runtime_step(rid, "manager_decision")["status"] == "COMPLETED"
    assert app.store.db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0


def test_start_02_fixed_smoke_without_question_files_or_research(app):
    configure(app)
    calls = []
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"id":"offline-smoke", "model":"manual-id",
            "choices":[{"message":{"role":"assistant","content":"HTRSA_OK"},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":5,"completion_tokens":5}})
    value = asyncio.run(check_model(app, "m", {"mode":"text","consent":True},
        client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(respond))))
    assert value["ledger_reservations"] == 1 and len(calls) == 1
    assert calls[0]["model"] == "manual-id"
    assert app.store.db.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0
    assert app.store.db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0


@pytest.mark.parametrize("required,reason",[(True,"LITERATURE_EVIDENCE_MISSING"),(False,"ANALYSIS_DATA_REQUIRED")])
def test_evidence_policy_runs_planning_before_honest_stop(app,required,reason):
    configure(app)
    body={"question":"근거를 검토","model_profile_id":"m","run_limit_usd":".10","egress":"selected",
        "search_policy":"DISABLED","search_required":required}
    created=app.create(body);rid=created["research_id"]
    app.command(rid,"start",{"expected_version":0,"idempotency_key":"literature-stop-test"})
    calls=[]
    def respond(request):
        calls.append(request)
        return httpx.Response(200,json={"model":"manual-id","choices":[{"message":{"content":json.dumps(MANAGER)},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":10,"completion_tokens":20}})
    def provider(store,research_id,snapshot):
        return RoutedGateway(store,app.credentials,research_id,snapshot,client_factory=lambda *_:httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    asyncio.run(execute(app.database,app.workspace,rid,provider_factory=provider))
    assert len(calls)==1 and app.store.run(rid)["status"]=="INSUFFICIENT_DATA"
    event=app.store.db.execute("SELECT details_json FROM runtime_events WHERE research_id=? AND event_type='RESEARCH_INPUT_LIMITATION'",(rid,)).fetchone()
    assert json.loads(event[0])["reason"]==reason
    assert app.store.run(rid)["snapshot"]["search_required"] is required


def test_advanced_reasoning_sent_without_sampling_or_breadth_override(app):
    from htrsa.providers.normalized import CapabilityEvidence
    from htrsa.schemas import utc_now
    configure(app)
    profile=app.store.config("model","m")
    profile.update(reasoning_levels=["LOW","HIGH"],reasoning_policy="LOW",temperature=0,
        capabilities={"reasoning":CapabilityEvidence(status="SUPPORTED",source="USER_DECLARED",checked_at=utc_now().isoformat()).model_dump(mode="json")})
    app.store.put("model","m",profile,1)
    body={"question":"관계 검토","model_profile_id":"m","run_limit_usd":".10","egress":"selected",
        "performance_profile":"BALANCED","advanced_performance_profile":"DEEP","model_reasoning":"HIGH","sampling_mode":"provider_default"}
    created=app.create(body);rid=created["research_id"];observed=[]
    def respond(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200,json={"model":"manual-id","choices":[{"message":{"content":json.dumps(MANAGER)},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":10,"completion_tokens":20}})
    def provider(store,research_id,snapshot):
        return RoutedGateway(store,app.credentials,research_id,snapshot,client_factory=lambda *_:httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    app.command(rid,"start",{"expected_version":0,"idempotency_key":"advanced-real-payload"})
    asyncio.run(execute(app.database,app.workspace,rid,provider_factory=provider))
    assert len(observed)==1 and observed[0]["reasoning_effort"]=="high"
    assert "temperature" not in observed[0]
    assert app.store.run(rid)["snapshot"]["performance_profile"]=="DEEP"
