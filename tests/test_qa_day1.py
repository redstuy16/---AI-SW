"""기존 동작과 검증 경계를 확인하는 회귀 테스트."""
from __future__ import annotations

import json

import pytest

from probe.dashboard import project_evidence, project_experiments, project_overview, project_usage
from probe.context_compiler import ContextCompiler
from probe.database import initialize
from probe.demo import run_demo_a, run_demo_b
from probe.final_report import ReportValidationError, build_final_conclusion, validate_final_conclusion
from probe.release import ReleaseExportError, export_release
from probe.service import StateService
from probe.schemas import ContextPolicy, ContextRef, RefType, ResearchContract, new_id
from probe.storage import Workspace


@pytest.fixture(scope="module")
def qa_demo_a(tmp_path_factory):
    root = tmp_path_factory.mktemp("qa-day1-a")
    result = run_demo_a(root / "state.sqlite", root / "workspace")
    db = initialize(root / "state.sqlite")
    state = StateService(db, Workspace(root / "workspace"))
    yield root, result, state
    db.close()


@pytest.fixture(scope="module")
def qa_demo_b(tmp_path_factory):
    root = tmp_path_factory.mktemp("qa-day1-b")
    result = run_demo_b(root / "state.sqlite", root / "workspace")
    db = initialize(root / "state.sqlite")
    state = StateService(db, Workspace(root / "workspace"))
    yield root, result, state
    db.close()


def test_qa_release_rejects_post_commit_artifact_corruption(qa_demo_a, tmp_path):
    _, result, state = qa_demo_a
    rid = result["research_id"]
    row = state._one("SELECT relative_path FROM artifacts WHERE research_id=? AND artifact_type='STATS_RESULT' LIMIT 1", (rid,))
    path = state.workspace.path(rid, row["relative_path"])
    original = path.read_bytes()
    try:
        changed = bytearray(original)
        changed[0] ^= 1
        path.write_bytes(changed)
        with pytest.raises(ReleaseExportError, match="artifact hash mismatch"):
            export_release(state, rid, tmp_path / "corrupted")
        assert state._db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?", (rid,)).fetchone()[0] == 2
    finally:
        path.write_bytes(original)


def test_qa_release_rejects_post_import_dataset_corruption(qa_demo_a, tmp_path):
    _, result, state = qa_demo_a
    rid = result["research_id"]
    row = state._one("SELECT stored_path FROM datasets WHERE research_id=?", (rid,))
    path = state.workspace.path(rid, row["stored_path"])
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"X")
        with pytest.raises(ReleaseExportError, match="dataset hash mismatch"):
            export_release(state, rid, tmp_path / "corrupted-dataset")
    finally:
        path.write_bytes(original)


def test_qa_release_rejects_stale_or_forged_report(qa_demo_a, tmp_path):
    _, result, state = qa_demo_a
    rid = result["research_id"]
    path = (__import__("probe.report_publication", fromlist=["report_root"]).report_root(state, rid) / 'final_report.md')
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"\nUnverified estimate: 999\n")
        with pytest.raises(ReleaseExportError, match="report file hash mismatch"):
            export_release(state, rid, tmp_path / "forged-report")
    finally:
        path.write_bytes(original)


def test_qa_literature_metadata_and_abstract_tampering_blocks_report(qa_demo_a):
    _, result, state = qa_demo_a
    rid = result["research_id"]
    source = state._one("SELECT source_id,abstract,metadata_hash FROM sources WHERE research_id=? AND status='VERIFIED' LIMIT 1", (rid,))
    try:
        state._db.execute("UPDATE sources SET abstract=? WHERE source_id=?", ("forged abstract", source["source_id"]))
        with pytest.raises(ReportValidationError):
            build_final_conclusion(state, rid)
    finally:
        state._db.execute("UPDATE sources SET abstract=? WHERE source_id=?", (source["abstract"], source["source_id"]))
    try:
        state._db.execute("UPDATE sources SET metadata_hash=? WHERE source_id=?", ("0" * 64, source["source_id"]))
        with pytest.raises(ReportValidationError):
            build_final_conclusion(state, rid)
    finally:
        state._db.execute("UPDATE sources SET metadata_hash=? WHERE source_id=?", (source["metadata_hash"], source["source_id"]))


