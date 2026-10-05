"""실제 키·HTTP 없이 QA 실행 중 소스 변경과 부분 정산 보존을 검사한다."""
import asyncio
from contextlib import ExitStack
from decimal import Decimal
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "qa"))
import science_live_validation as validation
from probe import preflight
from probe.control_plane import ControlStore
from probe.control_runtime import RoutedGateway
from probe.database import initialize
from probe.providers.normalized import GenerationRequest, GenerationResult, GenerationUsage


def fixed_state():
    return {"product_source_fingerprint": "product-a", "qa_fingerprint": "qa-a", "qa_files": {"qa/example.py": "a"}}


@pytest.mark.parametrize("change", ["product", "python", "browser", "added", "removed"])
def test_source_guard_pins_product_and_recursive_qa_code(tmp_path, monkeypatch, change):
    folder = tmp_path / "qa" / "nested"
    folder.mkdir(parents=True)
    py, cjs = folder / "case.py", folder / "case.cjs"
    validation.write_text(py, "# 초기 모의 코드\n")
    validation.write_text(cjs, "// 초기 모의 코드\n")
    product = ["product-a"]
    monkeypatch.setattr(validation, "ROOT", tmp_path)
    monkeypatch.setattr(preflight, "_source_fingerprint", lambda: product[0])
    guard = validation.ValidationSourceGuard()
    guard.check("UNCHANGED")
    assert set(guard.baseline["qa_files"]) == {"qa/nested/case.py", "qa/nested/case.cjs"}
    if change == "product": product[0] = "product-b"
    elif change == "python": validation.write_text(py, "# 바뀐 모의 코드\n")
    elif change == "browser": validation.write_text(cjs, "// 바뀐 모의 코드\n")
    elif change == "added": validation.write_text(folder / "new.py", "# 추가 모의 코드\n")
    else: py.unlink()
    with pytest.raises(ValueError, match="SOURCE_CHANGED"):
        guard.check("MODEL_GENERATE")
    assert guard.metadata()["failure"]["boundary"] == "MODEL_GENERATE"
    assert guard.metadata()["status"] == "SOURCE_CHANGED"


@pytest.mark.parametrize("boundary", ["initial", "later"])
def test_source_guard_read_failure_blocks_without_recording_error_body(monkeypatch, boundary):
    def unavailable(): raise OSError("private-error-body-canary")
    monkeypatch.setattr(validation, "_validation_source_state", fixed_state)
    if boundary == "initial": monkeypatch.setattr(validation, "_validation_source_state", unavailable)
    guard = validation.ValidationSourceGuard()
    monkeypatch.setattr(validation, "_validation_source_state", unavailable)
    with pytest.raises(ValueError, match="SOURCE_CHANGED"):
        guard.check("MODEL_GENERATE")
    assert guard.failure["reason"] == "SOURCE_GUARD_UNAVAILABLE"
    assert "private-error-body-canary" not in json.dumps(guard.metadata())


def test_guarded_generate_preserves_original_result_and_blocks_next_call(monkeypatch):
    state, calls = fixed_state(), []
    monkeypatch.setattr(validation, "_validation_source_state", lambda: dict(state))
    result = GenerationResult(model_id="gpt-6-luna", output_text="완료", usage=GenerationUsage(input_tokens=7, output_tokens=3))
    async def original(gateway, request, *, output_type=None):
        calls.append(request)
        state["product_source_fingerprint"] = "product-b"
        return result, None, 1
    monkeypatch.setattr(RoutedGateway, "generate", original)
    guard = validation.ValidationSourceGuard()
    async def scenario():
        gateway = object.__new__(RoutedGateway)
        first = await gateway.generate("first")
        assert first[0] is result and first[0].usage.input_tokens == 7
        with pytest.raises(ValueError, match="SOURCE_CHANGED"):
            await gateway.generate("second")
    with ExitStack() as cleanup:
        guard.install(cleanup)
        asyncio.run(scenario())
    assert calls == ["first"] and RoutedGateway.generate is original


