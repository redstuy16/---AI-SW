import json
import math

import pytest

from probe.real_runtime import execute_real_tools, run_real_cycle
from probe.schemas import StagedResult, Verdict
from probe.service import VerificationRequiredError
from probe.storage import sha256_file

from test_real_tools import CSV, real_context


def test_real_csv_cycle_commits_only_after_verification(real_context):
    db, state, workspace, _, _, _, _ = real_context
    payload, ids = execute_real_tools(state, CSV)
    research_id = ids["research_id"]
    science = payload.scientific
    dataset = state.dataset_record(science.dataset_id, research_id)
    assert dataset["sha256"] == sha256_file(workspace.path(research_id, dataset["stored_path"]))
    for artifact_id in (science.profile_artifact_id, science.stats_artifact_id, science.figure_artifact_id):
        record = state.file_artifact(artifact_id, research_id)
        assert record["status"] == "PENDING"
    stats_record = state.file_artifact(science.stats_artifact_id, research_id)
    document = json.loads(workspace.path(research_id, stats_record["relative_path"]).read_text(encoding="utf-8"))
    assert document["result"] == payload.agent_result.output
    assert math.isclose(document["result"]["estimate"], 0.9974086507360695, rel_tol=1e-12)
    assert document["result"]["n"] == 8
    mutation_id = state.stage(payload)
    assert state.artifact(payload.agent_result.result_id) is None
    assert db.execute("SELECT status FROM experiments WHERE experiment_id=?", (science.experiment_id,)).fetchone()[0] == "PENDING_VERIFICATION"
    assert state.state_version(research_id) == 0
    verification = state.verify(mutation_id)
    assert verification.verdict == Verdict.PASS, verification.errors
    assert state.commit(mutation_id) == 1
    assert state.artifact(payload.agent_result.result_id)["output"] == document["result"]
    assert db.execute("SELECT status FROM experiments WHERE experiment_id=?", (science.experiment_id,)).fetchone()[0] == "VERIFIED"
    assert db.execute("SELECT status FROM evidence WHERE evidence_id=?", (science.evidence_id,)).fetchone()[0] == "VERIFIED"
    assert state.state_version(research_id) == 1
    assert db.execute("SELECT COUNT(*) FROM state_events WHERE mutation_id=?", (mutation_id,)).fetchone()[0] == 1
    assert set(science.numeric_provenance) == {"n", "missing_excluded", "estimate", "statistic", "p_value", "confidence_interval.0", "confidence_interval.1"}
    assert all(item.dataset_id == science.dataset_id and item.artifact_id == science.stats_artifact_id
               and item.tool_call_id == payload.tool_request.request_id and item.dataset_sha256 == dataset["sha256"]
               for item in science.numeric_provenance.values())


def test_corrupted_stats_artifact_blocks_commit(real_context):
    db, state, workspace, _, _, _, _ = real_context
    payload, ids = execute_real_tools(state, CSV)
    science = payload.scientific
    mutation_id = state.stage(payload)
    record = state.file_artifact(science.stats_artifact_id, ids["research_id"])
    path = workspace.path(ids["research_id"], record["relative_path"])
    content = bytearray(path.read_bytes())
    content[10] ^= 1
    path.write_bytes(content)
    verification = state.verify(mutation_id)
    assert verification.verdict == Verdict.FAIL
    assert "ARTIFACT_HASH_MATCH" in verification.errors
    with pytest.raises(VerificationRequiredError):
        state.commit(mutation_id)
    assert state.state_version(ids["research_id"]) == 0
    assert state.artifact(payload.agent_result.result_id) is None
    assert db.execute("SELECT status FROM experiments WHERE experiment_id=?", (science.experiment_id,)).fetchone()[0] != "VERIFIED"
    assert db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?", (ids["research_id"],)).fetchone()[0] == 0


def test_dataset_tamper_after_stage_blocks_commit(real_context):
    _, state, workspace, _, _, _, _ = real_context
    payload, ids = execute_real_tools(state, CSV)
    mutation_id = state.stage(payload)
    dataset = state.dataset_record(ids["dataset_id"], ids["research_id"])
    path = workspace.path(ids["research_id"], dataset["stored_path"])
    with path.open("ab") as stream:
        stream.write(b"tampered")
    verification = state.verify(mutation_id)
    assert verification.verdict == Verdict.FAIL
    assert "DATASET_HASH_MATCH" in verification.errors
    with pytest.raises(VerificationRequiredError):
        state.commit(mutation_id)
    assert state.state_version(ids["research_id"]) == 0


def test_artifact_changed_after_pass_is_rechecked_at_commit(real_context):
    _, state, workspace, _, _, _, _ = real_context
    payload, ids = execute_real_tools(state, CSV)
    mutation_id = state.stage(payload)
    assert state.verify(mutation_id).verdict == Verdict.PASS
    record = state.file_artifact(payload.scientific.stats_artifact_id, ids["research_id"])
    path = workspace.path(ids["research_id"], record["relative_path"])
    with path.open("ab") as stream:
        stream.write(b"X")
    from probe.service import StateConflictError
    with pytest.raises(StateConflictError):
        state.commit(mutation_id)
    assert state.state_version(ids["research_id"]) == 0
    assert state.artifact(payload.agent_result.result_id) is None


def test_forged_numeric_output_blocks_commit(real_context):
    _, state, _, _, _, _, _ = real_context
    payload, ids = execute_real_tools(state, CSV)
    forged = payload.agent_result.model_copy(update={"output": {**payload.agent_result.output, "estimate": 0.91}})
    payload = StagedResult(agent_result=forged, tool_request=payload.tool_request,
                           tool_result=payload.tool_result, scientific=payload.scientific)
    mutation_id = state.stage(payload)
    verification = state.verify(mutation_id)
    assert verification.verdict == Verdict.FAIL
    assert "NUMERIC_PROVENANCE" in verification.errors
    assert "RESULT_FIELD_MATCH" in verification.errors
    with pytest.raises(VerificationRequiredError):
        state.commit(mutation_id)
    assert state.state_version(ids["research_id"]) == 0
    assert state.artifact(forged.result_id) is None


def test_fresh_runtime_writes_manifest(tmp_path):
    result = run_real_cycle(tmp_path / "new" / "state.sqlite", tmp_path / "workspace", CSV)
    assert result["verdict"] == "PASS" and result["state_version"] == 1
    manifest = json.loads(__import__("pathlib").Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["research_id"] == result["research_id"]
    assert len(manifest["datasets"]) == 1
    assert any(item["artifact_type"] == "FIGURE" for item in manifest["artifacts"])
