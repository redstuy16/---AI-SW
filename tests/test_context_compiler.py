"""기존 동작과 검증 경계를 확인하는 회귀 테스트."""
from __future__ import annotations

import pytest

from probe.context_compiler import ContextBudgetError, ContextCompiler, ContextConfig
from probe.database import initialize
from probe.research_schemas import HypothesisProposal, HypothesisScore
from probe.schemas import Constraints, ContextPolicy, ContextRef, RefType, ResearchContract, new_id
from probe.service import ContractViolationError, InvalidStateTransitionError, StateService, LoopDetectedError, ActionLimitError
from probe.storage import Workspace


SCORE = HypothesisScore(plausibility=0.7, testability=0.9, data_availability=1,
                        information_value=0.8, cost=0.2)


def setup(tmp_path, *, role="manager", preserve=None, inputs=None, tokens=6000):
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    research_id = state.create_research("temperature growth association")
    state.configure_budget(research_id, 0.25, 0.75, 1)
    state.set_research_question(research_id, "Does temperature predict growth?")
    contract = ResearchContract(contract_id=new_id("C"), research_id=research_id,
                                task_type="context_test", issued_by="system", assigned_role=role,
                                objective="temperature growth", inputs=inputs or [],
                                context_policy=ContextPolicy(must_preserve=preserve or [],
                                                             max_context_tokens=tokens),
                                constraints=Constraints(max_tool_calls=0, max_cost_usd=1),
                                output_schema_id="context")
    state.issue_contract(contract)
    return db, state, research_id, contract


def proposal(statement):
    return HypothesisProposal(statement=statement, rationale="Testable with the dataset.", score=SCORE)


def test_mandatory_early_fact_and_recent_events(tmp_path):
    db, state, rid, _ = setup(tmp_path)
    decision_id = state.record_research_decision(rid, "Use only the original calibrated sensor.")
    for index in range(22):
        state.record_action(rid, "REPLAN", f"unrelated action {index}")
        state.runtime_event(rid, "NOISE", {"index": index})
    contract = ResearchContract(contract_id=new_id("C"), research_id=rid,
                                task_type="handoff", issued_by="manager", assigned_role="analysis_planner_worker",
                                objective="temperature growth", allowed_tools=[],
                                context_policy=ContextPolicy(must_preserve=[ContextRef(type=RefType.decision, id=decision_id)],
                                                             max_context_tokens=6000), output_schema_id="context")
    state.issue_contract(contract)
    bundle = ContextCompiler(state).compile(contract)
    assert ContextRef(type=RefType.decision, id=decision_id) in bundle.mandatory
    assert any(item.ref_id == decision_id and "calibrated sensor" in item.text for item in bundle.items)
    assert 1 <= len(bundle.recent_events) <= 3
    assert bundle.stable_core["research_question"] == "Does temperature predict growth?"
    assert bundle.metrics.mandatory_count == len(bundle.mandatory)
    assert db.execute("SELECT COUNT(*) FROM research_actions WHERE research_id=?", (rid,)).fetchone()[0] == 22
    db.close()


def test_direct_dependency_survives_low_semantic_similarity(tmp_path):
    db, state, rid, _ = setup(tmp_path)
    decision = state.record_research_decision(rid, "Retain calibration batch Z from the first meeting.")
    hypothesis = state.create_hypothesis(rid, proposal("temperature growth is monotonic"), created_by="manager")
    state.add_entity_edge(rid, ContextRef(type=RefType.hypothesis, id=hypothesis), "depends_on",
                          ContextRef(type=RefType.decision, id=decision))
    contract = ResearchContract(contract_id=new_id("C"), research_id=rid,
                                task_type="dependency", issued_by="manager", assigned_role="analysis_planner_worker",
                                objective="temperature growth", inputs=[ContextRef(type=RefType.hypothesis, id=hypothesis)],
                                context_policy=ContextPolicy(max_context_tokens=6000), output_schema_id="context")
    state.issue_contract(contract)
    bundle = ContextCompiler(state).compile(contract)
    assert ContextRef(type=RefType.decision, id=decision) in bundle.dependencies
    assert any(item.ref_id == decision and item.layer == "dependency" for item in bundle.items)
    db.close()


def test_semantic_filters_invalidated_and_superseded_records(tmp_path):
    db, state, rid, contract = setup(tmp_path)
    valid = state.create_hypothesis(rid, proposal("temperature growth positive association"), created_by="manager")
    stale = state.create_hypothesis(rid, proposal("temperature growth artifact leakage"), created_by="manager")
    state.set_hypothesis_status(rid, stale, "INVALIDATED", decided_by="manager", rationale="leakage")
    old = state.record_research_decision(rid, "temperature growth old calibration")
    new = state.record_research_decision(rid, "temperature growth corrected calibration")
    state.supersede_decision(rid, old, new)
    bundle = ContextCompiler(state).compile(contract)
    ids = {ref.id for ref in bundle.semantic}
    assert valid in ids
    assert stale not in ids
    assert old not in ids
    assert any("SUPERSEDED decision" in warning for warning in bundle.warnings)
    db.close()