def test_changed_source_blocks_eligible_native_release_proof(monkeypatch, tmp_path):
    state = fixed_state()
    monkeypatch.setattr(validation, "_validation_source_state", lambda: dict(state))
    guard = validation.ValidationSourceGuard()
    summary = {"execution": "LIVE_USER_SESSION", "live_paid_calls": 6,
        "ledger": {"reserved": "0", "unresolved": "0", "spent": "0.1"},
        "runs": [{"case": "literature", "category": "core", "case_requirements_met": True} for _ in range(6)]}
    calls = []
    monkeypatch.setattr(preflight, "record_search_validation", lambda **kwargs: calls.append(kwargs))
    state["qa_fingerprint"] = "qa-b"
    validation.record_completed_native_search(tmp_path, summary, source_guard=guard)
    assert summary["status"] == "SOURCE_CHANGED" and summary["native_search_validation"] == "NOT_VALIDATED"
    assert summary["live_paid_calls"] == 6 and summary["ledger"]["spent"] == "0.1" and not calls


@pytest.mark.parametrize("change_phase", ["during_research", "after_metrics", "before_release"])
def test_validation_keeps_settled_usage_and_stops_after_source_change(tmp_path, monkeypatch, change_phase):
    state, calls, executions, budget_closed = fixed_state(), [], [], []
    monkeypatch.setattr(validation, "_validation_source_state", lambda: dict(state))
    connection, profile, _ = validation.mock_settings()
    monkeypatch.setattr(validation, "source_settings", lambda *_: (connection, profile, {"mode": "MOCK_CONFIG", "explicit_model_limits": {}}))
    monkeypatch.setattr(validation, "Credentials", lambda *_: SimpleNamespace(get=lambda *_: "offline-placeholder", active_secrets=lambda *_: ()))
    class OfflineBudget:
        available_usd = Decimal("3")
        metadata = {"mode": "OFFLINE_BUDGET_FIXTURE"}
        def __init__(self, *args): pass
        def acquire(self): pass
        def close(self): budget_closed.append(True)
    monkeypatch.setattr(validation, "LiveValidationBudget", OfflineBudget)
    monkeypatch.setattr(validation, "CASES", (validation.CASES[0],))
    async def original_generate(gateway, request, *, output_type=None):
        calls.append(request.request_id)
        reservation = gateway.store.reserve(rid=gateway.rid, connection=connection.connection_id, model="gpt-6-luna", role="manager",
            purpose="research", bound="0.01", run_limit="0.1", monthly_limit="3", request_limit="0.1", attempts=20, revision="offline-source-guard")
        gateway.store.transition(reservation, "DISPATCHED")
        result = GenerationResult(model_id="gpt-6-luna", response_id="offline-"+gateway.rid, output_text="모의 완료",
            usage=GenerationUsage(input_tokens=7, output_tokens=3))
        trace = {"response_id": result.response_id, "usage": result.usage.model_dump(mode="json"), "requested_model_id": result.model_id}
        gateway.store.settle_output(reservation, "cache-"+gateway.rid, result.model_id, result, "0.005", response_id=result.response_id, trace=trace)
        if change_phase == "during_research": state["product_source_fingerprint"] = "product-b"
        return result, None, 1
    monkeypatch.setattr(RoutedGateway, "generate", original_generate)
    async def execute_offline(database, workspace, rid, **kwargs):
        executions.append(rid)
        db = initialize(database)
        store = ControlStore(db)
        snapshot = store.run(rid)["snapshot"]
        gateway = RoutedGateway(store, SimpleNamespace(), rid, snapshot)
        request = GenerationRequest(request_id="first", research_id=rid, role="manager", model_profile_id="science_luna", input_text="모의 입력")
        try:
            await gateway.generate(request)
            if change_phase == "during_research":
                with pytest.raises(ValueError, match="SOURCE_CHANGED"):
                    await gateway.generate(request.model_copy(update={"request_id": "second"}))
            db.execute("UPDATE control_runs SET status='COMPLETED' WHERE research_id=?", (rid,))
        finally:
            db.close()
    monkeypatch.setattr(validation, "execute", execute_offline)
    monkeypatch.setattr(validation, "execute_direct_single", execute_offline)
    original_metrics = validation.metrics
    def metrics(*args, **kwargs):
        result = original_metrics(*args, **kwargs)
        if change_phase != "before_release":
            result[0].update(finished=True, case_requirements_met=True)
            result[1]["requirements_met"] = True
        if change_phase == "after_metrics": state["qa_fingerprint"] = "qa-b"
        return result
    monkeypatch.setattr(validation, "metrics", metrics)
    original_record = validation.record_completed_native_search
    def record(*args, **kwargs):
        if change_phase == "before_release": state["qa_fingerprint"] = "qa-b"
        return original_record(*args, **kwargs)
    monkeypatch.setattr(validation, "record_completed_native_search", record)
    release_calls = []
    monkeypatch.setattr(preflight, "record_search_validation", lambda **kwargs: release_calls.append(kwargs))
    folder = tmp_path / "session"
    folder.mkdir()
    summary = asyncio.run(validation.run_validation(folder, tmp_path / "never-opened-original.sqlite", smoke=False))
    expected = 12 if change_phase == "before_release" else 1
    assert summary["status"] == "SOURCE_CHANGED" and summary["blocked_reason"] == "SOURCE_CHANGED"
    assert len(executions) == len(calls) == expected and summary["live_paid_calls"] == expected
    assert Decimal(summary["ledger"]["spent"]) == Decimal("0.005") * expected
    assert summary["ledger"]["reserved"] == summary["ledger"]["unresolved"] == "0"
    assert not release_calls and budget_closed and RoutedGateway.generate is original_generate
    first = summary["runs"][0]
    raw = json.loads((folder / "raw" / (first["research_id"] + ".json")).read_text(encoding="utf-8", errors="strict"))
    assert raw["provider_measurements"][0]["usage"]["input_tokens"] == 7
    assert raw["provider_measurements"][0]["usage"]["output_tokens"] == 3
    if change_phase != "before_release":
        assert not first["finished"] and not first["case_requirements_met"] and not raw["requirements_met"]


