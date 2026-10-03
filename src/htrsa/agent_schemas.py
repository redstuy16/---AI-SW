"""잠정 출력과 상위 판단의 구조화 스키마."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .schemas import ContextRef, StrictModel
from .analysis_skills import SkillPlan
from .cycle5 import GoalScope


class ManagerDecision(StrictModel):
    decision_type: Literal["INITIAL_PLAN", "DELEGATE", "REPLAN", "ESCALATE", "TERMINATE"]
    research_question: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    coordinator_role: Literal["experiment_coordinator"]
    objective: str = Field(min_length=1)


class CoordinatorDecision(StrictModel):
    decision_type: Literal["DELEGATE", "ESCALATE"]
    objective: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    assigned_role: Literal["analysis_planner_worker"]
    input_refs: list[ContextRef]
    allowed_tools: list[str]
    max_tool_calls: int = Field(ge=1)
    max_retries: int = Field(ge=0)
    max_runtime_sec: int = Field(gt=0)
    max_cost_usd: float = Field(ge=0)


class AnalysisPlan(StrictModel):
    dataset_id: str = Field(min_length=1)
    selected_variables: list[str] = Field(min_length=2, max_length=2)
    method: Literal["pearson_correlation", "spearman_correlation", "ridge_holdout", "ridge_rolling_origin"]
    justification: str = Field(min_length=1)
    requested_tools: list[str]
    reported_r: float | None = None
    skill_plan: SkillPlan | None = None
    semantic_scope: GoalScope | None = None


class FailureEvent(StrictModel):
    code: str
    severity: Literal["LOW", "MEDIUM", "HIGH"]
    source_role: str
    contract_id: str
    attempt: int = Field(ge=1)
    deterministic_signals: list[str] = Field(default_factory=list)
    impact: str


class EscalationDecision(StrictModel):
    level: Literal[0, 1, 2, 3]
    action: str
    reason: str
