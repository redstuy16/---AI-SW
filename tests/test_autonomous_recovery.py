"""기존 동작과 검증 경계를 확인하는 회귀 테스트."""
from __future__ import annotations

import asyncio
import json

import pytest

from htrsa.autonomous_loop import AutonomousResearchLoop
from htrsa.agent_runtime import RuntimeFailure
from htrsa.agent_policy import UnknownPriceLimits
from htrsa.agent_schemas import ManagerDecision
from htrsa.database import initialize
from htrsa.providers.base import ModelProviderError
from htrsa.providers.fake import FakeProvider
from htrsa.recovery import FaultInjector, InjectedCrash, ResearchRuntimeCursor
from htrsa.service import LoopDetectedError, StateConflictError, StateService
from htrsa.storage import Workspace

from test_autonomous_loop import CSV, MANAGER, MODELS, fake_replies


def runtime(tmp_path, replies, fault=None):
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    provider = FakeProvider(replies)
    injector = FaultInjector(lambda point: (_ for _ in ()).throw(InjectedCrash(point))
                             if point == fault else None)
    return db, state, provider, AutonomousResearchLoop(state, provider, MODELS, faults=injector)


def crashed_run(tmp_path, fault):
    replies = fake_replies()
    db, state, provider, loop = runtime(tmp_path, replies, fault)
    with pytest.raises(InjectedCrash, match=fault):
        asyncio.run(loop.run("Analyze temperature and growth without inferring causality.", CSV))
    research_id = db.execute("SELECT research_id FROM research_runs").fetchone()[0]
    consumed = len(provider.calls)
    db.close()
    return research_id, replies, consumed


@pytest.mark.parametrize("fault", ["AFTER_INTAKE_IMPORT", "AFTER_INTAKE_PROFILE",
                                   "BEFORE_TOOL", "AFTER_TOOL", "AFTER_STAGE", "AFTER_VERIFY",
                                   "AFTER_COMMIT", "AFTER_CRITIC", "AFTER_REPLAN",
                                   "AFTER_FOLLOWUP_CONTRACT", "AFTER_HYPOTHESIS"])
def test_each_crash_boundary_resumes_without_duplicate_effects(tmp_path, fault):
    research_id, replies, consumed = crashed_run(tmp_path, fault)
    db, state, provider, loop = runtime(tmp_path, replies[consumed:])
    result = asyncio.run(loop.resume(research_id))
    assert result["stop_reason"] == "GOAL_ANSWERED"
    assert result["action_count"] == 15
    assert db.execute("SELECT COUNT(*) FROM experiments WHERE research_id=?", (research_id,)).fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM evidence WHERE research_id=?", (research_id,)).fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?", (research_id,)).fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name IN ('stats.run','visualization.render','evidence.record')").fetchone()[0] == 6
    assert db.execute("SELECT COUNT(*) FROM datasets WHERE research_id=?", (research_id,)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name IN ('data.import','data.profile')").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM critic_reviews WHERE research_id=?", (research_id,)).fetchone()[0] == 2
    assert state.load_runtime_cursor(research_id).stage == "STOPPED"
    assert state.budget(research_id)["unknown_price_calls"] == len(fake_replies())
    assert state.budget(research_id)["manager_calls"] == 3
    assert not provider.replies
    db.close()


def test_cursor_round_trip_and_terminal_resume(tmp_path):
    db, state, _, loop = runtime(tmp_path, fake_replies())
    result = asyncio.run(loop.run("Analyze association.", CSV))
    cursor = state.load_runtime_cursor(result["research_id"])
    assert ResearchRuntimeCursor.model_validate_json(cursor.model_dump_json()) == cursor
    assert cursor.action_index == 15
    assert cursor.branch_depths[result["hypothesis_id"]] == 1
    assert sum(cursor.loop_fingerprints.values()) == 15
    before = db.execute("SELECT COUNT(*) FROM research_actions").fetchone()[0]
    terminal = asyncio.run(loop.resume(result["research_id"]))
    assert terminal["outcome"] == "ALREADY_TERMINAL"
    assert db.execute("SELECT COUNT(*) FROM research_actions").fetchone()[0] == before
    db.close()


