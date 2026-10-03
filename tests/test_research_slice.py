"""기존 동작과 검증 경계를 확인하는 회귀 테스트."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "qa"))
from f3p_eval import prepare
from htrsa.agent_runtime import AgentRuntime
from htrsa.database import initialize, to_json
from htrsa.final_report import export_final_report
from htrsa.literature import LiteralSentenceReviewer, extract_abstract_evidence
from htrsa.providers.fake import FakeProvider
from htrsa.research_slice import numeric_equal, shared_dependencies
from htrsa.research_slice_schemas import NumericSlot, ResearchSliceConfig, VerifierObligation
from htrsa.schemas import StagedResult
from htrsa.scholarly import NormalizedSource, source_from_row
from htrsa.service import StateConflictError, StateService
from htrsa.storage import Workspace, sha256_file
from htrsa.release import export_release, ReleaseExportError

ON = ResearchSliceConfig(claim_evidence_provenance=True, verifier_dependency_catalog=True)


@pytest.fixture
def research(tmp_path):
    db, state, runtime, prepared, _ = prepare(tmp_path, slice_config=ON)
    outcome = asyncio.run(runtime.resume(prepared["research_id"]))
    yield db, state, runtime, prepared["research_id"], outcome
    db.close()


def copied_claim(state, rid):
    claim = state.research_slice.current(rid)[0].model_copy(deep=True)
    bindings = state.research_slice.bindings(claim)
    return state.research_slice._next_revision(claim, bindings)


def add_source(state, rid, *, suffix="A", contradiction=False):
    sentence = "Temperature and growth show no association in this sample." if contradiction else "Temperature and growth show a positive association in this sample."
    source = NormalizedSource(title="Temperature and growth study " + suffix, authors=["Offline Fixture"],
                              abstract=sentence, url="https://example.org/" + suffix, provider="fake")
    sid, _ = state.upsert_source(rid, source)
    state._db.execute("UPDATE sources SET status='RELEVANT' WHERE source_id=?", (sid,))
    source = source_from_row(state._one("SELECT * FROM sources WHERE source_id=?", (sid,)))
    item = extract_abstract_evidence(sid, source)
    eid = state.verify_literature_evidence(rid, item, LiteralSentenceReviewer())
    return sid, eid, next(c for c in state.research_slice.current(rid) if c.created_from == eid)


def test_default_off_is_schema_and_execution_compatible(tmp_path):
    db, state, runtime, prepared, _ = prepare(tmp_path)
    assert not runtime.research_slice_config.claim_evidence_provenance
    assert not runtime.research_slice_config.verifier_dependency_catalog
    outcome = asyncio.run(runtime.resume(prepared["research_id"]))
    assert outcome["verdict"] == "PASS"
    assert state.research_slice.snapshot(prepared["research_id"]) is None
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='claim_revisions'").fetchone()
    assert db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 6
    db.close()


def test_integrated_claim_is_material_and_semantically_scoped(research):
    db, state, _, rid, result = research
    claim = state.research_slice.current(rid)[0]
    assert claim.claim_type == "association" and claim.support_state == "SUPPORTED"
    assert claim.scope["dataset_revision"] and claim.scope["sampling"] == "iid"
    assert {"n", "metrics.estimate", "metrics.p_value", "uncertainty.bounds.0", "uncertainty.bounds.1"} <= {s.name for s in claim.numeric_slots}
    assert {s.unit for s in claim.numeric_slots} == {"observations", "dimensionless"}
    assert result["verdict"] == "PASS"
    assert state.research_slice.snapshot(rid)["analysis_precommits"][0]["result_access_state"] == "UNKNOWN"
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 1


@pytest.mark.parametrize("fault", ["locator", "wrong_equal_field", "unit", "causal", "text", "revision", "unknown_zero"])
def test_claim_faults_are_rejected(research, fault):
    _, state, _, rid, _ = research
    claim, bindings = copied_claim(state, rid)
    slot = claim.numeric_slots[0]
    if fault == "locator": slot.locator = "/result/not_present"
    if fault == "wrong_equal_field": slot.locator = "/result/counts/used"
    if fault == "unit": slot.unit = "kilograms"
    if fault == "causal": claim.claim_type = "causal"
    if fault == "text": claim.text = "The method proves a causal effect."
    if fault == "revision": slot.artifact_revision = "99"
    if fault == "unknown_zero": slot.value = None
    with pytest.raises(ValueError):
        state.publish_claim(claim, bindings)
    assert state.research_slice.current(rid)[0].revision == 1


def test_rounding_and_unknown_are_not_zero(research):
    _, state, _, rid, _ = research
    claim, bindings = copied_claim(state, rid)
    slot = next(s for s in claim.numeric_slots if s.name == "metrics.estimate")
    slot.value = round(slot.value, 3)
    slot.display_precision = 3
    slot.tolerance_policy = "ROUND_HALF_EVEN"
    assert state.publish_claim(claim, bindings).revision == 2
    assert state.publish_claim(claim, bindings).revision == 2
    unknown = slot.model_copy(update={"value": None})
    assert numeric_equal(unknown, None) and not numeric_equal(unknown, 0)
    zero = slot.model_copy(update={"value": 0, "tolerance_policy": "EXACT"})
    assert numeric_equal(zero, 0) and not numeric_equal(zero, None)


@pytest.mark.parametrize("kind", ["dataset", "artifact", "plan", "qualification", "source", "experiment"])
def test_parent_change_appends_revision_preserves_history(research, kind):
    db, state, _, rid, result = research
    claim = state.research_slice.current(rid)[0]
    binding = state.research_slice.bindings(claim)[0]
    if kind == "source":
        sid, _, claim = add_source(state, rid)
        state.invalidate_source(rid, sid, "withdrawal")
    elif kind == "dataset":
        state.invalidate_dataset(rid, claim.scope["dataset_id"], "new dataset revision")
    elif kind == "qualification":
        ob = state.research_slice.obligation_records(rid)[0]
        state.change_verifier_qualification(rid, ob.obligation_id, "FAILED")
    elif kind == "experiment":
        state.invalidate_experiment(rid, binding.locator["experiment_id"], "leakage")
    else:
        aid = binding.target_id
        if kind == "plan":
            payload = StagedResult.model_validate_json(db.execute("SELECT payload_json FROM staged_mutations WHERE mutation_id=?", (result["mutation_id"],)).fetchone()[0])
            aid = payload.scientific.plan_artifact_id
        state.invalidate_claim_parent(rid, "artifact", aid, "parent revision")
    revised = next(c for c in state.research_slice.current(rid) if c.claim_id == claim.claim_id)
    assert revised.revision == 2 and revised.support_state == "NEEDS_REVALIDATION"
    assert db.execute("SELECT COUNT(*) FROM claim_revisions WHERE claim_id=?", (claim.claim_id,)).fetchone()[0] == 2
    assert state.research_slice.bindings(claim)[0].status == "ACTIVE"
    assert state.research_slice.bindings(revised)[0].status == "STALE"
    state.revalidate_claim(rid, claim.claim_id)
    assert next(c for c in state.research_slice.current(rid) if c.claim_id == claim.claim_id).support_state == "INCONCLUSIVE"


@pytest.mark.parametrize("fault", ["hash", "dataset", "artifact_status"])
def test_stale_pass_projection_fails_closed_even_without_writer(research, fault):
    db, state, _, rid, _ = research
    claim = state.research_slice.current(rid)[0]
    b = state.research_slice.bindings(claim)[0]
    if fault == "hash":
        record = state.file_artifact(b.target_id, rid)
        state.workspace.path(rid, record["relative_path"]).write_bytes(b"{}")
    elif fault == "dataset":
        db.execute("UPDATE datasets SET sha256=? WHERE dataset_id=?", ("f" * 64, claim.scope["dataset_id"]))
    else:
        db.execute("UPDATE artifacts SET status='INVALIDATED' WHERE artifact_id=?", (b.target_id,))
    assert state.research_slice.snapshot(rid)["claims"][0]["effective_support_state"] == "NEEDS_REVALIDATION"


@pytest.mark.parametrize("fault", ["url_only", "full_text", "wrong_span", "wrong_relation"])
def test_source_binding_requires_actual_support_and_access(research, fault):
    _, state, _, rid, _ = research
    _, _, original = add_source(state, rid)
    claim, bindings = state.research_slice._next_revision(original, state.research_slice.bindings(original))
    b = bindings[0]
    if fault == "url_only": claim.text = "The valid URL proves a treatment effect."
    if fault == "full_text": b.locator["access_level"] = "FULL_TEXT"
    if fault == "wrong_span": b.locator["span"] = "This was never retrieved."
    if fault == "wrong_relation": b.relation = "CONTRADICTS"
    with pytest.raises(ValueError): state.publish_claim(claim, bindings)


def test_multiple_paths_revalidation_and_contradiction_preservation(research):
    _, state, _, rid, _ = research
    first, _, claim = add_source(state, rid, suffix="A")
    second, _, another = add_source(state, rid, suffix="B")
    revised, bindings = state.research_slice._next_revision(claim, state.research_slice.bindings(claim))
    b = state.research_slice.bindings(another)[0].model_copy(deep=True)
    b.claim_id, b.claim_revision, b.scope = revised.claim_id, revised.revision, revised.scope
    b.binding_id += "-second"
    bindings.append(b)
    state.publish_claim(revised, bindings)
    state.invalidate_source(rid, first, "one support withdrawn")
    current = next(c for c in state.research_slice.current(rid) if c.claim_id == claim.claim_id)
    assert current.support_state == "NEEDS_REVALIDATION"
    assert state.revalidate_claim(rid, claim.claim_id).support_state == "SUPPORTED"
    _, _, contradicted = add_source(state, rid, suffix="C", contradiction=True)
    assert contradicted.support_state == "NOT_SUPPORTED"
    from htrsa.research_slice import support_state
    support = state.research_slice.bindings(another)[0]
    contradiction = state.research_slice.bindings(contradicted)[0]
    assert support_state([support, contradiction]) == "CONFLICTED"
    assert support_state([contradiction]) == "NOT_SUPPORTED"


@pytest.mark.parametrize("outcome", ["FAIL", "UNKNOWN", "NOT_RUN", "MISSING", "STALE", "FAILED_QUALIFICATION"])
def test_critical_obligation_cannot_be_overridden(tmp_path, outcome):
    db, state, runtime, prepared, _ = prepare(tmp_path, slice_config=ON)
    original = state.commit
    def commit(mid):
        records = state.research_slice.obligation_records(prepared["research_id"], mid)
        r = records[0]
        if outcome == "MISSING": db.execute("DELETE FROM verifier_obligations WHERE obligation_id=?", (r.obligation_id,))
        else:
            if outcome == "STALE": r.tested_revision = "old"
            elif outcome == "FAILED_QUALIFICATION": r.qualification_status = "FAILED"
            else: r.outcome = outcome
            db.execute("UPDATE verifier_obligations SET payload_json=? WHERE obligation_id=?", (to_json(r), r.obligation_id))
        return original(mid)
    state.commit = commit
    with pytest.raises(ValueError, match="CRITICAL_OBLIGATION"):
        asyncio.run(runtime.resume(prepared["research_id"]))
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    assert not state.research_slice.current(prepared["research_id"])
    db.close()


def test_dependency_unknown_and_shared_wrappers(research):
    _, state, _, rid, _ = research
    records = state.research_slice.obligation_records(rid)
    assert records and all(r.model_family == "UNKNOWN" for r in records)
    assert all(r.qualification_status == "UNKNOWN" for r in records)
    pairs = shared_dependencies(records[:2])
    assert pairs[0]["shared"]["implementation_dependency"]
    assert pairs[0]["independence"] == "NOT_ESTABLISHED"
    unknown = records[0].model_copy(update={"implementation_dependency": ["UNKNOWN"], "data_dependency": ["UNKNOWN"], "evidence_dependency": ["UNKNOWN"]})
    assert not any(shared_dependencies([unknown, records[1]])[0]["shared"].values())


def test_missing_slot_and_claim_staging_abort_atomic_commit(tmp_path):
    db, state, runtime, prepared, _ = prepare(tmp_path, slice_config=ON)
    stage = state.stage
    def incomplete(payload):
        mid = stage(payload)
        value = json.loads(db.execute("SELECT payload_json FROM slice_staging WHERE mutation_id=?", (mid,)).fetchone()[0])
        value["claim"]["numeric_slots"] = []
        db.execute("UPDATE slice_staging SET payload_json=? WHERE mutation_id=?", (to_json(value), mid))
        return mid
    state.stage = incomplete
    with pytest.raises(ValueError): asyncio.run(runtime.resume(prepared["research_id"]))
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    assert not state.research_slice.current(prepared["research_id"])
    assert db.execute("SELECT COUNT(*) FROM staged_mutations WHERE status='COMMITTED'").fetchone()[0] == 0
    db.close()


def test_slice_export_hashes_history_and_stale_tamper(research, tmp_path):
    db, state, _, rid, result = research
    claim, bindings = copied_claim(state, rid)
    state.publish_claim(claim, bindings)
    state.stop_research(rid, "BUDGET_EXHAUSTED")
    export_final_report(state, rid)
    exported = export_release(state, rid, tmp_path / "release")
    payload = json.loads((tmp_path / "release/research_output/research_slice.json").read_text(encoding="utf-8"))
    assert len(payload["claims"]) == 2 and payload["obligations"] and payload["analysis_precommits"]
    assert not any(str(tmp_path) in to_json(item) for item in payload["bindings"])
    for record in exported["files"]:
        assert sha256_file(tmp_path / "release" / record["path"]) == record["sha256"]
    path = state.workspace.path(rid, "research_output/research_slice.json")
    path.write_bytes(b"{}")
    with pytest.raises(ReleaseExportError): export_release(state, rid, tmp_path / "tampered-release")


def test_policy_change_resume_is_blocked(research):
    _, state, _, rid, _ = research
    wrong = AgentRuntime(state, FakeProvider([]), claim_evidence_provenance_enabled=False,
                         verifier_dependency_catalog_enabled=False, verified_analysis_skills_enabled=True,
                         verification_repair_enabled=True)
    with pytest.raises(StateConflictError, match="RESEARCH_SLICE_POLICY"):
        asyncio.run(wrong.resume(rid))


@pytest.mark.parametrize("kind", ["regression", "backtest"])
def test_other_skills_keep_exact_predictive_and_time_scope(tmp_path, monkeypatch, kind):
    from test_verified_analysis_skills import test_other_skill_paths_commit_and_export
    monkeypatch.setenv("HTRSA_CLAIM_EVIDENCE_PROVENANCE", "1")
    monkeypatch.setenv("HTRSA_VERIFIER_DEPENDENCY_CATALOG", "1")
    test_other_skill_paths_commit_and_export(kind, tmp_path)
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    rid = db.execute("SELECT research_id FROM research_runs").fetchone()[0]
    claim = state.research_slice.current(rid)[0]
    assert claim.claim_type == "prediction" and claim.support_state == "SUPPORTED"
    assert claim.scope["skill_version"] == "1.0.0" and claim.scope["split"]
    metric = next(s for s in claim.numeric_slots if s.name == "metrics.candidate.mae")
    assert metric.unit == ("cm" if kind == "regression" else "units")
    if kind == "regression":
        assert claim.scope["alpha"] == 1 and claim.scope["variables"] == {"feature_1": "x", "target": "y"}
    else:
        assert claim.scope["horizon"] == 1 and claim.scope["split"]["lags"] == [1, 2]
        assert all(t["training_label_cutoff"] <= t["origin"] < t["target"] for t in claim.scope["time_trace"])
    db.close()


@pytest.mark.parametrize("variant", ["identical_repeat", "source_order", "prompt_paraphrase", "irrelevant_distractor", "response_format", "dataset_revision", "source_withdrawal", "artifact_hash", "leakage_discovery", "result_invalidation"])
def test_lab_actual_delivered_evidence_and_change_expectations(tmp_path, variant):
    from reliability_lab import perturbation_case, MANIFEST, fixed_ids
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    with fixed_ids():
        row = perturbation_case(tmp_path, variant, manifest, evidence_change=variant in manifest["evidence_changing"])
    assert row["passed"] and row["commits"] == 1
    assert row["actually_delivered_evidence_hashes"]


@pytest.mark.parametrize("boundary", ["AFTER_CLAIM_STAGE", "DURING_CLAIM_COMMIT", "DURING_CLAIM_REVALIDATION", "AFTER_EXPORT"])
def test_claim_recovery_atomicity_and_export_replay(tmp_path, boundary):
    from research_slice_recovery_probe import probe
    assert probe("crash", tmp_path, boundary)["crash_reached"]
    assert probe("resume", tmp_path, boundary)["passed"]


def test_consistency_is_separate_from_correctness_and_independence():
    from htrsa.reliability import compatible, error_correlation
    from htrsa.research_slice_schemas import StructuredConclusion
    a = StructuredConclusion(question_id="q", estimand_id="e", scope={}, support_status="SUPPORTED", estimate=-99,
                             validity_status="VALID")
    assert compatible(a, a.model_copy())
    assert a.estimate != 0.8
    assert error_correlation([0, 0, 0], [0, 0, 0]) == {"status": "NOT_ESTIMABLE", "pairs": 3, "correlation": None}
    assert error_correlation([0, 1, 0, 1], [0, 1, 0, 1])["correlation"] == pytest.approx(1)


def test_slice_report_secret_canary_is_rejected(research, tmp_path):
    db, state, _, rid, _ = research
    record = state.research_slice.obligation_records(rid)[0]
    record.model_family = "sk-researchslicecanary987654321"
    db.execute("UPDATE verifier_obligations SET payload_json=? WHERE obligation_id=?", (to_json(record), record.obligation_id))
    state.stop_research(rid, "BUDGET_EXHAUSTED")
    export_final_report(state, rid)
    with pytest.raises(ReleaseExportError): export_release(state, rid, tmp_path / "canary-release")


def test_derivation_cycles_rejected_without_reusing_contradiction_edges(research):
    _, state, _, rid, _ = research
    claim = state.research_slice.current(rid)[0]
    binding = state.research_slice.bindings(claim)[0]
    with pytest.raises(ValueError, match="DERIVATION_CYCLE"):
        state.research_slice._edge(rid, "artifact", binding.target_id, "claim", f"{claim.claim_id}@1")


def test_read_api_projects_typed_current_state_without_mutation(research, tmp_path):
    from htrsa.dashboard import DashboardReadAPI
    db, state, _, rid, _ = research
    version = state.state_version(rid)
    api = DashboardReadAPI(tmp_path / "state.sqlite", tmp_path / "workspace")
    reply = api.request(f"/api/research/{rid}/research-slice")
    assert reply.status == 200 and reply.body["claims"][0]["effective_support_state"] == "SUPPORTED"
    assert state.state_version(rid) == version
    api.close()


@pytest.mark.parametrize("fault", ["missing_policy", "changed_policy"])
def test_commit_cannot_silently_disable_staged_policy(tmp_path, fault):
    db, state, runtime, prepared, _ = prepare(tmp_path, slice_config=ON)
    original = state.commit
    def commit(mid):
        if fault == "missing_policy":
            db.execute("DELETE FROM runtime_steps WHERE step_key='research_slice_config'")
        else:
            changed = ResearchSliceConfig().model_dump(mode="json")
            db.execute("UPDATE runtime_steps SET output_json=? WHERE step_key='research_slice_config'", (to_json(changed),))
        return original(mid)
    state.commit = commit
    with pytest.raises(StateConflictError): asyncio.run(runtime.resume(prepared["research_id"]))
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    db.close()


@pytest.mark.parametrize("fault", ["qualification", "missing_claim"])
def test_report_cannot_cite_stale_or_missing_claim(research, fault):
    from htrsa.final_report import ReportValidationError
    db, state, _, rid, _ = research
    if fault == "qualification":
        ob = state.research_slice.obligation_records(rid)[0]
        state.change_verifier_qualification(rid, ob.obligation_id, "FAILED")
    else:
        db.execute("UPDATE claim_revisions SET current=0 WHERE research_id=?", (rid,))
    state.stop_research(rid, "BUDGET_EXHAUSTED")
    with pytest.raises(ReportValidationError): export_final_report(state, rid)


def test_source_snapshot_hash_revision_revokes_current_support(research):
    from htrsa.scholarly import metadata_digest
    db, state, _, rid, _ = research
    sid, _, claim = add_source(state, rid)
    original = state.research_slice.bindings(claim)[0]
    source = source_from_row(state._one("SELECT * FROM sources WHERE source_id=?", (sid,)))
    revised = source.model_copy(update={"abstract": "Temperature and growth show no association in the revised sample."})
    new_hash = metadata_digest(revised)
    # 외부 부모 수정본의 오류를 주입한다. 제공사 쓰기 경로는 아니다.
    db.execute("UPDATE sources SET abstract=?,metadata_hash=? WHERE source_id=?", (revised.abstract, new_hash, sid))
    current = next(c for c in state.research_slice.snapshot(rid)["claims"] if c["claim_id"] == claim.claim_id)
    assert current["effective_support_state"] == "NEEDS_REVALIDATION"
    state.invalidate_claim_parent(rid, "source", sid, "source revision:" + new_hash)
    assert next(c for c in state.research_slice.current(rid) if c.claim_id == claim.claim_id).revision == 2
    assert original.locator["source_snapshot"]["abstract"] == source.abstract
    assert original.target_hash != new_hash


def test_changed_checker_implementation_is_not_current_support(research, monkeypatch):
    import htrsa.research_slice as module
    _, state, _, rid, _ = research
    monkeypatch.setattr(module, "implementation_dependencies", lambda check: ["code:" + "f" * 64])
    assert state.research_slice.snapshot(rid)["claims"][0]["effective_support_state"] == "NEEDS_REVALIDATION"


def test_changed_checker_version_between_verify_commit_blocks(tmp_path, monkeypatch):
    import htrsa.research_slice as module
    db, state, runtime, prepared, _ = prepare(tmp_path, slice_config=ON)
    commit = state.commit
    def changed(mid):
        monkeypatch.setattr(module, "implementation_dependencies", lambda check: ["code:" + "f" * 64])
        return commit(mid)
    state.commit = changed
    with pytest.raises(ValueError, match="CRITICAL_OBLIGATION"):
        asyncio.run(runtime.resume(prepared["research_id"]))
    assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    db.close()
