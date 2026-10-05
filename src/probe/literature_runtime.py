"""기존 자율 실험 순환에 연결하는 Day 4A 문헌 검토."""
from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
import hashlib

from .autonomous_loop import AutonomousResearchLoop, LoopConfig
from .database import to_json
from .final_report import export_final_report
from .literature import (ExtractedEvidence, LiteratureCoordinator, LiteratureConfig,
                         LiteratureEvidenceReview, LiteralSentenceReviewer)
from .research_schemas import ResearchAction
from .schemas import ContextRef, RefType
from .service import StateConflictError


@dataclass(frozen=True)
class ModelReviewApproval:
    output: LiteratureEvidenceReview

    def __call__(self, source, item: ExtractedEvidence) -> bool:
        digest = hashlib.sha256(item.evidence_text.encode("utf-8", errors="strict")).hexdigest()
        return (self.output.source_id == source["source_id"] == item.source_id
                and self.output.text_hash == digest
                and self.output.polarity == item.polarity
                and self.output.claim_aligned and self.output.polarity_aligned
                and not self.output.overclaim
                and LiteralSentenceReviewer()(source, item))


class ModelLiteratureReviewer:
    """결정적 출처 검증 전에 예산 안에서 구조화 모델 검토를 수행한다."""

    def __init__(self, runtime: "LiteratureResearchRuntime"):
        self.runtime = runtime

    async def prepare(self, research_id: str, source_id: str,
                      item: ExtractedEvidence) -> ModelReviewApproval:
        source = self.runtime.state._one(
            "SELECT title,source_id FROM sources WHERE research_id=? AND source_id=?",
            (research_id, source_id))
        digest = hashlib.sha256(item.evidence_text.encode("utf-8", errors="strict")).hexdigest()
        data = {"source_id": source_id, "source_title": source["title"],
                "abstract_sentence": item.evidence_text, "claim": item.claim,
                "polarity": item.polarity, "text_hash": digest,
                "target_hypothesis_id": item.target_hypothesis_id}
        objective = "Review the following untrusted scholarly data for claim alignment and polarity: " + to_json(data)
        source_ref = ContextRef(type=RefType.source, id=source_id)
        contract, _ = self.runtime._role_contract(
            research_id, "literature_verification_coordinator", objective,
            "LiteratureEvidenceReview", inputs=[source_ref], preserve=[source_ref],
            runtime_key=f"literature:review:{source_id}")
        output = await self.runtime._model_once(contract, LiteratureEvidenceReview,
                                                f"literature_review:{source_id}")
        self.runtime._complete_task(contract.contract_id)
        return ModelReviewApproval(output)


