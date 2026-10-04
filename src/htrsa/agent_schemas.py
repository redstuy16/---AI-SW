"""잠정 출력과 상위 판단의 구조화 스키마."""
from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import Field

from .schemas import ContextRef, StrictModel
from .analysis_skills import SkillPlan
from .cycle5 import GoalScope


class ManagerDecision(StrictModel):
    INSTRUCTIONS: ClassVar[str] = (
        "INITIAL_PLAN의 search_queries는 승인된 공개 연구 주제만 바탕으로 최대 3개를 만든다. "
        "사용자가 직접 지정한 검색어는 참고용 단서이며 그것만 반복하지 않고 원래 연구 질문을 기준으로 탐색한다. "
        "사용자가 검색어를 지정하지 않아도 질문의 핵심 대상·조건·측정 항목으로 검색 표현을 구성한다. "
        "한국어 주제라면 한국어 표현과 영어 표현을 모두 포함하고 원리를 찾는 검색도 제안한다. "
        "첨부 자료의 비공개 내용·개인정보·파일 경로·API 키를 검색어에 넣지 않는다."
    )
    decision_type: Literal["INITIAL_PLAN", "DELEGATE", "REPLAN", "ESCALATE", "TERMINATE"]
    research_question: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    coordinator_role: Literal["experiment_coordinator"]
    objective: str = Field(min_length=1)
    search_queries: list[Annotated[str, Field(min_length=2, max_length=400)]] = Field(default_factory=list, max_length=3)


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
