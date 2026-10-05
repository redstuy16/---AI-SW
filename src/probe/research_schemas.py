"""계획·비판·자율 순환 기록의 스키마."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field

from .schemas import ContextRef, StrictModel


class HypothesisScore(StrictModel):
    plausibility: float = Field(ge=0, le=1)
    testability: float = Field(ge=0, le=1)
    data_availability: float = Field(ge=0, le=1)
    information_value: float = Field(ge=0, le=1)
    cost: float = Field(ge=0, le=1)

    def aggregate(self) -> float:
        return (0.20 * self.plausibility + 0.25 * self.testability
                + 0.20 * self.data_availability + 0.25 * self.information_value
                + 0.10 * (1 - self.cost))


class HypothesisCriterion(StrictModel):
    """분석 전에 확정하는 판정 기준. 기존 가설의 방향을 추정하지 않는다."""
    method: str = Field(min_length=1)
    confirmatory_methods: list[str] = Field(default_factory=list, max_length=5)
    variables: dict[str, str]
    expected_direction: Literal["positive", "negative"]
    alpha: float = Field(gt=0, lt=1)


class HypothesisProposal(StrictModel):
    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    score: HypothesisScore
    parent_hypothesis_id: str | None = None
    criterion: HypothesisCriterion | None = None


class HypothesisShortlist(StrictModel):
    hypotheses: list[HypothesisProposal] = Field(min_length=1, max_length=5)
    rationale: str = Field(min_length=1)


class HypothesisStatusDecision(StrictModel):
    hypothesis_id: str
    new_status: Literal["SUPPORTED", "WEAKENED", "REJECTED", "INCONCLUSIVE"]
    rationale: str = Field(min_length=1)
    evidence_refs: list[ContextRef] = Field(min_length=1)


class CriticIssue(StrictModel):
    code: str
    severity: Literal["LOW", "MEDIUM", "HIGH"]
    detail: str = Field(min_length=1)


class FollowupRequest(StrictModel):
    method: Literal["pearson_correlation", "spearman_correlation"]
    rationale: str = Field(min_length=1)
    hypothesis_id: str


class CriticResult(StrictModel):
    verdict: Literal["ACCEPT", "ACCEPT_WITH_LIMITATION", "FOLLOW_UP_REQUIRED", "REJECT"]
    issues: list[CriticIssue] = Field(default_factory=list)
    alternative_explanations: list[str] = Field(default_factory=list)
    confounders: list[str] = Field(default_factory=list)
    requested_followups: list[FollowupRequest] = Field(default_factory=list)
    conclusion_strength: Literal["NONE", "WEAK", "MODERATE", "STRONG"]


class FollowupDecision(StrictModel):
    approve: bool
    rationale: str = Field(min_length=1)
    method: Literal["pearson_correlation", "spearman_correlation"] | None = None
    objective: str = Field(min_length=1)


class StopDecision(StrictModel):
    stop: bool
    reason: Literal["GOAL_ANSWERED", "INSUFFICIENT_DATA", "UNRESOLVED_VERIFICATION"]
    rationale: str = Field(min_length=1)


class ConclusionCandidate(StrictModel):
    statement: str = Field(min_length=1)
    support_level: Literal["NONE", "WEAK", "MODERATE", "STRONG"]
    evidence_refs: list[ContextRef]
    limitation_refs: list[ContextRef] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)


class ResearchAction(str, Enum):
    SEARCH_LITERATURE = "SEARCH_LITERATURE"
    SCREEN_SOURCE = "SCREEN_SOURCE"
    VERIFY_LITERATURE = "VERIFY_LITERATURE"
    SYNTHESIZE_LITERATURE = "SYNTHESIZE_LITERATURE"
    LINK_LITERATURE_HYPOTHESIS = "LINK_LITERATURE_HYPOTHESIS"
    REFINE_QUESTION = "REFINE_QUESTION"
    GENERATE_HYPOTHESES = "GENERATE_HYPOTHESES"
    SELECT_HYPOTHESIS = "SELECT_HYPOTHESIS"
    DESIGN_EXPERIMENT = "DESIGN_EXPERIMENT"
    RUN_EXPERIMENT = "RUN_EXPERIMENT"
    VERIFY_RESULT = "VERIFY_RESULT"
    CRITIQUE_RESULT = "CRITIQUE_RESULT"
    REPLAN = "REPLAN"
    FOLLOWUP_EXPERIMENT = "FOLLOWUP_EXPERIMENT"
    UPDATE_HYPOTHESIS = "UPDATE_HYPOTHESIS"
    STOP = "STOP"


class StopReason(str, Enum):
    SCIENCE_INQUIRY_COMPLETED = "SCIENCE_INQUIRY_COMPLETED"
    QUALIFIED_PROCEDURE_COMPLETED = "QUALIFIED_PROCEDURE_COMPLETED"
    LITERATURE_REVIEW_COMPLETED = "LITERATURE_REVIEW_COMPLETED"
    LITERATURE_DESIGN_COMPLETED = "LITERATURE_DESIGN_COMPLETED"
    GOAL_ANSWERED = "GOAL_ANSWERED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    ACTION_LIMIT_REACHED = "ACTION_LIMIT_REACHED"
    NO_TESTABLE_HYPOTHESIS = "NO_TESTABLE_HYPOTHESIS"
    ALL_HYPOTHESES_REJECTED = "ALL_HYPOTHESES_REJECTED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNRESOLVED_VERIFICATION = "UNRESOLVED_VERIFICATION"
    USER_STOP = "USER_STOP"
    FATAL_ERROR = "FATAL_ERROR"
