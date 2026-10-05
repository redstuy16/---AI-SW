"""실행 검증·과학적 참과 구분되는 주장의 의미 출처."""
from __future__ import annotations

from typing import Literal
from pydantic import Field, StrictFloat, StrictInt
from .schemas import StrictModel, utc_now

SupportState = Literal["SUPPORTED", "NOT_SUPPORTED", "INCONCLUSIVE", "NEEDS_REVIEW",
                       "CONFLICTED", "INVALIDATED", "NEEDS_REVALIDATION"]


class ResearchSliceConfig(StrictModel):
    version: Literal["2.0.0"] = "2.0.0"
    claim_evidence_provenance: bool = False
    revision_invalidation: bool = True
    verifier_dependency_catalog: bool = False
    dependency_metadata: bool = True
    reliability_lab: bool = False


class NumericSlot(StrictModel):
    name: str = Field(min_length=1)
    locator: str = Field(pattern=r"^/result/")
    value: StrictInt | StrictFloat | None
    unit: str = "UNKNOWN"
    method: str = "UNKNOWN"
    estimand_id: str | None = None
    artifact_id: str
    artifact_revision: str
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    display_precision: int | None = Field(default=None, ge=0, le=12)
    tolerance_policy: Literal["EXACT", "ROUND_HALF_EVEN", "ABSOLUTE_1E-10"] = "EXACT"


class Claim(StrictModel):
    claim_id: str
    revision: int = Field(default=1, ge=1)
    research_id: str
    text: str = Field(min_length=1)
    claim_type: Literal["association", "prediction", "comparison", "causal", "literature_summary", "methodological", "limitation"]
    scope: dict
    estimand_id: str | None = None
    support_state: SupportState = "NEEDS_REVIEW"
    numeric_slots: list[NumericSlot] = Field(default_factory=list)
    evidence_binding_ids: list[str] = Field(default_factory=list)
    created_from: str
    supersedes: int | None = None
    invalidated_by: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())


class EvidenceBinding(StrictModel):
    binding_id: str
    claim_id: str
    claim_revision: int = Field(ge=1)
    evidence_kind: Literal["artifact", "source"]
    target_id: str
    target_revision: str
    target_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    relation: Literal["SUPPORTS", "CONTRADICTS", "QUALIFIES"]
    usage_type: Literal["DIRECT_MEASUREMENT", "NUMERIC_RESULT", "SOURCE_SPAN", "SOURCE_SUMMARY", "DERIVED_INFERENCE", "LIMITATION"]
    locator: dict
    scope: dict
    assumptions: list[str] = Field(default_factory=list)
    verification_record_ids: list[str] = Field(default_factory=list)
    status: Literal["ACTIVE", "STALE", "INVALID"] = "ACTIVE"
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class VerifierObligation(StrictModel):
    obligation_id: str
    research_id: str
    subject_id: str
    check_id: str
    check_version: str = "UNKNOWN"
    method_family: Literal["HASH_SCHEMA", "NUMERIC_RECOMPUTE", "DATASET_MEMBERSHIP", "TIME_SPLIT_RECONSTRUCTION", "ORIGINAL_CONTRACT_CONFORMITY", "CLAIM_SOURCE_SUPPORT", "SEMANTIC_REVIEW"]
    model_family: str = "UNKNOWN"
    data_dependency: list[str] = Field(default_factory=lambda: ["UNKNOWN"])
    implementation_dependency: list[str] = Field(default_factory=lambda: ["UNKNOWN"])
    evidence_dependency: list[str] = Field(default_factory=lambda: ["UNKNOWN"])
    qualification_status: Literal["QUALIFIED", "FAILED", "UNKNOWN"] = "UNKNOWN"
    tested_revision: str
    outcome: Literal["PASS", "FAIL", "UNKNOWN", "NOT_RUN"]
    critical: bool = True
    evidence_refs: list[str] = Field(default_factory=list)


class AnalysisPrecommit(StrictModel):
    analysis_plan_hash: str
    contract_hash: str
    estimand_id: str
    dataset_revision: str
    primary_method: str
    split_policy: dict
    stopping_rule: str
    seed: int | None
    budget: dict
    versions: dict
    result_access_state: Literal["STRUCTURE_ONLY_SEEN", "OUTCOME_SUMMARY_SEEN", "PRIOR_RESULT_SEEN", "HOLDOUT_UNSEEN", "UNKNOWN"] = "UNKNOWN"
    label: Literal["Analysis Precommit"] = "Analysis Precommit"


class StructuredConclusion(StrictModel):
    question_id: str
    estimand_id: str | None
    scope: dict
    support_status: SupportState
    direction: str = "UNKNOWN"
    estimate: StrictFloat | StrictInt | None = None
    interval: list[float] | None = None
    material_claim_ids: list[str] = Field(default_factory=list)
    validity_status: Literal["VALID", "NEEDS_REVALIDATION", "INCOMPLETE"]
    limitations: list[str] = Field(default_factory=list)
