"""기존 동작과 검증 경계를 확인하는 회귀 테스트."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from htrsa.agent_policy import BudgetExceededError, Price, PricingRegistry, UnknownPriceLimits
from htrsa.agent_runtime import AgentRuntime, RuntimeFailure
from htrsa.agent_schemas import ManagerDecision
from htrsa.database import initialize
from htrsa.mock import MockAnalysisTool, MockExperimentCoordinator, MockManager, MockWorker
from htrsa.providers.base import ModelProviderError
from htrsa.providers.fake import FakeProvider
from htrsa.providers.openai_agents import OpenAIAgentsProvider, normalize_openai_error
from htrsa.real_tools import StatsTool
from htrsa.schemas import StagedResult, ToolResult
from htrsa.service import ContractViolationError, StateService
from htrsa.storage import Workspace


CSV = Path(__file__).parent / "fixtures" / "temperature_growth.csv"
MODELS = {"manager": "test-manager", "experiment_coordinator": "test-coordinator",
          "analysis_planner_worker": "test-worker"}
MANAGER = {"decision_type": "INITIAL_PLAN", "research_question": "Is temperature associated with growth?",
           "rationale": "Use the provided dataset and deterministic statistics.",
           "coordinator_role": "experiment_coordinator", "objective": "Analyze temperature and growth."}


def coordinator_reply(call: dict) -> dict:
    active = json.loads(call["input_text"])["active_state"]
    dataset_id = active["datasets"][0]["dataset_id"]
    profile_id = active["profile_artifacts"][0]["artifact_id"]
    return {"decision_type": "DELEGATE", "objective": "Plan one association analysis.",
            "rationale": "The profiled columns are suitable for correlation.",
            "assigned_role": "analysis_planner_worker",
            "input_refs": [{"type": "dataset", "id": dataset_id},
                           {"type": "artifact", "id": profile_id}],
            "allowed_tools": ["stats.run", "visualization.render", "evidence.record"],
            "max_tool_calls": 3, "max_retries": 1, "max_runtime_sec": 120, "max_cost_usd": 0.5}


def worker_reply(call: dict) -> dict:
    refs = json.loads(call["input_text"])["active_state"]["contract"]["inputs"]
    dataset_id = next(ref["id"] for ref in refs if ref["type"] == "dataset")
    return {"dataset_id": dataset_id, "selected_variables": ["temperature", "growth"],
            "method": "pearson_correlation", "justification": "Both columns are numeric.",
            "requested_tools": ["stats.run", "visualization.render", "evidence.record"],
            "reported_r": 0.5}


def runtime(tmp_path, replies):
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    provider = FakeProvider(replies)
    return AgentRuntime(state, provider, MODELS), state, db, provider


def test_fake_provider_scientific_e2e_ignores_forged_number(tmp_path):
    agent, state, db, provider = runtime(tmp_path, [MANAGER, coordinator_reply, worker_reply])
    result = asyncio.run(agent.run("Analyze temperature and growth.", CSV))
    assert result["verdict"] == "PASS"
    assert result["state_version"] == 1
    assert result["result"]["estimate"] != 0.5
    assert state.artifact(result["result_artifact_id"])["output"] == result["result"]
    assert [call["role"] for call in provider.calls] == ["manager", "experiment_coordinator",
                                                    "analysis_planner_worker"]
    db.close()


def test_openai_adapter_structured_output_and_usage(monkeypatch):
    import agents

    async def fake_run(agent, input_text):
        assert agent.output_type is ManagerDecision
        assert agent.model == "configured-model"
        usage = SimpleNamespace(requests=2, input_tokens=100, output_tokens=30,
                                input_tokens_details=SimpleNamespace(cached_tokens=20),
                                output_tokens_details=SimpleNamespace(reasoning_tokens=5))
        return SimpleNamespace(final_output=ManagerDecision.model_validate(MANAGER),
                               context_wrapper=SimpleNamespace(usage=usage), last_response_id="rsp-1")

    monkeypatch.setattr(agents.Runner, "run", fake_run)
    result = asyncio.run(OpenAIAgentsProvider().run_structured(
        role="manager", instructions="bounded", input_text="goal", output_type=ManagerDecision,
        model="configured-model"))
    assert result.output.research_question == MANAGER["research_question"]
    assert result.usage() | {"provider": result.provider} == {
        "request_count": 2, "input_tokens": 100, "cached_input_tokens": 20,
        "output_tokens": 30, "reasoning_tokens": 5, "latency_ms": result.latency_ms,
        "provider_run_id": "rsp-1", "provider": "openai"}


@pytest.mark.parametrize("exc,code", [
    (TimeoutError(), "PROVIDER_TIMEOUT"),
    (type("RateLimitError", (Exception,), {})(), "PROVIDER_RATE_LIMIT"),
    (type("AuthenticationError", (Exception,), {})(), "PROVIDER_AUTH"),
    (Exception("network details must not persist"), "PROVIDER_ERROR"),
])
def test_provider_error_normalization(exc, code):
    assert normalize_openai_error(exc).code == code


def test_manager_coordinator_contracts_and_checkpoint(tmp_path):
    agent, state, db, _ = runtime(tmp_path, [MANAGER, coordinator_reply])
    prepared = asyncio.run(agent.prepare("Analyze temperature and growth.", CSV))
    roles = [row["assigned_role"] for row in db.execute("SELECT assigned_role FROM tasks ORDER BY rowid")]
    assert roles == ["manager", "experiment_coordinator", "analysis_planner_worker"]
    assert prepared["stage"] == "WORKER_READY"
    checkpoint = state.latest_checkpoint(prepared["research_id"])
    assert checkpoint["cursor"]["active_task_ids"] == prepared["active_task_ids"]
    assert checkpoint["state_version"] == 0
    assert db.execute("SELECT COUNT(*) FROM agent_runs WHERE provider='fake' AND status='COMPLETED'").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    db.close()


def test_invalid_generated_contract_rejected(tmp_path):
    def bad(call):
        draft = coordinator_reply(call)
        draft["max_tool_calls"] = 11
        return draft

    agent, state, db, _ = runtime(tmp_path, [MANAGER, bad])
    with pytest.raises(ContractViolationError, match="hard cap"):
        asyncio.run(agent.prepare("Analyze temperature and growth.", CSV))
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 2
    db.close()


def test_disallowed_tool_and_stale_ref_rejected(tmp_path):
    def bad_tool(call):
        draft = coordinator_reply(call)
        draft["allowed_tools"].append("python.execute")
        return draft

    agent, state, db, _ = runtime(tmp_path / "tool", [MANAGER, bad_tool])
    with pytest.raises(ContractViolationError, match="TOOL_NOT_ALLOWED"):
        asyncio.run(agent.prepare("Analyze temperature and growth.", CSV))
    db.close()

    def stale_ref(call):
        draft = coordinator_reply(call)
        draft["input_refs"].append({"type": "artifact", "id": "ART-stale"})
        return draft

    agent, state, db, _ = runtime(tmp_path / "ref", [MANAGER, stale_ref])
    with pytest.raises(ContractViolationError, match="stale or unresolved ref"):
        asyncio.run(agent.prepare("Analyze temperature and growth.", CSV))
    db.close()


@pytest.mark.parametrize("replies,code", [
    ([ModelProviderError("PROVIDER_TIMEOUT", True), ModelProviderError("PROVIDER_TIMEOUT", True)], "PROVIDER_TIMEOUT"),
    ([ModelProviderError("PROVIDER_RATE_LIMIT", True), ModelProviderError("PROVIDER_RATE_LIMIT", True)], "PROVIDER_RATE_LIMIT"),
    ([{"unexpected": "shape"}, {"unexpected": "shape"}], "STRUCTURED_OUTPUT_INVALID"),
    ([None, None], "STRUCTURED_OUTPUT_INVALID"),
    ([RuntimeError("secret details"), RuntimeError("secret details")], "PROVIDER_ERROR"),
])
def test_fake_provider_failures_are_bounded_and_noncanonical(tmp_path, replies, code):
    agent, state, db, provider = runtime(tmp_path, replies)
    with pytest.raises(RuntimeFailure) as caught:
        asyncio.run(agent.prepare("Analyze temperature and growth.", CSV))
    assert caught.value.code == code
    assert len(provider.calls) == 2
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    assert [row[0] for row in db.execute("SELECT error_code FROM agent_runs")] == [code, code]
    assert state.budget(db.execute("SELECT research_id FROM research_runs").fetchone()[0])["manager_calls"] == 2
    db.close()


def test_retry_exhaustion_reaches_coordinator_without_manager_recall(tmp_path):
    replies = [MANAGER, coordinator_reply,
               ModelProviderError("PROVIDER_TIMEOUT", True), ModelProviderError("PROVIDER_TIMEOUT", True),
               coordinator_reply]
    agent, state, db, provider = runtime(tmp_path, replies)
    prepared = asyncio.run(agent.prepare("Analyze temperature and growth.", CSV))
    with pytest.raises(RuntimeFailure, match="PROVIDER_TIMEOUT"):
        asyncio.run(agent.resume(prepared["research_id"]))
    assert [call["role"] for call in provider.calls].count("manager") == 1
    assert [call["role"] for call in provider.calls].count("experiment_coordinator") == 2
    assert state.budget(prepared["research_id"])["manager_calls"] == 1
    assert db.execute("SELECT COUNT(*) FROM runtime_events WHERE event_type='ESCALATION_L2'").fetchone()[0] >= 1
    assert state.state_version(prepared["research_id"]) == 0
    assert state.latest_checkpoint(prepared["research_id"])["cursor"]["replans"] == 1
    db.close()


def test_coordinator_replan_can_resume_with_new_worker_contract(tmp_path):
    replies = [MANAGER, coordinator_reply,
               ModelProviderError("PROVIDER_TIMEOUT", True), ModelProviderError("PROVIDER_TIMEOUT", True),
               coordinator_reply, worker_reply]
    agent, state, db, _ = runtime(tmp_path, replies)
    prepared = asyncio.run(agent.prepare("Analyze temperature and growth.", CSV))
    with pytest.raises(RuntimeFailure, match="PROVIDER_TIMEOUT"):
        asyncio.run(agent.resume(prepared["research_id"]))
    result = asyncio.run(agent.resume(prepared["research_id"]))
    assert result["verdict"] == "PASS"
    assert state.budget(prepared["research_id"])["manager_calls"] == 1
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE assigned_role='analysis_planner_worker' AND status='FAILED'").fetchone()[0] == 1
    db.close()


def test_transient_stats_tool_failure_retries_without_manager_recall(tmp_path, monkeypatch):
    def more_calls(call):
        draft = coordinator_reply(call)
        draft["max_tool_calls"] = 4
        return draft

    agent, state, db, _ = runtime(tmp_path, [MANAGER, more_calls, worker_reply])
    prepared = asyncio.run(agent.prepare("Analyze temperature and growth.", CSV))
    original = StatsTool.run
    attempts = 0

    def fail_once(self, request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return ToolResult(ok=False, request_id=request.request_id, tool_name=request.tool_name,
                              error="transient tool error")
        return original(self, request)

    monkeypatch.setattr(StatsTool, "run", fail_once)
    result = asyncio.run(agent.resume(prepared["research_id"]))
    assert result["verdict"] == "PASS"
    assert attempts == 2
    assert state.budget(prepared["research_id"])["manager_calls"] == 1
    assert db.execute("SELECT COUNT(*) FROM runtime_events WHERE event_type='RETRY_SCHEDULED'").fetchone()[0] == 1
    db.close()


def test_strategic_contradiction_calls_manager(tmp_path):
    agent, state, db, provider = runtime(tmp_path, [MANAGER, coordinator_reply,
                                                  {**MANAGER, "decision_type": "REPLAN"}])
    prepared = asyncio.run(agent.prepare("Analyze temperature and growth.", CSV))
    decision = asyncio.run(agent.escalate(prepared["research_id"], "CANONICAL_CONTRADICTION"))
    assert decision.decision_type == "REPLAN"
    assert state.budget(prepared["research_id"])["manager_calls"] == 2
    assert db.execute("SELECT COUNT(*) FROM runtime_events WHERE event_type='ESCALATION_L3'").fetchone()[0] == 1
    db.close()


def test_soft_budget_warns_and_hard_budget_blocks(tmp_path):
    prices = {("fake", "test-manager"): Price(10_000, 10_000, 10_000, 0.1),
              ("fake", "test-coordinator"): Price(10_000, 10_000, 10_000, 0.1),
              ("fake", "test-worker"): Price(10_000, 10_000, 10_000, 0.8)}
    agent, state, db, _ = runtime(tmp_path, [MANAGER, coordinator_reply, worker_reply])
    agent.pricing = PricingRegistry(prices)
    agent.budget.pricing = agent.pricing
    prepared = asyncio.run(agent.prepare("Analyze temperature and growth.", CSV,
                                         target_usd=0.1, soft_limit_usd=0.15, hard_limit_usd=1.0))
    assert state.budget(prepared["research_id"])["spent_usd"] == pytest.approx(0.4)
    assert db.execute("SELECT COUNT(*) FROM runtime_events WHERE event_type='BUDGET_SOFT_LIMIT'").fetchone()[0] >= 1
    with pytest.raises(BudgetExceededError):
        asyncio.run(agent.resume(prepared["research_id"]))
    assert state.state_version(prepared["research_id"]) == 0
    assert db.execute("SELECT COUNT(*) FROM runtime_events WHERE event_type='BUDGET_HARD_LIMIT'").fetchone()[0] == 1
    db.close()


@pytest.mark.parametrize("limits", [
    UnknownPriceLimits(max_calls=1),
    UnknownPriceLimits(max_input_tokens=15, reserve_input_tokens=10),
    UnknownPriceLimits(max_output_tokens=15, reserve_output_tokens=10),
])
def test_unknown_price_call_and_token_caps_block_new_invocation(tmp_path, limits):
    agent, state, db, provider = runtime(tmp_path, [MANAGER, coordinator_reply])
    agent.budget.unknown_limits = limits
    with pytest.raises(BudgetExceededError):
        asyncio.run(agent.prepare("Analyze temperature and growth.", CSV))
    rid = db.execute("SELECT research_id FROM research_runs").fetchone()[0]
    budget = state.budget(rid)
    assert budget["unknown_price_calls"] == 1
    assert budget["spent_usd"] == 0
    assert db.execute("SELECT estimated_cost_usd FROM agent_runs WHERE provider='fake'").fetchone()[0] is None
    assert db.execute("SELECT COUNT(*) FROM runtime_events WHERE event_type='BUDGET_HARD_LIMIT'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    db.close()


def test_restart_resume_and_no_duplicate_tool_effect(tmp_path):
    agent, state, db, _ = runtime(tmp_path, [MANAGER, coordinator_reply])
    prepared = asyncio.run(agent.prepare("Analyze temperature and growth.", CSV))
    research_id = prepared["research_id"]
    before = db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    assert before == 2
    db.close()
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    restored = AgentRuntime(state, FakeProvider([worker_reply]), MODELS)
    result = asyncio.run(restored.resume(research_id))
    assert result["state_version"] == 1
    assert db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 5
    assert asyncio.run(restored.resume(research_id))["already_completed"] is True
    assert db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 5
    assert state.latest_checkpoint(research_id)["cursor"]["stage"] == "COMPLETED"
    db.close()


def test_resume_replays_completed_tool_without_side_effect(tmp_path):
    agent, state, db, _ = runtime(tmp_path, [MANAGER, coordinator_reply])
    prepared = asyncio.run(agent.prepare("Analyze temperature and growth.", CSV))
    worker, task_id = state.contract(prepared["active_contract_ids"][1])
    agent._dispatch(agent._registry(worker.contract_id), worker, task_id, "stats.run",
                    {"dataset_id": prepared["dataset_id"], "method": "pearson_correlation",
                     "variables": {"x": "temperature", "y": "growth"}, "parameters": {}})
    db.close()
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    restored = AgentRuntime(state, FakeProvider([worker_reply]), MODELS)
    result = asyncio.run(restored.resume(prepared["research_id"]))
    assert result["verdict"] == "PASS"
    assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name='stats.run'").fetchone()[0] == 1
    db.close()


def test_resume_recovers_commit_before_completion_checkpoint(tmp_path, monkeypatch):
    agent, state, db, _ = runtime(tmp_path, [MANAGER, coordinator_reply, worker_reply])
    prepared = asyncio.run(agent.prepare("Analyze temperature and growth.", CSV))
    original = state.checkpoint

    def interrupted_checkpoint(research_id, reason, cursor=None):
        if reason == "committed":
            raise RuntimeError("simulated process stop")
        return original(research_id, reason, cursor)

    monkeypatch.setattr(state, "checkpoint", interrupted_checkpoint)
    with pytest.raises(RuntimeError, match="simulated process stop"):
        asyncio.run(agent.resume(prepared["research_id"]))
    assert state.state_version(prepared["research_id"]) == 1
    tool_count = db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    db.close()
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    restored = AgentRuntime(state, FakeProvider([]), MODELS)
    result = asyncio.run(restored.resume(prepared["research_id"]))
    assert result["already_completed"] is True
    assert result["verdict"] == "PASS"
    assert db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == tool_count
    db.close()


def _commit_competing_result(state, research_id):
    parent = MockManager().create_contract(research_id, "Competing valid result")
    parent_task = state.issue_contract(parent)
    worker_contract = MockExperimentCoordinator().create_contract(parent, parent_task)
    task_id = state.issue_contract(worker_contract)
    worker = MockWorker()
    request = worker.make_request(worker_contract, task_id, [1, 2, 3], [2, 4, 6])
    tool = MockAnalysisTool().run(request)
    state.record_execution(worker_contract.contract_id, worker.role, request, tool)
    mutation_id = state.stage(StagedResult(agent_result=worker.consume(worker_contract, tool),
                                           tool_request=request, tool_result=tool))
    assert state.verify(mutation_id).verdict.value == "PASS"
    state.commit(mutation_id)


def test_stale_agent_result_rejected_after_valid_competing_commit(tmp_path):
    agent, state, db, _ = runtime(tmp_path, [MANAGER, coordinator_reply])
    prepared = asyncio.run(agent.prepare("Analyze temperature and growth.", CSV))

    def delayed_worker(call):
        _commit_competing_result(state, prepared["research_id"])
        return worker_reply(call)

    agent.provider.replies.extend([delayed_worker, coordinator_reply, coordinator_reply])
    with pytest.raises(RuntimeFailure, match="STALE_AGENT_RESULT"):
        asyncio.run(agent.resume(prepared["research_id"]))
    assert state.state_version(prepared["research_id"]) == 1
    assert db.execute("SELECT COUNT(*) FROM runtime_events WHERE event_type='STALE_AGENT_RESULT'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name='stats.run'").fetchone()[0] == 0
    db.close()
