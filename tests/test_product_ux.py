"""GPT 우선 UX의 비용·키·설정·보고서 경계를 실제 저장소와 실행기로 확인한다."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
import os
from pathlib import Path

import httpx
import pytest

from probe.control_plane import ControlBoundary, ControlError, Credentials, ModelProfile, PriceRecord, ROLES
from probe.control_runtime import RoutedGateway, boundary, execute
from probe.database import to_json
from probe.final_report import export_final_report
from probe.product_policy import catalog, completion_budget, effective_cap, effective_snapshot, maybe_select_worker, optional_admission
from probe.providers.fake import FakeProvider
from probe.providers.normalized import CapabilityEvidence
from probe.report_ux import friendly_report, markdown_report
from probe.research_settings import apply_pending, projection, queue
from probe.release import export_release, ReleaseExportError, _secret_free, _PROTECTED_VALUES
from probe.schemas import utc_now
from probe.secret_store import WindowsCredentialStore
from probe.storage import sha256_file
from test_workbench import app, configure, create, gateway, invoke
from test_autonomous_loop import fake_replies
from test_verification_repair import prepare_case, corrupt_first_output


def product(app, **changes):
    if not app.store.configs("model"):
        configure(app)
    request = {"title": "검증 연구", "question": "측정값의 관계는?", "source_relative": "data.csv",
               "performance_profile": "BALANCED", "model_profile_id": "m", "egress": "selected", **changes}
    return app.create(request)


def paid(app, *, rate=1):
    configure(app)
    raw = app.store.config("model", "m")
    raw.update(local_api_unmetered=False, price=PriceRecord(input_per_million=rate, output_per_million=rate,
        checked_at=utc_now(), source="오프라인 가격 fixture", revision="offline", owner_verified=True).model_dump(mode="json"))
    app.store.put("model", "m", ModelProfile.model_validate(raw), 1)


def settings(app, rid, **changes):
    s = effective_snapshot(app.store, rid)
    value = {"performance_profile": s.get("performance_profile"), "adaptive_budget": s["adaptive_budget"],
             "run_limit_usd": s["run_limit_usd"], "max_elapsed_sec": s["max_elapsed_sec"], "egress": s["egress"], **changes}
    return {"expected_version": app.store.run(rid)["version"], "value": value}


@pytest.mark.parametrize("preset,depth", [("FAST", "explore"), ("BALANCED", "standard"), ("DEEP", "deep"), ("MAX", "focused")])
def test_performance_actual_snapshot_and_single_model(app, preset, depth):
    result = product(app, performance_profile=preset)
    s = result["snapshot"]
    assert s["research_depth"] == depth and s["adaptive_budget"] is True
    assert set(s["models"]) == set(ROLES)
    assert {m["profile_id"] for m in s["models"].values()} == {"m"}
    assert s["depth_limits"]["sources"] == 0
    assert not s["verification_repair"] and not s["verified_analysis_skills"]
    assert not app.store.ledger()["requests"]


def test_manual_role_override_and_supported_reasoning(app):
    configure(app)
    raw = app.store.config("model", "m")
    raw.update(profile_id="worker", model_id="worker-real", reasoning_levels=["LOW", "HIGH"],
               capabilities={"reasoning": CapabilityEvidence(status="SUPPORTED", source="STATIC_ADAPTER_RULE").model_dump(mode="json")})
    app.store.put("model", "worker", ModelProfile.model_validate(raw))
    s = product(app, manual_role_override=True, routing={"analysis_planner_worker": "worker"}, role_reasoning={"analysis_planner_worker": "LOW"})["snapshot"]
    assert s["models"]["manager"]["profile_id"] == "m"
    assert s["models"]["analysis_planner_worker"]["reasoning_policy"] == "LOW"
    assert s["preset_customized"]
    with pytest.raises(ControlError, match="REASONING_UNSUPPORTED"):
        product(app, role_reasoning={"manager": "MAX"})


def test_catalog_preserves_unvalidated_prices_and_filters_nontext(app):
    response = app.request("POST", "/api/control/catalog/select", {"model_id": "gpt-6.1-sol", "approve_price": True, "approve_destination": True})
    assert response.status == 201
    value = app.request("GET", "/api/control/settings")
    assert value.status == 200
    assert all(m["status"] == "UNTESTED" and not m["operational"] for m in value.body["catalog"]["models"])
    profile = ModelProfile(profile_id="embedding", connection_id="gpt-default", model_id="arbitrary", output_modalities=["audio"])
    app.store.put("model", "embedding", profile)
    assert not any(m.get("profile_id") == "embedding" for m in catalog(app.store)["models"])
    assert not app.store.ledger()["requests"]


def test_provider_discovery_is_metadata_not_execution(app):
    configure(app)
    app.store.put("catalog_discovery", "local", {"connection_id": "local", "checked_at": utc_now().isoformat(),
        "models": [{"model_id": "explicit-text", "capabilities": {"text": {"status": "SUPPORTED"}}},
                   {"model_id": "embedding", "capabilities": {"text": {"status": "UNSUPPORTED"}}}]})
    entries = catalog(app.store)["models"]
    assert any(m["model_id"] == "explicit-text" and m["status"] == "UNTESTED" for m in entries)
    assert not any(m["model_id"] == "embedding" for m in entries)


@pytest.mark.parametrize("scenario", ["ample", "optional_reduced", "mandatory_short", "unknown_price", "unsettled", "off", "tightened", "f3p"])
def test_budget_auto_cases(app, scenario):
    paid(app)
    cap = "1" if scenario == "ample" else ".25"
    r = product(app, run_limit_usd=cap, adaptive_budget=scenario != "off", verification_repair=scenario == "f3p")
    rid, snap = r["research_id"], r["snapshot"]
    if scenario == "mandatory_short":
        snap["run_limit_usd"] = ".01"
    elif scenario == "unknown_price":
        for raw in snap["models"].values(): raw["price"] = None
    elif scenario == "unsettled":
        reservation = app.store.reserve(rid=rid, connection="local", model="manual-id", role="manager", purpose="research", bound=".02", run_limit=cap, monthly_limit=20, request_limit=1, attempts=20, revision="test")
        app.store.transition(reservation, "DISPATCHED")
        app.store.transition(reservation, "UNRESOLVED")
    elif scenario == "tightened":
        queue(app.store, rid, settings(app, rid, run_limit_usd=".01"))
    view = completion_budget(app.store, rid, snap)
    if scenario in {"ample", "optional_reduced", "unsettled"}: assert view["can_complete"]
    if scenario in {"mandatory_short", "unknown_price", "tightened"}: assert not view["can_complete"]
    if scenario == "unknown_price": assert view["status"] == "UNKNOWN" and view["completion_reserve_usd"] is None
    if scenario == "optional_reduced": assert not optional_admission(app.store, rid, snap)
    if scenario == "off": assert optional_admission(app.store, rid, snap) and not view["adaptive_budget"]
    if scenario == "unsettled": assert view["unsettled_exposure_usd"] == ".02" or Decimal(view["unsettled_exposure_usd"]) == Decimal(".02")
    if scenario == "f3p": assert Decimal(view["f3p_reserve_usd"]) > 0
    assert view["ai_narrative"] == "NOT_RUN"


def test_completion_guard_zero_dispatch_and_atomic_owner_tightening(app):
    paid(app)
    result = product(app, run_limit_usd=".01")
    calls = []
    broker = RoutedGateway(app.store, app.credentials, result["research_id"], result["snapshot"],
        client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request))))
    with pytest.raises(ControlBoundary, match="COMPLETION_BUDGET_INCOMPLETE"):
        invoke(broker)
    assert not calls and not app.store.ledger()["requests"]
    queue(app.store, result["research_id"], settings(app, result["research_id"], run_limit_usd=".001"))
    with pytest.raises(ControlError, match="BUDGET_BLOCKED"):
        app.store.reserve(rid=result["research_id"], connection="local", model="manual-id", role="manager", purpose="research", bound=".01", run_limit=100, monthly_limit=100, request_limit=1, attempts=20, revision="test")
    assert not app.store.ledger()["requests"]


def test_settings_boundary_history_restart_and_stale(app):
    rid = product(app)["research_id"]
    original = deepcopy(app.store.run(rid)["snapshot"])
    body = settings(app, rid, performance_profile="DEEP", run_limit_usd="2", max_elapsed_sec=600,
                    report_style="technical", manual_role_override=True, routing={"analysis_planner_worker": "m"})
    response = app.request("POST", f"/api/control/research/{rid}/settings", body)
    assert response.status == 200 and not response.body["applied"]
    assert app.request("POST", f"/api/control/research/{rid}/settings", body).body["error"] == "STATE_STALE"
    snap = deepcopy(original)
    apply_pending(app.store, app.read._state, rid, snap)
    assert snap["performance_profile"] == "DEEP" and snap["max_elapsed_sec"] == 600
    assert snap["report_style"] == "technical" and snap["manual_role_override"]
    assert all(value == "m" for value in snap["routing"].values())
    assert app.request("GET", f"/api/control/research/{rid}/report-view").body["report_style"] == "technical"
    assert app.store.run(rid)["snapshot"] == original
    assert effective_snapshot(app.store, rid)["run_limit_usd"] == "2"
    history = projection(app.store, rid)
    assert len(history["history"]) == 2 and history["pending"]["applied"]
    apply_pending(app.store, app.read._state, rid, snap)
    assert len(projection(app.store, rid)["history"]) == 2


def test_semantic_revision_preserves_original_and_requires_new_approval(app):
    rid = product(app)["research_id"]
    original = deepcopy(app.store.run(rid)["snapshot"])
    body = {"expected_version": 0, "question": "새 관계 질문", "analysis_plan": {"dataset_id": "D-existing", "selected_variables": ["x", "y"], "method": "spearman_correlation", "justification": "별도 분석", "requested_tools": ["stats.run"]}}
    response = app.request("POST", f"/api/control/research/{rid}/analysis-plan-revision", body)
    assert response.status == 201
    assert response.body["child_research_id"] != rid and not response.body["approved_for_execution"]
    assert app.store.run(rid)["snapshot"] == original
    assert app.store.run(response.body["child_research_id"])["status"] == "DRAFT"
    assert not app.store.ledger()["requests"]


def test_settings_and_legacy_depth_follow_latest_owner_request(app):
    from types import SimpleNamespace
    rid = product(app)["research_id"]
    app.store.put("depth", rid, {"research_depth": "focused", "applied": True})
    assert effective_snapshot(app.store, rid)["research_depth"] == "focused"
    queue(app.store, rid, settings(app, rid, performance_profile="FAST", max_followups=0))
    snapshot = effective_snapshot(app.store, rid)
    runtime = SimpleNamespace(models={}, context_budget=0, config=None)
    boundary(app.store, app.read._state, rid, runtime, snapshot)
    assert snapshot["research_depth"] == "explore" and runtime.config.max_followups_per_hypothesis == 0
    assert app.store.config("depth", rid)["superseded_by_settings"] == 1
    assert effective_snapshot(app.store, rid)["research_depth"] == "explore"
    app.store.put("depth", rid, {"research_depth": "deep", "applied": False}, expected_revision=1)
    boundary(app.store, app.read._state, rid, runtime, snapshot)
    assert runtime.config.max_followups_per_hypothesis == 2
    assert effective_snapshot(app.store, rid)["research_depth"] == "deep"
    assert effective_snapshot(app.store, rid)["preset_customized"]
    assert not app.store.ledger()["requests"]


@pytest.mark.parametrize("enabled", [False, True])
def test_worker_downgrade_owner_allowlist_same_provider_only(app, enabled):
    paid(app, rate=2)
    r = product(app, run_limit_usd=".43", adaptive_budget=enabled, approved_worker_profiles=["cheap"])
    raw = app.store.config("model", "m")
    raw.update(profile_id="cheap", model_id="cheap-worker", price={**raw["price"], "input_per_million": ".1", "output_per_million": ".1"},
        capabilities={k: CapabilityEvidence(status="SUPPORTED", source="LIVE_CAPABILITY_TEST").model_dump(mode="json") for k in ("text", "structured_output")})
    app.store.put("model", "cheap", ModelProfile.model_validate(raw))
    changed = maybe_select_worker(app.store, r["research_id"], r["snapshot"])
    assert changed is enabled
    assert r["snapshot"]["models"]["manager"]["model_id"] == "manual-id"
    assert r["snapshot"]["models"]["verification_coordinator"]["model_id"] == "manual-id"
    if enabled:
        assert effective_snapshot(app.store, r["research_id"])["models"]["analysis_planner_worker"]["model_id"] == "cheap-worker"


def test_normal_runtime_partial_report_no_skipped_critic(app):
    r = product(app, max_followups=0)
    rid = r["research_id"]
    app.store.db.execute("UPDATE control_runs SET status='STARTING' WHERE research_id=?", (rid,))
    providers = []
    def factory(*_):
        replies = fake_replies()
        def status(call):
            active = json.loads(call["input_text"])["active_state"]
            return {"hypothesis_id": active["active_hypotheses"][0]["hypothesis_id"], "new_status": "INCONCLUSIVE", "rationale": "한 가지 방법의 제한된 근거",
                    "evidence_refs": [{"type": "evidence", "id": x["evidence_id"]} for x in active["verified_evidence"]]}
        p = FakeProvider(replies[:5] + [status, replies[-1]]); providers.append(p); return p
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=factory))
    view = friendly_report(app.read._state, rid)
    assert not view["complete"] and len(view["titles"]) == 9
    assert any(c["role"] == "verification_coordinator" for c in providers[0].calls)
    assert view["numbers"] and "인과" in view["limitations"]["현재 말할 수 없는 것"]
    exported = export_release(app.read._state, rid, app.workspace / "test-export")
    paths = {f["path"] for f in exported["files"]}
    assert {"friendly_report.md", "friendly_report.json", "research_config_history.json"} <= paths
    for f in exported["files"]:
        assert sha256_file(app.workspace / "test-export" / f["path"]) == f["sha256"]


@pytest.mark.parametrize("kind", ["association", "regression", "timeseries", "repair", "inconclusive"])
def test_report_fixture_numeric_provenance_and_tamper(tmp_path, kind):
    if kind == "timeseries":
        from test_verified_analysis_skills import backtest_plan, series_rows, MODELS
        from probe.agent_runtime import AgentRuntime
        from probe.database import initialize
        from probe.service import StateService
        from probe.storage import Workspace
        source = tmp_path / "series.csv"
        data = "time,y\n" + "".join(row["time"] + "," + row["y"] + "\n" for row in series_rows())
        source.write_bytes(data.encode("utf-8", errors="strict"))
        db = initialize(tmp_path / "state.sqlite")
        state = StateService(db, Workspace(tmp_path / "workspace"))
        def coordinator(call):
            active = json.loads(call["input_text"])["active_state"]
            return {"decision_type": "DELEGATE", "objective": "고정 시계열", "rationale": "분리 검증", "assigned_role": "analysis_planner_worker",
                    "input_refs": [{"type": "dataset", "id": active["datasets"][0]["dataset_id"]}, {"type": "artifact", "id": active["profile_artifacts"][0]["artifact_id"]}],
                    "allowed_tools": ["analysis.skill", "evidence.record"], "max_tool_calls": 2, "max_retries": 0, "max_runtime_sec": 120, "max_cost_usd": .5}
        def worker(call):
            dataset = json.loads(call["input_text"])["active_state"]["skill_dataset_hashes"][0]
            plan = backtest_plan(dataset_id=dataset["dataset_id"], dataset_sha256=dataset["sha256"])
            return {"dataset_id": dataset["dataset_id"], "selected_variables": ["time", "y"], "method": plan.method,
                    "justification": "고정 시계열", "requested_tools": ["analysis.skill", "evidence.record"], "skill_plan": plan.model_dump(mode="json")}
        manager = {"decision_type": "INITIAL_PLAN", "research_question": "고정 시계열의 예측은?", "rationale": "재현 검사", "coordinator_role": "experiment_coordinator", "objective": "시계열 분석"}
        agent = AgentRuntime(state, FakeProvider([manager, coordinator, worker]), MODELS, verified_analysis_skills_enabled=True)
        result = asyncio.run(agent.run("고정 시계열의 예측은?", source))
        rid = result["research_id"]
    else:
        db, state, agent, prepared = prepare_case(tmp_path, kind="association" if kind in {"repair", "inconclusive"} else kind)
        rid = prepared["research_id"]
        if kind == "repair": corrupt_first_output(state)
        if kind != "inconclusive": asyncio.run(agent.resume(rid))
    try:
        state.stop_research(rid, "BUDGET_EXHAUSTED")
        export_final_report(state, rid)
        view = friendly_report(state, rid)
        assert not view["complete"]
        assert bool(view["numbers"]) == (kind != "inconclusive")
        assert len(view["titles"]) == 9 and view["ai_narrative"] == "NOT_RUN"
        artifact = state._db.execute("SELECT a.* FROM artifacts a JOIN experiments e ON e.result_artifact_id=a.artifact_id WHERE e.research_id=? AND e.status='VERIFIED'", (rid,)).fetchone()
        if artifact:
            path = state.workspace.path(rid, artifact["relative_path"])
            path.write_bytes(b"{}");
            with pytest.raises(Exception): friendly_report(state, rid)
    finally:
        db.close()


@pytest.mark.parametrize("path", ["/api/control/connections/local/credential?value=KEY-CANARY", "/api/control/connections/local/credential#secret"])
def test_secret_query_never_saved_or_echoed(app, path):
    configure(app)
    for method in ("GET", "POST", "PUT", "DELETE"):
        response = app.request(method, path, {"value": "KEY-CANARY"})
        assert response.status in {400, 405} and "KEY-CANARY" not in json.dumps(response.body)
    assert b"KEY-CANARY" not in app.database.read_bytes()


@pytest.mark.parametrize("case", range(1, 15))
def test_key_canary_covered_paths(tmp_path, monkeypatch, case):
    name, value = "PROBE_QA_KEY", "qa-private-canary-" + str(case) + "-0123456789"
    credentials = Credentials(tmp_path / "repo", tmp_path / "workspace", tmp_path / "private/secrets.env")
    class MemoryStore:
        available = True
        def __init__(self): self.values = {}
        def read(self, name): return self.values.get(name), None
        def write(self, name, value):
            if value is None: self.values.pop(name, None)
            else: self.values[name] = value
    credentials.os_store = MemoryStore()
    credentials.save(name, value)
    assert credentials.get(name) == value and value not in json.dumps(credentials.metadata(name))
    if case in {1, 2, 3}:
        credentials.save(name, value + "-replacement")
        assert credentials.get(name) != value
    elif case == 4:
        credentials.save(name, None); assert credentials.get(name) is None
    elif case in {5, 6}:
        monkeypatch.setenv(name, "environment-canary")
        assert credentials.get(name) == "environment-canary"
        assert value in credentials.active_secrets([name])
        assert credentials.metadata(name)["shadowed_saved_key"]
    elif case == 7:
        from probe.sandbox import clean_environment
        monkeypatch.setenv(name, value); assert value not in json.dumps(clean_environment())
    elif case in {8, 9, 10}:
        token = _PROTECTED_VALUES.set((value,))
        try:
            assert not _secret_free("report.md", value.encode("utf-8"))
            assert not _secret_free("trace.json", json.dumps({"result": value}).encode("utf-8"))
            assert not _secret_free("figure.png", b"\x89PNG\r\n" + value.encode("utf-8"))
        finally: _PROTECTED_VALUES.reset(token)
    elif case == 11:
        with pytest.raises(ControlError): credentials.save(name, value + "\nINJECT=key")
    elif case == 12:
        credentials.file = tmp_path / "workspace/secrets.env"
        with pytest.raises(ControlError): credentials.save(name, value)
    elif case == 13:
        with pytest.raises(ControlError): credentials.save("INVALID/name", value)
    else:
        assert not credentials.file.exists()
    assert value not in json.dumps(credentials.metadata(name))


@pytest.mark.os_secret_integration
@pytest.mark.skipif(os.name != "nt", reason="Windows OS 저장소 검사")
def test_real_windows_store_isolated_canary_roundtrip():
    import secrets
    store = WindowsCredentialStore("Probe-QA-" + secrets.token_hex(8))
    value = "OS-canary-not-a-real-key-123456789"
    try:
        from probe.secret_store import SecretStoreUnavailable
        try:
            store.write("QA_KEY", value)
        except SecretStoreUnavailable as exc:
            if exc.errno in {1312, 50}:
                pytest.skip("현재 Windows 실행 토큰에 자격 증명 저장 세션 없음 · NOT_VALIDATED")
            raise
        found, changed = store.read("QA_KEY")
        assert found == value and changed
        store.write("QA_KEY", value + "-rotated")
        assert store.read("QA_KEY")[0] == value + "-rotated"
        store.write("QA_KEY", None)
        assert store.read("QA_KEY") == (None, None)
    finally: store.write("QA_KEY", None)


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085", "\u00a0"])
def test_unicode_secret_separator_rejected_for_save_environment_and_file(tmp_path, monkeypatch, separator):
    target = tmp_path / "private/secrets.env"
    credentials = Credentials(tmp_path / "repo", tmp_path / "workspace", target)
    value = "unicode-canary" + separator + "INJECTED_KEY=value"
    with pytest.raises(ControlError, match="SECRET_INPUT_INVALID"):
        credentials.save("QA_KEY", value)
    assert not target.exists()
    monkeypatch.setenv("QA_KEY", value)
    with pytest.raises(ControlError, match="SECRET_INPUT_INVALID"):
        credentials.get("QA_KEY")
    monkeypatch.delenv("QA_KEY")
    target.parent.mkdir()
    target.write_bytes(("QA_KEY=" + value + "\n").encode("utf-8", errors="strict"))
    monkeypatch.setattr(credentials, "_secure", lambda *args: None)
    with pytest.raises(ControlError, match="SECRET_FILE_INVALID"):
        credentials.get("QA_KEY")


def test_file_backend_reload_metadata_and_environment_shadow(tmp_path, monkeypatch):
    target = tmp_path / "private/secrets.env"
    target.parent.mkdir()
    value = "file-only-canary-0123456789"
    target.write_bytes(("QA_KEY=" + value + "\n").encode("utf-8", errors="strict"))
    credentials = Credentials(tmp_path / "repo", tmp_path / "workspace", target)
    class EmptyStore:
        available = True
        def read(self, name): return None, None
    credentials.os_store = EmptyStore()
    monkeypatch.setattr(credentials, "_secure", lambda *args: None)
    assert credentials.get("QA_KEY") == value
    info = credentials.metadata("QA_KEY")
    assert info["active_source"] == "protected_file" and info["storage_backend"] == "PROTECTED_PLAINTEXT"
    monkeypatch.setenv("QA_KEY", "env-shadow-canary")
    info = credentials.metadata("QA_KEY")
    assert info["active_source"] == "environment" and info["shadowed_saved_key"]
    assert info["storage_backend"] == "PROTECTED_PLAINTEXT" and value not in json.dumps(info)


def test_os_read_session_unavailable_falls_back_but_permission_error_blocks(tmp_path, monkeypatch):
    if os.name != "nt":
        return
    import ctypes
    from probe.secret_store import SecretStoreUnavailable
    store = WindowsCredentialStore("Probe-QA-read-only")
    class FailingRead:
        def CredReadW(self, *args): return False
    store.dll = FailingRead()
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 1312)
    assert store.read("QA_KEY") == (None, None) and not store.available
    store.available = True
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)
    with pytest.raises(SecretStoreUnavailable) as failure:
        store.read("QA_KEY")
    assert failure.value.errno == 5 and store.available


def test_owner_semantic_request_blocks_different_method_before_tools(tmp_path):
    from probe.service import ContractViolationError
    db, state, agent, prepared = prepare_case(tmp_path)
    rid = prepared["research_id"]
    try:
        dataset = state.dataset_record(prepared["dataset_id"], rid)
        requested = {"dataset_id": prepared["dataset_id"], "selected_variables": ["x", "y"], "method": "spearman_correlation", "justification": "다른 의미의 새 계획", "requested_tools": ["analysis.skill", "evidence.record"]}
        state.finish_runtime_step(rid, "owner_analysis_plan_request", {"plan": requested, "input_sha256": dataset["sha256"]})
        with pytest.raises(ContractViolationError, match="OWNER_ANALYSIS_PLAN_MISMATCH"):
            asyncio.run(agent.resume(rid))
        assert not db.execute("SELECT * FROM experiments WHERE research_id=?", (rid,)).fetchall()
        assert not db.execute("SELECT * FROM staged_mutations WHERE research_id=?", (rid,)).fetchall()
    finally:
        db.close()


def test_insufficient_completion_runtime_checkpoint_partial_report_zero_http(app):
    paid(app)
    r = product(app, run_limit_usd=".01")
    rid, calls = r["research_id"], []
    app.store.db.execute("UPDATE control_runs SET status='STARTING' WHERE research_id=?", (rid,))
    def factory(store, rid, snapshot):
        return RoutedGateway(store, app.credentials, rid, snapshot,
            client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request))))
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=factory))
    assert not calls and not app.store.ledger(rid)["requests"]
    assert app.store.run(rid)["status"] == "BUDGET_BLOCKED"
    assert friendly_report(app.read._state, rid)["stop_reason"] == "BUDGET_EXHAUSTED"
    assert app.read._state.load_runtime_cursor(rid)


def test_windows_junction_secret_path_rejected_before_write(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows junction 검사")
    import subprocess
    from probe.sandbox import clean_environment
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "private-junction"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "New-Item -ItemType Junction -Path $env:QA_JUNCTION_PATH -Target $env:QA_JUNCTION_TARGET | Out-Null"],
        env={**clean_environment(), "QA_JUNCTION_PATH": str(junction), "QA_JUNCTION_TARGET": str(target)}, capture_output=True, timeout=10)
    assert result.returncode == 0
    credentials = Credentials(tmp_path / "repo", tmp_path / "workspace", junction / "secrets.env")
    with pytest.raises(ControlError, match="SECRET_PATH_UNSAFE"):
        credentials.save("QA_KEY", "junction-canary-0123456789")
    assert not (target / "secrets.env").exists()


@pytest.mark.parametrize("changes,expected", [
    ({}, "LOW"),
    ({"model_reasoning": "HIGH"}, "HIGH"),
    ({"model_reasoning": "MAX"}, "MAX"),
    ({"model_reasoning": "AUTO"}, "AUTO"),
    ({"role_reasoning": {"manager": "HIGH"}}, "HIGH"),
])
def test_roomy_budget_keeps_default_reasoning_low_and_preserves_choices(app, changes, expected):
    """충분한 예산과 심화 연구 범위가 추론 수준을 자동으로 올리지 않는다."""
    configure(app)
    raw = app.store.config("model", "m")
    raw.update(reasoning_levels=["LOW", "MEDIUM", "HIGH", "MAX"],
        capabilities={**raw.get("capabilities", {}), "reasoning":
            CapabilityEvidence(status="SUPPORTED", source="STATIC_ADAPTER_RULE").model_dump(mode="json")})
    app.store.put("model", "m", ModelProfile.model_validate(raw), 1)
    snapshot = product(app, settings_version=2, performance_profile="MAX", run_limit_usd="10", **changes)["snapshot"]
    assert snapshot["models"]["manager"]["reasoning_policy"] == expected
    assert snapshot["research_depth"] == "focused"


def test_unverified_low_reasoning_uses_provider_default(app):
    configure(app)
    raw = app.store.config("model", "m")
    raw.update(reasoning_levels=["LOW", "HIGH"], capabilities={})
    app.store.put("model", "m", ModelProfile.model_validate(raw), 1)
    snapshot = product(app, settings_version=2, run_limit_usd="10")["snapshot"]
    assert snapshot["models"]["manager"]["reasoning_policy"] == "AUTO"