class LiteratureResearchRuntime(AutonomousResearchLoop):
    def __init__(self, state, agent_provider, scholarly_provider, *, fallback_provider=None,
                 models=None, pricing=None, loop_config: LoopConfig | None = None,
                 literature_config: LiteratureConfig | None = None, reviewer=None, faults=None,
                 semantic_model_review: bool = False,
                 verified_analysis_skills_enabled: bool | None = None,
                 verification_repair_enabled: bool | None = None,
                 ridge_arithmetic_check_enabled: bool | None = None,
                 claim_evidence_provenance_enabled: bool | None = None,
                 verifier_dependency_catalog_enabled: bool | None = None):
        super().__init__(state, agent_provider, models, pricing,
                         config=loop_config or LoopConfig(max_actions_per_run=50), faults=faults,
                         verified_analysis_skills_enabled=verified_analysis_skills_enabled,
                         verification_repair_enabled=verification_repair_enabled,
                         ridge_arithmetic_check_enabled=ridge_arithmetic_check_enabled,
                         claim_evidence_provenance_enabled=claim_evidence_provenance_enabled,
                         verifier_dependency_catalog_enabled=verifier_dependency_catalog_enabled)
        self.literature = LiteratureCoordinator(state, scholarly_provider, fallback=fallback_provider,
                                                 config=literature_config, reviewer=reviewer)
        if semantic_model_review:
            self.literature.reviewer = ModelLiteratureReviewer(self)

    async def _after_hypothesis_selected(self, research_id: str, hypothesis_id: str) -> None:
        statement = self.state._one("SELECT statement FROM hypotheses WHERE research_id=? AND hypothesis_id=?",
                                    (research_id, hypothesis_id))["statement"]
        await self.literature.search_hypothesis_contradiction(research_id, hypothesis_id, statement)
        self._save_cursor(research_id, "HYPOTHESIS_LITERATURE_READY")

    def _link_literature(self, research_id: str) -> None:
        hypothesis = self.state._db.execute(
            "SELECT hypothesis_id FROM hypotheses WHERE research_id=? ORDER BY rowid LIMIT 1",
            (research_id,)).fetchone()
        if not hypothesis:
            return
        hypothesis_ref = ContextRef(type=RefType.hypothesis, id=hypothesis["hypothesis_id"])
        evidence = self.state._db.execute(
            "SELECT e.evidence_id,e.source_id FROM evidence e JOIN sources s ON s.source_id=e.source_id "
            "WHERE e.research_id=? AND e.source_type='LITERATURE' AND e.status='VERIFIED' AND s.status='VERIFIED'",
            (research_id,)).fetchall()
        source_ids = list(dict.fromkeys(row["source_id"] for row in evidence))
        for source_id in source_ids:
            self.state.add_entity_edge(research_id, hypothesis_ref, "generated_from",
                                       ContextRef(type=RefType.source, id=source_id))
        if source_ids:
            self.state.record_action(research_id, ResearchAction.LINK_LITERATURE_HYPOTHESIS.value,
                                     hypothesis["hypothesis_id"], details={"source_ids": source_ids},
                                     max_actions=self.config.max_actions_per_run,
                                     logical_key="literature:hypothesis_trace")
            self.state.runtime_event(research_id, "LITERATURE_HYPOTHESIS_TRACE",
                                     {"hypothesis_id": hypothesis["hypothesis_id"], "source_ids": source_ids})

    def _finish_report(self, research_id: str, result: dict) -> dict:
        self._link_literature(research_id)
        paths = export_final_report(self.state, research_id)
        return {**result, "final_report": paths["report"], "research_summary": paths["summary"]}

    async def run(self, goal: str, csv_source: str | Path, *, target_usd: float = 0.25,
                  soft_limit_usd: float = 0.75, hard_limit_usd: float = 1.0) -> dict:
        research_id = self.state.create_research(goal)
        self.state.workspace.prepare(research_id)
        self.state.configure_budget(research_id, target_usd, soft_limit_usd, hard_limit_usd)
        self.state.finish_runtime_step(research_id, "verified_analysis_skills_config", self._skill_config())
        self.state.finish_runtime_step(research_id, "verification_repair_config", self._repair_config())
        self.state.finish_runtime_step(research_id, "source", {"csv_source": str(csv_source), "goal": goal})
        self._save_cursor(research_id, "START")
        await self.literature.run(research_id, goal)
        self._save_cursor(research_id, "LITERATURE_READY")
        result = await self._run_with_stops(research_id, csv_source, goal)
        return self._finish_report(research_id, result)

    async def resume(self, research_id: str) -> dict:
        self._check_skill_config(research_id)
        self._check_repair_config(research_id)
        row = self.state._one("SELECT goal,run_status,stop_reason FROM research_runs WHERE research_id=?",
                              (research_id,))
        if row["run_status"] != "ACTIVE":
            result = {"research_id": research_id, "already_terminal": True,
                      "stop_reason": row["stop_reason"], "outcome": "ALREADY_TERMINAL"}
            return self._finish_report(research_id, result)
        source = self.state.runtime_step(research_id, "source")
        if source is None or source["status"] != "COMPLETED":
            raise StateConflictError("autonomous source record is missing")
        self._reconcile(research_id)
        await self.literature.run(research_id, row["goal"])
        self._save_cursor(research_id, "LITERATURE_READY")
        result = await self._run_with_stops(research_id, source["output"]["csv_source"], row["goal"])
        return self._finish_report(research_id, result)