@pytest.mark.parametrize("boundary", ["credential", "price", "budget"])
def test_source_guard_preserves_existing_live_block_and_budget_cleanup(tmp_path, monkeypatch, boundary):
    monkeypatch.setattr(validation, "_validation_source_state", fixed_state)
    connection, profile, _ = validation.mock_settings()
    if boundary == "price": profile.price.owner_verified = False
    monkeypatch.setattr(validation, "source_settings", lambda *_: (connection, profile, {"mode": "MOCK_CONFIG"}))
    monkeypatch.setattr(validation, "Credentials", lambda *_: SimpleNamespace(get=lambda *_: None if boundary == "credential" else "offline-placeholder"))
    closed = []
    class BlockedBudget:
        metadata = {"mode": "OFFLINE_BUDGET_FIXTURE"}
        def __init__(self, *args): pass
        def acquire(self): raise ValueError("GLOBAL_BUDGET_STOPPED")
        def close(self): closed.append(True)
    monkeypatch.setattr(validation, "LiveValidationBudget", BlockedBudget)
    release_calls = []
    monkeypatch.setattr(preflight, "record_search_validation", lambda **kwargs: release_calls.append(kwargs))
    folder = tmp_path / "session"
    folder.mkdir()
    summary = asyncio.run(validation.run_validation(folder, tmp_path / "never-opened-original.sqlite"))
    assert summary["status"] == "BLOCKED"
    assert summary["blocked_reason"] == {"credential": "CREDENTIAL_UNCONFIGURED", "price": "PRICE_UNKNOWN", "budget": "GLOBAL_BUDGET_STOPPED"}[boundary]
    assert summary["live_paid_calls"] == 0 and not summary["runs"] and not release_calls
    assert summary["source_guard"]["status"] == "UNCHANGED"
    assert bool(closed) == (boundary == "budget")


