"""기존 동작과 검증 경계를 확인하는 회귀 테스트."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from probe.agent_runtime import RuntimeFailure
from probe.autonomous_loop import AutonomousResearchLoop, LoopConfig
from probe.context_compiler import ContextCompiler
from probe.database import initialize
from probe.providers.fake import FakeProvider
from probe.schemas import ContextPolicy, ContextRef, RefType, ResearchContract, new_id
from probe.service import ContractViolationError, StateService
from probe.storage import Workspace
from probe.research_tree import build_research_tree


# 완료·복구 시나리오는 두 사전 지정 방법 모두 유의한 기존 자료를 사용한다.
# 극단값 자료의 비유의 결과는 신뢰성 경계 테스트에서 별도로 확인한다.
CSV = Path(__file__).parent / "fixtures" / "temperature_growth.csv"
MODELS = {"manager": "fake-manager", "experiment_coordinator": "fake-coordinator",
          "analysis_planner_worker": "fake-worker", "verification_coordinator": "fake-critic"}
MANAGER = {"decision_type": "INITIAL_PLAN", "research_question": "How are temperature and growth associated?",
           "rationale": "Use the calibrated sample and check association without claiming causality.",
           "coordinator_role": "experiment_coordinator", "objective": "Test temperature and growth."}
SCORE = {"plausibility": 0.8, "testability": 0.9, "data_availability": 1,
         "information_value": 0.9, "cost": 0.1}


def shortlist():
    return {"hypotheses": [
        {"statement": "Temperature and growth have a monotonic association.",
         "rationale": "Both columns are measured in the same sample.", "score": SCORE, "criterion": {"method": "pearson_correlation", "confirmatory_methods": ["spearman_correlation"], "variables": {"x": "temperature", "y": "growth"}, "expected_direction": "positive", "alpha": 0.05}},
        {"statement": "Temperature and growth are unrelated.",
         "rationale": "Null alternative is testable.",
         "score": {**SCORE, "information_value": 0.2}},
        {"statement": "Growth changes only after a temperature threshold.",
         "rationale": "A nonlinear alternative is testable.",
         "score": {**SCORE, "plausibility": 0.5}}],
            "rationale": "Keep three differentiated, testable hypotheses."}


def initial_coordinator(call):
    active = json.loads(call["input_text"])["active_state"]
    dataset = active["datasets"][0]["dataset_id"]
    profile = active["profile_artifacts"][0]["artifact_id"]
    return {"decision_type": "DELEGATE", "objective": "First Pearson association test.",
            "rationale": "Start with a linear sensitivity check.",
            "assigned_role": "analysis_planner_worker",
            "input_refs": [{"type": "dataset", "id": dataset}, {"type": "artifact", "id": profile}],
            "allowed_tools": ["stats.run", "visualization.render", "evidence.record"],
            "max_tool_calls": 3, "max_retries": 1, "max_runtime_sec": 120, "max_cost_usd": 0.5}


def worker(method):
    def reply(call):
        refs = json.loads(call["input_text"])["active_state"]["contract"]["inputs"]
        dataset = next(ref["id"] for ref in refs if ref["type"] == "dataset")
        return {"dataset_id": dataset, "selected_variables": ["temperature", "growth"],
                "method": method, "justification": "Profile confirms paired numeric columns.",
                "requested_tools": ["stats.run", "visualization.render", "evidence.record"],
                "reported_r": 0.5}
    return reply


def first_critic(call):
    active = json.loads(call["input_text"])["active_state"]
    assert active["verified_experiments"][0]["method"] == "pearson_correlation"
    hypothesis = active["active_hypotheses"][0]["hypothesis_id"]
    return {"verdict": "FOLLOW_UP_REQUIRED",
            "issues": [{"code": "NONLINEARITY", "severity": "MEDIUM",
                        "detail": "An extreme endpoint may mask a monotonic relationship."}],
            "alternative_explanations": ["Endpoint influence"], "confounders": [],
            "requested_followups": [{"method": "spearman_correlation",
                                     "rationale": "Use a rank correlation sensitivity analysis.",
                                     "hypothesis_id": hypothesis}],
            "conclusion_strength": "NONE"}


def second_critic(call):
    active = json.loads(call["input_text"])["active_state"]
    assert active["verified_experiments"][0]["method"] == "spearman_correlation"
    return {"verdict": "ACCEPT_WITH_LIMITATION",
            "issues": [{"code": "CAUSALITY", "severity": "LOW",
                        "detail": "Association does not establish causation."}],
            "alternative_explanations": ["Unmeasured confounding"],
            "confounders": ["Unmeasured factors"], "requested_followups": [],
            "conclusion_strength": "MODERATE"}


def status_decision(call):
    active = json.loads(call["input_text"])["active_state"]
    hypothesis = active["active_hypotheses"][0]["hypothesis_id"]
    evidence = active["verified_evidence"]
    assert len(evidence) == 2
    return {"hypothesis_id": hypothesis, "new_status": "SUPPORTED",
            "rationale": "Two verified methods support a monotonic association with a causality limitation.",
            "evidence_refs": [{"type": "evidence", "id": item["evidence_id"]} for item in evidence]}


def fake_replies():
    return [MANAGER, shortlist(), initial_coordinator, worker("pearson_correlation"),
            first_critic,
            {"approve": True, "rationale": "A rank-based sensitivity check addresses the issue.",
             "method": "spearman_correlation", "objective": "Run bounded Spearman follow-up."},
            worker("spearman_correlation"), second_critic, status_decision,
            {"stop": True, "reason": "GOAL_ANSWERED", "rationale": "Two verified experiments answer the bounded question."}]


def run_fixture(tmp_path):
    db = initialize(tmp_path / "research.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    provider = FakeProvider(fake_replies())
    loop = AutonomousResearchLoop(state, provider, MODELS)
    result = asyncio.run(loop.run("Analyze temperature and growth without inferring causality.", CSV))
    return db, state, provider, result


def test_adaptive_autonomous_loop_15_actions_and_numeric_provenance(tmp_path):
    db, state, provider, result = run_fixture(tmp_path)
    rid = result["research_id"]
    assert result["stop_reason"] == "GOAL_ANSWERED"
    assert result["action_count"] >= 15
    assert len(result["experiment_ids"]) == 2
    assert result["first_result"]["method"] == "pearson_correlation"
    assert result["last_result"]["method"] == "spearman_correlation"
    assert abs(result["first_result"]["estimate"]) < abs(result["last_result"]["estimate"])
    assert result["last_result"]["estimate"] != 0.5
    assert db.execute("SELECT status FROM hypotheses WHERE hypothesis_id=?", (result["hypothesis_id"],)).fetchone()[0] == "SUPPORTED"
    assert [row[0] for row in db.execute("SELECT status FROM evidence WHERE research_id=? ORDER BY rowid", (rid,))] == ["VERIFIED", "VERIFIED"]
    assert db.execute("SELECT COUNT(*) FROM critic_reviews WHERE research_id=?", (rid,)).fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?", (rid,)).fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM research_actions WHERE research_id=? AND action_type='REPLAN'", (rid,)).fetchone()[0] == 1
    assert state.budget(rid)["manager_calls"] == 3
    assert len(result["conclusion"]["evidence_refs"]) == 2
    assert all(ref["id"] in result["evidence_ids"] for ref in result["conclusion"]["evidence_refs"])
    assert all(row["status"] == "VERIFIED" for row in db.execute(
        "SELECT status FROM experiments WHERE research_id=?", (rid,)))
    assert [call["role"] for call in provider.calls].count("verification_coordinator") == 2
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE assigned_role='analysis_planner_worker'", ()).fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM milestone_summaries WHERE research_id=?", (rid,)).fetchone()[0] == 2
    tree = build_research_tree(state, rid)
    assert tree["state_version"] == result["state_version"]
    assert len(next(item for item in tree["hypotheses"] if item["ref_id"] == result["hypothesis_id"])["evidence"]) == 2
    assert db.execute("SELECT COUNT(*) FROM context_metrics WHERE research_id=?", (rid,)).fetchone()[0] == len(provider.calls)
    for row in db.execute("SELECT payload_json FROM staged_mutations WHERE research_id=? AND status='COMMITTED'", (rid,)):
        payload = json.loads(row["payload_json"])
        assert payload["scientific"]["numeric_provenance"]["estimate"]["value"] == payload["agent_result"]["output"]["estimate"]
    db.close()


def test_later_evidence_invalidation_reopens_research_and_warns_context(tmp_path):
    db, state, _, result = run_fixture(tmp_path)
    rid = result["research_id"]
    state.invalidate_experiment(rid, result["experiment_ids"][1], "Data leakage found in follow-up")
    row = db.execute("SELECT run_status,conclusion_json FROM research_runs WHERE research_id=?", (rid,)).fetchone()
    assert row["run_status"] == "ACTIVE" and row["conclusion_json"] is None
    assert db.execute("SELECT status FROM hypotheses WHERE hypothesis_id=?", (result["hypothesis_id"],)).fetchone()[0] == "INCONCLUSIVE"
    assert db.execute("SELECT status FROM evidence WHERE evidence_id=?", (result["evidence_ids"][1],)).fetchone()[0] == "INVALIDATED"
    contract = ResearchContract(contract_id=new_id("C"), research_id=rid,
                                task_type="review", issued_by="system", assigned_role="manager",
                                objective="reconsider growth evidence",
                                context_policy=ContextPolicy(max_context_tokens=6000),
                                output_schema_id="review")
    state.issue_contract(contract)
    bundle = ContextCompiler(state).compile(contract)
    assert result["evidence_ids"][1] not in {ref.id for ref in bundle.semantic}
    assert any("Data leakage" in warning for warning in bundle.warnings)
    with pytest.raises(ContractViolationError, match="stale evidence"):
        state.stop_research(rid, "GOAL_ANSWERED", result["conclusion"])
    db.close()


def test_critic_followup_decline_stops_without_second_experiment(tmp_path):
    def one_evidence_status(call):
        active = json.loads(call["input_text"])["active_state"]
        return {"hypothesis_id": active["active_hypotheses"][0]["hypothesis_id"],
                "new_status": "INCONCLUSIVE", "rationale": "Follow-up was declined.",
                "evidence_refs": [{"type": "evidence", "id": active["verified_evidence"][0]["evidence_id"]}]}

    replies = [MANAGER, shortlist(), initial_coordinator, worker("pearson_correlation"),
               first_critic,
               {"approve": False, "rationale": "The proposed method is not worth the remaining scope.",
                "method": None, "objective": "Decline follow-up."},
               one_evidence_status,
               {"stop": True, "reason": "INSUFFICIENT_DATA", "rationale": "Evidence remains inconclusive."}]
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    provider = FakeProvider(replies)
    result = asyncio.run(AutonomousResearchLoop(state, provider, MODELS).run("Analyze association.", CSV))
    assert result["stop_reason"] == "INSUFFICIENT_DATA"
    assert len(result["experiment_ids"]) == 1
    assert result["conclusion"] is None
    assert db.execute("SELECT COUNT(*) FROM runtime_events WHERE event_type='FOLLOWUP_DECLINED'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name='stats.run'").fetchone()[0] == 1
    db.close()


def test_high_severity_final_critic_blocks_answered_conclusion(tmp_path):
    replies = fake_replies()
    replies[7] = {
        "verdict": "ACCEPT_WITH_LIMITATION",
        "issues": [{"code": "LEAKAGE", "severity": "HIGH",
                    "detail": "Potential leakage remains unresolved."}],
        "alternative_explanations": [], "confounders": [],
        "requested_followups": [], "conclusion_strength": "MODERATE"}
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    result = asyncio.run(AutonomousResearchLoop(state, FakeProvider(replies), MODELS).run(
        "Analyze association.", CSV))
    assert result["stop_reason"] == "UNRESOLVED_VERIFICATION"
    assert result["conclusion"] is None
    assert db.execute("SELECT conclusion_json FROM research_runs").fetchone()[0] is None
    db.close()


def test_repeated_followup_method_triggers_loop_stop(tmp_path):
    def repeated_critic(call):
        active = json.loads(call["input_text"])["active_state"]
        return {"verdict": "FOLLOW_UP_REQUIRED", "issues": [],
                "alternative_explanations": [], "confounders": [],
                "requested_followups": [{"method": "pearson_correlation",
                                         "rationale": "Repeat exactly the same test.",
                                         "hypothesis_id": active["active_hypotheses"][0]["hypothesis_id"]}],
                "conclusion_strength": "NONE"}

    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    result = asyncio.run(AutonomousResearchLoop(state,
                         FakeProvider([MANAGER, shortlist(), initial_coordinator,
                                       worker("pearson_correlation"), repeated_critic]), MODELS).run(
                                           "Analyze association.", CSV))
    assert result["stop_reason"] == "FATAL_ERROR"
    assert db.execute("SELECT COUNT(*) FROM runtime_events WHERE event_type='LOOP_DETECTED'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 1
    db.close()


def test_action_limit_stops_and_cancels_unfinished_contract(tmp_path):
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    provider = FakeProvider([MANAGER, shortlist(), initial_coordinator])
    loop = AutonomousResearchLoop(state, provider, MODELS,
                                  config=LoopConfig(max_actions_per_run=3))
    result = asyncio.run(loop.run("Analyze association.", CSV))
    assert result["stop_reason"] == "ACTION_LIMIT_REACHED"
    assert db.execute("SELECT COUNT(*) FROM research_actions").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE status='RUNNING'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    assert not any(call["role"] == "verification_coordinator" for call in provider.calls)
    db.close()


def test_malformed_critic_output_does_not_change_hypothesis(tmp_path):
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    provider = FakeProvider([MANAGER, shortlist(), initial_coordinator,
                             worker("pearson_correlation"), {"bad": 1}, {"bad": 1}])
    loop = AutonomousResearchLoop(state, provider, MODELS)
    with pytest.raises(RuntimeFailure, match="STRUCTURED_OUTPUT_INVALID"):
        asyncio.run(loop.run("Analyze association.", CSV))
    rid = db.execute("SELECT research_id FROM research_runs").fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?", (rid,)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM hypotheses WHERE research_id=? AND status='SUPPORTED'", (rid,)).fetchone()[0] == 0
    assert db.execute("SELECT conclusion_json FROM research_runs WHERE research_id=?", (rid,)).fetchone()[0] is None
    db.close()


def test_followup_method_mismatch_is_rejected_before_second_worker(tmp_path):
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    replies = [MANAGER, shortlist(), initial_coordinator, worker("pearson_correlation"),
               first_critic,
               {"approve": True, "rationale": "Wrong method", "method": "pearson_correlation",
                "objective": "Repeat Pearson"}]
    with pytest.raises(ContractViolationError, match="approved follow-up method differs"):
        asyncio.run(AutonomousResearchLoop(state, FakeProvider(replies), MODELS).run("Analyze association.", CSV))
    assert db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name='stats.run'").fetchone()[0] == 1
    db.close()
