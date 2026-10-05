"""기존 상태 서비스를 사용하는 선택적·제한적 검증 복구."""
from __future__ import annotations

from hashlib import sha256
import csv
import io
import math
from typing import Any, Literal

import numpy as np
from pydantic import Field

from .agent_schemas import AnalysisPlan
from .database import from_json, to_json
from .schemas import StrictModel, VerificationResult, new_id


REPAIR_POLICY_VERSION = "0.3.0"
VERIFICATION_POLICY_VERSION = "1"
MAX_REPAIR_ATTEMPTS = 2
LOCAL_RECOVERY_RESERVE_USD = 0.01
REEXECUTABLE_CHECKS = frozenset({
    "execution_trace", "result_output", "ARTIFACT_HASH_MATCH", "RESULT_FIELD_MATCH",
    "NUMERIC_PROVENANCE", "SKILL_PLAN_MATCH", "PENDING_ENTITIES",
    "F3P_INDEPENDENT_ASSOCIATION",
    "CLAIM_SUPPORT",
})
FROZEN_FIELDS = (
    "dataset_id", "selected_variables", "method", "requested_tools", "skill_plan", "semantic_scope",
)


class VerificationFailureEvidence(StrictModel):
    failure_id: str
    verification_policy_version: Literal["1"] = VERIFICATION_POLICY_VERSION
    repair_policy_version: Literal["0.3.0"] = REPAIR_POLICY_VERSION
    research_id: str
    task_id: str
    contract_id: str
    experiment_id: str | None = None
    analysis_revision: int = Field(ge=0)
    mutation_id: str
    check_ids: list[str]
    check_details: list[dict[str, Any]]
    verdict: Literal["FAIL"] = "FAIL"
    severity: Literal["HIGH"] = "HIGH"
    authoritative_contract_ref: str
    authoritative_contract_hash: str
    frozen_plan_hash: str
    input_snapshot_refs: list[str]
    input_hashes: dict[str, str]
    observed_value: Any = None
    expected_relation_or_condition: str = "all required verification checks pass"
    difference: float | None = None
    tolerance_information: dict[str, Any] = Field(default_factory=dict)
    affected_output_fields: list[str] = Field(default_factory=list)
    relevant_artifact_refs: list[str] = Field(default_factory=list)
    trace_refs: list[str] = Field(default_factory=list)
    applicability_preconditions: list[str] = Field(default_factory=list)
    applicability_exclusions: list[str] = Field(default_factory=list)
    known_failure_scope: str = "staged_result"
    unknown_cause: bool = True
    allowed_repair_scope: list[str] = Field(default_factory=lambda: ["reexecute frozen plan"])
    forbidden_semantic_changes: list[str] = Field(default_factory=lambda: list(FROZEN_FIELDS))
    required_post_repair_checks: list[str]
    suggested_diagnostic_actions: list[str] = Field(default_factory=lambda: ["qualify checker", "reexecute frozen plan"])
    budget_required_for_recovery: float
    budget_available: float
    checker_identity: str = "StateService._evaluate"
    checker_version: str = VERIFICATION_POLICY_VERSION
    checker_qualification_status: str = "NOT_ASSESSED"


class RepairRequest(StrictModel):
    original_contract_id: str
    frozen_plan: AnalysisPlan
    frozen_plan_hash: str
    current_revision: int = Field(ge=0)
    failure: VerificationFailureEvidence
    authorized_inputs: list[str]
    allowed_tools: list[str]
    allowed_repair_scope: list[str]
    forbidden_semantic_changes: list[str]
    remaining_budget_usd: float
    max_repair_attempts: int = Field(ge=0, le=MAX_REPAIR_ATTEMPTS)
    mandatory_post_repair_checks: list[str]


class RepairDecision(StrictModel):
    action: Literal["REPAIR", "REQUEST_MISSING_INFORMATION", "CREATE_NEW_PLAN_REVISION",
                    "ESCALATE", "ABORT_AS_UNSUPPORTED", "LEAVE_UNRESOLVED"]
    rationale: str = Field(min_length=1)
    proposed_plan: AnalysisPlan | None = None


