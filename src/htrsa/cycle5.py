"""기존 StateService·수정본·검증 의무에 연결하는 선택적 의미 검사."""
from __future__ import annotations

import argparse
import csv
import io
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Literal

from pydantic import Field, FiniteFloat, model_validator

from .database import from_json, to_json
from .research_slice import digest
from .schemas import StrictModel, utc_now
from .storage import DatasetIntegrityError, ArtifactIntegrityError, UnsafeWorkspacePathError


class SemanticReviewRequired(BaseException):
    """검토 전에는 분석을 실행하지 않고 기존 안전 경계에서 대기한다."""


INTEGRITY_ERRORS = (DatasetIntegrityError, ArtifactIntegrityError, UnsafeWorkspacePathError)


class GoalScope(StrictModel):
    intent: Literal["association", "prediction", "descriptive", "causal", "comparison"]
    estimand: str = Field(min_length=1, max_length=400)
    analysis_method: Literal["pearson_correlation", "spearman_correlation", "ridge_holdout", "ridge_rolling_origin", "descriptive", "causal"] | None = None
    quantity: dict[str, str] = Field(min_length=1, max_length=16)
    baseline: dict[str, str | None] = Field(min_length=1, max_length=16)
    units: dict[str, str] = Field(min_length=1, max_length=16)
    population_scope: str = Field(min_length=1, max_length=400)
    spatial_scope: str = Field(min_length=1, max_length=400)
    temporal_scope: str = Field(min_length=1, max_length=400)
    limitations: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def columns(self):
        if set(self.quantity) != set(self.baseline) or set(self.quantity) != set(self.units):
            raise ValueError("SCOPE_COLUMNS_MISMATCH")
        if any(not k.strip() or not v.strip() for k, v in self.quantity.items()):
            raise ValueError("EXPLICIT_QUANTITY_REQUIRED")
        return self


class SourceSemanticRecord(StrictModel):
    record_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    research_id: str
    revision: int = Field(default=1, ge=1)
    target_kind: Literal["dataset", "source", "artifact"]
    target_id: str
    target_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    column: str = Field(min_length=1, max_length=200)
    quantity_kind: Literal["absolute_temperature", "temperature_anomaly", "temperature_difference", "declared_other", "unknown"]
    quantity_name: str = Field(min_length=1, max_length=200)
    unit: str = Field(min_length=1, max_length=80)
    storage_scale: FiniteFloat = Field(default=1.0, gt=0, le=1e12)
    baseline: str | None = Field(default=None, max_length=400)
    spatial_scope: str = Field(min_length=1, max_length=400)
    temporal_scope: str = Field(min_length=1, max_length=400)
    column_meaning: str = Field(min_length=1, max_length=1000)
    source_precision: int | None = Field(default=None, ge=0, le=15)
    proposed_by: str = Field(min_length=1, max_length=100)
    review_status: Literal["NEEDS_REVIEW", "APPROVED", "REJECTED"] = "NEEDS_REVIEW"
    reviewed_by: Literal["owner"] | None = None

    @model_validator(mode="after")
    def explicit_meaning(self):
        if self.quantity_kind == "temperature_anomaly" and not self.baseline:
            raise ValueError("ANOMALY_BASELINE_REQUIRED")
        if self.review_status == "APPROVED" and (self.reviewed_by != "owner" or self.quantity_kind == "unknown"):
            raise ValueError("INDEPENDENT_SEMANTIC_REVIEW_REQUIRED")
        return self


class GoalWitness(StrictModel):
    record_id: Literal["goal"] = "goal"
    research_id: str
    revision: int = Field(default=1, ge=1)
    original_question: str = Field(min_length=1)
    question_snapshot: str | None = None
    scope: GoalScope
    reviewed_by: Literal["owner"] = "owner"