def test_unexplained_state_change_fails_closed(tmp_path):
    research_id, replies, consumed = crashed_run(tmp_path, "AFTER_COMMIT")
    db, state, _, loop = runtime(tmp_path, replies[consumed:])
    state.create_hypothesis(research_id, {"statement": "Unexpected external branch",
                             "rationale": "Conflict injection", "score": {
                                 "plausibility": 0.5, "testability": 0.5, "data_availability": 0.5,
                                 "information_value": 0.5, "cost": 0.5}}, created_by="system")
    with pytest.raises(StateConflictError, match="RESUME_STATE_CONFLICT"):
        asyncio.run(loop.resume(research_id))
    assert db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?", (research_id,)).fetchone()[0] == 1
    db.close()


def test_action_fingerprint_count_survives_restart(tmp_path):
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db)
    research_id = state.create_research("Loop guard persistence")
    state.record_action(research_id, "REPLAN", "Repeat same method", method="spearman_correlation",
                        logical_key="repeat:1")
    db.close()
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db)
    state.record_action(research_id, "REPLAN", "Repeat same method", method="spearman_correlation",
                        logical_key="repeat:2")
    with pytest.raises(LoopDetectedError):
        state.record_action(research_id, "REPLAN", "Repeat same method",
                            method="spearman_correlation", logical_key="repeat:3")
    assert db.execute("SELECT COUNT(*) FROM research_actions WHERE research_id=?",
                      (research_id,)).fetchone()[0] == 3
    db.close()


def test_interrupted_model_attempt_and_escalation_survive_restart(tmp_path):
    class CrashingProvider:
        name = "fake"

        async def run_structured(self, **kwargs):
            raise InjectedCrash("during provider call")

    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    research_id = state.create_research("Retry persistence")
    state.workspace.prepare(research_id)
    state.configure_budget(research_id, 0.25, 0.75, 1.0)
    loop = AutonomousResearchLoop(state, CrashingProvider(), MODELS)
    contract, _ = loop._role_contract(research_id, "manager", "Plan research", "ManagerDecision",
                                       runtime_key="auto:manager:retry_test")
    loop._save_cursor(research_id, "MODEL_READY")
    with pytest.raises(InjectedCrash):
        asyncio.run(loop._model_once(contract, ManagerDecision, "retry_test"))
    assert db.execute("SELECT status FROM agent_runs").fetchone()[0] == "RUNNING"
    db.close()

    db, state, provider, loop = runtime(tmp_path, [ModelProviderError("PROVIDER_TIMEOUT", retryable=True)])
    loop._reconcile(research_id)
    assert state.budget(research_id)["unknown_price_calls"] == 1
    contract, task_id = state.runtime_contract(research_id, "auto:manager:retry_test")
    with pytest.raises(RuntimeFailure, match="PROVIDER_TIMEOUT"):
        asyncio.run(loop._model_once(contract, ManagerDecision, "retry_test"))
    assert len(provider.calls) == 1
    assert db.execute("SELECT COUNT(*) FROM agent_runs WHERE status='FAILED'").fetchone()[0] == 2
    assert state.task_status(task_id) == "WAITING_ESCALATION"
    assert state.budget(research_id)["manager_calls"] == 2
    db.close()

    db, state, provider, loop = runtime(tmp_path, fake_replies())
    loop._reconcile(research_id)
    contract, _ = state.runtime_contract(research_id, "auto:manager:retry_test")
    with pytest.raises(RuntimeFailure, match="RETRY_EXHAUSTED"):
        asyncio.run(loop._model_once(contract, ManagerDecision, "retry_test"))
    assert provider.calls == []
    assert state.budget(research_id)["unknown_price_calls"] == 2
    db.close()


def test_unknown_price_manager_cap_remains_terminal_after_restart(tmp_path):
    db, state, _, loop = runtime(tmp_path, [MANAGER])
    loop.budget.unknown_limits = UnknownPriceLimits(max_manager_calls=1)
    result = asyncio.run(loop.run("Analyze association.", CSV))
    assert result["stop_reason"] == "BUDGET_EXHAUSTED"
    research_id = result["research_id"]
    assert state.budget(research_id)["manager_calls"] == 1
    assert state.budget(research_id)["unknown_price_calls"] == 1
    db.close()
    db, state, provider, loop = runtime(tmp_path, fake_replies())
    assert asyncio.run(loop.resume(research_id))["outcome"] == "ALREADY_TERMINAL"
    assert provider.calls == []
    assert state.budget(research_id)["manager_calls"] == 1
    db.close()