def repair_context_projection(request: RepairRequest) -> dict:
    """정본 요청은 보존하고 중복된 식별자·후검사 목록만 문맥에서 한 번 전달한다."""
    value = request.model_dump(mode="json")
    failure = value["failure"]
    value["failure"] = {k: failure[k] for k in (
        "failure_id", "check_ids", "check_details", "authoritative_contract_hash",
        "input_hashes", "applicability_preconditions", "applicability_exclusions",
        "checker_identity", "checker_version", "checker_qualification_status",
        "observed_value", "expected_relation_or_condition", "difference", "tolerance_information",
        "affected_output_fields", "relevant_artifact_refs", "trace_refs", "unknown_cause")}
    value["canonical_request_ref"] = "repair_request:" + request.original_contract_id
    value["canonical_request_hash"] = sha256(to_json(request).encode("utf-8", errors="strict")).hexdigest()
    return value


def plan_hash(plan: AnalysisPlan) -> str:
    return sha256(to_json(plan).encode("utf-8", errors="strict")).hexdigest()


def frozen_plan_matches(original: AnalysisPlan, proposal: AnalysisPlan | None) -> bool:
    return proposal is None or all(
        getattr(original, field) == getattr(proposal, field) for field in FROZEN_FIELDS
    )


def repair_transformation(state, research_id: str, record_id: str, expected_revision: int,
                          *, actor_role: str, fault=None) -> dict:
    """고정 선언의 미확정 CSV 산출물만 복구한다. 입력 데이터셋·질문·의미는 수정하지 않는다."""
    import csv
    import io
    from .cycle5 import TransformationLineage
    from .research_slice import digest
    from .storage import sha256_bytes
    if actor_role != "owner" or not state.cycle5.enabled(research_id):
        raise ValueError("OWNER_REPAIR_REQUIRED")
    policy = state.runtime_step(research_id, "verification_repair_config")
    if not policy or not policy["output"].get("enabled") or policy["output"].get("version") != REPAIR_POLICY_VERSION:
        raise ValueError("F3P_REPAIR_DISABLED")
    lineage = next(r for r in state.cycle5.records(research_id, "lineage") if r.record_id == record_id)
    verdict = state.cycle5.verify_lineage(lineage)
    events = state._db.execute("SELECT payload_json FROM planning_events WHERE research_id=? AND entity_id=? AND event_type='CYCLE5_TRANSFORM_REPAIRED'", (research_id, record_id)).fetchall()
    if lineage.revision != expected_revision:
        if verdict["passed"] and any(from_json(e[0])["before_revision"] == expected_revision for e in events):
            return {"status": "ALREADY_REPAIRED", "revision": lineage.revision, "verification": verdict}
        raise ValueError("STALE_REPAIR_REQUIRES_NEW_PLAN_REVIEW")
    if verdict["passed"]:
        return {"status": "NO_REPAIR_REQUIRED", "revision": lineage.revision, "verification": verdict}
    if len(events) >= MAX_REPAIR_ATTEMPTS or lineage.child_kind != "artifact" or lineage.parent_kind != "dataset":
        raise ValueError("REPAIR_SCOPE_REQUIRES_NEW_ANALYSIS_PLAN")
    artifact = state.file_artifact(lineage.child_id, research_id)
    contract, _ = state.contract(lineage.contract_id)
    if (artifact["status"] != "PENDING" or artifact["artifact_type"] != "TRANSFORM_CSV"
            or artifact["contract_id"] != contract.contract_id
            or contract.research_id != research_id
            or not any(ref.type.value == "dataset" and ref.id == lineage.parent_id for ref in contract.inputs)):
        raise ValueError("REPAIR_ORIGINAL_CONTRACT_MISMATCH")
    headers, rows = state.cycle5._expected_rows(lineage)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=headers, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    content = stream.getvalue().encode("utf-8", errors="strict")
    output_hash = sha256_bytes(content)
    relative = f"artifacts/transform-{digest([record_id, expected_revision, output_hash])}.csv"
    path = state.workspace.path(research_id, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != content:
        raise ValueError("REPAIR_OUTPUT_TAMPERED")
    path.write_bytes(content)
    semantics = []
    refs = dict(lineage.semantic_refs)
    for source in state.cycle5.records(research_id, "source"):
        if source.target_kind == "artifact" and source.target_id == lineage.child_id:
            updated = source.model_copy(update={"revision": source.revision + 1, "target_hash": output_hash})
            semantics.append(updated)
            refs[source.record_id] = updated.revision
    updated = TransformationLineage.model_validate(lineage.model_copy(update={"revision": lineage.revision + 1, "child_hash": output_hash, "semantic_refs": refs}))
    records = [("source", s) for s in semantics] + [("lineage", updated)]
    audit = {"before_revision": expected_revision, "contract_hash": digest(contract),
             "frozen_declaration": digest(lineage), "before_child_hash": lineage.child_hash,
             "after_child_hash": output_hash, "repair_policy_version": REPAIR_POLICY_VERSION,
             "input_dataset_unchanged": True, "semantic_meaning_unchanged": True,
             "scope": "pending_csv_artifact", "cost_usd": 0,
             "records": [{"kind": k, "record": r.model_dump(mode="json"), "sha256": digest(r)} for k, r in records]}
    def write():
        current = next(r for r in state.cycle5.records(research_id, "lineage") if r.record_id == record_id)
        if current != lineage or state.file_artifact(lineage.child_id, research_id)["status"] != "PENDING":
            raise ValueError("STALE_REPAIR_REQUIRES_NEW_PLAN_REVIEW")
        state.cycle5._expected_rows(lineage)
        if fault: fault("BEFORE_COMMIT")
        state._db.execute("UPDATE artifacts SET relative_path=?,sha256=?,size_bytes=?,payload_json=? WHERE research_id=? AND artifact_id=? AND status='PENDING'", (relative, output_hash, len(content), to_json({"relative_path": relative}), research_id, lineage.child_id))
        for kind, record in records:
            state.cycle5._store(kind, record)
    state._planning_commit(research_id, "CYCLE5_TRANSFORM_REPAIRED", "artifact", record_id, audit, write)
    if fault: fault("AFTER_COMMIT")
    checked = state.cycle5.verify_lineage(updated)
    if not checked["passed"]:
        raise ValueError("REPAIR_REVALIDATION_FAILED")
    return {"status": "REPAIRED", "revision": updated.revision, "verification": checked,
            "scientific_commit": False, "live_efficacy": "NOT_VALIDATED"}


def eligible_for_reexecution(check_ids: list[str]) -> bool:
    return bool(check_ids) and set(check_ids) <= REEXECUTABLE_CHECKS


def build_failure(*, verification: VerificationResult, research_id: str, task_id: str,
                  contract_id: str, contract_hash: str, plan: AnalysisPlan,
                  mutation_id: str, experiment_id: str | None, revision: int,
                  dataset_hash: str, artifact_refs: list[str], trace_refs: list[str],
                  available_usd: float, required_usd: float) -> VerificationFailureEvidence:
    failed = [dict(check.model_dump(mode="json"), check_version=VERIFICATION_POLICY_VERSION,
                   check_category=("numerical" if check.check_id in {
                       "RESULT_FIELD_MATCH", "NUMERIC_PROVENANCE", "F3P_INDEPENDENT_ASSOCIATION"}
                       else "integrity"), execution_status="FAILED")
              for check in verification.checks if not check.passed]
    return VerificationFailureEvidence(
        failure_id=new_id("VF"), research_id=research_id, task_id=task_id,
        contract_id=contract_id, experiment_id=experiment_id, analysis_revision=revision,
        mutation_id=mutation_id, check_ids=[item["check_id"] for item in failed],
        check_details=failed, authoritative_contract_ref=contract_id,
        authoritative_contract_hash=contract_hash, frozen_plan_hash=plan_hash(plan),
        input_snapshot_refs=[plan.dataset_id], input_hashes={plan.dataset_id: dataset_hash},
        relevant_artifact_refs=artifact_refs, trace_refs=trace_refs,
        required_post_repair_checks=[check.check_id for check in verification.checks],
        budget_required_for_recovery=required_usd, budget_available=available_usd)


def qualify_association(plan: AnalysisPlan, observed: dict, rows: list[dict[str, str]],
                        checker_value: float | None = None) -> str:
    """고정된 두 연관 분석에만 NumPy 참조값을 사용한다. 제한된 수치 비교이며 과학적 타당성 증명은 아니다."""
    if plan.skill_plan is None:
        if plan.method not in {"pearson_correlation", "spearman_correlation"}:
            return "UNRESOLVED_DISAGREEMENT"
        names = dict(zip(("x", "y"), plan.selected_variables))
    else:
        if plan.skill_plan.skill_id != "tabular_association_v1":
            return "UNRESOLVED_DISAGREEMENT"
        if plan.skill_plan.method != plan.method or plan.skill_plan.sampling != "iid":
            return "ORIGINAL_CONTRACT_MISMATCH"
        names = plan.skill_plan.variables
    try:
        pairs = []
        for row in rows:
            try:
                pair = (float(row[names["x"]]), float(row[names["y"]]))
            except ValueError:
                continue
            if all(math.isfinite(value) for value in pair):
                pairs.append(pair)
        values = np.asarray(pairs, dtype=float)
        if len(values) < 3 or not np.all(np.isfinite(values)):
            return "UNRESOLVED_DISAGREEMENT"
        x, y = values[:, 0], values[:, 1]
        if plan.method == "spearman_correlation":
            from scipy.stats import rankdata
            x, y = rankdata(x), rankdata(y)
        reference = float(np.corrcoef(x, y)[0, 1])
        subject = float(observed.get("metrics", {}).get("estimate", observed.get("estimate")))
    except (KeyError, TypeError, ValueError, IndexError, FloatingPointError):
        return "UNRESOLVED_DISAGREEMENT"
    if not np.isfinite(reference) or not np.isfinite(subject):
        return "UNRESOLVED_DISAGREEMENT"
    tolerance = 1e-10
    if checker_value is not None and abs(subject - reference) <= tolerance and abs(checker_value - reference) > tolerance:
        return "CHECKER_QUALIFICATION_FAILED"
    if abs(subject - reference) > tolerance:
        return "ANALYSIS_CHECK_FAILED"
    return "NUMERICAL_CHECKS_PASSED_FOR_SCOPE"


class CheckerQualification(StrictModel):
    status: Literal["ANALYSIS_CHECK_FAILED", "CHECKER_QUALIFICATION_FAILED",
                    "ORIGINAL_CONTRACT_MISMATCH", "UNRESOLVED_DISAGREEMENT",
                    "NUMERICAL_CHECKS_PASSED_FOR_SCOPE"]
    checker_identity: str = "numpy.corrcoef"
    checker_version: str = "f3p-association-reference-1"
    frozen_plan_hash: str
    failure_id: str
    input_hashes: dict[str, str]
    scope: str = "association estimate only; ranks for fixed Spearman"
    tolerance: float = 1e-10


def qualify_association_from_state(state, research_id: str, plan: AnalysisPlan,
                                   observed: dict) -> str:
    record = state.dataset_record(plan.dataset_id, research_id)
    path = state.workspace.path(research_id, record["stored_path"])
    with io.StringIO(path.read_bytes().decode("utf-8-sig", errors="strict"), newline="") as stream:
        rows = list(csv.DictReader(stream))
    return qualify_association(plan, observed, rows)


def persist_audit_artifact(state, research_id: str, contract_id: str,
                           artifact_id: str, artifact_type: str, document: dict) -> None:
    """변경 불가·해시 등록된 실패 근거. 정본 과학 결과가 아니다."""
    from .storage import ArtifactIntegrityError, sha256_bytes
    content = (to_json(document) + "\n").encode("utf-8", errors="strict")
    relative = f"artifacts/{artifact_id}.json"
    path = state.workspace.path(research_id, relative)
    digest = sha256_bytes(content)
    if path.exists():
        if path.read_bytes() != content:
            raise ArtifactIntegrityError(artifact_id)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    row = state._db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
    if row is None:
        state.register_file_artifact(artifact_id, research_id, contract_id, artifact_type,
                                     relative, "verification_repair", contract_id, digest)
    else:
        state.file_artifact(artifact_id, research_id)


class ArithmeticCheck(StrictModel):
    status: Literal["passed", "failed", "not_applicable", "needs_reference_check"]
    reason: str
    tolerance: float = 1e-10
    details: dict[str, Any] = Field(default_factory=dict)


def ridge_arithmetic_check(plan: AnalysisPlan, result: dict,
                            rows: list[dict[str, str]]) -> ArithmeticCheck:
    """벌점 없는 절편·alpha=1인 v1 설계를 검사한다. 정규방정식·절편 잔차를 정규화하고 float64 허용오차 1e-10을 적용한다. 불량 조건수·비유한 값은 참조 검사가 필요하다."""
    skill = plan.skill_plan
    if skill is None or skill.skill_id != "tabular_regression_evaluation_v1":
        return ArithmeticCheck(status="not_applicable", reason="fixed Ridge holdout only")
    if plan.method != "ridge_holdout" or skill.method != plan.method:
        return ArithmeticCheck(status="failed", reason="ORIGINAL_CONTRACT_MISMATCH")
    try:
        names = [skill.variables[key] for key in sorted(skill.variables) if key.startswith("feature_")]
        retained = []
        for row in rows:
            try:
                target = float(row[skill.variables["target"]])
            except ValueError:
                continue
            if not math.isfinite(target):
                continue
            features = []
            for name in names:
                try:
                    value = float(row[name])
                except ValueError:
                    value = float("nan")
                features.append(value if math.isfinite(value) else float("nan"))
            retained.append((target, features))
        target = np.asarray([item[0] for item in retained])
        features = np.asarray([item[1] for item in retained])
        test_n = math.ceil(len(target) * skill.holdout_fraction)
        order = np.random.default_rng(skill.seed).permutation(len(target))
        train, test = order[test_n:], order[:test_n]
        if len(train) < 5 or len(test) < 2:
            return ArithmeticCheck(status="needs_reference_check", reason="infeasible split")
        model = result["diagnostics"]["fitted_training_only"]
        means, scales = np.asarray(model["imputer_means"]), np.asarray(model["scales"])
        beta = np.asarray(model["coefficients"])
        expected_means = np.mean(features[train], axis=0)
        filled_train = np.where(np.isnan(features[train]), expected_means, features[train])
        expected_scales = np.std(filled_train, axis=0)
        expected_scales = np.where(expected_scales == 0, 1.0, expected_scales)
        if not all(np.all(np.isfinite(value)) for value in (means, scales, beta, expected_means, expected_scales)):
            return ArithmeticCheck(status="needs_reference_check", reason="nonfinite realized training state")
        if means.shape != expected_means.shape or scales.shape != expected_scales.shape or beta.shape != (len(names) + 1,):
            return ArithmeticCheck(status="failed", reason="model dimensions")
        training_only = (np.allclose(means, expected_means, rtol=1e-10, atol=1e-10) and
                         np.allclose(scales, expected_scales, rtol=1e-10, atol=1e-10))
        if np.any(scales <= 0) or model.get("alpha") != 1.0:
            return ArithmeticCheck(status="failed", reason="frozen preprocessing or alpha")
        design = np.column_stack((np.ones(len(train)), (filled_train - means) / scales))
        penalty = np.diag([0.0] + [1.0] * len(names))
        matrix, rhs = design.T @ design + penalty, design.T @ target[train]
        condition = float(np.linalg.cond(matrix))
        if not math.isfinite(condition) or condition > 1e12:
            return ArithmeticCheck(status="needs_reference_check", reason="ill-conditioned system")
        normal_residual = float(np.linalg.norm(matrix @ beta - rhs) /
                                (1 + np.linalg.norm(rhs) + np.linalg.norm(matrix) * np.linalg.norm(beta)))
        train_prediction = design @ beta
        intercept_residual = float(abs(np.sum(train_prediction - target[train])) /
                                   (1 + np.sum(np.abs(train_prediction)) + np.sum(np.abs(target[train]))))
        filled_test = np.where(np.isnan(features[test]), means, features[test])
        prediction = np.column_stack((np.ones(len(test)), (filled_test - means) / scales)) @ beta
        error = target[test] - prediction
        reconstructed = {"mae": float(np.mean(np.abs(error))), "rmse": float(np.sqrt(np.mean(error**2)))}
        metrics_match = all(np.isclose(result["metrics"]["candidate"][name], value,
                                       rtol=1e-10, atol=1e-10) for name, value in reconstructed.items())
        passed = training_only and normal_residual <= 1e-10 and intercept_residual <= 1e-10 and metrics_match
        return ArithmeticCheck(status="passed" if passed else "failed", reason="fixed Ridge numerical consistency",
                               details={"training_only": bool(training_only), "normal_residual": normal_residual,
                                        "intercept_residual": intercept_residual,
                                        "reconstructed_metrics_match": bool(metrics_match), "condition": condition})
    except (KeyError, TypeError, ValueError, IndexError, np.linalg.LinAlgError):
        return ArithmeticCheck(status="needs_reference_check", reason="unsupported realized state")


def ridge_arithmetic_from_state(state, research_id: str, plan: AnalysisPlan, result: dict) -> ArithmeticCheck:
    record = state.dataset_record(plan.dataset_id, research_id)
    with io.StringIO(state.workspace.path(research_id, record["stored_path"]).read_bytes().decode(
            "utf-8-sig", errors="strict"), newline="") as stream:
        return ridge_arithmetic_check(plan, result, list(csv.DictReader(stream)))
