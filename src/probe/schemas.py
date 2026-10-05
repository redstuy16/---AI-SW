"""외부 데이터 스키마의 기준은 Pydantic이다."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RefType(str, Enum):
    research = "research"
    hypothesis = "hypothesis"
    evidence = "evidence"
    source = "source"
    dataset = "dataset"
    experiment = "experiment"
    artifact = "artifact"
    decision = "decision"
    checkpoint = "checkpoint"
    task = "task"
    contract = "contract"
    summary = "summary"


class ContextRef(StrictModel):
    type: RefType
    id: str = Field(min_length=1)


class ContextPolicy(StrictModel):
    must_preserve: list[ContextRef] = Field(default_factory=list)
    max_context_tokens: int = Field(default=2048, gt=0)
    include_recent_events: int = Field(default=10, ge=0)
    semantic_top_k: int = Field(default=0, ge=0)


class Constraints(StrictModel):
    max_tool_calls: int = Field(default=1, ge=0)
    max_retries: int = Field(default=0, ge=0)
    max_runtime_sec: int = Field(default=60, gt=0)
    max_cost_usd: float = Field(default=0, ge=0)


class AcceptanceCheck(StrictModel):
    id: str = Field(min_length=1)
    kind: str
    required: bool = True
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def known_kind(cls, value: str) -> str:
        if value not in {"automatic", "llm_review"}:
            raise ValueError("unsupported acceptance kind")
        return value


class Escalation(StrictModel):
    hard_triggers: list[str] = Field(default_factory=list)
    confidence_threshold: float = Field(default=0, ge=0, le=1)
    max_failures: int = Field(default=1, ge=0)


class ResearchContract(StrictModel):
    contract_id: str = Field(min_length=1)
    research_id: str = Field(min_length=1)
    parent_task_id: str | None = None
    task_type: str = Field(min_length=1)
    issued_by: str = Field(min_length=1)
    assigned_role: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    inputs: list[ContextRef] = Field(default_factory=list)
    context_policy: ContextPolicy = Field(default_factory=ContextPolicy)
    allowed_tools: list[str] = Field(default_factory=list)
    constraints: Constraints = Field(default_factory=Constraints)
    acceptance: list[AcceptanceCheck] = Field(default_factory=list)
    escalation: Escalation = Field(default_factory=Escalation)
    output_schema_id: str = Field(min_length=1)
    priority: int = Field(default=0, ge=0)


class AgentAction(StrictModel):
    action_id: str
    research_id: str
    contract_id: str
    actor_role: str
    action_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentResult(StrictModel):
    result_id: str
    research_id: str
    contract_id: str
    actor_role: str
    status: str
    output: dict[str, Any]
    artifact_refs: list[ContextRef] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    provenance: dict[str, Any] = Field(default_factory=dict)


class ToolRequest(StrictModel):
    request_id: str
    research_id: str
    task_id: str
    actor_id: str
    idempotency_key: str
    tool_name: str
    args: dict[str, Any]


class ToolResult(StrictModel):
    ok: bool
    request_id: str
    tool_name: str
    result: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ContextRef] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class Verdict(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class VerificationCheck(StrictModel):
    check_id: str
    passed: bool
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(StrictModel):
    verification_id: str
    research_id: str
    subject_type: str
    subject_id: str
    verdict: Verdict
    checks: list[VerificationCheck]
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    verified_at: datetime = Field(default_factory=utc_now)

    @field_validator("verified_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone aware")
        return value.astimezone(timezone.utc)


class ResearchDecision(StrictModel):
    decision_id: str
    research_id: str
    actor: str
    decision_type: str
    rationale: str
    status: str
    supersedes_id: str | None = None


class ContextItem(StrictModel):
    ref_type: RefType
    ref_id: str
    source_id: str | None = None
    polarity: str | None = None
    status: str
    state_version: int = Field(ge=0)
    layer: Literal["mandatory", "dependency", "semantic"]
    text: str
    score: float | None = None
    marked_invalid: bool = False


class ContextMetrics(StrictModel):
    estimated_tokens: int = Field(ge=0)
    mandatory_count: int = Field(ge=0)
    dependency_count: int = Field(ge=0)
    semantic_count: int = Field(ge=0)
    recent_event_count: int = Field(ge=0)
    invalidated_warning_count: int = Field(ge=0)
    estimator: str


class ContextBundle(StrictModel):
    bundle_id: str
    research_id: str
    target_role: str
    contract_id: str
    stable_core: dict[str, Any] = Field(default_factory=dict)
    active_state: dict[str, Any] = Field(default_factory=dict)
    mandatory: list[ContextRef] = Field(default_factory=list)
    dependencies: list[ContextRef] = Field(default_factory=list)
    semantic: list[ContextRef] = Field(default_factory=list)
    recent_events: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    budget: dict[str, float | int | None] = Field(default_factory=dict)
    state_version: int = Field(ge=0)
    items: list[ContextItem] = Field(default_factory=list)
    metrics: ContextMetrics | None = None


class NumericProvenance(StrictModel):
    value: int | float
    field: str
    artifact_id: str
    tool_call_id: str
    dataset_id: str
    dataset_sha256: str


class ScientificMutation(StrictModel):
    dataset_id: str
    profile_artifact_id: str
    stats_artifact_id: str
    figure_artifact_id: str
    experiment_id: str
    evidence_id: str
    method: str
    claim: str
    polarity: Literal["support", "contradict", "neutral"]
    numeric_provenance: dict[str, NumericProvenance]
    plan_artifact_id: str | None = None


class StagedResult(StrictModel):
    agent_result: AgentResult
    tool_request: ToolRequest
    tool_result: ToolResult
    scientific: ScientificMutation | None = None