class TransformationLineage(StrictModel):
    record_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    research_id: str
    revision: int = Field(default=1, ge=1)
    contract_id: str
    contract_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    parent_kind: Literal["dataset", "artifact"] = "dataset"
    parent_id: str
    parent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    child_kind: Literal["dataset", "artifact"]
    child_id: str
    child_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    key_columns: list[str] = Field(min_length=1, max_length=4)
    columns: dict[str, str] = Field(min_length=1, max_length=16)
    operation: Literal["affine", "unit_conversion"]
    scale: FiniteFloat = Field(gt=0, le=1e12)
    offset: FiniteFloat = Field(default=0, ge=-1e12, le=1e12)
    semantic_refs: dict[str, int] = Field(min_length=2, max_length=32)
    reviewed_by: Literal["owner"] = "owner"

    @model_validator(mode="after")
    def mapping(self):
        if len(set(self.key_columns)) != len(self.key_columns) or len(set(self.columns.values())) != len(self.columns):
            raise ValueError("AMBIGUOUS_TRANSFORM_MAPPING")
        if set(self.key_columns) & (set(self.columns) | set(self.columns.values())):
            raise ValueError("KEY_TRANSFORMATION_FORBIDDEN")
        if (self.parent_kind, self.parent_id) == (self.child_kind, self.child_id):
            raise ValueError("DERIVATION_CYCLE")
        return self


MODELS = {"source": SourceSemanticRecord, "goal": GoalWitness, "lineage": TransformationLineage}
CHECK_IDS = ("SOURCE_SEMANTIC_MATCH", "TRANSFORMATION_LINEAGE", "GOAL_SCOPE_MATCH", "CLAIM_SUPPORT")