def test_invalidated_committed_experiment_cannot_be_reused(tmp_path):
    research_id, replies, consumed = crashed_run(tmp_path, "AFTER_COMMIT")
    db, state, _, loop = runtime(tmp_path, replies[consumed:])
    experiment_id = db.execute("SELECT experiment_id FROM experiments WHERE research_id=?",
                               (research_id,)).fetchone()[0]
    state.invalidate_experiment(research_id, experiment_id, "leakage discovered")
    result = asyncio.run(loop.resume(research_id))
    assert result["stop_reason"] == "INSUFFICIENT_DATA"
    assert db.execute("SELECT conclusion_json FROM research_runs WHERE research_id=?",
                      (research_id,)).fetchone()[0] is None
    assert db.execute("SELECT status FROM evidence WHERE experiment_id=?",
                      (experiment_id,)).fetchone()[0] == "INVALIDATED"
    db.close()


def semantic_result(db, research_id):
    row = db.execute("SELECT stop_reason,conclusion_json FROM research_runs WHERE research_id=?",
                     (research_id,)).fetchone()
    experiments = []
    for item in db.execute("SELECT method,status,result_artifact_id FROM experiments WHERE research_id=? ORDER BY rowid",
                           (research_id,)):
        artifact = db.execute("SELECT relative_path FROM artifacts WHERE artifact_id=?",
                              (item["result_artifact_id"],)).fetchone()
        experiments.append((item["method"], item["status"], artifact is not None))
    evidence = [(item["claim"], item["status"]) for item in db.execute(
        "SELECT claim,status FROM evidence WHERE research_id=? ORDER BY rowid", (research_id,))]
    conclusion = json.loads(row["conclusion_json"]) if row["conclusion_json"] else None
    numeric = [json.loads(item[0])["agent_result"]["output"]["estimate"] for item in db.execute(
        "SELECT payload_json FROM staged_mutations WHERE research_id=? AND status='COMMITTED' ORDER BY rowid",
        (research_id,))]
    return {"stop": row["stop_reason"], "experiments": experiments, "evidence": evidence,
            "support_level": conclusion["support_level"] if conclusion else None,
            "support_count": len(conclusion["evidence_refs"]) if conclusion else 0,
            "numeric": numeric}


def test_multi_crash_adaptive_loop_matches_uninterrupted(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    db, _, _, loop = runtime(baseline_dir, fake_replies())
    baseline = asyncio.run(loop.run("Analyze temperature and growth without inferring causality.", CSV))
    expected = semantic_result(db, baseline["research_id"])
    db.close()

    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    replies = fake_replies()
    consumed = 0
    research_id = None
    for fault in ("AFTER_COMMIT", "AFTER_CRITIC", "AFTER_COMMIT"):
        calls_at_boundary = {"AFTER_COMMIT": 1, "AFTER_CRITIC": 1}
        seen = 0
        def trigger(point):
            nonlocal seen
            if point == fault:
                seen += 1
                if seen == calls_at_boundary[fault]:
                    raise InjectedCrash(point)
        db, state, provider, loop = runtime(replay_dir, replies[consumed:])
        loop.faults = FaultInjector(trigger)
        with pytest.raises(InjectedCrash):
            if research_id is None:
                asyncio.run(loop.run("Analyze temperature and growth without inferring causality.", CSV))
            else:
                asyncio.run(loop.resume(research_id))
        research_id = db.execute("SELECT research_id FROM research_runs").fetchone()[0]
        consumed += len(provider.calls)
        db.close()
    db, state, provider, loop = runtime(replay_dir, replies[consumed:])
    resumed = asyncio.run(loop.resume(research_id))
    assert semantic_result(db, research_id) == expected
    assert resumed["action_count"] == baseline["action_count"] == 15
    assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name='stats.run'").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 2
    for row in db.execute("SELECT payload_json FROM staged_mutations WHERE research_id=? AND status='COMMITTED'",
                          (research_id,)):
        payload = json.loads(row["payload_json"])
        science = payload["scientific"]
        dataset = db.execute("SELECT sha256 FROM datasets WHERE dataset_id=?",
                             (science["dataset_id"],)).fetchone()
        for field, provenance in science["numeric_provenance"].items():
            value = payload["agent_result"]["output"]
            for part in field.split("."):
                value = value[int(part)] if part.isdigit() else value[part]
            assert provenance["value"] == value
            assert provenance["artifact_id"] == science["stats_artifact_id"]
            assert provenance["dataset_sha256"] == dataset["sha256"]
            assert db.execute("SELECT 1 FROM tool_calls WHERE request_id=?",
                              (provenance["tool_call_id"],)).fetchone() is not None
    assert not provider.replies
    db.close()