def test_initial_guard_failure_prevents_settings_and_credential_lookup(tmp_path, monkeypatch):
    def unavailable(): raise OSError("읽기 실패")
    def forbidden(*args, **kwargs): raise AssertionError("설정·키 조회 금지")
    monkeypatch.setattr(validation, "_validation_source_state", unavailable)
    monkeypatch.setattr(validation, "source_settings", forbidden)
    monkeypatch.setattr(validation, "Credentials", forbidden)
    folder = tmp_path / "session"
    folder.mkdir()
    summary = asyncio.run(validation.run_validation(folder, tmp_path / "never-opened-original.sqlite"))
    assert summary["status"] == "SOURCE_CHANGED" and summary["live_paid_calls"] == 0 and not summary["runs"]
    assert summary["source_guard"]["failure"]["reason"] == "SOURCE_GUARD_UNAVAILABLE"


def test_unchanged_source_keeps_eligible_native_marker_path(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "_validation_source_state", fixed_state)
    monkeypatch.setattr(validation, "ROOT", tmp_path)
    guard = validation.ValidationSourceGuard()
    folder = tmp_path / "session"
    summary = {"execution": "LIVE_USER_SESSION", "live_paid_calls": 6,
        "ledger": {"reserved": "0", "unresolved": "0", "spent": "0.1"},
        "runs": [{"research_id": str(index), "raw_sha256": "a"*64, "case": "literature", "category": "core", "case_requirements_met": True} for index in range(6)]}
    calls = []
    def record(**kwargs):
        calls.append(kwargs)
        return {"validated": True}
    monkeypatch.setattr(preflight, "record_search_validation", record)
    validation.record_completed_native_search(folder, summary, source_guard=guard)
    assert summary["native_search_validation"] == "VALIDATED" and len(calls) == 1
    assert calls[0]["provider"] == "openai.web_search" and len(calls[0]["evidence"]["runs"]) == 6


def test_queue_wait_rechecks_source_before_dispatch(monkeypatch):
    from contextlib import asynccontextmanager
    import probe.resource_queue as resource_queue
    state, dispatches = fixed_state(), []
    monkeypatch.setattr(validation, "_validation_source_state", lambda: dict(state))
    connection, profile, _ = validation.mock_settings()
    db = initialize(":memory:")
    store = ControlStore(db)
    snapshot = {"execution_mode": "SCIENCE_AUTO", "models": {"manager": profile.model_dump(mode="json")},
        "connections": {connection.connection_id: connection.model_dump(mode="json")}}
    class WaitingPool:
        def __init__(self, *args): pass
        @asynccontextmanager
        async def lease(self, *args, **kwargs):
            await asyncio.sleep(0)
            state["product_source_fingerprint"] = "product-b"
            yield {}
    monkeypatch.setattr(resource_queue, "ResourcePool", WaitingPool)
    async def dispatch(gateway, request, **kwargs):
        dispatches.append(request)
        raise AssertionError("큐 대기 중 변경 후 전송 금지")
    monkeypatch.setattr(RoutedGateway, "_generate", dispatch)
    guard = validation.ValidationSourceGuard()
    gateway = RoutedGateway(store, SimpleNamespace(), "queue", snapshot)
    request = GenerationRequest(request_id="queued", research_id="queue", role="manager", model_profile_id=profile.profile_id, input_text="모의 입력")
    with ExitStack() as cleanup:
        guard.install(cleanup)
        with pytest.raises(ValueError, match="SOURCE_CHANGED"):
            asyncio.run(gateway.generate(request))
    assert not dispatches and guard.failure["boundary"] == "MODEL_DISPATCH_READY"
    assert store.db.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0] == 0
    assert RoutedGateway._generate is dispatch
    db.close()