class Cycle5:
    def __init__(self, state):
        self.state, self.db = state, state._db

    def enabled(self, rid):
        step = self.state.runtime_step(rid, "cycle5_config")
        return bool(step and step["status"] == "COMPLETED" and step["output"] == {"enabled": True, "version": "1"})

    def enable(self, rid, *, actor_role):
        if actor_role != "owner":
            raise ValueError("OWNER_REVIEW_REQUIRED")
        if self.enabled(rid):
            return
        if self.db.execute("SELECT 1 FROM staged_mutations WHERE research_id=?", (rid,)).fetchone():
            raise ValueError("CYCLE5_ENABLE_BEFORE_ANALYSIS_REQUIRED")
        if not self.state.runtime_step(rid, "research_slice_config"):
            from .research_slice_schemas import ResearchSliceConfig
            self.state.configure_research_slice(rid, ResearchSliceConfig(claim_evidence_provenance=True, verifier_dependency_catalog=True))
        cfg = self.state.research_slice.config(rid)
        if not (cfg.claim_evidence_provenance and cfg.verifier_dependency_catalog):
            raise ValueError("CYCLE5_REQUIRES_EXISTING_PROVENANCE_AND_CATALOG")
        self.state.finish_runtime_step(rid, "cycle5_config", {"enabled": True, "version": "1"})

    def records(self, rid, kind, *, history=False):
        if not self.enabled(rid):
            return []
        found = {}
        for row in self.db.execute("SELECT step_key,output_json FROM runtime_steps WHERE research_id=? AND step_key LIKE ? AND status='COMPLETED' ORDER BY rowid", (rid, "cycle5:" + kind + ":%")):
            value = from_json(row["output_json"])
            record = MODELS[kind].model_validate(value["record"])
            events = self.db.execute("SELECT payload_json FROM planning_events WHERE research_id=? AND (entity_id=? OR event_type='CYCLE5_TRANSFORM_REPAIRED') AND event_type IN ('CYCLE5_REVISION','CYCLE5_TRANSFORM_REPAIRED')", (rid, record.record_id)).fetchall()
            if (record.research_id != rid or value["sha256"] != digest(value["record"])
                    or not any(value in from_json(e[0])["records"] for e in events)):
                raise ValueError("SEMANTIC_RECORD_TAMPERED")
            key = (record.record_id, record.revision) if history else record.record_id
            if history or key not in found or found[key].revision < record.revision:
                found[key] = record
        return list(found.values())

    def _target(self, rid, kind, target):
        if kind == "dataset":
            row = self.state.dataset_record(target, rid)
            return row["sha256"], self.state.workspace.path(rid, row["stored_path"])
        if kind == "artifact":
            row = self.state.file_artifact(target, rid)
            return row["sha256"], self.state.workspace.path(rid, row["relative_path"])
        from .scholarly import metadata_digest, source_from_row
        row = self.state._one("SELECT * FROM sources WHERE research_id=? AND source_id=?", (rid, target))
        if row["status"] == "INVALIDATED" or metadata_digest(source_from_row(row)) != row["metadata_hash"]:
            raise ValueError("STALE_SOURCE_SNAPSHOT")
        return row["metadata_hash"], None

    def _csv(self, rid, kind, target, expected_hash):
        current, path = self._target(rid, kind, target)
        if current != expected_hash or path is None or path.stat().st_size > 10_000_000:
            raise ValueError("LINEAGE_INPUT_CHANGED")
        reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig", errors="strict"), newline=""))
        headers, rows = reader.fieldnames or [], list(reader)
        if (not headers or len(set(headers)) != len(headers) or not rows
                or any(None in r or any(v is None for v in r.values()) for r in rows)):
            raise ValueError("INVALID_LINEAGE_CSV")
        return headers, rows

    def _store(self, kind, record):
        value = {"kind": kind, "record": record.model_dump(mode="json"), "sha256": digest(record)}
        key = f"cycle5:{kind}:{record.record_id}:{record.revision}"
        self.db.execute("INSERT INTO runtime_steps(research_id,step_key,status,output_json,attempt,updated_at) VALUES(?,?,'COMPLETED',?,1,?)", (record.research_id, key, to_json(value), utc_now().isoformat()))
        return value

    def _save(self, kind, record, *, actor_role, review=False):
        if isinstance(record, StrictModel):
            record = record.model_dump(mode="json")
        record = MODELS[kind].model_validate(record)
        rid = record.research_id
        if not self.enabled(rid):
            raise ValueError("CYCLE5_DISABLED")
        if kind != "source" and actor_role != "owner":
            raise ValueError("OWNER_REVIEW_REQUIRED")
        if kind == "source":
            if review and actor_role != "owner":
                raise ValueError("WORKER_SELF_APPROVAL_FORBIDDEN")
            if not review and (record.review_status != "NEEDS_REVIEW" or record.reviewed_by is not None or record.proposed_by != actor_role):
                raise ValueError("WORKER_SELF_APPROVAL_FORBIDDEN")
            target_hash, _ = self._target(rid, record.target_kind, record.target_id)
            if target_hash != record.target_hash:
                raise ValueError("SEMANTIC_TARGET_HASH_MISMATCH")
            if record.target_kind != "source":
                headers, _ = self._csv(rid, record.target_kind, record.target_id, target_hash)
                if record.column not in headers:
                    raise ValueError("SEMANTIC_COLUMN_MISSING")
        elif kind == "goal":
            if record.original_question != self.state._one("SELECT goal FROM research_runs WHERE research_id=?", (rid,))[0]:
                raise ValueError("ORIGINAL_QUESTION_CHANGED")
        else:
            contract, _ = self.state.contract(record.contract_id)
            if contract.research_id != rid:
                raise ValueError("LINEAGE_CONTRACT_MISMATCH")
            self._csv(rid, record.parent_kind, record.parent_id, record.parent_hash)
            self._csv(rid, record.child_kind, record.child_id, record.child_hash)
            # 원래 선언의 의미와 계수는 소유자가 먼저 고정한다.
            self._coefficients(record)
        def write():
            existing = next((r for r in self.records(rid, kind) if r.record_id == record.record_id), None)
            if record.revision != (existing.revision + 1 if existing else 1):
                raise ValueError("SEMANTIC_REVISION_CONFLICT")
            if kind == "lineage" and existing and existing.model_dump(exclude={"revision"}) != record.model_dump(exclude={"revision"}):
                raise ValueError("TRANSFORM_DECLARATION_CHANGED_NEW_ANALYSIS_PLAN_REQUIRED")
            if kind == "source" and any(r.record_id != record.record_id and (r.target_kind, r.target_id, r.column) == (record.target_kind, record.target_id, record.column) for r in self.records(rid, kind)):
                raise ValueError("AMBIGUOUS_COLUMN_SEMANTICS")
            if kind == "lineage":
                if any(r.record_id != record.record_id and (r.child_kind, r.child_id) == (record.child_kind, record.child_id) for r in self.records(rid, kind)):
                    raise ValueError("AMBIGUOUS_TRANSFORMATION_LINEAGE")
                self.state.research_slice._edge(rid, record.child_kind, record.child_id, record.parent_kind, record.parent_id)
            self.state.research_slice.invalidate(rid, "research" if kind == "goal" else (record.target_kind if kind == "source" else record.child_kind), rid if kind == "goal" else (record.target_id if kind == "source" else record.child_id), "semantic revision", within_transaction=True)
            self._store(kind, record)
        value = {"kind": kind, "record": record.model_dump(mode="json"), "sha256": digest(record)}
        self.state._planning_commit(rid, "CYCLE5_REVISION", kind, record.record_id, {"records": [value]}, write)
        return record

    def propose_source(self, record, *, actor_role):
        return self._save("source", record, actor_role=actor_role)

    def review_source(self, rid, record_id, expected_revision, *, actor_role, approve=True):
        if actor_role != "owner":
            raise ValueError("WORKER_SELF_APPROVAL_FORBIDDEN")
        record = next(r for r in self.records(rid, "source") if r.record_id == record_id)
        if record.revision != expected_revision or record.review_status != "NEEDS_REVIEW":
            raise ValueError("SEMANTIC_REVIEW_STALE")
        revised = record.model_copy(update={"revision": record.revision + 1, "review_status": "APPROVED" if approve else "REJECTED", "reviewed_by": "owner"})
        return self._save("source", revised, actor_role=actor_role, review=True)

    def set_goal(self, record, *, actor_role):
        record = GoalWitness.model_validate(record)
        run = self.state._one("SELECT goal,research_question FROM research_runs WHERE research_id=?", (record.research_id,))
        expected = run["research_question"] or run["goal"]
        if record.question_snapshot is not None and record.question_snapshot != expected:
            raise ValueError("WITNESS_QUESTION_SNAPSHOT_CHANGED")
        record = record.model_copy(update={"question_snapshot": expected})
        return self._save("goal", record, actor_role=actor_role)

    def declare_lineage(self, record, *, actor_role):
        record = TransformationLineage.model_validate(record)
        contract, _ = self.state.contract(record.contract_id)
        expected = digest(contract)
        if record.contract_hash is not None and record.contract_hash != expected:
            raise ValueError("LINEAGE_ORIGINAL_CONTRACT_CHANGED")
        record = record.model_copy(update={"contract_hash": expected})
        return self._save("lineage", record, actor_role=actor_role)

    def _semantic(self, rid, kind, target, column):
        record = next((r for r in self.records(rid, "source") if (r.target_kind, r.target_id, r.column) == (kind, target, column)), None)
        if record is None or record.review_status != "APPROVED" or self._target(rid, kind, target)[0] != record.target_hash:
            raise ValueError("SEMANTIC_REVIEW_REQUIRED")
        return record

    def _coefficients(self, lineage):
        contract, _ = self.state.contract(lineage.contract_id)
        if lineage.contract_hash != digest(contract):
            raise ValueError("LINEAGE_ORIGINAL_CONTRACT_CHANGED")
        records = {r.record_id: r for r in self.records(lineage.research_id, "source")}
        used, relations = set(), []
        for source, dest in lineage.columns.items():
            a = self._semantic(lineage.research_id, lineage.parent_kind, lineage.parent_id, source)
            b = self._semantic(lineage.research_id, lineage.child_kind, lineage.child_id, dest)
            used.update((a.record_id, b.record_id))
            if any(getattr(a, k) != getattr(b, k) for k in ("quantity_kind", "quantity_name", "baseline", "spatial_scope", "temporal_scope")):
                raise ValueError("TRANSFORM_MEANING_CHANGED_NEW_PLAN_REQUIRED")
            scale, offset = Decimal(str(lineage.scale)), Decimal(str(lineage.offset))
            if lineage.operation == "unit_conversion":
                conversions = {("degC", "degF"): (Decimal("1.8"), Decimal("32")), ("degF", "degC"): (Decimal(5) / 9, -Decimal(160) / 9), ("degC", "K"): (Decimal(1), Decimal("273.15")), ("K", "degC"): (Decimal(1), Decimal("-273.15"))}
                if a.quantity_kind not in {"absolute_temperature", "temperature_anomaly", "temperature_difference"}:
                    raise ValueError("UNSUPPORTED_UNIT_CONVERSION")
                unit_scale, unit_offset = (Decimal(1), Decimal(0)) if a.unit == b.unit else conversions.get((a.unit, b.unit), (None, None))
                if unit_scale is None:
                    raise ValueError("UNSUPPORTED_UNIT_CONVERSION")
                if a.quantity_kind != "absolute_temperature":
                    unit_offset = Decimal(0)
                expected = (unit_scale * Decimal(str(a.storage_scale)) / Decimal(str(b.storage_scale)), unit_offset / Decimal(str(b.storage_scale)))
                if abs(scale - expected[0]) > Decimal("1e-12") or abs(offset - expected[1]) > Decimal("1e-12"):
                    raise ValueError("TRANSFORM_DECLARATION_WRONG_SCALE_OR_OFFSET")
                scale, offset = expected
            elif a.unit != b.unit:
                raise ValueError("UNIT_CHANGE_REQUIRES_UNIT_CONVERSION")
            elif a.quantity_kind in {"absolute_temperature", "temperature_anomaly", "temperature_difference"} and (scale != Decimal(str(a.storage_scale)) / Decimal(str(b.storage_scale)) or offset != 0):
                raise ValueError("TEMPERATURE_MEANING_REQUIRES_SCALE_ONLY_STORAGE_CONVERSION")
            relations.append((source, dest, scale, offset, b.source_precision))
        if set(lineage.semantic_refs) != used or any(records[k].revision != v for k, v in lineage.semantic_refs.items()):
            raise ValueError("STALE_TRANSFORM_SEMANTICS")
        return relations

    def _expected_rows(self, lineage):
        relations = self._coefficients(lineage)
        headers, rows = self._csv(lineage.research_id, lineage.parent_kind, lineage.parent_id, lineage.parent_hash)
        if not set(lineage.key_columns) <= set(headers) or not set(lineage.columns) <= set(headers):
            raise ValueError("LINEAGE_COLUMN_MISSING")
        target_headers = [lineage.columns.get(h, h) for h in headers]
        if len(set(target_headers)) != len(target_headers):
            raise ValueError("AMBIGUOUS_TRANSFORM_MAPPING")
        expected, keys = [], set()
        for row in rows:
            key = tuple(row[k] for k in lineage.key_columns)
            if any(not k.strip() for k in key) or key in keys:
                raise ValueError("PARENT_DUPLICATE_OR_MISSING_KEY")
            keys.add(key)
            transformed = {lineage.columns.get(k, k): v for k, v in row.items()}
            for source, dest, scale, offset, precision in relations:
                value = Decimal(row[source])
                if not value.is_finite():
                    raise ValueError("NONFINITE_OR_MISSING_PARENT_VALUE")
                value = value * scale + offset
                if precision is not None:
                    value = value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_EVEN)
                transformed[dest] = str(value)
            expected.append(transformed)
        return target_headers, expected

    def verify_lineage(self, lineage):
        try:
            headers, expected = self._expected_rows(lineage)
            child_headers, actual = self._csv(lineage.research_id, lineage.child_kind, lineage.child_id, lineage.child_hash)
            if set(headers) != set(child_headers):
                raise ValueError("CHILD_SCHEMA_CHANGED")
            index = {}
            for row in actual:
                key = tuple(row[k] for k in lineage.key_columns)
                if any(not k.strip() for k in key) or key in index:
                    raise ValueError("CHILD_DUPLICATE_OR_MISSING_KEY")
                index[key] = row
            if set(index) != {tuple(r[k] for k in lineage.key_columns) for r in expected}:
                raise ValueError("CHILD_MISSING_OR_FABRICATED_ROWS")
            for row in expected:
                actual_row = index[tuple(row[k] for k in lineage.key_columns)]
                for col, value in row.items():
                    if col in lineage.columns.values():
                        number = Decimal(actual_row[col])
                        if not number.is_finite() or abs(number - Decimal(value)) > Decimal("1e-10"):
                            raise ValueError("CHILD_VALUE_OR_KEY_ALIGNMENT_MISMATCH")
                    elif actual_row[col] != value:
                        raise ValueError("UNDECLARED_COLUMN_CHANGE")
            return {"check_id": "TRANSFORMATION_LINEAGE", "passed": True, "message": "키별 선언 변환 재검산 통과", "row_count": len(expected)}
        except (ValueError, KeyError, OSError, ArithmeticError, UnicodeError, *INTEGRITY_ERRORS) as exc:
            return {"check_id": "TRANSFORMATION_LINEAGE", "passed": False, "message": str(exc), "row_count": None}

    def _goal(self, rid, plan):
        witness = next(iter(self.records(rid, "goal")), None)
        if witness is None or plan.semantic_scope is None:
            raise ValueError("GOAL_WITNESS_AND_ANALYSIS_SCOPE_REQUIRED")
        original = self.state._one("SELECT goal,research_question FROM research_runs WHERE research_id=?", (rid,))
        intent = "prediction" if plan.method.startswith("ridge_") else "association"
        if (witness.original_question != original["goal"] or witness.scope != plan.semantic_scope
                or witness.question_snapshot != (original["research_question"] or original["goal"])
                or witness.scope.intent != intent or witness.scope.analysis_method != plan.method
                or set(witness.scope.quantity) != set(plan.selected_variables)):
            raise ValueError("GOAL_SCOPE_CHANGED_NEW_ANALYSIS_PLAN_REQUIRED")
        return witness, original

    def _selected(self, rid, plan, witness):
        semantics = [self._semantic(rid, "dataset", plan.dataset_id, col) for col in plan.selected_variables]
        for record in semantics:
            s, col = witness.scope, record.column
            if record.storage_scale != 1:
                raise ValueError("ANALYSIS_REQUIRES_DECLARED_STORAGE_SCALE_TRANSFORMATION")
            if (record.quantity_kind != s.quantity[col] or record.baseline != s.baseline[col] or record.unit != s.units[col]
                    or record.spatial_scope != s.spatial_scope or record.temporal_scope != s.temporal_scope
                    or plan.skill_plan and plan.skill_plan.units.get(col) != record.unit):
                raise ValueError("SOURCE_SEMANTIC_SCOPE_MISMATCH")
        return semantics

    def _lineages(self, rid, dataset_id):
        lineages, pending, visited = [], [("dataset", dataset_id)], set()
        declared = self.records(rid, "lineage")
        while pending:
            target = pending.pop()
            if target in visited:
                raise ValueError("DERIVATION_CYCLE")
            visited.add(target)
            for record in declared:
                if (record.child_kind, record.child_id) == target:
                    lineages.append(record)
                    pending.append((record.parent_kind, record.parent_id))
        return lineages

    def _binding(self, rid, plan):
        witness, original = self._goal(rid, plan)
        semantics = self._selected(rid, plan, witness)
        lineages = self._lineages(rid, plan.dataset_id)
        return {"goal": digest(witness), "question": digest(dict(original)), "plan": digest(plan),
                "semantics": {r.record_id: digest(r) for r in semantics}, "lineages": {r.record_id: digest(r) for r in lineages}}

    def bind_plan(self, rid, contract, plan):
        if not self.enabled(rid):
            return
        value = self._binding(rid, plan)
        self.state.finish_runtime_step(rid, "cycle5_binding:" + contract.contract_id, value, contract.contract_id)

    def require_review(self, rid, dataset_id):
        if not self.enabled(rid):
            return
        try:
            goal = next(iter(self.records(rid, "goal")))
            for col in goal.scope.quantity:
                self._semantic(rid, "dataset", dataset_id, col)
        except (ValueError, StopIteration, *INTEGRITY_ERRORS):
            raise SemanticReviewRequired("SEMANTIC_REVIEW_REQUIRED") from None

    def validate_planning_events(self, rid, events):
        for event in events:
            if event["event_type"] not in {"CYCLE5_REVISION", "CYCLE5_TRANSFORM_REPAIRED"}:
                continue
            values = from_json(event["payload_json"])["records"]
            if not values:
                raise ValueError("SEMANTIC_EVENT_TAMPERED")
            for value in values:
                record = MODELS[value["kind"]].model_validate(value["record"])
                step = self.state.runtime_step(rid, f"cycle5:{value['kind']}:{record.record_id}:{record.revision}")
                if record.research_id != rid or value["sha256"] != digest(record) or not step or step["output"] != value:
                    raise ValueError("SEMANTIC_EVENT_TAMPERED")

    def reconcile_review_checkpoint(self, rid):
        """분석 전 의미 검토 이벤트만 반영한다. 임의 버전 변경이나 고정 계획은 다시 승인하지 않는다."""
        if not self.enabled(rid) or not self.db.execute("SELECT 1 FROM checkpoints WHERE research_id=?", (rid,)).fetchone():
            return
        checkpoint = self.state.latest_checkpoint(rid)
        current = self.state.state_version(rid)
        if current == checkpoint["state_version"] or not checkpoint["cursor"] or checkpoint["cursor"].get("stage") != "WORKER_READY":
            return
        if self.db.execute("SELECT 1 FROM staged_mutations WHERE research_id=?", (rid,)).fetchone() or self.db.execute("SELECT 1 FROM runtime_steps WHERE research_id=? AND step_key LIKE 'worker_plan:%' AND status='COMPLETED'", (rid,)).fetchone():
            return
        events = self.db.execute("SELECT event_type,state_version,payload_json FROM planning_events WHERE research_id=? AND state_version>? ORDER BY state_version", (rid, checkpoint["state_version"])).fetchall()
        if ([r["state_version"] for r in events] != list(range(checkpoint["state_version"] + 1, current + 1))
                or any(r["event_type"] != "CYCLE5_REVISION" for r in events)):
            return
        self.validate_planning_events(rid, events)
        self.records(rid, "source")
        self.records(rid, "goal")
        self.state.checkpoint(rid, "분석 전 의미 검토 반영", checkpoint["cursor"])

    def validate_binding(self, rid, contract_id, plan, supplied=None):
        saved = self.state.runtime_step(rid, "cycle5_binding:" + contract_id)
        if not saved or saved["status"] != "COMPLETED" or saved["output"] != self._binding(rid, plan) or supplied is not None and supplied != saved["output"]:
            raise ValueError("STALE_SEMANTIC_REVISION_REVALIDATION_REQUIRED")
        relevant = saved["output"]["lineages"]
        for lineage in self.records(rid, "lineage"):
            if lineage.record_id in relevant and not self.verify_lineage(lineage)["passed"]:
                raise ValueError("TRANSFORMATION_LINEAGE_FAILED")

    def checks(self, rid, payload):
        if payload.scientific is None:
            return []
        marker = payload.agent_result.provenance.get("cycle5_policy")
        if not self.enabled(rid):
            return [{"check_id": cid, "passed": False, "message": "CYCLE5_POLICY_CHANGED"} for cid in CHECK_IDS] if marker else []
        if marker != {"enabled": True, "version": "1"}:
            return [{"check_id": cid, "passed": False, "message": "CYCLE5_POLICY_MISSING"} for cid in CHECK_IDS]
        contract_id = payload.agent_result.contract_id
        checks = []
        def run(cid, function):
            try:
                function()
                checks.append({"check_id": cid, "passed": True, "message": "현재 승인 수정본과 지원 범위 일치"})
            except (ValueError, KeyError, OSError, TypeError, StopIteration, *INTEGRITY_ERRORS) as exc:
                checks.append({"check_id": cid, "passed": False, "message": str(exc)})
        def plan_and_stamp():
            from .agent_schemas import AnalysisPlan
            saved = self.state.runtime_step(rid, "worker_plan:" + contract_id)
            plan = AnalysisPlan.model_validate(saved["output"] if saved else {})
            binding = self.state.runtime_step(rid, "cycle5_binding:" + contract_id)
            if not binding or binding["output"] != payload.agent_result.provenance.get("cycle5_binding"):
                raise ValueError("MISSING_OR_CHANGED_SEMANTIC_BINDING")
            return plan, binding["output"]
        def goal():
            plan, stamp = plan_and_stamp()
            witness, original = self._goal(rid, plan)
            if stamp["goal"] != digest(witness) or stamp["question"] != digest(dict(original)) or stamp["plan"] != digest(plan):
                raise ValueError("STALE_GOAL_SCOPE_NEW_PLAN_REQUIRED")
        def source():
            plan, stamp = plan_and_stamp()
            witness = next(iter(self.records(rid, "goal")))
            semantics = self._selected(rid, plan, witness)
            if stamp["semantics"] != {r.record_id: digest(r) for r in semantics}:
                raise ValueError("STALE_SEMANTIC_REVISION_REVALIDATION_REQUIRED")
        def transform():
            plan, stamp = plan_and_stamp()
            lineages = self._lineages(rid, plan.dataset_id)
            if stamp["lineages"] != {r.record_id: digest(r) for r in lineages}:
                raise ValueError("STALE_TRANSFORMATION_LINEAGE")
            for lineage in lineages:
                verdict = self.verify_lineage(lineage)
                if not verdict["passed"]:
                    raise ValueError(verdict["message"])
        def claim():
            plan, _ = plan_and_stamp()
            if any(not c["passed"] for c in checks) or payload.scientific.dataset_id != plan.dataset_id or payload.scientific.method != plan.method:
                raise ValueError("CLAIM_EXCEEDS_GOAL_SCOPE")
            result = payload.tool_result.result
            estimate = result.get("metrics", {}).get("estimate", result.get("estimate"))
            if estimate is None:
                delta = result["metrics"]["candidate_minus_baseline_mae"]
                expected = f"Fixed {plan.method} candidate MAE differs from its baseline by {delta:.6g}; predictive evaluation is not causal evidence."
            else:
                x, y = plan.selected_variables
                direction = "positive" if estimate > 0 else "negative" if estimate < 0 else "no monotonic"
                expected = f"{x} and {y} show a {direction} association in the imported sample; this does not establish causality."
            if payload.scientific.claim != expected or payload.agent_result.output != result:
                raise ValueError("CLAIM_SUPPORT_MISMATCH")
        for cid, function in zip(CHECK_IDS, (source, transform, goal, claim)):
            run(cid, function)
        return checks

    def validate_completed(self, rid):
        from .schemas import StagedResult
        from .service import StateConflictError
        for row in self.db.execute("SELECT payload_json FROM staged_mutations WHERE research_id=? AND status='COMMITTED' AND json_extract(payload_json,'$.agent_result.provenance.cycle5_policy.enabled')=1", (rid,)):
            if any(not c["passed"] for c in self.checks(rid, StagedResult.model_validate_json(row[0]))):
                raise StateConflictError("STALE_SEMANTIC_REVISION_NEW_PLAN_REQUIRED")

    def snapshot(self, rid):
        if not self.enabled(rid):
            return {"enabled": False}
        sources, goals, lineages = (self.records(rid, k) for k in ("source", "goal", "lineage"))
        from .schemas import StagedResult
        checks = [{"mutation_id": r["mutation_id"], "checks": self.checks(rid, StagedResult.model_validate_json(r["payload_json"]))}
                  for r in self.db.execute("SELECT mutation_id,payload_json FROM staged_mutations WHERE research_id=? AND status IN ('PENDING','COMMITTED')", (rid,))]
        return {"enabled": True, "version": "1", "sources": [r.model_dump(mode="json") for r in sources],
                "goal": goals[0].model_dump(mode="json") if goals else None,
                "lineages": [{**r.model_dump(mode="json"), "verification": self.verify_lineage(r)} for r in lineages],
                "history": {k: [r.model_dump(mode="json") for r in self.records(rid, k, history=True)] for k in MODELS},
                "current_checks": checks,
                "datasets": [dict(r) for r in self.db.execute("SELECT dataset_id,original_name,sha256,schema_json,status FROM datasets WHERE research_id=?", (rid,))],
                "live_efficacy": "NOT_VALIDATED"}


