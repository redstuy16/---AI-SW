"""기존 동작과 검증 경계를 확인하는 회귀 테스트."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from htrsa.context_compiler import ContextCompiler
from htrsa.database import initialize
from htrsa.final_report import (ReportValidationError, build_final_conclusion,
                                export_final_report, resolve_numeric_placeholders)
from htrsa.literature import (LiteratureCoordinator, LiteratureConfig,
                              LiteralSentenceReviewer, default_intents, extract_abstract_evidence,
                              screen_source, synthesize_literature)
from htrsa.literature_runtime import LiteratureResearchRuntime
from htrsa.providers.fake import FakeProvider
from htrsa.scholarly import (CrossrefProvider, FakeScholarlyProvider, NormalizedSource,
                             OpenAlexProvider, ScholarlyError, ScholarlyHTTPClient,
                             SearchRequest, normalize_crossref_work,
                             normalize_doi, normalize_openalex_work)
from htrsa.schemas import ContextPolicy, ResearchContract, new_id
from htrsa.service import ContractViolationError, StateService
from htrsa.storage import Workspace

from test_autonomous_loop import CSV, MODELS, fake_replies


GOAL = "Analyze temperature and growth without inferring causality."


def make_state(tmp_path):
    db = initialize(tmp_path / "research.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    rid = state.create_research(GOAL)
    state.workspace.prepare(rid)
    state.configure_budget(rid, 0.25, 0.75, 1.0)
    return db, state, rid


def work(title, abstract, doi, openalex=None):
    return NormalizedSource(title=title, abstract=abstract, doi=doi, openalex_id=openalex,
                            publication_year=2024, authors=["Researcher A"],
                            provider="scholarly.fake", provider_ids={"doi": doi or ""})


def results(question=GOAL):
    intents = default_intents(question)
    positive = work("Temperature and growth association in seedlings",
                    "Temperature and growth were positively associated in a controlled seedling sample.",
                    "10.1000/support", "W123")
    negative = work("Null temperature growth relationship",
                    "No association between temperature and growth was observed in an independent cohort.",
                    "10.1000/null", "W124")
    mechanism = work("Temperature and growth mechanism",
                     "Temperature and growth were linked by metabolic activity.", "10.1000/mechanism")
    hypothesis_null = work("Temperature growth null association in field samples",
                           "No association between temperature and growth was found in field samples.",
                           "10.1000/hypothesis-null")
    irrelevant = work("Distant galaxy survey", "No relation to the plant sample was discussed.",
                      "10.1000/galaxy")
    return {intents[0].query: [positive, negative, irrelevant],
            intents[1].query: [mechanism, irrelevant],
            intents[2].query: [negative],
            default_intents("Temperature and growth have a monotonic association.")[2].query: [hypothesis_null]}


def test_provider_normalization_and_doi():
    assert normalize_doi("https://doi.org/10.1000/ABC") == "10.1000/abc"
    assert normalize_doi("doi:10.1000/ABC") == "10.1000/abc"
    openalex = normalize_openalex_work({"id": "https://openalex.org/W123", "title": "Test paper",
                                        "doi": "https://doi.org/10.1000/ABC", "publication_year": 2024,
                                        "authorships": [{"author": {"display_name": "A B"}}],
                                        "abstract_inverted_index": {"No": [0], "association": [1], "found": [2]},
                                        "open_access": {"is_oa": True}, "cited_by_count": 7})
    assert (openalex.openalex_id, openalex.doi, openalex.abstract) == ("W123", "10.1000/abc", "No association found")
    crossref = normalize_crossref_work({"DOI": "10.1000/ABC", "title": ["Test paper"],
                                        "author": [{"given": "A", "family": "B"}],
                                        "published": {"date-parts": [[2024, 1, 1]]}})
    assert crossref.abstract is None and crossref.doi == openalex.doi
    assert crossref.authors == ["A B"]
    with pytest.raises(ValueError):
        SearchRequest(research_id="R-test", query="temperature", limit=1000)


def test_search_filters_do_not_trust_provider_metadata():
    source = work("Temperature growth", "Temperature and growth were associated.", None)
    provider = FakeScholarlyProvider({"temperature": [source]})
    result = asyncio.run(provider.search(SearchRequest(research_id="R-test", query="temperature",
                                                       require_doi=True, open_access_only=True)))
    assert result.sources == []
    source = source.model_copy(update={"doi": "10.1000/filter", "is_open_access": True})
    provider = FakeScholarlyProvider({"temperature": [source]})
    result = asyncio.run(provider.search(SearchRequest(research_id="R-test", query="temperature",
                                                       require_doi=True, open_access_only=True,
                                                       year_from=2020, year_to=2025)))
    assert len(result.sources) == 1


def test_http_retry_timeout_malformed_and_size(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"results": []})
    client = ScholarlyHTTPClient(retries=1, transport=httpx.MockTransport(handler))
    result = asyncio.run(client.get_json("https://api.openalex.org/works"))
    assert result == {"results": []} and len(calls) == 2
    def timeout(request):
        raise httpx.ReadTimeout("test")
    with pytest.raises(ScholarlyError, match="request failed"):
        asyncio.run(ScholarlyHTTPClient(retries=1, transport=httpx.MockTransport(timeout)).get_json("https://api.openalex.org/works"))
    with pytest.raises(ScholarlyError, match="JSON"):
        asyncio.run(ScholarlyHTTPClient(retries=0, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="bad"))).get_json("https://api.openalex.org/works"))
    with pytest.raises(ScholarlyError, match="size limit"):
        asyncio.run(ScholarlyHTTPClient(retries=0, max_bytes=5, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text='{"too":10}'))).get_json("https://api.openalex.org/works"))


def test_openalex_and_crossref_adapters_with_mock_transport():
    def handler(request):
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, json={"results": [{"id": "https://openalex.org/W123",
                "title": "Temperature growth", "doi": "10.1000/a", "abstract_inverted_index": None}],
                "meta": {"count": 1}})
        return httpx.Response(200, json={"message": {"DOI": "10.1000/a", "title": ["Temperature growth"]}})
    client = ScholarlyHTTPClient(retries=0, transport=httpx.MockTransport(handler))
    request = SearchRequest(research_id="R-test", query="temperature growth", limit=1)
    result = asyncio.run(OpenAlexProvider(client, api_key="secret").search(request))
    assert result.sources[0].abstract is None
    assert result.sources[0].openalex_id == "W123"
    assert asyncio.run(CrossrefProvider(client).get_work("doi:10.1000/a")).doi == "10.1000/a"


def test_source_dedup_relevance_and_alignment(tmp_path):
    db, state, rid = make_state(tmp_path)
    candidate = results()[default_intents(GOAL)[0].query][0]
    source_id, created = state.upsert_source(rid, candidate)
    assert created
    crossref = candidate.model_copy(update={"provider": "scholarly.crossref", "openalex_id": None,
                                            "doi": "doi:10.1000/support"})
    same_id, created = state.upsert_source(rid, crossref)
    assert same_id == source_id and not created
    assert db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    conflicting = candidate.model_copy(update={"title": "Different bibliographic title"})
    same_id, created = state.upsert_source(rid, conflicting)
    assert same_id == source_id and not created
    assert db.execute("SELECT collision_warning FROM sources WHERE source_id=?",
                      (source_id,)).fetchone()[0] == "DOI_METADATA_CONFLICT"
    relevance = screen_source(source_id, candidate, GOAL)
    assert relevance.relevance == "DIRECT"
    state.set_source_relevance(rid, relevance)
    state.mark_source_extracted(rid, source_id)
    evidence = extract_abstract_evidence(source_id, candidate)
    assert evidence.polarity == "SUPPORT"
    with pytest.raises(ContractViolationError):
        state.verify_literature_evidence(rid, evidence.model_copy(update={"claim": "Fabricated outcome"}), LiteralSentenceReviewer())
    with pytest.raises(ContractViolationError):
        state.verify_literature_evidence(rid, evidence.model_copy(update={"evidence_text": "No such text"}), LiteralSentenceReviewer())
    with pytest.raises(ContractViolationError):
        state.verify_literature_evidence(rid, evidence, None)
    evidence_id = state.verify_literature_evidence(rid, evidence, LiteralSentenceReviewer())
    row = db.execute("SELECT source_metadata_hash,text_hash,status FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
    assert row["status"] == "VERIFIED" and len(row["text_hash"]) == 64 and len(row["source_metadata_hash"]) == 64
    db.close()


def test_cache_resume_limits_and_missing_abstract(tmp_path):
    db, state, rid = make_state(tmp_path)
    provider = FakeScholarlyProvider(results())
    coordinator = LiteratureCoordinator(state, provider)
    first = asyncio.run(coordinator.run(rid, GOAL))
    assert len(provider.calls) == 3
    second = asyncio.run(coordinator.run(rid, GOAL))
    assert len(provider.calls) == 3 and first.support_evidence_refs == second.support_evidence_refs
    assert first.contradiction_evidence_refs
    assert db.execute("SELECT COUNT(*) FROM scholarly_search_cache").fetchone()[0] == 3
    with pytest.raises(ValueError, match="hard caps"):
        LiteratureConfig(max_queries=100, max_results_per_query=1000)
    missing = work("Temperature growth metadata", None, "10.1000/noabstract")
    source_id, _ = state.upsert_source(rid, missing)
    relevance = screen_source(source_id, missing, GOAL)
    assert relevance.relevance == "INDIRECT"
    state.set_source_relevance(rid, relevance)
    assert extract_abstract_evidence(source_id, missing) is None
    db.close()


def test_provider_failure_fallback_and_no_results(tmp_path):
    db, state, rid = make_state(tmp_path)
    class FailingProvider(FakeScholarlyProvider):
        async def search(self, request):
            self.calls.append(request.query)
            raise ScholarlyError("temporary failure")
    primary = FailingProvider({})
    fallback = FakeScholarlyProvider(results())
    first = asyncio.run(LiteratureCoordinator(state, primary, fallback=fallback).run(rid, GOAL))
    assert len(primary.calls) == len(fallback.calls) == 3
    assert first.support_evidence_refs and first.contradiction_evidence_refs
    no_results = LiteratureCoordinator(state, FakeScholarlyProvider({}))
    other = state.create_research("Another temperature and growth question")
    state.workspace.prepare(other)
    state.configure_budget(other, 0.25, 0.75, 1.0)
    synthesis = asyncio.run(no_results.run(other, "Another temperature and growth question"))
    assert synthesis.gap_label == "INSUFFICIENT_EVIDENCE"
    assert "LITERATURE_INCOMPLETE: no verified source evidence" in synthesis.unresolved_issues
    db.close()


def test_source_fallback_collision_and_invalid_status(tmp_path):
    db, state, rid = make_state(tmp_path)
    candidate = work("Temperature growth title", "Temperature and growth were linked in samples.", None)
    first, _ = state.upsert_source(rid, candidate)
    same, created = state.upsert_source(rid, candidate)
    assert same == first and not created
    assert db.execute("SELECT collision_warning FROM sources WHERE source_id=?", (first,)).fetchone()[0]
    state.set_source_relevance(rid, screen_source(first, candidate, GOAL))
    state.mark_source_extracted(rid, first)
    evidence = extract_abstract_evidence(first, candidate)
    state.invalidate_source(rid, first, "Retraction notice")
    with pytest.raises(ContractViolationError, match="alignment failed"):
        state.verify_literature_evidence(rid, evidence, LiteralSentenceReviewer())
    db.close()


def test_crossref_metadata_is_enriched_before_evidence_verification(tmp_path):
    db, state, rid = make_state(tmp_path)
    crossref = work("Temperature and growth association", None, "10.1000/enrich")
    first, _ = state.upsert_source(rid, crossref)
    openalex = crossref.model_copy(update={
        "abstract": "Temperature and growth were associated in the cohort.",
        "openalex_id": "W987", "provider": "scholarly.openalex",
        "referenced_works": ["W123"]})
    same, created = state.upsert_source(rid, openalex)
    assert same == first and not created
    row = db.execute("SELECT abstract,openalex_id,referenced_works_json,metadata_hash FROM sources WHERE source_id=?",
                     (first,)).fetchone()
    assert row["abstract"] == openalex.abstract and row["openalex_id"] == "W987"
    assert json.loads(row["referenced_works_json"]) == ["W123"]
    state.set_source_relevance(rid, screen_source(first, openalex, GOAL))
    state.mark_source_extracted(rid, first)
    evidence = extract_abstract_evidence(first, openalex)
    state.verify_literature_evidence(rid, evidence, LiteralSentenceReviewer())
    assert len(row["metadata_hash"]) == 64
    db.close()


def test_neutral_abstract_is_preserved_without_support_claim(tmp_path):
    db, state, rid = make_state(tmp_path)
    candidate = work("Temperature growth measurements",
                     "Temperature and growth were measured during the sampling period.",
                     "10.1000/neutral")
    source_id, _ = state.upsert_source(rid, candidate)
    state.set_source_relevance(rid, screen_source(source_id, candidate, GOAL))
    state.mark_source_extracted(rid, source_id)
    item = extract_abstract_evidence(source_id, candidate)
    assert item.polarity == "NEUTRAL"
    evidence_id = state.verify_literature_evidence(rid, item, LiteralSentenceReviewer())
    synthesis = synthesize_literature(state, rid)
    assert synthesis.neutral_evidence_refs == [evidence_id]
    assert not synthesis.support_evidence_refs and not synthesis.contradiction_evidence_refs
    db.close()


def test_budgeted_model_semantic_review_cannot_override_source_alignment(tmp_path):
    db, state, rid = make_state(tmp_path)
    candidate = work("Temperature and growth association",
                     "Temperature and growth were associated in the sampled cohort.",
                     "10.1000/model-review")
    source_id, _ = state.upsert_source(rid, candidate)
    state.set_source_relevance(rid, screen_source(source_id, candidate, GOAL))
    state.mark_source_extracted(rid, source_id)
    evidence = extract_abstract_evidence(source_id, candidate)
    digest = hashlib.sha256(evidence.evidence_text.encode("utf-8")).hexdigest()
    def approved(call):
        assert call["role"] == "literature_verification_coordinator"
        assert "untrusted" in call["input_text"]
        return {"source_id": source_id, "text_hash": digest, "polarity": "SUPPORT",
                "claim_aligned": True, "polarity_aligned": True, "overclaim": False,
                "reason": "Exact abstract sentence describes the proposed association."}
    negative = work("Temperature and growth null relationship",
                    "No association between temperature and growth was observed in the field.",
                    "10.1000/model-rejected")
    negative_id, _ = state.upsert_source(rid, negative)
    state.set_source_relevance(rid, screen_source(negative_id, negative, GOAL))
    state.mark_source_extracted(rid, negative_id)
    negative_evidence = extract_abstract_evidence(negative_id, negative)
    negative_digest = hashlib.sha256(negative_evidence.evidence_text.encode("utf-8")).hexdigest()
    rejected = {"source_id": negative_id, "text_hash": negative_digest, "polarity": "CONTRADICT",
                "claim_aligned": True, "polarity_aligned": True, "overclaim": True,
                "reason": "The proposed inference overstates the source."}
    provider = FakeProvider([approved, rejected])
    runtime = LiteratureResearchRuntime(state, provider, FakeScholarlyProvider({}),
                                        models={**MODELS, "literature_verification_coordinator": "fake-review"},
                                        semantic_model_review=True)
    reviewer = asyncio.run(runtime.literature.reviewer.prepare(rid, source_id, evidence))
    evidence_id = state.verify_literature_evidence(rid, evidence, reviewer)
    assert db.execute("SELECT status FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()[0] == "VERIFIED"
    assert db.execute("SELECT COUNT(*) FROM agent_runs WHERE research_id=? AND actor_role='literature_verification_coordinator' AND status='COMPLETED'",
                      (rid,)).fetchone()[0] == 1
    with pytest.raises(ContractViolationError, match="cannot be replaced"):
        state.verify_literature_evidence(rid, evidence.model_copy(update={"limitations": ["Changed"]}), reviewer)
    fabricated = evidence.model_copy(update={"claim": "The study proved causality.",
                                             "evidence_text": "The study proved causality."})
    with pytest.raises(ContractViolationError, match="alignment failed"):
        state.verify_literature_evidence(rid, fabricated, reviewer)
    negative_reviewer = asyncio.run(runtime.literature.reviewer.prepare(rid, negative_id, negative_evidence))
    with pytest.raises(ContractViolationError, match="semantic review"):
        state.verify_literature_evidence(rid, negative_evidence, negative_reviewer)
    assert db.execute("SELECT COUNT(*) FROM evidence WHERE source_id=?", (negative_id,)).fetchone()[0] == 0
    db.close()


def test_literature_pipeline_uses_structured_model_review_when_enabled(tmp_path):
    db, state, rid = make_state(tmp_path)
    candidate = work("Temperature and growth association",
                     "Temperature and growth were associated in the sampled cohort.",
                     "10.1000/pipeline-review")
    source_id, _ = state.upsert_source(rid, candidate)
    evidence = extract_abstract_evidence(source_id, candidate)
    digest = hashlib.sha256(evidence.evidence_text.encode("utf-8")).hexdigest()
    review = {"source_id": source_id, "text_hash": digest, "polarity": "SUPPORT",
              "claim_aligned": True, "polarity_aligned": True, "overclaim": False,
              "reason": "The literal abstract sentence states an association."}
    queries = default_intents(GOAL)
    scholarly = FakeScholarlyProvider({queries[0].query: [candidate]})
    runtime = LiteratureResearchRuntime(
        state, FakeProvider([review]), scholarly,
        models={**MODELS, "literature_verification_coordinator": "fake-review"},
        semantic_model_review=True)
    synthesis = asyncio.run(runtime.literature.run(rid, GOAL))
    assert len(synthesis.support_evidence_refs) == 1
    assert db.execute("SELECT COUNT(*) FROM agent_runs WHERE research_id=? AND actor_role='literature_verification_coordinator'",
                      (rid,)).fetchone()[0] == 1
    db.close()


def test_literature_resume_uses_cache_after_interrupted_search(tmp_path):
    db = initialize(tmp_path / "research.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    class InterruptingProvider(FakeScholarlyProvider):
        async def search(self, request):
            if len(self.calls) == 1:
                self.calls.append(request.query)
                raise RuntimeError("simulated crash during second query")
            return await super().search(request)
    first_provider = InterruptingProvider(results())
    runtime = LiteratureResearchRuntime(state, FakeProvider(fake_replies()), first_provider, models=MODELS)
    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(runtime.run(GOAL, CSV))
    rid = db.execute("SELECT research_id FROM research_runs").fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM scholarly_search_cache WHERE research_id=?", (rid,)).fetchone()[0] == 1
    resumed_provider = FakeScholarlyProvider(results())
    resumed = LiteratureResearchRuntime(state, FakeProvider(fake_replies()), resumed_provider, models=MODELS)
    result = asyncio.run(resumed.resume(rid))
    assert result["stop_reason"] == "GOAL_ANSWERED"
    assert len(resumed_provider.calls) == 3
    assert db.execute("SELECT COUNT(*) FROM sources WHERE research_id=?", (rid,)).fetchone()[0] == 5
    db.close()


def test_literature_context_and_full_report_e2e(tmp_path):
    db = initialize(tmp_path / "research.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    scholarly = FakeScholarlyProvider(results())
    runtime = LiteratureResearchRuntime(state, FakeProvider(fake_replies()), scholarly, models=MODELS)
    result = asyncio.run(runtime.run(GOAL, CSV))
    rid = result["research_id"]
    assert result["stop_reason"] == "GOAL_ANSWERED" and result["action_count"] >= 20
    assert len(scholarly.calls) == 4
    assert db.execute("SELECT COUNT(*) FROM research_actions WHERE research_id=? AND action_type='SEARCH_LITERATURE'",
                      (rid,)).fetchone()[0] == 4
    conclusion = build_final_conclusion(state, rid)
    assert conclusion.support_level == "PARTIALLY_SUPPORTED"
    assert conclusion.contradiction_refs and len(conclusion.experiment_refs) == 2
    assert db.execute("SELECT COUNT(*) FROM evidence WHERE research_id=? AND source_type='LITERATURE' AND polarity='CONTRADICT' AND target_hypothesis_id=?",
                      (rid, result["hypothesis_id"])).fetchone()[0] == 1
    report = Path(result["final_report"]).read_text(encoding="utf-8")
    summary = json.loads(Path(result["research_summary"]).read_text(encoding="utf-8"))
    assert "## References" in report and "CONTRADICT" in report and "p_value=" in report
    assert summary["total_actions"] >= 20 and summary["verified_experiment_count"] == 2
    assert Path(result["final_report"]).parent.joinpath("manifests/artifact_manifest.json").is_file()
    manifest_dir = Path(result["final_report"]).parent / "manifests"
    artifact_manifest = json.loads((manifest_dir / "artifact_manifest.json").read_text(encoding="utf-8"))
    tool_manifest = json.loads((manifest_dir / "tool_manifest.json").read_text(encoding="utf-8"))
    assert any(item["artifact_type"] == "STATS_RESULT" for item in artifact_manifest["scientific_artifacts"])
    assert "stats.run" in {item["tool_name"] for item in tool_manifest["tools"]}
    assert db.execute("SELECT COUNT(*) FROM entity_edges WHERE research_id=? AND edge_type='generated_from' AND to_type='source'",
                      (rid,)).fetchone()[0] >= 1
    contract = ResearchContract(contract_id=new_id("C"), research_id=rid, task_type="review",
                                issued_by="system", assigned_role="manager", objective="temperature growth",
                                context_policy=ContextPolicy(max_context_tokens=10000), output_schema_id="review")
    state.issue_contract(contract)
    bundle = ContextCompiler(state).compile(contract)
    assert any(item["polarity"] == "CONTRADICT" for item in bundle.active_state["verified_literature"])
    assert any(item.source_id and item.polarity == "CONTRADICT" for item in bundle.items)
    assert asyncio.run(runtime.resume(rid))["already_terminal"]
    with pytest.raises(ReportValidationError, match="contradiction omitted"):
        export_final_report(state, rid, conclusion.model_copy(update={"contradiction_refs": []}))
    with pytest.raises(ReportValidationError, match="evidence reference"):
        export_final_report(state, rid, conclusion.model_copy(update={"evidence_refs": ["E-nonexistent"]}))
    with pytest.raises(ReportValidationError, match="limitation reference"):
        export_final_report(state, rid, conclusion.model_copy(update={"limitation_refs": ["SRC-" + "0" * 32]}))
    with pytest.raises(ReportValidationError, match="numeric"):
        resolve_numeric_placeholders(state, rid, f"{{{{NUM:{conclusion.experiment_refs[0]}:fake_p_value}}}}")
    with pytest.raises(ReportValidationError):
        export_final_report(state, rid, conclusion.model_copy(update={"conclusion": "Evidence proves p=0.00001"}))
    with pytest.raises(ReportValidationError, match="unresolved DOI"):
        export_final_report(state, rid, conclusion.model_copy(update={"conclusion": "See DOI 10.9999/fabricated"}))
    literature_source = db.execute("SELECT source_id FROM evidence WHERE evidence_id=?",
                                   (conclusion.evidence_refs[0],)).fetchone()[0]
    original_abstract = db.execute("SELECT abstract FROM sources WHERE source_id=?",
                                   (literature_source,)).fetchone()[0]
    with db:
        db.execute("UPDATE sources SET abstract='Tampered provider text' WHERE source_id=?", (literature_source,))
    with pytest.raises(ReportValidationError, match="metadata was modified"):
        export_final_report(state, rid)
    with db:
        db.execute("UPDATE sources SET abstract=? WHERE source_id=?", (original_abstract, literature_source))
    state.invalidate_source(rid, literature_source, "Retraction notice")
    assert db.execute("SELECT status FROM evidence WHERE evidence_id=?",
                      (conclusion.evidence_refs[0],)).fetchone()[0] == "INVALIDATED"
    state.invalidate_experiment(rid, conclusion.experiment_refs[1], "Data leakage")
    revised = build_final_conclusion(state, rid)
    assert conclusion.experiment_refs[1] not in revised.experiment_refs
    assert conclusion.experiment_refs[1] in revised.limitation_refs
    with pytest.raises(ReportValidationError, match="unverified numeric"):
        resolve_numeric_placeholders(state, rid,
                                     f"{{{{NUM:{conclusion.experiment_refs[1]}:estimate}}}}")
    with pytest.raises(ReportValidationError, match="active research"):
        export_final_report(state, rid)
    db.close()
