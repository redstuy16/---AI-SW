"""Pydantic 모델에서 JSON Schema를 생성한다."""
from __future__ import annotations

import argparse
from pathlib import Path

from .schemas import AgentResult, ContextBundle, ResearchContract, ToolRequest, ToolResult, VerificationResult
from .research_schemas import ConclusionCandidate, CriticResult, HypothesisShortlist, HypothesisStatusDecision
from .scholarly import SearchRequest, SearchResult, NormalizedSource
from .literature import RelevanceResult, ExtractedEvidence, LiteratureSynthesis, LiteratureEvidenceReview
from .final_report import FinalConclusion
from .dashboard import (DashboardProjection, EvidenceProjection, ExperimentProjection,
                         HypothesisProjection, ResearchOverview, ResearchTreeProjection,
                         VerificationProjection)
from .database import to_json
from .cycle5 import GoalScope, GoalWitness, SourceSemanticRecord, TransformationLineage
from .agent_schemas import AnalysisPlan
from .research_slice_schemas import (Claim, EvidenceBinding, NumericSlot, VerifierObligation,
                                     AnalysisPrecommit, StructuredConclusion, ResearchSliceConfig)


MODELS = {
    "source_semantic_record": SourceSemanticRecord, "transformation_lineage": TransformationLineage,
    "goal_witness": GoalWitness, "goal_scope": GoalScope, "analysis_plan": AnalysisPlan,
    "research_contract": ResearchContract, "agent_result": AgentResult,
    "tool_request": ToolRequest, "tool_result": ToolResult,
    "verification_result": VerificationResult, "context_bundle": ContextBundle,
    "hypothesis_shortlist": HypothesisShortlist, "critic_result": CriticResult,
    "hypothesis_status_decision": HypothesisStatusDecision,
    "conclusion_candidate": ConclusionCandidate,
    "search_request": SearchRequest, "search_result": SearchResult,
    "normalized_source": NormalizedSource, "relevance_result": RelevanceResult,
    "extracted_evidence": ExtractedEvidence, "literature_synthesis": LiteratureSynthesis,
    "literature_evidence_review": LiteratureEvidenceReview,
    "final_conclusion": FinalConclusion,
    "research_overview": ResearchOverview, "research_tree": ResearchTreeProjection,
    "hypothesis_projection": HypothesisProjection, "evidence_projection": EvidenceProjection,
    "experiment_projection": ExperimentProjection, "verification_projection": VerificationProjection,
    "dashboard_projection": DashboardProjection,
    "material_claim": Claim, "evidence_binding": EvidenceBinding, "numeric_slot": NumericSlot,
    "verifier_obligation": VerifierObligation, "analysis_precommit": AnalysisPrecommit,
    "structured_conclusion": StructuredConclusion, "research_slice_config": ResearchSliceConfig,
}


def generate(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, model in MODELS.items():
        content = to_json(model.model_json_schema()) + "\n"
        content.encode("utf-8", errors="strict")
        path = directory / f"{name}.schema.json"
        if not path.exists() or path.read_text(encoding="utf-8", errors="strict") != content:
            path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?", type=Path, default=Path("schemas/generated"))
    generate(parser.parse_args().directory)