def test_qa_report_hallucinated_ids_doi_and_numbers_are_rejected(qa_demo_a):
    _, result, state = qa_demo_a
    rid = result["research_id"]
    trusted = build_final_conclusion(state, rid)
    attacks = [
        trusted.model_copy(update={"evidence_refs": trusted.evidence_refs + ["EVIDENCE-fake"]}),
        trusted.model_copy(update={"conclusion": trusted.conclusion + " DOI 10.9999/fake"}),
        trusted.model_copy(update={"conclusion": trusted.conclusion + " Estimate 0.999"}),
    ]
    for candidate in attacks:
        with pytest.raises(ReportValidationError):
            validate_final_conclusion(state, rid, candidate)


def test_qa_invalidation_and_dashboard_match_canonical_state(qa_demo_b):
    _, result, state = qa_demo_b
    rid = result["research_id"]
    invalid_id = result["invalidated_experiment_id"]
    conclusion = build_final_conclusion(state, rid)
    assert invalid_id in conclusion.limitation_refs
    assert invalid_id not in conclusion.experiment_refs
    assert all(state._db.execute("SELECT experiment_id FROM evidence WHERE evidence_id=?", (eid,)).fetchone()[0] != invalid_id
               for eid in conclusion.evidence_refs)
    overview = project_overview(state, rid)
    experiments = project_experiments(state, rid)
    evidence = project_evidence(state, rid)
    usage = project_usage(state, rid)
    assert overview.state_version == state.state_version(rid)
    assert overview.verified_experiment_count == state._db.execute(
        "SELECT COUNT(*) FROM experiments WHERE research_id=? AND status='VERIFIED'", (rid,)).fetchone()[0]
    assert sum(row.status == "INVALIDATED" for row in experiments) == 1
    assert all(not row.valid for row in evidence if row.status == "INVALIDATED")
    assert usage.cost_status == "Unknown" and usage.estimated_cost_usd is None


def test_qa_contradiction_survives_synthesis_and_report(qa_demo_a):
    _, result, state = qa_demo_a
    rid = result["research_id"]
    conclusion = build_final_conclusion(state, rid)
    contradictory = {row["evidence_id"] for row in state._db.execute(
        "SELECT evidence_id FROM evidence WHERE research_id=? AND status='VERIFIED' AND polarity='CONTRADICT'", (rid,))}
    assert contradictory and contradictory <= set(conclusion.contradiction_refs)
    report = (__import__("probe.report_publication", fromlist=["report_root"]).report_root(state, rid) / 'final_report.md').read_text(encoding="utf-8")
    assert all(identity in report for identity in contradictory)


def test_qa_release_rejects_changed_canonical_state(qa_demo_a, tmp_path):
    _, result, state = qa_demo_a
    rid = result["research_id"]
    before = state.state_version(rid)
    state._db.execute("UPDATE research_runs SET state_version=? WHERE research_id=?", (before + 1, rid))
    try:
        with pytest.raises(ReleaseExportError, match="state version is stale"):
            export_release(state, rid, tmp_path / "stale")
    finally:
        state._db.execute("UPDATE research_runs SET state_version=? WHERE research_id=?", (before, rid))


def test_qa_release_omits_local_absolute_paths(qa_demo_a, tmp_path):
    _, result, state = qa_demo_a
    exported = export_release(state, result["research_id"], tmp_path / "release")
    manifest = (tmp_path / "release" / "research_output" / "demo_manifest.json").read_text(encoding="utf-8")
    assert "C:\\" not in manifest
    assert "research_output/final_report.md" in manifest
    assert exported["files"]


def test_qa_handoff_must_preserve_survives_fresh_database_open(tmp_path):
    database = tmp_path / "state.sqlite"
    workspace = tmp_path / "workspace"
    db = initialize(database)
    state = StateService(db, Workspace(workspace))
    rid = state.create_research("retain calibration decision")
    state.configure_budget(rid, 0.25, 0.75, 1.0)
    decision = state.record_research_decision(rid, "Use calibrated sensor Q only.")
    contract = ResearchContract(
        contract_id=new_id("C"), research_id=rid, task_type="qa_handoff",
        issued_by="experiment_coordinator", assigned_role="analysis_planner_worker",
        objective="analyze measurements", output_schema_id="context",
        context_policy=ContextPolicy(
            must_preserve=[ContextRef(type=RefType.decision, id=decision)],
            max_context_tokens=6000))
    state.issue_contract(contract)
    state.checkpoint(rid, "before worker handoff")
    db.close()
    reopened = initialize(database)
    try:
        state = StateService(reopened, Workspace(workspace))
        recovered, task_id = state.contract(contract.contract_id)
        bundle = ContextCompiler(state).compile(recovered)
        assert task_id
        assert decision in {ref.id for ref in bundle.mandatory}
        assert any(item.ref_id == decision and "sensor Q" in item.text for item in bundle.items)
    finally:
        reopened.close()
