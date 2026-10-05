import pytest

from probe.schemas import StagedResult, ToolResult, Verdict, new_id
from probe.service import DuplicateCommitError, StateConflictError, VerificationRequiredError


def test_mock_research_cycle_commits_verified_result(cycle):
    db, state, research_id, contract, payload = cycle
    artifact_id = payload.agent_result.result_id
    mutation_id = state.stage(payload)
    assert state.artifact(artifact_id) is None
    assert state.state_version(research_id) == 0
    with pytest.raises(VerificationRequiredError):
        state.commit(mutation_id)
    verification = state.verify(mutation_id)
    assert verification.verdict == Verdict.PASS
    assert all(c.passed for c in verification.checks)
    assert state.artifact(artifact_id) is None
    assert state.commit(mutation_id) == 1
    assert state.artifact(artifact_id)["output"] == {"n": 4, "x_mean": 2.5, "y_mean": 5.0}
    assert state.state_version(research_id) == 1
    assert db.execute("SELECT COUNT(*) FROM state_events WHERE mutation_id=?", (mutation_id,)).fetchone()[0] == 1
    assert state.mutation_status(mutation_id) == "COMMITTED"


def test_forbidden_tool_fails_without_canonical_contamination(cycle):
    db, state, research_id, contract, payload = cycle
    bad_request = payload.tool_request.model_copy(update={"request_id": new_id("TREQ"), "idempotency_key": new_id("IDEMP"), "tool_name": "forbidden.tool"})
    bad_tool = ToolResult(ok=True, request_id=bad_request.request_id, tool_name="forbidden.tool", result=payload.tool_result.result)
    state.record_execution(contract.contract_id, "analysis_worker", bad_request, bad_tool)
    bad = StagedResult(agent_result=payload.agent_result, tool_request=bad_request, tool_result=bad_tool)
    mutation_id = state.stage(bad)
    verdict = state.verify(mutation_id)
    assert verdict.verdict == Verdict.FAIL
    assert "allowed_tool" in verdict.errors
    assert "execution_trace" not in verdict.errors
    with pytest.raises(VerificationRequiredError):
        state.commit(mutation_id)
    assert state.artifact(payload.agent_result.result_id) is None
    assert state.state_version(research_id) == 0
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0


def test_duplicate_commit_rejected(cycle):
    _, state, research_id, _, payload = cycle
    mutation_id = state.stage(payload)
    state.verify(mutation_id)
    state.commit(mutation_id)
    with pytest.raises(DuplicateCommitError):
        state.commit(mutation_id)
    assert state.state_version(research_id) == 1


def test_stale_state_version_rejected(cycle):
    _, state, research_id, _, payload = cycle
    first = state.stage(payload)
    second = state.stage(payload)
    state.verify(first)
    state.commit(first)
    assert state.verify(second).verdict == Verdict.FAIL
    with pytest.raises(VerificationRequiredError):
        state.commit(second)
    assert state.state_version(research_id) == 1


def test_verified_then_stale_rechecked_at_commit(cycle):
    _, state, research_id, _, payload = cycle
    first = state.stage(payload)
    second = state.stage(payload)
    state.verify(first)
    state.verify(second)
    state.commit(first)
    with pytest.raises(StateConflictError):
        state.commit(second)
    assert state.state_version(research_id) == 1


def test_forged_tool_value_fails_deterministic_replay(cycle):
    _, state, research_id, contract, payload = cycle
    forged_tool = payload.tool_result.model_copy(update={"result": {"n": 4, "x_mean": 99, "y_mean": 5.0}})
    forged_request = payload.tool_request.model_copy(update={"request_id": new_id("TREQ")})
    forged_tool = forged_tool.model_copy(update={"request_id": forged_request.request_id})
    forged_agent = payload.agent_result.model_copy(update={"output": forged_tool.result, "result_id": new_id("ART")})
    state.record_execution(contract.contract_id, "analysis_worker", forged_request, forged_tool)
    mutation_id = state.stage(StagedResult(agent_result=forged_agent, tool_request=forged_request, tool_result=forged_tool))
    verdict = state.verify(mutation_id)
    assert verdict.verdict == Verdict.FAIL
    assert "deterministic_output" in verdict.errors
    with pytest.raises(VerificationRequiredError):
        state.commit(mutation_id)
    assert state.artifact(forged_agent.result_id) is None
    assert state.state_version(research_id) == 0
