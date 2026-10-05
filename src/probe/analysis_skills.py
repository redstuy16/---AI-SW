"""버전과 범위가 고정된 수치 분석. 모델 출력을 코드로 실행하지 않는다."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import math
from typing import Literal

import numpy as np
from pydantic import Field, model_validator
from scipy import stats

from .database import to_json
from .schemas import StrictModel


SKILL_VERSIONS = {
    "tabular_association_v1": "1.0.0",
    "tabular_regression_evaluation_v1": "1.0.0",
    "timeseries_backtest_v1": "1.0.0",
}


class SkillPlan(StrictModel):
    skill_id: Literal["tabular_association_v1", "tabular_regression_evaluation_v1", "timeseries_backtest_v1"]
    skill_version: Literal["1.0.0"]
    dataset_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    variables: dict[str, str]
    observation_id: str = Field(min_length=1)
    entity_column: str | None = None
    observation_unit: str = Field(min_length=1)
    units: dict[str, str]
    sampling: Literal["iid", "grouped", "paired", "temporal", "unspecified"]
    assumption_evidence: str = Field(min_length=1)
    method: Literal["pearson_correlation", "spearman_correlation", "ridge_holdout", "ridge_rolling_origin"]
    exclusion_policy: Literal["complete_case", "train_only_feature_imputation", "reject_missing"]
    seed: int = Field(ge=0, le=2**32 - 1)
    holdout_fraction: float = Field(default=0.25, gt=0, lt=0.5)
    lags: list[int] = Field(default_factory=lambda: [1, 2])
    min_train: int = Field(default=6, ge=4)
    timestamp_column: str | None = None
    timezone_policy: Literal["aware_utc", "naive_utc"] = "aware_utc"
    max_rows: int = Field(default=100_000, ge=3, le=100_000)

    @model_validator(mode="after")
    def compatible(self):
        expected = {"tabular_association_v1": ("pearson_correlation", "spearman_correlation"),
                    "tabular_regression_evaluation_v1": ("ridge_holdout",),
                    "timeseries_backtest_v1": ("ridge_rolling_origin",)}
        if self.method not in expected[self.skill_id] or self.skill_version != SKILL_VERSIONS[self.skill_id]:
            raise ValueError("skill method/version mismatch")
        if not self.lags or any(lag <= 0 or lag > 12 for lag in self.lags) or len(set(self.lags)) != len(self.lags):
            raise ValueError("lags must be distinct positive integers up to 12")
        if self.skill_id == "tabular_association_v1" and set(self.variables) != {"x", "y"}:
            raise ValueError("association requires x and y")
        if self.skill_id == "tabular_regression_evaluation_v1" and (
                "target" not in self.variables or not any(k.startswith("feature_") for k in self.variables)):
            raise ValueError("regression requires target and feature_ variables")
        if self.skill_id == "timeseries_backtest_v1" and (set(self.variables) != {"target"} or not self.timestamp_column):
            raise ValueError("backtest requires target and timestamp column")
        if len(set(self.variables.values())) != len(self.variables):
            raise ValueError("variable columns must be distinct")
        if self.observation_id in self.variables.values():
            if self.skill_id != "timeseries_backtest_v1" or self.observation_id != self.timestamp_column:
                raise ValueError("observation identity cannot be an analysis variable")
        if self.entity_column and self.entity_column in set(self.variables.values()) | {self.observation_id}:
            raise ValueError("entity column must be distinct from features, target and row identity")
        if self.skill_id != "timeseries_backtest_v1" and (self.timestamp_column is not None or
                                                            self.lags != [1, 2] or self.min_train != 6):
            raise ValueError("time-series parameters are unsupported for this Skill")
        if self.skill_id != "tabular_regression_evaluation_v1" and self.holdout_fraction != 0.25:
            raise ValueError("holdout fraction is unsupported for this Skill")
        if self.skill_id == "timeseries_backtest_v1" and self.observation_id != self.timestamp_column:
            raise ValueError("time-series row identity must be the timestamp")
        return self

    def fingerprint(self) -> str:
        return sha256(to_json(self).encode("utf-8", errors="strict")).hexdigest()


class SkillRequest(StrictModel):
    research_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    plan_ref: str = Field(min_length=1)
    plan_version: Literal["1"]
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan: SkillPlan


class SkillResult(StrictModel):
    applicability: Literal["applicable", "needs_review", "unsupported"]
    execution: Literal["not_run", "succeeded", "failed", "blocked"]
    verification: Literal["external_verifier_required"] = "external_verifier_required"
    reason_codes: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    skill_id: str
    skill_version: str
    method: str
    plan_fingerprint: str
    n: int = Field(ge=0)
    counts: dict[str, int] = Field(default_factory=dict)
    split: dict = Field(default_factory=dict)
    diagnostics: dict = Field(default_factory=dict)
    metrics: dict = Field(default_factory=dict)
    uncertainty: dict = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    trace: list[dict] = Field(default_factory=list)
    provenance: dict = Field(default_factory=dict)


class SkillApplicabilityError(ValueError):
    def __init__(self, code: str, applicability: str = "unsupported", requirement: str = ""):
        super().__init__(code)
        self.code, self.applicability, self.requirement = code, applicability, requirement


def _number(raw: str) -> float | None:
    if raw.strip() == "":
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise SkillApplicabilityError("NON_NUMERIC_VALUE") from exc
    return value if math.isfinite(value) else None


def _base(plan: SkillPlan, n: int, **updates) -> SkillResult:
    return SkillResult(applicability="applicable", execution="succeeded", skill_id=plan.skill_id,
                       skill_version=plan.skill_version, method=plan.method,
                       plan_fingerprint=plan.fingerprint(), n=n, **updates)


def _ridge_fit(features: np.ndarray, target: np.ndarray) -> dict:
    if np.any(np.all(np.isnan(features), axis=0)):
        raise SkillApplicabilityError("ALL_MISSING_TRAIN_FEATURE")
    means = np.nanmean(features, axis=0)
    filled = np.where(np.isnan(features), means, features)
    scales = np.std(filled, axis=0)
    scales = np.where(scales == 0, 1.0, scales)
    centered = (filled - means) / scales
    design = np.column_stack((np.ones(len(centered)), centered))
    penalty = np.diag([0.0] + [1.0] * centered.shape[1])
    coefficients = np.linalg.solve(design.T @ design + penalty, design.T @ target)
    if not np.all(np.isfinite(coefficients)):
        raise SkillApplicabilityError("UNDEFINED_MODEL")
    return {"imputer_means": means.tolist(), "scales": scales.tolist(),
            "coefficients": coefficients.tolist(), "alpha": 1.0}


def _ridge_predict(model: dict, features: np.ndarray) -> np.ndarray:
    means = np.asarray(model["imputer_means"])
    scales = np.asarray(model["scales"])
    filled = np.where(np.isnan(features), means, features)
    design = np.column_stack((np.ones(len(filled)), (filled - means) / scales))
    return design @ np.asarray(model["coefficients"])


def _metrics(truth: np.ndarray, prediction: np.ndarray) -> dict:
    error = truth - prediction
    return {"mae": float(np.mean(np.abs(error))), "rmse": float(np.sqrt(np.mean(error**2)))}


def _association(plan: SkillPlan, rows: list[dict[str, str]]) -> SkillResult:
    if plan.sampling != "iid" or len(plan.assumption_evidence.strip()) < 8:
        raise SkillApplicabilityError("INDEPENDENCE_UNDOCUMENTED", "needs_review", "document independent observation sampling")
    if plan.exclusion_policy != "complete_case":
        raise SkillApplicabilityError("EXCLUSION_POLICY_UNSUPPORTED")
    identities = [row[plan.observation_id] for row in rows]
    if any(not item for item in identities) or len(set(identities)) != len(identities):
        raise SkillApplicabilityError("DUPLICATE_OBSERVATION_ID", "needs_review", "provide independent unique observation IDs")
    x, y, missing, nonfinite = [], [], 0, 0
    for row in rows:
        raw = [row[plan.variables[key]].strip() for key in ("x", "y")]
        if not all(raw):
            missing += 1
            continue
        pair = [_number(value) for value in raw]
        if None in pair:
            nonfinite += 1
            continue
        x.append(pair[0]); y.append(pair[1])
    if len(x) < 3:
        raise SkillApplicabilityError("INSUFFICIENT_PAIRS")
    if len(set(x)) < 2 or len(set(y)) < 2:
        raise SkillApplicabilityError("CONSTANT_INPUT")
    if plan.method == "pearson_correlation":
        test = stats.pearsonr(x, y)
        ci = test.confidence_interval(0.95)
        uncertainty = {"status": "estimated", "method": "scipy_pearsonr_fisher", "level": 0.95,
                       "bounds": [float(ci.low), float(ci.high)]}
    else:
        test = stats.spearmanr(x, y)
        uncertainty = {"status": "not_estimated", "reason": "no preapproved interval for Spearman"}
    estimate, p_value = float(test.statistic), float(test.pvalue)
    if not all(math.isfinite(v) for v in (estimate, p_value)):
        raise SkillApplicabilityError("UNDEFINED_ASSOCIATION")
    return _base(plan, len(x), counts={"total": len(rows), "missing_excluded": missing,
                                      "nonfinite_excluded": nonfinite, "used": len(x)},
                 diagnostics={"x_mean": float(np.mean(x)), "y_mean": float(np.mean(y)),
                              "x_std": float(np.std(x, ddof=1)), "y_std": float(np.std(y, ddof=1))},
                 metrics={"estimate": estimate, "p_value": p_value}, uncertainty=uncertainty,
                 limitations=["observational association is not causal", "p-value assumes the documented sampling structure"])


def _regression(plan: SkillPlan, rows: list[dict[str, str]]) -> SkillResult:
    if plan.sampling != "iid" or len(plan.assumption_evidence.strip()) < 8:
        raise SkillApplicabilityError("IID_UNDOCUMENTED", "needs_review", "document IID sampling")
    if plan.exclusion_policy != "train_only_feature_imputation":
        raise SkillApplicabilityError("PREPROCESSING_POLICY_UNSUPPORTED")
    names = [plan.variables[key] for key in sorted(plan.variables) if key.startswith("feature_")]
    target_name = plan.variables["target"]
    if target_name in names or plan.observation_id in names or plan.observation_id == target_name:
        raise SkillApplicabilityError("TARGET_OR_ID_AS_FEATURE")
    ids = [row[plan.observation_id] for row in rows]
    if any(not item for item in ids) or len(set(ids)) != len(ids):
        raise SkillApplicabilityError("DUPLICATE_OBSERVATION_ID", "unsupported", "provide unique independent row identity")
    entity = plan.entity_column
    if entity and len({row[entity] for row in rows}) != len(rows):
        raise SkillApplicabilityError("REPEATED_ENTITY", "needs_review", "use group-aware evaluation outside v1")
    retained, missing_target = [], 0
    for index, row in enumerate(rows):
        target = _number(row[target_name])
        if target is None:
            missing_target += 1
            continue
        features = [_number(row[name]) for name in names]
        retained.append((index, target, [np.nan if value is None else value for value in features]))
    n = len(retained)
    test_n = math.ceil(n * plan.holdout_fraction)
    if n < 8 or n - test_n < 5 or test_n < 2:
        raise SkillApplicabilityError("INFEASIBLE_HOLDOUT")
    order = np.random.default_rng(plan.seed).permutation(n)
    train, test = order[test_n:], order[:test_n]
    matrix = np.asarray([entry[2] for entry in retained], dtype=float)
    target = np.asarray([entry[1] for entry in retained], dtype=float)
    if np.any(np.all(np.isnan(matrix[train]), axis=0)):
        raise SkillApplicabilityError("ALL_MISSING_TRAIN_FEATURE")
    model = _ridge_fit(matrix[train], target[train])
    baseline = np.full(len(test), np.mean(target[train]))
    candidate = _ridge_predict(model, matrix[test])
    baseline_metrics, candidate_metrics = _metrics(target[test], baseline), _metrics(target[test], candidate)
    return _base(plan, n, counts={"total": len(rows), "missing_target_excluded": missing_target,
                                  "train": len(train), "evaluation": len(test)},
                 split={"seed": plan.seed, "holdout_fraction": plan.holdout_fraction,
                        "train_row_sha256": sha256(to_json(sorted(ids[retained[i][0]] for i in train)).encode()).hexdigest(),
                        "evaluation_row_sha256": sha256(to_json(sorted(ids[retained[i][0]] for i in test)).encode()).hexdigest()},
                 diagnostics={"fitted_training_only": model, "feature_columns": names},
                 metrics={"baseline": baseline_metrics, "candidate": candidate_metrics,
                          "candidate_minus_baseline_mae": candidate_metrics["mae"] - baseline_metrics["mae"],
                          "candidate_minus_baseline_rmse": candidate_metrics["rmse"] - baseline_metrics["rmse"]},
                 uncertainty={"status": "not_estimated", "reason": "single fixed holdout; no interval procedure approved"},
                 limitations=["IID evidence is declared, not proven by a CSV", "negative metric difference favors candidate"])


def _backtest(plan: SkillPlan, rows: list[dict[str, str]]) -> SkillResult:
    if plan.sampling != "temporal" or len(plan.assumption_evidence.strip()) < 8:
        raise SkillApplicabilityError("TEMPORAL_STRUCTURE_UNDOCUMENTED", "needs_review", "document regular sampling")
    if plan.exclusion_policy != "reject_missing":
        raise SkillApplicabilityError("MISSING_POLICY_UNSUPPORTED")
    stamps, values = [], []
    for row in rows:
        try:
            stamp = datetime.fromisoformat(row[plan.timestamp_column].replace("Z", "+00:00"))
        except ValueError as exc:
            raise SkillApplicabilityError("INVALID_TIMESTAMP") from exc
        if plan.timezone_policy == "aware_utc":
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise SkillApplicabilityError("TIMEZONE_REQUIRED")
            stamp = stamp.astimezone(timezone.utc)
        elif stamp.tzinfo is not None:
            raise SkillApplicabilityError("TIMEZONE_POLICY_MISMATCH")
        value = _number(row[plan.variables["target"]])
        if value is None:
            raise SkillApplicabilityError("MISSING_SERIES_VALUE")
        stamps.append(stamp); values.append(value)
    if len(set(stamps)) != len(stamps):
        raise SkillApplicabilityError("DUPLICATE_TIMESTAMP")
    intervals = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
    if not intervals or intervals[0] <= 0 or any(delta != intervals[0] for delta in intervals):
        raise SkillApplicabilityError("IRREGULAR_OR_UNORDERED_TIME")
    lags = sorted(plan.lags)
    first = max(plan.min_train, max(lags) + 4)
    if len(values) - first < 2:
        raise SkillApplicabilityError("INSUFFICIENT_HISTORY")
    trace, truth, base, candidate = [], [], [], []
    series = np.asarray(values)
    for target_index in range(first, len(values)):
        train_indices = range(max(lags), target_index)
        features = np.asarray([[series[i - lag] for lag in lags] for i in train_indices])
        labels = np.asarray([series[i] for i in train_indices])
        model = _ridge_fit(features, labels)
        forecast = float(_ridge_predict(model, np.asarray([[series[target_index - lag] for lag in lags]]))[0])
        origin = target_index - 1
        trace.append({"origin": stamps[origin].isoformat(), "target": stamps[target_index].isoformat(),
                      "training_label_cutoff": stamps[origin].isoformat(), "prediction": forecast,
                      "baseline_prediction": values[origin]})
        truth.append(values[target_index]); base.append(values[origin]); candidate.append(forecast)
    baseline_metrics, candidate_metrics = _metrics(np.asarray(truth), np.asarray(base)), _metrics(np.asarray(truth), np.asarray(candidate))
    return _base(plan, len(truth), counts={"total": len(rows), "origins": len(trace), "missing_excluded": 0},
                 split={"frequency_seconds": intervals[0], "first_origin": trace[0]["origin"],
                        "last_origin": trace[-1]["origin"], "lags": lags, "window": "expanding"},
                 diagnostics={"per_origin_coverage": len(trace), "cutoff_checked": True},
                 metrics={"baseline": baseline_metrics, "candidate": candidate_metrics,
                          "candidate_minus_baseline_mae": candidate_metrics["mae"] - baseline_metrics["mae"],
                          "candidate_minus_baseline_rmse": candidate_metrics["rmse"] - baseline_metrics["rmse"]},
                 uncertainty={"status": "not_estimated", "reason": "dependent forecast errors; no approved interval method"},
                 limitations=["one-step forecasts only", "regularity does not prove absence of all temporal leakage"],
                 trace=trace)


def execute_skill(plan: SkillPlan, headers: list[str], rows: list[dict[str, str]]) -> SkillResult:
    needed = set(plan.variables.values()) | {plan.observation_id}
    if plan.entity_column:
        needed.add(plan.entity_column)
    if plan.timestamp_column:
        needed.add(plan.timestamp_column)
    if not needed <= set(headers):
        raise SkillApplicabilityError("UNKNOWN_COLUMN")
    if any(not plan.units.get(name) for name in plan.variables.values()):
        raise SkillApplicabilityError("UNITS_UNDOCUMENTED", "needs_review", "provide units for every variable")
    if len(rows) > plan.max_rows:
        raise SkillApplicabilityError("ROW_BUDGET_EXCEEDED")
    if plan.skill_id == "tabular_association_v1":
        return _association(plan, rows)
    if plan.skill_id == "tabular_regression_evaluation_v1":
        return _regression(plan, rows)
    return _backtest(plan, rows)
