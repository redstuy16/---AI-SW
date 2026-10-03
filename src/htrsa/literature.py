"""보수적인 문헌 선별·인용 검증·종합."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Literal

from pydantic import Field

from .database import from_json, to_json
from .schemas import StrictModel
from .scholarly import (NormalizedSource, ScholarlySearchProvider, SearchIntent,
                        SearchRequest, SearchResult, ScholarlyError, metadata_digest,
                        normalize_doi, source_from_row)


class RelevanceResult(StrictModel):
    source_id: str
    relevance: Literal["DIRECT", "INDIRECT", "IRRELEVANT"]
    reason: str = Field(min_length=1)
    target_hypotheses: list[str] = Field(default_factory=list)


class ExtractedEvidence(StrictModel):
    source_id: str
    target_hypothesis_id: str | None = None
    claim: str = Field(min_length=1)
    polarity: Literal["SUPPORT", "CONTRADICT", "NEUTRAL"]
    evidence_text: str = Field(min_length=1)
    evidence_location: str = Field(min_length=1)
    text_field: Literal["abstract", "title"]
    limitations: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class LiteratureEvidenceReview(StrictModel):
    source_id: str
    text_hash: str
    polarity: Literal["SUPPORT", "CONTRADICT", "NEUTRAL"]
    claim_aligned: bool
    polarity_aligned: bool
    overclaim: bool
    reason: str = Field(min_length=1)


class LiteratureSynthesis(StrictModel):
    research_id: str
    active_hypothesis_ids: list[str] = Field(default_factory=list)
    support_evidence_refs: list[str] = Field(default_factory=list)
    contradiction_evidence_refs: list[str] = Field(default_factory=list)
    neutral_evidence_refs: list[str] = Field(default_factory=list)
    unresolved_issues: list[str] = Field(default_factory=list)
    candidate_gap: str | None = None
    gap_label: Literal["OBSERVED_GAP", "POSSIBLE_GAP", "INSUFFICIENT_EVIDENCE"] = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class LiteratureConfig:
    max_queries: int = 4
    max_results_per_query: int = 10
    max_relevant: int = 12
    max_verified_sources: int = 8

    def __post_init__(self):
        if not (2 <= self.max_queries <= 4 and 1 <= self.max_results_per_query <= 10
                and 1 <= self.max_relevant <= 12 and 1 <= self.max_verified_sources <= 8):
            raise ValueError("literature hard caps exceeded")


def default_intents(question: str) -> list[SearchIntent]:
    terms = " ".join(question.strip().split())[:200]
    if len(terms) < 2:
        raise ValueError("research question is required")
    tokens = [word for word in re.findall(r"[A-Za-z][A-Za-z0-9-]*", terms)
              if word.lower() not in {"how", "are", "and", "the", "does", "what", "with", "between",
                                      "analyze", "assess", "investigate", "whether", "without",
                                      "inferring", "causality", "have", "has", "a", "an"}]
    subject = " ".join(tokens[:8]) or terms
    return [SearchIntent(kind="CORE", query=subject),
            SearchIntent(kind="MECHANISM", query=f"{subject} mechanism"),
            SearchIntent(kind="CONTRADICTION", query=f"null association negative result {subject}")]


def _terms(value: str) -> set[str]:
    return {word for word in re.findall(r"[a-z]{4,}", value.casefold())
            if word not in {"with", "from", "that", "this", "between", "result", "results", "study", "association"}}


def screen_source(source_id: str, source: NormalizedSource, question: str,
                  hypothesis_ids: list[str] | None = None) -> RelevanceResult:
    query = _terms(question)
    title_hits = query & _terms(source.title)
    all_hits = query & _terms(source.title + " " + (source.abstract or ""))
    if not all_hits:
        return RelevanceResult(source_id=source_id, relevance="IRRELEVANT",
                               reason="No lexical overlap with the research question", target_hypotheses=[])
    if source.abstract and (len(title_hits) >= 1 or len(all_hits) >= 2):
        return RelevanceResult(source_id=source_id, relevance="DIRECT",
                               reason="Title and retrieved abstract overlap the question",
                               target_hypotheses=hypothesis_ids or [])
    return RelevanceResult(source_id=source_id, relevance="INDIRECT",
                           reason="Metadata overlap only; abstract support is unavailable or weak",
                           target_hypotheses=hypothesis_ids or [])


_NEGATIVE = re.compile(r"\b(?:no (?:association|correlation|relationship|effect)|not (?:associated|correlated)|non-significant|nonsignificant|null (?:result|association|effect|relationship)|negative (?:result|association|correlation)|failed to (?:find|detect|show|observe))\b", re.I)
_POSITIVE = re.compile(r"\b(association|associated|correlation|correlated|relationship|linked|increased|decreased)\b", re.I)


def sentence_polarity(sentence: str) -> Literal["SUPPORT", "CONTRADICT", "NEUTRAL"]:
    if _NEGATIVE.search(sentence):
        return "CONTRADICT"
    if _POSITIVE.search(sentence):
        return "SUPPORT"
    return "NEUTRAL"


def extract_abstract_evidence(source_id: str, source: NormalizedSource,
                              target_hypothesis_id: str | None = None) -> ExtractedEvidence | None:
    if not source.abstract:
        return None
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", source.abstract) if part.strip()]
    sentences = [part for part in sentences if len(part) >= 20]
    if not sentences:
        return None
    substantive = next((part for part in sentences if sentence_polarity(part) != "NEUTRAL"), sentences[0])
    polarity = sentence_polarity(substantive)
    return ExtractedEvidence(source_id=source_id, target_hypothesis_id=target_hypothesis_id,
                             claim=substantive, polarity=polarity, evidence_text=substantive,
                             evidence_location="abstract sentence", text_field="abstract",
                             limitations=["Abstract only; no full-text or independent replication reviewed"],
                             confidence=0.5 if polarity == "NEUTRAL" else 0.7)


class LiteralSentenceReviewer:
    """명시적인 지원·반박 표현이 있는 원문 문장만 받는다."""

    def __call__(self, source, item: ExtractedEvidence) -> bool:
        return (item.claim.strip() == item.evidence_text.strip()
                and sentence_polarity(item.evidence_text) == item.polarity
                and len(_terms(source["title"]) & _terms(item.evidence_text)) >= 2
                and (item.polarity == "NEUTRAL" or bool(_POSITIVE.search(item.evidence_text) or _NEGATIVE.search(item.evidence_text))))


def verify_alignment(source, item: ExtractedEvidence,
                     reviewer: Callable | None) -> dict[str, object]:
    def fail(reason: str) -> dict[str, object]:
        return {"passed": False, "reason": reason}

    if source["status"] not in {"RELEVANT", "EVIDENCE_EXTRACTED", "VERIFIED"}:
        return fail("source status is not relevant")
    if metadata_digest(source_from_row(source)) != source["metadata_hash"]:
        return fail("source metadata hash mismatch")
    if source["doi"] and normalize_doi(source["doi"]) != source["doi"]:
        return fail("source DOI is not canonical")
    if item.text_field != "abstract":
        return fail("title or metadata alone cannot verify a scientific claim")
    text = source[item.text_field]
    if not text or item.evidence_text not in text:
        return fail("quoted text is missing from retrieved source")
    if item.claim.strip() != item.evidence_text.strip():
        return fail("claim is not a literal retrieved sentence")
    if sentence_polarity(item.evidence_text) != item.polarity:
        return fail("polarity conflicts with retrieved sentence")
    if reviewer is None or not reviewer(source, item):
        return fail("semantic review did not pass")
    return {"passed": True, "reason": "literal source alignment and semantic review passed",
            "reviewer": type(reviewer).__name__}


def synthesize_literature(state, research_id: str) -> LiteratureSynthesis:
    rows = state._db.execute("SELECT e.evidence_id,e.polarity,e.source_id FROM evidence e JOIN sources s ON s.source_id=e.source_id WHERE e.research_id=? AND e.source_type='LITERATURE' AND e.status='VERIFIED' AND s.status='VERIFIED' ORDER BY e.rowid",
                             (research_id,)).fetchall()
    support = [r["evidence_id"] for r in rows if r["polarity"] == "SUPPORT"]
    contradiction = [r["evidence_id"] for r in rows if r["polarity"] == "CONTRADICT"]
    neutral = [r["evidence_id"] for r in rows if r["polarity"] == "NEUTRAL"]
    missing = state._db.execute("SELECT COUNT(*) FROM sources WHERE research_id=? AND status='RELEVANT' AND abstract IS NULL",
                                (research_id,)).fetchone()[0]
    issues = []
    if missing:
        issues.append(f"ABSTRACT_UNAVAILABLE: {missing} relevant sources")
    if not rows:
        issues.append("LITERATURE_INCOMPLETE: no verified source evidence")
    elif not support and not contradiction:
        issues.append("No directional abstract evidence was verified")
    if contradiction:
        issues.append("Contradictory abstract evidence remains unresolved")
    active = [r[0] for r in state._db.execute("SELECT hypothesis_id FROM hypotheses WHERE research_id=? AND status='ACTIVE'",
                                               (research_id,))]
    return LiteratureSynthesis(research_id=research_id, active_hypothesis_ids=active,
                               support_evidence_refs=support, contradiction_evidence_refs=contradiction,
                               neutral_evidence_refs=neutral, unresolved_issues=issues,
                               candidate_gap="Evidence is mixed or incomplete" if issues else None,
                               gap_label="INSUFFICIENT_EVIDENCE" if not support and not contradiction else "POSSIBLE_GAP")


class LiteratureCoordinator:
    def __init__(self, state, provider: ScholarlySearchProvider, *, fallback: ScholarlySearchProvider | None = None,
                 config: LiteratureConfig | None = None, reviewer: Callable | None = None):
        self.state = state
        self.provider = provider
        self.fallback = fallback
        self.config = config or LiteratureConfig()
        self.reviewer = reviewer or LiteralSentenceReviewer()

    async def _cached_search(self, provider: ScholarlySearchProvider, request: SearchRequest) -> SearchResult:
        request = SearchRequest.model_validate(request.model_dump())
        key_data = {"provider": provider.name, "query": " ".join(request.query.casefold().split()),
                    "year_from": request.year_from, "year_to": request.year_to, "limit": request.limit,
                    "require_doi": request.require_doi, "open_access_only": request.open_access_only,
                    "page": request.page}
        key = hashlib.sha256(to_json(key_data).encode("utf-8", errors="strict")).hexdigest()
        previous = self.state.search_cache_get(request.research_id, key)
        if previous and (not hasattr(provider, 'can_reuse') or provider.can_reuse(request, previous)):
            return SearchResult.model_validate(previous)
        result = await provider.search(request)
        if result.provider != provider.name or result.request != request or len(result.sources) > request.limit:
            raise ScholarlyError("provider violated search result contract")
        self.state.search_cache_put(request.research_id, key, provider.name, request, result)
        return result

    async def _process_intent(self, research_id: str, question: str, intent: SearchIntent,
                              logical_key: str, target_hypothesis_id: str | None = None) -> None:
        from .research_schemas import ResearchAction

        existing_action = self.state._db.execute(
            "SELECT action_id FROM research_actions WHERE research_id=? AND logical_key=?",
            (research_id, logical_key)).fetchone()
        search_count = self.state._db.execute(
            "SELECT COUNT(*) FROM research_actions WHERE research_id=? AND action_type=?",
            (research_id, ResearchAction.SEARCH_LITERATURE.value)).fetchone()[0]
        if existing_action is None and search_count >= self.config.max_queries:
            raise ValueError("literature query hard cap exceeded")
        relevant_count = self.state._db.execute(
            "SELECT COUNT(*) FROM sources WHERE research_id=? AND status IN ('RELEVANT','EVIDENCE_EXTRACTED','VERIFIED')",
            (research_id,)).fetchone()[0]
        verified_count = self.state._db.execute(
            "SELECT COUNT(DISTINCT source_id) FROM evidence WHERE research_id=? AND source_type='LITERATURE' AND status='VERIFIED'",
            (research_id,)).fetchone()[0]
        request = SearchRequest(research_id=research_id, query=intent.query,
                                limit=self.config.max_results_per_query)
        provider = self.provider
        try:
            result = await self._cached_search(provider, request)
            if not result.sources and self.fallback:
                provider = self.fallback
                result = await self._cached_search(provider, request)
        except ScholarlyError as exc:
            if exc.status == "RATE_LIMITED" or exc.code == "SEARCH_RATE_LIMITED":
                self.state.runtime_event(research_id, "SEARCH_RATE_LIMITED", {
                    "intent": intent.kind, "provider": provider.name,
                    "retry_after": exc.retry_after})
            if not self.fallback:
                self.state.runtime_event(research_id, "LITERATURE_INCOMPLETE", {
                    "intent": intent.kind, "provider": provider.name,
                    "status": exc.status, "error_code": exc.code})
                return
            provider = self.fallback
            try:
                result = await self._cached_search(provider, request)
            except ScholarlyError as fallback_error:
                if fallback_error.status == "RATE_LIMITED" or fallback_error.code == "SEARCH_RATE_LIMITED":
                    self.state.runtime_event(research_id, "SEARCH_RATE_LIMITED", {
                        "intent": intent.kind, "provider": provider.name,
                        "retry_after": fallback_error.retry_after})
                self.state.runtime_event(research_id, "LITERATURE_INCOMPLETE", {
                    "intent": intent.kind, "provider": provider.name,
                    "status": fallback_error.status, "error_code": fallback_error.code})
                return
        self.state.record_action(research_id, ResearchAction.SEARCH_LITERATURE.value, intent.query,
                                 details={"intent": intent.kind, "provider": provider.name,
                                          "candidate_count": len(result.sources),
                                          "target_hypothesis_id": target_hypothesis_id},
                                 max_actions=50, logical_key=logical_key)
        for candidate in result.sources:
            source_id, _ = self.state.upsert_source(research_id, candidate)
            canonical = source_from_row(self.state._one(
                "SELECT * FROM sources WHERE research_id=? AND source_id=?", (research_id, source_id)))
            source_status = canonical.status
            if source_status in {"IRRELEVANT", "VERIFIED", "INVALIDATED"}:
                continue
            if source_status == "DISCOVERED" and relevant_count >= self.config.max_relevant:
                break
            if source_status == "DISCOVERED":
                decision = screen_source(source_id, canonical, question,
                                         [target_hypothesis_id] if target_hypothesis_id else None)
                self.state.set_source_relevance(research_id, decision)
                self.state.record_action(research_id, ResearchAction.SCREEN_SOURCE.value, candidate.title,
                                         details={"source_id": source_id, "relevance": decision.relevance},
                                         max_actions=50, logical_key=f"literature:screen:{source_id}")
                if decision.relevance == "IRRELEVANT":
                    continue
                relevant_count += 1
            if verified_count >= self.config.max_verified_sources:
                continue
            evidence = extract_abstract_evidence(source_id, canonical, target_hypothesis_id)
            if evidence is None:
                self.state.runtime_event(research_id, "ABSTRACT_UNAVAILABLE", {"source_id": source_id})
                continue
            self.state.mark_source_extracted(research_id, source_id)
            reviewer = self.reviewer
            if hasattr(reviewer, "prepare"):
                try:
                    reviewer = await reviewer.prepare(research_id, source_id, evidence)
                except Exception as exc:
                    from .agent_policy import BudgetExceededError
                    from .agent_runtime import RuntimeFailure
                    from .providers.base import ModelProviderError
                    if not isinstance(exc, (BudgetExceededError, RuntimeFailure, ModelProviderError)):
                        raise
                    self.state.runtime_event(research_id, "LITERATURE_REVIEW_UNAVAILABLE",
                                             {"source_id": source_id, "error_code": type(exc).__name__})
                    continue
            try:
                evidence_id = self.state.verify_literature_evidence(research_id, evidence, reviewer)
            except Exception as exc:
                from .service import ContractViolationError
                if not isinstance(exc, ContractViolationError):
                    raise
                self.state.runtime_event(research_id, "LITERATURE_EVIDENCE_REJECTED", {"source_id": source_id})
                continue
            verified_count += 1
            self.state.record_action(research_id, ResearchAction.VERIFY_LITERATURE.value, evidence.claim,
                                     details={"source_id": source_id, "evidence_id": evidence_id,
                                              "polarity": evidence.polarity},
                                     max_actions=50, logical_key=f"literature:verify:{source_id}")

    async def run(self, research_id: str, question: str,
                  intents: list[SearchIntent] | None = None) -> LiteratureSynthesis:
        from .research_schemas import ResearchAction

        plan = [SearchIntent.model_validate(item.model_dump() if isinstance(item, SearchIntent) else item)
                for item in (intents or default_intents(question))]
        if len(plan) < 2 or len(plan) > self.config.max_queries:
            raise ValueError("literature query hard cap exceeded")
        if len({intent.kind for intent in plan}) != len(plan) or not any(
                item.kind == "CONTRADICTION" for item in plan):
            raise ValueError("structured contradiction intent is required")
        for index, intent in enumerate(plan):
            await self._process_intent(research_id, question, intent, f"literature:query:{index}")
        synthesis = synthesize_literature(self.state, research_id)
        self.state.save_literature_synthesis(research_id, synthesis)
        self.state.record_action(research_id, ResearchAction.SYNTHESIZE_LITERATURE.value, question,
                                 details={"support": len(synthesis.support_evidence_refs),
                                          "contradict": len(synthesis.contradiction_evidence_refs)},
                                 max_actions=50, logical_key="literature:synthesis")
        return synthesis

    async def search_hypothesis_contradiction(self, research_id: str, hypothesis_id: str,
                                              statement: str) -> LiteratureSynthesis:
        intent = default_intents(statement)[2]
        await self._process_intent(research_id, statement, intent,
                                   f"literature:hypothesis:{hypothesis_id}", hypothesis_id)
        synthesis = synthesize_literature(self.state, research_id)
        self.state.save_literature_synthesis(research_id, synthesis)
        return synthesis