def main():
    parser = argparse.ArgumentParser(description="소유자가 명시적으로 설정하는 Cycle 5 의미 검증")
    parser.add_argument("database", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("research_id")
    parser.add_argument("action", choices=["enable", "source", "review", "goal", "lineage", "repair", "show"])
    parser.add_argument("--json", type=Path)
    parser.add_argument("--record-id")
    parser.add_argument("--revision", type=int)
    args = parser.parse_args()
    from .database import initialize
    from .service import StateService
    from .storage import Workspace
    db = initialize(args.database)
    try:
        state = StateService(db, Workspace(args.workspace))
        cycle = state.cycle5
        data = from_json(args.json.read_text(encoding="utf-8", errors="strict")) if args.json else None
        if data and data.get("research_id") != args.research_id:
            parser.error("JSON의 research_id가 명령의 연구와 다릅니다")
        if args.action == "enable": cycle.enable(args.research_id, actor_role="owner")
        elif args.action == "source": cycle.propose_source(data, actor_role="owner")
        elif args.action == "review": cycle.review_source(args.research_id, args.record_id, args.revision, actor_role="owner")
        elif args.action == "goal": cycle.set_goal(data, actor_role="owner")
        elif args.action == "lineage": cycle.declare_lineage(data, actor_role="owner")
        elif args.action == "repair":
            from .verification_repair import repair_transformation
            repair_transformation(state, args.research_id, args.record_id, args.revision, actor_role="owner")
        print(to_json(cycle.snapshot(args.research_id)))
    finally:
        db.close()


if __name__ == "__main__":
    main()