def test_context_budget_drops_semantic_but_preserves_mandatory(tmp_path):
    db, state, rid, _ = setup(tmp_path)
    required = state.record_research_decision(rid, "Use first calibrated sensor only.")
    for index in range(20):
        state.record_research_decision(rid, f"temperature growth distractor topic {index}")
    contract = ResearchContract(contract_id=new_id("C"), research_id=rid,
                                task_type="budget", issued_by="manager", assigned_role="analysis_planner_worker",
                                objective="temperature growth",
                                context_policy=ContextPolicy(must_preserve=[ContextRef(type=RefType.decision, id=required)],
                                                             max_context_tokens=1150), output_schema_id="context")
    state.issue_contract(contract)
    bundle = ContextCompiler(state).compile(contract)
    assert required in {ref.id for ref in bundle.mandatory}
    assert bundle.metrics.estimated_tokens <= 1150
    assert len(bundle.semantic) < 5
    with pytest.raises(ContextBudgetError):
        ContextCompiler(state).compile(contract.model_copy(update={
            "context_policy": ContextPolicy(must_preserve=[ContextRef(type=RefType.decision, id=required)],
                                             max_context_tokens=100)}))
    db.close()


def test_semantic_rerank_and_dedup(tmp_path):
    db, state, rid, contract = setup(tmp_path)
    state.record_research_decision(rid, "temperature growth association")
    state.record_research_decision(rid, "temperature growth association")
    state.record_research_decision(rid, "unrelated seismology report")
    bundle = ContextCompiler(state).compile(contract)
    matching = [item for item in bundle.items if item.layer == "semantic" and
                "temperature growth association" in item.text]
    assert len(matching) == 1
    assert matching[0].score is not None and matching[0].score > 0
    db.close()


def test_superseded_must_preserve_is_marked_invalid_not_silent(tmp_path):
    db, state, rid, _ = setup(tmp_path)
    old = state.record_research_decision(rid, "Use temperature growth calibration A.")
    contract = ResearchContract(contract_id=new_id("C"), research_id=rid,
                                task_type="handoff", issued_by="manager", assigned_role="analysis_planner_worker",
                                objective="temperature growth",
                                context_policy=ContextPolicy(must_preserve=[ContextRef(type=RefType.decision, id=old)],
                                                             max_context_tokens=6000), output_schema_id="context")
    state.issue_contract(contract)
    replacement = state.record_research_decision(rid, "Use temperature growth calibration B.")
    state.supersede_decision(rid, old, replacement)
    bundle = ContextCompiler(state).compile(contract)
    assert old in {ref.id for ref in bundle.mandatory}
    assert any(item.ref_id == old and item.marked_invalid for item in bundle.items)
    assert old not in {ref.id for ref in bundle.semantic}
    assert any("SUPERSEDED" in warning for warning in bundle.warnings)
    db.close()


def test_semantic_role_visibility_filter(tmp_path):
    db, state, rid, contract = setup(tmp_path, role="analysis_planner_worker")
    hidden = state.record_research_decision(rid, "temperature growth private strategy", visibility="manager")
    visible = state.record_research_decision(rid, "temperature growth public strategy")
    bundle = ContextCompiler(state).compile(contract)
    assert hidden not in {ref.id for ref in bundle.semantic}
    assert visible in {ref.id for ref in bundle.semantic}
    db.close()


def test_hypothesis_transitions_shortlist_cap_and_active_cap(tmp_path):
    db, state, rid, _ = setup(tmp_path)
    shortlist = {"hypotheses": [proposal(f"temperature growth variant {i}").model_dump() for i in range(3)],
                 "rationale": "Three testable variants."}
    ids = state.shortlist_hypotheses(rid, shortlist, created_by="manager")
    assert len(ids) == 3
    for hypothesis_id in ids[:2]:
        state.set_hypothesis_status(rid, hypothesis_id, "ACTIVE", decided_by="manager", rationale="selected")
    with pytest.raises(ContractViolationError, match="active hypothesis cap"):
        state.set_hypothesis_status(rid, ids[2], "ACTIVE", decided_by="manager", rationale="too many")
    with pytest.raises(InvalidStateTransitionError):
        state.set_hypothesis_status(rid, ids[0], "SHORTLISTED", decided_by="manager", rationale="backwards")
    with pytest.raises(ContractViolationError, match="shortlist cap"):
        state.shortlist_hypotheses(rid, {"hypotheses": [proposal(f"new {i}").model_dump() for i in range(4)],
                                          "rationale": "too many"}, created_by="manager")
    db.close()


def test_action_fingerprint_and_hard_cap(tmp_path):
    db, state, rid, _ = setup(tmp_path)
    state.record_action(rid, "DESIGN_EXPERIMENT", " Temperature   Growth ", method="pearson_correlation")
    state.record_action(rid, "DESIGN_EXPERIMENT", "temperature growth", method="pearson_correlation")
    with pytest.raises(LoopDetectedError):
        state.record_action(rid, "DESIGN_EXPERIMENT", "temperature growth", method="pearson_correlation")
    with pytest.raises(ActionLimitError):
        state.record_action(rid, "STOP", "finish", max_actions=3)
    db.close()
