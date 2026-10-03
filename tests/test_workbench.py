"""기존 동작과 검증 경계를 확인하는 회귀 테스트."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from http.server import HTTPServer
import json
import os
from pathlib import Path
import sqlite3
import threading

import httpx
import pytest

from htrsa.control_plane import (Connection, ControlBoundary, ControlError, ControlStore, Credentials, DEPTHS,
    ModelProfile, NewResearch, PinnedTransport, PriceRecord, admitted_cost, classify_http, micro, process_alive, safe_source, validate_endpoint)
from htrsa.control_runtime import RoutedGateway, boundary, execute
from htrsa.database import initialize
from htrsa.providers.base import ModelProviderError
from htrsa.providers.fake import FakeProvider
from htrsa.schemas import utc_now
from htrsa.workbench import Handler, OwnerSession, SmokeOutput, WorkbenchAPI, redact
from test_autonomous_loop import CSV, fake_replies
from test_verification_repair import prepare_case, corrupt_first_output
from htrsa.agent_runtime import RuntimeFailure
from htrsa.recovery import FaultInjector, InjectedCrash


@pytest.fixture
def app(tmp_path):
    api = WorkbenchAPI(tmp_path / "state.sqlite", tmp_path / "workspace", launch=False)
    (api.workspace / "inputs/data.csv").write_bytes(CSV.read_bytes())
    yield api
    api.close()


def configure(app):
    c = Connection(connection_id="local", display_name="Local", adapter_id="openai_compatible", base_url="http://127.0.0.1:1234/v1",
                   endpoint_class="loopback", destination_approved=True)
    app.store.put("connection", "local", c)
    m = ModelProfile(profile_id="m", connection_id="local", model_id="manual-id", protocol="chat",
                     capability_status="supported", local_api_unmetered=True)
    app.store.put("model", "m", m)
    return c, m


def create(app, depth="standard"):
    return app.create({"title": "한국어 연구 이름", "question": "Association?", "source_relative": "data.csv", "research_depth": depth,
                       "routing": {r: "m" for r in ("manager", "experiment_coordinator", "analysis_planner_worker", "verification_coordinator")}, "egress": "selected"})["research_id"]


def test_empty_keyless_workbench(app):
    assert app.request("GET", "/api/control/research").body == []
    assert app.request("GET", "/api/control/settings").status == 200
    assert app.request("GET", "/api/control/environment").body["generated_code"]
    rid = app.create({"title": "Draft", "question": "Question?", "source_relative": "data.csv"})["research_id"]
    result = app.request("POST", f"/api/control/research/{rid}/start", {"idempotency_key": "blocked-test", "expected_version": 0})
    assert result.status == 409
    assert not app.store.ledger()["requests"]
    assert app.store.db.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0


@pytest.mark.parametrize("depth", list(DEPTHS))
def test_depth_snapshot_and_flags_are_real_and_immutable(app, depth):
    configure(app)
    rid = create(app, depth)
    snap = app.store.run(rid)["snapshot"]
    assert snap["requested_depth_limits"] == DEPTHS[depth]
    assert snap["depth_limits"]["hypotheses"] <= 5
    assert snap["depth_limits"]["experiments"] <= 2
    assert snap["depth_limits"]["attempts"] <= 20
    assert snap["depth_limits"]["sources"] == 0
    assert not any(snap[k] for k in ("verified_analysis_skills", "verification_repair", "ridge_arithmetic_check"))
    app.store.put("defaults", "global", {"research_depth": "explore"})
    assert app.store.run(rid)["snapshot"] == snap


def test_double_start_stale_version_and_browser_read_do_not_dispatch(app):
    configure(app)
    rid = create(app)
    body = {"idempotency_key": "same-request", "expected_version": 0}
    a = app.command(rid, "start", body)
    assert app.command(rid, "start", body) == a
    assert app.request("GET", f"/api/control/research/{rid}/control").body["status"] == "STARTING"
    with pytest.raises(ControlError, match="STATE_STALE"):
        app.command(rid, "start", {**body, "idempotency_key": "other-request"})
    assert app.store.db.execute("SELECT COUNT(*) FROM control_commands").fetchone()[0] == 1
    assert not app.store.ledger()["requests"]


def test_existing_runtime_pause_resume_and_commit_boundary(app):
    configure(app)
    rid = create(app)
    app.command(rid, "start", {"idempotency_key": "start-life", "expected_version": 0})
    replies = fake_replies()
    made = []
    def provider(store, rid, snapshot):
        fake = FakeProvider(replies)
        original = fake.run_structured
        async def call(**kwargs):
            value = await original(**kwargs)
            if len(fake.calls) == 1:
                store.db.execute("UPDATE control_runs SET status='PAUSE_REQUESTED' WHERE research_id=?", (rid,))
            return value
        fake.run_structured = call
        made.append(fake)
        return fake
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=provider))
    run = app.store.run(rid)
    assert run["status"] == "PAUSED"
    assert len(made[0].calls) == 1
    assert app.read._state.load_runtime_cursor(rid)
    assert app.store.db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    app.command(rid, "resume", {"idempotency_key": "resume-life", "expected_version": run["version"]})
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=lambda *_: FakeProvider(replies[1:])))
    assert app.store.run(rid)["status"] == "COMPLETED"
    assert app.store.db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 2
    before = app.store.db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=lambda *_: FakeProvider([])))
    assert app.store.db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == before
    assert app.request("GET", f"/api/research/{rid}/report").body["available"]


@pytest.mark.parametrize("action,expected", [("pause", "PAUSED"), ("stop", "STOPPED")])
def test_control_requests_are_safe_boundary_states(app, action, expected):
    configure(app)
    rid = create(app)
    app.store.db.execute("UPDATE control_runs SET status='RUNNING' WHERE research_id=?", (rid,))
    result = app.command(rid, action, {"idempotency_key": "safe-request", "expected_version": 0})
    assert result["status"].endswith("REQUESTED")
    with pytest.raises(ControlBoundary):
        boundary(app.store, app.read._state, rid)
    assert not app.store.ledger()["requests"]


@pytest.mark.parametrize("action,expected", [("pause", "PAUSED"), ("stop", "STOPPED")])
def test_queued_worker_cancel_has_no_provider_or_tool_effect(app, action, expected):
    configure(app)
    rid = create(app)
    app.command(rid, "start", {"idempotency_key": "queued-start", "expected_version": 0})
    result = app.command(rid, action, {"idempotency_key": "queued-cancel", "expected_version": 1})
    assert result["status"] == expected
    if action == "stop":
        assert app.store.db.execute("SELECT stop_reason FROM research_runs WHERE research_id=?", (rid,)).fetchone()[0] == "USER_STOP"
    fake = FakeProvider([])
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=lambda *_: fake))
    assert not fake.calls
    assert app.store.db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
    assert not app.store.ledger()["requests"]


def test_running_stop_finishes_canonical_lifecycle_at_boundary(app):
    configure(app)
    rid = create(app)
    app.command(rid, "start", {"idempotency_key": "live-stop-start", "expected_version": 0})
    fake = FakeProvider(fake_replies())
    original = fake.run_structured
    async def call(**kwargs):
        value = await original(**kwargs)
        app.store.db.execute("UPDATE control_runs SET status='STOP_REQUESTED' WHERE research_id=?", (rid,))
        return value
    fake.run_structured = call
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=lambda *_: fake))
    assert len(fake.calls) == 1
    assert app.store.run(rid)["status"] == "STOPPED"
    assert app.store.db.execute("SELECT stop_reason FROM research_runs WHERE research_id=?", (rid,)).fetchone()[0] == "USER_STOP"
    assert app.request("GET", f"/api/research/{rid}/report").body["available"]


def test_worker_start_error_is_visible_without_dispatch(app, monkeypatch):
    configure(app)
    rid = create(app)
    app.launch = True
    def fail(*_args, **_kwargs):
        raise OSError("cannot start")
    monkeypatch.setattr("htrsa.workbench.subprocess.Popen", fail)
    with pytest.raises(ControlError, match="WORKER_START_FAILED"):
        app.command(rid, "start", {"idempotency_key": "launch-error", "expected_version": 0})
    assert app.store.run(rid)["status"] == "FAILED"
    assert not app.store.ledger()["requests"]


def test_worker_broker_failure_keeps_typed_reason_and_no_retries(app):
    configure(app)
    rid = create(app)
    app.command(rid, "start", {"idempotency_key": "broker-error", "expected_version": 0})
    calls = []
    class BlockedProvider:
        name = "control_broker"
        async def run_structured(self, **_kwargs):
            calls.append(1)
            raise ControlError("PRICE_REQUIRED")
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=lambda *_: BlockedProvider()))
    run = app.store.run(rid)
    assert run["status"] == "BUDGET_BLOCKED"
    assert run["error"] == "PRICE_REQUIRED"
    assert len(calls) == 1
    assert not app.store.ledger()["requests"]


def test_dead_worker_detected_by_read_keeps_liability(app):
    configure(app)
    rid = create(app)
    reservation = reserve(app.store, rid=rid)
    app.store.transition(reservation, "DISPATCHED")
    app.store.db.execute("UPDATE control_runs SET status='RUNNING',pid=NULL WHERE research_id=?", (rid,))
    result = app.request("GET", f"/api/control/research/{rid}/control")
    assert result.body["status"] == "NEEDS_RECONCILIATION"
    assert app.store.ledger(rid)["unresolved"] == "0.6"


def reserve(store, rid="r", bound="0.6", **kwargs):
    return store.reserve(rid=rid, connection="c", model="m", role="manager", purpose="research", bound=bound,
                         run_limit=kwargs.get("run_limit", "1"), monthly_limit=kwargs.get("monthly_limit", "1"),
                         request_limit="1", attempts=10, revision="p1")


def test_atomic_concurrent_reservations_share_real_sqlite_cap(app):
    gate = threading.Barrier(2)
    def work(i):
        db = initialize(app.database)
        store = ControlStore(db)
        gate.wait()
        try:
            return reserve(store, rid=str(i))
        except ControlError as e:
            return e.code
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(work, range(2)))
    assert len([v for v in result if v.startswith("RSV-")]) == 1
    assert "BUDGET_BLOCKED" in result


@pytest.mark.parametrize("initial", ["RESERVED", "DISPATCHED"])
def test_crash_recovery_retains_exposure_and_blocks_replay(app, initial):
    identity = reserve(app.store)
    if initial == "DISPATCHED":
        app.store.transition(identity, "DISPATCHED")
    app.store.recover_ledger("r")
    app.store.recover_ledger("r")
    assert app.store.ledger()["unresolved"] == "0.6"
    with pytest.raises(ControlError):
        reserve(app.store, bound="0.1")
    assert app.store.ledger()["unresolved"] == "0.6"


def test_settlement_idempotency_month_carry_and_no_checkpoint_refund(app):
    identity = reserve(app.store)
    app.store.transition(identity, "DISPATCHED")
    app.store.transition(identity, "SETTLED", settled="0.4")
    with pytest.raises(ControlError):
        app.store.transition(identity, "SETTLED", settled="0")
    pending = reserve(app.store, rid="p", bound="0.5")
    app.store.transition(pending, "DISPATCHED")
    app.store.transition(pending, "UNRESOLVED")
    app.store.db.execute("UPDATE spend_ledger SET month='2001-01'")
    with pytest.raises(ControlError, match="BUDGET_BLOCKED"):
        reserve(app.store, rid="next", bound="0.6")
    assert app.store.ledger()["spent"] == "0.4"
    assert app.store.ledger()["unresolved"] == "0.5"


def gateway(app, *, mode="ok", paid=False, capability="supported", egress="selected"):
    conn, model = configure(app)
    model.capability_status = capability
    if paid:
        model.local_api_unmetered = False
        model.price = PriceRecord(input_per_million="1", output_per_million="2", checked_at=utc_now(), source="offline fixture", revision="p1", owner_verified=True)
    snap = {"models": {"manager": model.model_dump(mode="json")}, "connections": {"local": conn.model_dump(mode="json")}, "egress": egress,
            "run_limit_usd": "1", "monthly_limit_usd": "1", "request_limit_usd": "1", "depth_limits": {"attempts": 4}}
    calls = []
    def transport(request):
        calls.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("accepted maybe", request=request)
        if mode == "partial_stream":
            class Partial(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'{"id":"accepted","usage":'
                    raise httpx.ReadTimeout("late acceptance", request=request)
            return httpx.Response(200, stream=Partial())
        document = {"id": "fixture-response", "usage": {"prompt_tokens": 30, "completion_tokens": 10}, "choices": [{"message": {"content": '{"status":"ok"}'}}]}
        if mode == "missing_usage":
            document.pop("usage")
        if mode == "spend_limit":
            return httpx.Response(429, json={"error": {"code": "insufficient_quota"}})
        if mode == "invalid_json":
            document["choices"][0]["message"]["content"] = "invalid"
        if mode == "echo_key":
            document["choices"][0]["message"]["content"] = json.dumps({"status": os.environ["OPENAI_API_KEY"]})
        return httpx.Response(200, json=document)
    broker = RoutedGateway(app.store, app.credentials, "gateway", snap, client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    return broker, calls


def invoke(broker):
    return asyncio.run(broker.run_structured(role="manager", instructions="Test", input_text="data", output_type=SmokeOutput, model="manual-id"))


@pytest.mark.parametrize("mode", ["ok", "invalid_json", "missing_usage", "timeout", "partial_stream", "spend_limit"])
def test_gateway_dispatch_settlement_and_ambiguous_failures(app, mode):
    broker, calls = gateway(app, mode=mode, paid=True)
    if mode == "ok":
        assert invoke(broker).output.status == "ok"
    else:
        with pytest.raises((ControlError, ModelProviderError)):
            invoke(broker)
    assert len(calls) == 1
    ledger = app.store.ledger()
    expected = "SETTLED" if mode in {"ok", "invalid_json"} else "UNRESOLVED"
    assert ledger["requests"][0]["status"] == expected
    if expected == "UNRESOLVED":
        with pytest.raises(ControlError):
            invoke(broker)
        assert len(calls) == 1
    assert "Authorization" not in json.dumps(ledger)


def test_provider_key_echo_is_never_persisted(app, monkeypatch):
    secret = "gui-test-provider-key-canary-123456"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    broker, calls = gateway(app, mode="echo_key", paid=True)
    with pytest.raises(ControlError, match="SECRET_IN_PROVIDER_RESPONSE"):
        invoke(broker)
    assert len(calls) == 1
    assert app.store.ledger()["requests"][0]["status"] == "UNRESOLVED"
    assert secret.encode() not in app.database.read_bytes()
    assert not app.store.db.execute("SELECT * FROM control_model_cache").fetchall()


def test_expired_research_time_has_zero_dispatch(app):
    broker, calls = gateway(app, paid=True)
    rid = create(app)
    broker.rid = rid
    broker.snapshot["max_elapsed_sec"] = 10
    app.store.db.execute("UPDATE control_runs SET started_at=? WHERE research_id=?", ((utc_now() - timedelta(seconds=20)).isoformat(), rid))
    with pytest.raises(ControlError, match="TIME_LIMIT"):
        invoke(broker)
    assert not calls
    assert not app.store.ledger()["requests"]


def test_spreadsheet_export_escapes_formulas_and_keeps_canonical_archive(tmp_path):
    from hashlib import sha256
    import csv
    import io
    from htrsa.demo import run_demo_a
    from htrsa.service import StateService
    from htrsa.storage import Workspace
    from htrsa.release import export_release
    root = tmp_path / "csv-release"
    result = run_demo_a(root / "state.sqlite", root / "workspace")
    db = initialize(root / "state.sqlite")
    try:
        state = StateService(db, Workspace(root / "workspace"))
        rid = result["research_id"]
        version = state.state_version(rid)
        source = state.workspace.path(rid, "research_output")
        raw = b'label,value\n"=HYPERLINK(\"\"https://evil.example\"\")",-1.2\n+CMD,3\n@SUM(1),4\n-EXEC,5\n\t=CMD,6\n'
        (source / "display.csv").write_bytes(raw)
        hidden = source / "private_diagnostic.json"
        hidden.write_bytes(b'{"private":"unallowlisted diagnostic"}')
        manifest_path = source / "manifests/artifact_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["display.csv"] = sha256(raw).hexdigest()
        manifest_path.write_bytes(json.dumps(manifest).encode("utf-8", errors="strict"))
        exported = export_release(state, rid, tmp_path / "release")
        target = Path(exported["output"])
        safe = (target / "research_output/display.csv").read_bytes()
        rows = list(csv.reader(io.StringIO(safe.decode("utf-8"))))
        assert all(r[0].startswith("\x27") for r in rows[1:])
        assert rows[1][1] == "-1.2"
        assert (source / "display.csv").read_bytes() == raw
        assert (target / "canonical_text/display.csv.raw.txt").read_bytes() == raw
        assert state.state_version(rid) == version
        assert not (target / "research_output/private_diagnostic.json").exists()
        portable = json.loads((target / "research_output/manifests/artifact_manifest.json").read_text(encoding="utf-8"))
        assert portable["files"]["display.csv"] == sha256(safe).hexdigest()
        assert portable["export_transformations"]["display.csv"]["canonical_sha256"] == sha256(raw).hexdigest()
        assert all(sha256((target / item["path"]).read_bytes()).hexdigest() == item["sha256"] for item in exported["files"])
    finally:
        db.close()


@pytest.mark.parametrize("reason", ["egress", "capability", "price", "context", "connection_changed"])
def test_blocked_gateway_causes_zero_actual_transport_dispatch(app, reason):
    broker, calls = gateway(app, paid=True, egress="none" if reason == "egress" else "selected", capability="unknown" if reason == "capability" else "supported")
    if reason == "price":
        broker.snapshot["models"]["manager"]["price"] = None
    if reason == "context":
        broker.snapshot["models"]["manager"]["input_byte_limit"] = 256
    if reason == "connection_changed":
        old = app.store.config("connection", "local")
        old["display_name"] = "changed"
        app.store.put("connection", "local", old, 1)
    with pytest.raises(ControlError):
        invoke(broker)
    assert not calls
    assert not app.store.ledger()["requests"]


@pytest.mark.parametrize("url,kind", [("http://169.254.169.254/v1", "loopback"), ("file:///v1", "cloud"), ("https://user:key@example.com/v1", "cloud"),
    ("http://example.com/v1", "cloud"), ("http://localhost:11434/v1", "loopback"), ("https://example.com/v1?x=a", "cloud")])
def test_endpoint_ssrf_and_credential_redirect_boundaries(url, kind):
    with pytest.raises(ControlError):
        validate_endpoint(url, kind)


def test_pinned_transport_denies_cross_origin_and_metadata(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *_args, **_kw: [(2, 1, 6, "", ("169.254.169.254", 443))])
    conn = Connection(connection_id="cloud", display_name="Cloud", base_url="https://example.com/v1", destination_approved=True, adapter_id="openai_compatible")
    with pytest.raises(ControlError, match="ENDPOINT_DENIED"):
        PinnedTransport(conn)


def test_credential_secret_input_metadata_environment_and_unsafe_path(tmp_path, monkeypatch):
    repo, workspace = tmp_path / "repo", tmp_path / "workspace"
    credentials = Credentials(repo, workspace, tmp_path / "private/secrets.env")
    canary = "sk-workbench-canary-0123456789"
    metadata = credentials.save("TEST_API_KEY", canary)
    assert metadata["configured"]
    assert canary not in json.dumps(metadata)
    assert credentials.get("TEST_API_KEY") == canary
    monkeypatch.setenv("TEST_API_KEY", "override-value")
    assert credentials.metadata("TEST_API_KEY")["active_source"] == "environment"
    assert credentials.get("TEST_API_KEY") == "override-value"
    credentials.save("TEST_API_KEY", None)
    assert credentials.metadata("TEST_API_KEY")["active_source"] == "environment"
    unsafe = Credentials(repo, workspace, workspace / "secrets.env")
    with pytest.raises(ControlError):
        unsafe.save("TEST_API_KEY", canary)
    with pytest.raises(ControlError):
        credentials.save("TEST_API_KEY", "value\nNEXT_KEY=oops")


def test_invalid_secret_route_never_echoes_key(app, monkeypatch):
    configure(app)
    secret = "sk-workbench-canary-0123456789"
    result = app.request("POST", "/api/control/connections/local/credential", {"value": secret, "unexpected": secret})
    assert result.status == 400
    assert secret not in repr(result)
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    assert secret not in json.dumps(app.request("GET", "/api/control/settings").body)
    assert secret not in json.dumps(redact({"log": secret, "html": "<script>"}))
    assert secret.encode() not in app.database.read_bytes()


def test_owner_session_http_origin_csrf_no_cache_and_pairing(tmp_path):
    ready = threading.Event()
    shared = {}
    def server_thread():
        api = WorkbenchAPI(tmp_path / "http.sqlite", tmp_path / "workspace", launch=False)
        session = OwnerSession(allow_pairing=True)
        server = HTTPServer(("127.0.0.1", 0), type("TestHandler", (Handler,), {"api": api, "session": session, "authority": ""}))
        server.RequestHandlerClass.authority = f"127.0.0.1:{server.server_port}"
        shared.update(server=server, code=session.pairing)
        ready.set()
        server.serve_forever()
        api.close()
    thread = threading.Thread(target=server_thread)
    thread.start()
    ready.wait(10)
    origin = f"http://127.0.0.1:{shared['server'].server_port}"
    try:
        with httpx.Client(base_url=origin, trust_env=False) as client:
            assert client.get("/api/control/settings").status_code == 401
            assert client.get("/api/control/settings", headers={"Host": "evil.example"}).status_code == 403
            assert client.post("/api/session", json={"pairing_code": shared["code"]}, headers={"Origin": "https://evil.example"}).status_code == 403
            response = client.post("/api/session", json={"pairing_code": shared["code"]}, headers={"Origin": origin})
            assert response.status_code == 200
            assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=Strict" in response.headers["set-cookie"]
            assert client.get("/api/control/settings").status_code == 200
            assert client.post("/api/control/defaults", json={"value": {}}, headers={"Origin": origin}).status_code == 403
            assert client.post("/api/session", json={"pairing_code": shared["code"]}, headers={"Origin": origin}).status_code == 403
            assert response.headers["cache-control"] == "no-store"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    finally:
        shared["server"].shutdown()
        shared["server"].server_close()
        thread.join(10)


@pytest.mark.parametrize("skill", [False, True])
def test_f3p_ui_success_full_sequence_is_real_and_scoped(tmp_path, skill):
    db, state, agent, prepared = prepare_case(tmp_path, skill=skill)
    corrupt_first_output(state)
    asyncio.run(agent.resume(prepared["research_id"]))
    db.close()
    api = WorkbenchAPI(tmp_path / "state.sqlite", tmp_path / "workspace", launch=False)
    try:
        p = api.repair_projection(prepared["research_id"])
        assert len(p["chains"]) == 1
        c = p["chains"][0]
        assert c["new_revision"]
        assert c["decisions"] and c["revalidations"]
        assert c["final_disposition"] == "VERIFIED"
        assert not c["remaining_checks"]
        assert c["procedure_approved_for_reuse"] is False
        assert bool(p["skills"]) == skill
        artifacts = api.request("GET", f"/api/research/{prepared['research_id']}/artifacts").body
        assert all(not a["preview_allowed"] for a in artifacts if a["status"] in {"INVALIDATED", "SUPERSEDED"})
        assert not p["ridge"]["enabled"]
        assert api.request("GET", "/api/control/research/R-absent/repair").status >= 400
    finally:
        api.close()


def test_f3p_ui_tampered_audit_cannot_show_pass(tmp_path):
    db, state, agent, prepared = prepare_case(tmp_path)
    corrupt_first_output(state)
    asyncio.run(agent.resume(prepared["research_id"]))
    row = db.execute("SELECT relative_path FROM artifacts WHERE artifact_type='F3P_REVALIDATION'").fetchone()
    state.workspace.path(prepared["research_id"], row[0]).write_bytes(b'{}')
    db.close()
    api = WorkbenchAPI(tmp_path / "state.sqlite", tmp_path / "workspace", launch=False)
    assert api.repair_projection(prepared["research_id"])["chains"][0]["final_disposition"] != "VERIFIED"
    api.close()


@pytest.mark.parametrize("point", ["AFTER_FAILURE_EVIDENCE", "AFTER_REPAIR_DECISION", "AFTER_REPAIRED_EXECUTION",
                                  "DURING_REVALIDATION", "BEFORE_REPAIR_COMMIT", "AFTER_REPAIR_COMMIT"])
def test_f3p_ui_process_reopen_keeps_history_and_one_commit(tmp_path, point):
    from htrsa.agent_runtime import AgentRuntime
    from htrsa.service import StateService
    from htrsa.storage import Workspace
    from test_verified_analysis_skills import MODELS
    db, state, agent, prepared = prepare_case(tmp_path)
    corrupt_first_output(state)
    agent.repair_faults = FaultInjector(lambda name: (_ for _ in ()).throw(InjectedCrash(name)) if name == point else None)
    with pytest.raises(InjectedCrash):
        asyncio.run(agent.resume(prepared["research_id"]))
    remaining = list(agent.provider.replies)
    db.close()
    preview = WorkbenchAPI(tmp_path / "state.sqlite", tmp_path / "workspace", launch=False)
    assert preview.repair_projection(prepared["research_id"])["chains"][0]["failure"]
    preview.close()
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    resumed = AgentRuntime(state, FakeProvider(remaining), MODELS, verified_analysis_skills_enabled=True, verification_repair_enabled=True)
    asyncio.run(resumed.resume(prepared["research_id"]))
    db.close()
    api = WorkbenchAPI(tmp_path / "state.sqlite", tmp_path / "workspace", launch=False)
    assert api.repair_projection(prepared["research_id"])["chains"][0]["final_disposition"] == "VERIFIED"
    assert api.store.db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 1
    assert api.store.db.execute("SELECT COUNT(*) FROM staged_mutations").fetchone()[0] == 2
    api.close()


def test_ridge_ui_displays_actual_check_and_default_off(tmp_path):
    db, state, agent, prepared = prepare_case(tmp_path, kind="regression", ridge=True)
    asyncio.run(agent.resume(prepared["research_id"]))
    db.close()
    api = WorkbenchAPI(tmp_path / "state.sqlite", tmp_path / "workspace", launch=False)
    projection = api.repair_projection(prepared["research_id"])
    assert projection["ridge"]["enabled"]
    assert projection["ridge"]["checks"]
    assert all(c["arithmetic_status"] == "passed" for c in projection["ridge"]["checks"])
    api.close()


def test_sdk_tracing_disabled_before_agent_and_runner(monkeypatch):
    import agents
    from htrsa.providers.openai_agents import OpenAIAgentsProvider
    calls = []
    monkeypatch.setattr(agents, "set_tracing_disabled", lambda value: calls.append(("disabled", value)))
    def agent(**kw):
        assert calls == [("disabled", True)]
        return object()
    monkeypatch.setattr(agents, "Agent", agent)
    async def run(_agent, _text, run_config):
        assert run_config.tracing_disabled and not run_config.trace_include_sensitive_data
        from types import SimpleNamespace
        return SimpleNamespace(final_output={"status": "ok"}, context_wrapper=None)
    monkeypatch.setattr(agents.Runner, "run", run)
    result = asyncio.run(OpenAIAgentsProvider().run_structured(role="manager", instructions="test", input_text="test", output_type=SmokeOutput, model="fixture"))
    assert result.output.status == "ok"


@pytest.mark.parametrize("status,code", [(401,"PROVIDER_AUTH"),(403,"PROVIDER_PERMISSION"),(404,"MODEL_NOT_FOUND"),(400,"PROVIDER_PARAMETER"),(503,"SERVER_UNAVAILABLE")])
def test_provider_actionable_errors_are_distinct(status, code):
    assert classify_http(status) == code


def test_control_policy_content_cannot_change_permissions(app):
    before = app.store.defaults()
    result = app.request("POST", "/api/control/defaults", {"value": {"tool": "read key", "install": "pip install", "force_canonical": True}})
    assert result.status == 400
    assert app.store.defaults() == before
    assert app.request("POST", "/api/research/R-invalid/commit", {}).status == 409
    assert process_alive(os.getpid())


def test_gateway_settled_response_replay_does_not_repeat_remote_bill(app):
    broker, calls = gateway(app, paid=True)
    first = invoke(broker)
    second = invoke(broker)
    assert first.output == second.output
    assert second.request_count == 0
    assert len(calls) == 1
    assert len(app.store.ledger()["requests"]) == 1


def test_depth_change_only_applies_at_safe_boundary_and_preserves_caps(app):
    from htrsa.autonomous_loop import AutonomousResearchLoop
    configure(app)
    rid = create(app)
    snapshot = deepcopy(app.store.run(rid)["snapshot"])
    app.store.db.execute("UPDATE control_runs SET status='RUNNING' WHERE research_id=?", (rid,))
    response = app.request("POST", f"/api/control/research/{rid}/depth", {"research_depth": "explore", "expected_version": 0})
    assert response.status == 200
    assert not response.body["applied"]
    runtime = AutonomousResearchLoop(app.read._state, FakeProvider([]))
    assert runtime.config.max_shortlist == 3
    boundary(app.store, app.read._state, rid, runtime, snapshot)
    assert runtime.config.max_shortlist == 1
    assert runtime.config.max_branch_depth == 1
    assert app.store.run(rid)["snapshot"]["run_limit_usd"] == snapshot["run_limit_usd"]
    assert app.store.run(rid)["snapshot"]["egress"] == snapshot["egress"]
    assert app.store.run(rid)["snapshot"]["research_depth"] == "standard"
    assert app.store.config("depth", rid)["applied"]


def test_sandbox_cli_subprocess_never_inherits_provider_credentials(monkeypatch):
    import htrsa.sandbox as sandbox
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-canary-0123456789")
    monkeypatch.setenv("OTHER_PROVIDER_TOKEN", "private-token")
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "docker")
    calls = []
    def run(args, **kwargs):
        calls.append(kwargs)
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(sandbox.subprocess, "run", run)
    assert sandbox.docker_available()
    assert "OPENAI_API_KEY" not in calls[0]["env"]
    assert "OTHER_PROVIDER_TOKEN" not in calls[0]["env"]
    assert set(calls[0]["env"]) <= set(sandbox.clean_environment())
    from htrsa.preflight import _run
    assert _run(["docker", "info"]).returncode == 0
    assert "OPENAI_API_KEY" not in calls[1]["env"]
    assert "OTHER_PROVIDER_TOKEN" not in calls[1]["env"]


def test_hybrid_routing_runs_existing_full_loop_through_accounted_gateway(app, monkeypatch):
    from htrsa.demo import _agent_replies
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only-canary-0123456789")
    configure(app)
    cloud = Connection(connection_id="cloud", display_name="API", adapter_id="openai_compatible", base_url="https://api.openai.com/v1", destination_approved=True)
    app.store.put("connection", "cloud", cloud)
    manager = ModelProfile(profile_id="manager-api", connection_id="cloud", model_id="api-manager", protocol="chat", capability_status="supported",
                           price=PriceRecord(input_per_million="1", output_per_million="2", checked_at=utc_now(), source="offline fixture", revision="p1", owner_verified=True))
    app.store.put("model", "manager-api", manager)
    raw = {"title": "Hybrid offline", "question": "Association?", "source_relative": "data.csv", "egress": "selected",
           "routing": {"manager": "manager-api", "experiment_coordinator": "m", "analysis_planner_worker": "m", "verification_coordinator": "m"}}
    rid = app.create(raw)["research_id"]
    app.command(rid, "start", {"idempotency_key": "hybrid-start", "expected_version": 0})
    replies = list(fake_replies())
    calls = []
    def factory(store, rid, snapshot):
        def respond(request):
            data = json.loads(request.content)
            reply = replies.pop(0)
            if callable(reply):
                reply = reply({"input_text": data["messages"][1]["content"]})
            calls.append((request.url.host, data["model"]))
            return httpx.Response(200, json={"id": f"mock-{len(calls)}", "usage": {"prompt_tokens": 50, "completion_tokens": 50},
                                           "choices": [{"message": {"content": json.dumps(reply)}}]})
        return RoutedGateway(store, app.credentials, rid, snapshot, client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=factory))
    assert app.store.run(rid)["status"] == "COMPLETED"
    assert ("api.openai.com", "api-manager") in calls
    assert ("127.0.0.1", "manual-id") in calls
    assert len(app.store.ledger()["requests"]) == len(calls)
    assert all(r["status"] == "SETTLED" for r in app.store.ledger()["requests"])
    assert app.store.db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 2
    assert app.request("GET", f"/api/research/{rid}/experiments").body[0]["measured_values"]


@pytest.mark.parametrize("case_id", ["compound_defect", "faulty_checker", "semantic_mutation", "insufficient_budget", "unsupported_repair"])
def test_f3p_ui_real_failure_scenarios_never_present_success(tmp_path, case_id):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "qa"))
    import f3p_eval
    config = json.loads(f3p_eval.CONFIG.read_text(encoding="utf-8"))
    case = next(c for c in config["cases"] if c["id"] == case_id)
    result = f3p_eval.run_case(case, tmp_path, config["dataset_seed"])
    assert result["fixture_correct"]
    api = WorkbenchAPI(tmp_path / "state.sqlite", tmp_path / "workspace", mode="DEMO", launch=False)
    try:
        rid = api.store.db.execute("SELECT research_id FROM research_runs").fetchone()[0]
        projection = api.repair_projection(rid)
        assert projection["chains"]
        assert all(c["final_disposition"] != "VERIFIED" for c in projection["chains"])
        assert any(c["remaining_checks"] for c in projection["chains"])
        if case_id in {"faulty_checker", "semantic_mutation", "insufficient_budget"}:
            assert any(c["final_disposition"] == case["expected"] for c in projection["chains"])
        assert api.store.db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    finally:
        api.close()
