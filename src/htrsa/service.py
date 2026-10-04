"""저장된 연구 상태의 유일한 쓰기 경계."""
from __future__ import annotations

import logging
import sqlite3
import hashlib
import re

from pydantic import ValidationError

from .database import from_json, to_json
from .mock import MockAnalysisTool
from .real_schemas import DatasetRecord
from .storage import Workspace, sha256_file, DatasetIntegrityError, ArtifactIntegrityError
from .schemas import (
    ResearchContract, StagedResult, VerificationCheck, VerificationResult,
    Verdict, new_id, utc_now,
)


log = logging.getLogger("htrsa.state")


class StateConflictError(Exception):
    """잠정 결과의 기준 버전 또는 반영 전제 조건이 바뀌었다."""


class VerificationRequiredError(Exception):
    """변경에 대한 통과 검증이 없다."""


class InvalidStateTransitionError(Exception):
    """현재 변경 상태에서 허용되지 않은 전이다."""


class DuplicateCommitError(Exception):
    """이미 반영한 변경이다."""


class EntityNotFoundError(Exception):
    """필요한 저장 기록이 없다."""


class ContractViolationError(Exception):
    """실행·작업 정보가 계약을 위반한다."""


class DuplicateToolRequestError(Exception):
    """이미 예약한 중복 방지 키다."""


class LoopDetectedError(Exception):
    """동일한 제한 작업을 세 번 시도했다."""


class ActionLimitError(Exception):
    """설정한 작업 수 상한에 도달했다."""


def _emit(event: str, **fields: object) -> None:
    log.info(to_json({"event": event, **fields}))


class StateService:
    """쓰기 담당자는 하나이며 호출자에게 변경 가능한 DB 연결을 제공하지 않는다."""

    def __init__(self, connection: sqlite3.Connection, workspace: Workspace | None = None):
        self._db = connection
        self.workspace = workspace

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row:
        row = self._db.execute(sql, params).fetchone()
        if row is None:
            raise EntityNotFoundError(sql)
        return row

    @property
    def cycle5(self):
        from .cycle5 import Cycle5
        return Cycle5(self)

    @property
    def research_slice(self):
        from .research_slice import ResearchSlice
        return ResearchSlice(self)

    def configure_research_slice(self, research_id: str, config) -> None:
        from .research_slice_schemas import ResearchSliceConfig
        self.research_slice.configure(research_id, ResearchSliceConfig.model_validate(config))

    def publish_claim(self, claim, bindings):
        return self.research_slice.publish(claim, bindings)

    def revalidate_claim(self, research_id: str, claim_id: str):
        return self.research_slice.revalidate(research_id, claim_id)

    def invalidate_claim_parent(self, research_id: str, target_type: str, target_id: str, reason: str):
        return self.research_slice.invalidate(research_id, target_type, target_id, reason)

    def change_verifier_qualification(self, research_id: str, obligation_id: str, status: str):
        return self.research_slice.change_qualification(research_id, obligation_id, status)

    def invalidate_dataset(self, research_id: str, dataset_id: str, reason: str):
        if not reason.strip():
            raise ValueError("invalidation reason required")
        self._one("SELECT dataset_id FROM datasets WHERE research_id=? AND dataset_id=?", (research_id, dataset_id))
        def write():
            self.research_slice.invalidate(research_id, "dataset", dataset_id, reason, within_transaction=True)
            self._db.execute("UPDATE datasets SET status='INVALID' WHERE research_id=? AND dataset_id=?", (research_id, dataset_id))
        return self._planning_commit(research_id, "DATASET_INVALIDATED", "dataset", dataset_id, {"reason": reason}, write)

    def create_research(self, goal: str, *, research_id: str | None = None) -> str:
        if not goal.strip():
            raise ValueError("goal is required")
        if research_id is not None:
            prior = self._db.execute("SELECT goal FROM research_runs WHERE research_id=?", (research_id,)).fetchone()
            if prior:
                if prior["goal"] != goal:
                    raise StateConflictError("research creation identity conflict")
                return research_id
        research_id = research_id or new_id("R")
        with self._db:
            self._db.execute(
                "INSERT INTO research_runs(research_id,goal,state_version,created_at) VALUES (?,?,0,?)",
                (research_id, goal, utc_now().isoformat()),
            )
        _emit("research_created", research_id=research_id)
        return research_id

    def issue_contract(self, contract: ResearchContract, *, runtime_key: str | None = None) -> str:
        """결과 반영 전에 운영 작업·계약 정보를 저장한다."""
        self._one("SELECT research_id FROM research_runs WHERE research_id=?", (contract.research_id,))
        if contract.parent_task_id is not None:
            parent = self._one("SELECT research_id FROM tasks WHERE task_id=?", (contract.parent_task_id,))
            if parent["research_id"] != contract.research_id:
                raise ContractViolationError("parent task belongs to another research")
        task_id = new_id("TASK")
        with self._db:
            from .research_design import bind_contract
            bind_contract(self, contract)
            self._db.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?)",
                (task_id, contract.research_id, contract.parent_task_id, contract.assigned_role, "ISSUED"),
            )
            self._db.execute(
                "INSERT INTO contracts(contract_id,research_id,task_id,contract_json,runtime_key) VALUES (?,?,?,?,?)",
                (contract.contract_id, contract.research_id, task_id, to_json(contract), runtime_key),
            )
        _emit("contract_issued", contract_id=contract.contract_id, task_id=task_id)
        return task_id

    def record_execution(self, contract_id: str, actor_role: str, request, result) -> str:
        """실행 추적을 저장한다. 정본 연구 결과로 취급하지 않는다."""
        row = self._one("SELECT research_id,task_id FROM contracts WHERE contract_id=?", (contract_id,))
        if row["research_id"] != request.research_id or row["task_id"] != request.task_id:
            raise ContractViolationError("execution is bound to a different task")
        agent_run_id = new_id("ARUN")
        reserved = self._db.execute("SELECT 1 FROM tool_dispatches WHERE idempotency_key=? AND request_id=?", (request.idempotency_key, request.request_id)).fetchone()
        with self._db:
            self._db.execute(
                "INSERT INTO agent_runs(agent_run_id,contract_id,actor_role,started_at) VALUES (?,?,?,?)",
                (agent_run_id, contract_id, actor_role, utc_now().isoformat()),
            )
            self._db.execute(
                "INSERT INTO tool_calls (request_id,agent_run_id,tool_name,request_json,result_json,started_at,finished_at,latency_ms,status,estimated_cost_usd,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (request.request_id, agent_run_id, request.tool_name, to_json(request), to_json(result),
                 result.provenance.get("started_at"), result.provenance.get("finished_at"),
                 result.provenance.get("latency_ms"), "SUCCESS" if result.ok else "FAILED", 0,
                 request.idempotency_key if reserved else None),
            )
        _emit("agent_invoked", agent_run_id=agent_run_id)
        _emit("tool_invoked", request_id=request.request_id, tool_name=request.tool_name)
        return agent_run_id

    def contract(self, contract_id: str) -> tuple[ResearchContract, str]:
        row = self._one("SELECT contract_json,task_id FROM contracts WHERE contract_id=?", (contract_id,))
        return ResearchContract.model_validate_json(row["contract_json"]), row["task_id"]

    def runtime_contract(self, research_id: str, runtime_key: str) -> tuple[ResearchContract, str] | None:
        row = self._db.execute("SELECT contract_id FROM contracts WHERE research_id=? AND runtime_key=?",
                               (research_id, runtime_key)).fetchone()
        return self.contract(row["contract_id"]) if row else None

    def task_status(self, task_id: str) -> str:
        return self._one("SELECT status FROM tasks WHERE task_id=?", (task_id,))["status"]

    def set_task_status(self, contract_id: str, status: str) -> None:
        row = self._one("SELECT task_id,status FROM contracts WHERE contract_id=?", (contract_id,))
        transitions = {"ISSUED": {"RUNNING", "CANCELLED"},
                       "RUNNING": {"WAITING_RETRY", "WAITING_ESCALATION", "COMPLETED", "FAILED", "CANCELLED"},
                       "WAITING_RETRY": {"RUNNING", "FAILED", "CANCELLED"},
                       "WAITING_ESCALATION": {"RUNNING", "FAILED", "CANCELLED"}}
        if status not in transitions.get(row["status"], set()):
            raise InvalidStateTransitionError(f"{row['status']} -> {status}")
        with self._db:
            self._db.execute("UPDATE tasks SET status=? WHERE task_id=?", (status, row["task_id"]))
            self._db.execute("UPDATE contracts SET status=? WHERE contract_id=?", (status, contract_id))

    def configure_budget(self, research_id: str, target: float, soft_limit: float, hard_limit: float) -> None:
        self.state_version(research_id)
        if not 0 <= target <= soft_limit <= hard_limit:
            raise ValueError("budget limits must be ordered")
        with self._db:
            self._db.execute("INSERT INTO research_budgets(research_id,target_usd,soft_limit_usd,hard_limit_usd) VALUES (?,?,?,?)",
                             (research_id, target, soft_limit, hard_limit))

    def budget(self, research_id: str) -> dict:
        row = self._one("SELECT * FROM research_budgets WHERE research_id=?", (research_id,))
        result = dict(row)
        result["remaining_usd"] = max(0.0, row["hard_limit_usd"] - row["spent_usd"])
        return result

    def update_owner_budget(self, research_id: str, hard_limit: float) -> None:
        """소유자 제어 경계만 호출한다. 이미 기록된 지출과 미확인 호출은 보존한다."""
        self.budget(research_id)
        if not 0 < hard_limit <= 100:
            raise ValueError("예산 범위 오류")
        with self._db:
            self._db.execute("UPDATE research_budgets SET target_usd=?,soft_limit_usd=?,hard_limit_usd=? WHERE research_id=?",
                             (hard_limit * .25, hard_limit * .75, hard_limit, research_id))

    def runtime_event(self, research_id: str, event_type: str, details: dict | None = None) -> None:
        self.state_version(research_id)
        with self._db:
            self._db.execute("INSERT INTO runtime_events(research_id,event_type,details_json,created_at) VALUES (?,?,?,?)",
                             (research_id, event_type, to_json(details or {}), utc_now().isoformat()))
        _emit(event_type, research_id=research_id)

    def begin_model_run(self, contract_id: str, role: str, provider: str, model: str,
                        base_state_version: int) -> str:
        contract, _ = self.contract(contract_id)
        if contract.assigned_role != role or self.state_version(contract.research_id) != base_state_version:
            raise StateConflictError("stale model invocation or wrong actor")
        run_id = new_id("ARUN")
        with self._db:
            self._db.execute("INSERT INTO agent_runs(agent_run_id,contract_id,actor_role,started_at,research_id,provider,model,status,base_state_version) VALUES (?,?,?,?,?,?,?,?,?)",
                             (run_id, contract_id, role, utc_now().isoformat(), contract.research_id,
                              provider, model, "RUNNING", base_state_version))
            if role == "manager":
                self._db.execute("UPDATE research_budgets SET manager_calls=manager_calls+1 WHERE research_id=?",
                                 (contract.research_id,))
        self.runtime_event(contract.research_id, "AGENT_RUN_STARTED", {"agent_run_id": run_id, "role": role})
        return run_id

    def finish_model_run(self, run_id: str, status: str, usage: dict | None = None,
                         error_code: str | None = None, estimated_cost_usd: float | None = None) -> None:
        if status not in {"COMPLETED", "FAILED", "STALE"}:
            raise ValueError("invalid model run status")
        row = self._one("SELECT research_id,status FROM agent_runs WHERE agent_run_id=?", (run_id,))
        if row["status"] != "RUNNING":
            raise InvalidStateTransitionError(row["status"])
        usage = usage or {}
        with self._db:
            self._db.execute("UPDATE agent_runs SET finished_at=?,latency_ms=?,status=?,request_count=?,input_tokens=?,cached_input_tokens=?,output_tokens=?,reasoning_tokens=?,estimated_cost_usd=?,error_code=?,provider_run_id=? WHERE agent_run_id=?",
                             (utc_now().isoformat(), usage.get("latency_ms"), status,
                              usage.get("request_count"), usage.get("input_tokens"),
                              usage.get("cached_input_tokens"), usage.get("output_tokens"),
                              usage.get("reasoning_tokens"), estimated_cost_usd, error_code,
                              usage.get("provider_run_id"), run_id))
            if estimated_cost_usd is not None:
                self._db.execute("UPDATE research_budgets SET spent_usd=spent_usd+? WHERE research_id=?",
                                 (estimated_cost_usd, row["research_id"]))
            elif row["research_id"] is not None:
                self._db.execute("UPDATE research_budgets SET unknown_price_calls=unknown_price_calls+1,unknown_price_input_tokens=unknown_price_input_tokens+?,unknown_price_output_tokens=unknown_price_output_tokens+? WHERE research_id=?",
                                 (usage.get("input_tokens") or 0, usage.get("output_tokens") or 0,
                                  row["research_id"]))
        self.runtime_event(row["research_id"], "AGENT_RUN_COMPLETED" if status == "COMPLETED" else "AGENT_RUN_FAILED",
                           {"agent_run_id": run_id, "error_code": error_code})

    def resolve_ref(self, research_id: str, ref) -> bool:
        table = {"research": ("research_runs", "research_id"), "dataset": ("datasets", "dataset_id"),
                 "artifact": ("artifacts", "artifact_id"), "task": ("tasks", "task_id"),
                 "contract": ("contracts", "contract_id"), "evidence": ("evidence", "evidence_id"),
                 "experiment": ("experiments", "experiment_id"), "decision": ("decisions", "decision_id"),
                 "checkpoint": ("checkpoints", "checkpoint_id"), "source": ("sources", "source_id"),
                 "hypothesis": ("hypotheses", "hypothesis_id"),
                 "summary": ("milestone_summaries", "summary_id")}.get(ref.type.value)
        if table is None:
            return False
        name, key = table
        row = self._db.execute(f"SELECT * FROM {name} WHERE {key}=? AND research_id=?", (ref.id, research_id)).fetchone()
        if row is None or ("status" in row.keys() and row["status"] in {"INVALIDATED", "CANCELLED", "FAILED", "SUPERSEDED"}):
            return False
        if ref.type.value == "dataset":
            self.dataset_record(ref.id, research_id)
        elif ref.type.value == "artifact" and row["relative_path"]:
            self.file_artifact(ref.id, research_id)
        return True

    def replay_tool(self, idempotency_key: str, request) -> object | None:
        from .schemas import ToolRequest, ToolResult
        row = self._db.execute("SELECT tc.request_json,tc.result_json,td.status FROM tool_dispatches td LEFT JOIN tool_calls tc ON tc.idempotency_key=td.idempotency_key WHERE td.idempotency_key=?",
                               (idempotency_key,)).fetchone()
        if row is None:
            return None
        if row["status"] not in {"FINISHED", "FAILED"} or row["result_json"] is None:
            raise DuplicateToolRequestError("unfinished tool request cannot be replayed")
        previous = ToolRequest.model_validate_json(row["request_json"])
        if previous != request:
            raise DuplicateToolRequestError("idempotency key reused with different request")
        return ToolResult.model_validate_json(row["result_json"])

    def reserve_tool_request(self, contract_id: str, request) -> None:
        contract, task_id = self.contract(contract_id)
        if request.research_id != contract.research_id or request.task_id != task_id or request.actor_id != contract.assigned_role:
            raise ContractViolationError("tool request identity mismatch")
        if request.tool_name not in contract.allowed_tools:
            raise ContractViolationError("tool is not allowed")
        used = self._db.execute(
            "SELECT COUNT(*) FROM tool_calls tc JOIN agent_runs ar USING(agent_run_id) WHERE ar.contract_id=?",
            (contract_id,),
        ).fetchone()[0]
        if used >= contract.constraints.max_tool_calls:
            raise ContractViolationError("contract tool-call budget exhausted")
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO tool_dispatches VALUES (?,?,?,?,?,?)",
                    (request.idempotency_key, request.request_id, request.research_id, request.tool_name, "STARTED", utc_now().isoformat()),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateToolRequestError(request.idempotency_key) from exc
        _emit("TOOL_REQUESTED", request_id=request.request_id, tool_name=request.tool_name)

    def finish_tool_request(self, request, result) -> None:
        with self._db:
            self._db.execute("UPDATE tool_dispatches SET status=? WHERE idempotency_key=?", ("FINISHED" if result.ok else "FAILED", request.idempotency_key))
        _emit("TOOL_FINISHED", request_id=request.request_id, ok=result.ok)

    def register_dataset(self, record: DatasetRecord) -> None:
        if self.workspace is None:
            raise ValueError("workspace is required")
        self._one("SELECT research_id FROM research_runs WHERE research_id=?", (record.research_id,))
        path = self.workspace.path(record.research_id, record.stored_path)
        if not path.is_file() or sha256_file(path) != record.sha256 or path.stat().st_size != record.size_bytes:
            raise DatasetIntegrityError(record.dataset_id)
        with self._db:
            self._db.execute(
                "INSERT INTO datasets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record.dataset_id, record.research_id, record.source_type, record.original_name,
                 record.stored_path, record.sha256, record.size_bytes, record.format,
                 record.row_count, record.column_count, to_json(record.column_schema) if record.column_schema else None,
                 record.created_at, record.status),
            )
        _emit("DATASET_IMPORTED", dataset_id=record.dataset_id)

    def dataset_record(self, dataset_id: str, research_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM datasets WHERE dataset_id=? AND research_id=?", (dataset_id, research_id))
        if row["status"] == "INVALID":
            raise DatasetIntegrityError(dataset_id)
        if self.workspace is None:
            raise ValueError("workspace is required")
        path = self.workspace.path(research_id, row["stored_path"])
        if not path.is_file() or sha256_file(path) != row["sha256"]:
            raise DatasetIntegrityError(dataset_id)
        return row

    def set_dataset_profile(self, dataset_id: str, research_id: str, profile: dict) -> None:
        self.dataset_record(dataset_id, research_id)
        with self._db:
            self._db.execute(
                "UPDATE datasets SET row_count=?,column_count=?,schema_json=?,status='PROFILED' WHERE dataset_id=? AND research_id=?",
                (profile["row_count"], profile["column_count"], to_json(profile["columns"]), dataset_id, research_id),
            )
        _emit("DATASET_PROFILED", dataset_id=dataset_id)

    def register_file_artifact(self, artifact_id: str, research_id: str, contract_id: str,
                               artifact_type: str, relative_path: str, producer_type: str,
                               producer_id: str, digest: str) -> None:
        if self.workspace is None:
            raise ValueError("workspace is required")
        path = self.workspace.path(research_id, relative_path)
        if not path.is_file() or sha256_file(path) != digest:
            raise ArtifactIntegrityError(artifact_id)
        row = self._one("SELECT research_id FROM contracts WHERE contract_id=?", (contract_id,))
        if row["research_id"] != research_id:
            raise ContractViolationError("artifact contract belongs to another research")
        with self._db:
            self._db.execute(
                "INSERT INTO artifacts (artifact_id,research_id,contract_id,kind,payload_json,producer_type,producer_id,artifact_type,relative_path,sha256,size_bytes,created_at,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (artifact_id, research_id, contract_id, "file", to_json({"relative_path": relative_path}),
                 producer_type, producer_id, artifact_type, relative_path, digest, path.stat().st_size,
                 utc_now().isoformat(), "PENDING"),
            )
        _emit("ARTIFACT_CREATED", artifact_id=artifact_id, artifact_type=artifact_type)

    def file_artifact(self, artifact_id: str, research_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM artifacts WHERE artifact_id=? AND research_id=? AND relative_path IS NOT NULL", (artifact_id, research_id))
        if row["status"] in {"INVALIDATED", "SUPERSEDED"}:
            raise ArtifactIntegrityError(artifact_id)
        if self.workspace is None:
            raise ValueError("workspace is required")
        path = self.workspace.path(research_id, row["relative_path"])
        if not path.is_file() or sha256_file(path) != row["sha256"] or path.stat().st_size != row["size_bytes"]:
            raise ArtifactIntegrityError(artifact_id)
        return row

    def stage(self, payload: StagedResult) -> str:
        result = payload.agent_result
        if payload.scientific and self.cycle5.enabled(result.research_id):
            result.provenance["cycle5_policy"] = {"enabled": True, "version": "1"}
            bound = self.runtime_step(result.research_id, "cycle5_binding:" + result.contract_id)
            if bound:
                result.provenance.setdefault("cycle5_binding", bound["output"])
        slice_config = self.research_slice.config(result.research_id)
        if slice_config.claim_evidence_provenance or slice_config.verifier_dependency_catalog:
            expected_policy = slice_config.model_dump(mode="json")
            if result.provenance.get("research_slice_policy", expected_policy) != expected_policy:
                raise StateConflictError("RESEARCH_SLICE_POLICY_CHANGED")
            result.provenance["research_slice_policy"] = expected_policy
        self._one("SELECT contract_id FROM contracts WHERE contract_id=?", (result.contract_id,))
        version = self.state_version(result.research_id)
        mutation_id = new_id("MUT")
        with self._db:
            self._db.execute(
                "INSERT INTO staged_mutations VALUES (?,?,?,?,?,?,NULL,?)",
                (mutation_id, result.research_id, result.contract_id, version, "PENDING", to_json(payload), utc_now().isoformat()),
            )
            if payload.scientific is not None:
                science = payload.scientific
                self._db.execute(
                    "INSERT INTO experiments (experiment_id,research_id,hypothesis_ref,dataset_ref,payload_json,dataset_id,task_id,method,status,result_artifact_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (science.experiment_id, result.research_id, None, science.dataset_id, to_json(science),
                     science.dataset_id, payload.tool_request.task_id, science.method,
                     "PENDING_VERIFICATION", science.stats_artifact_id, utc_now().isoformat()),
                )
                self._db.execute(
                    "INSERT INTO evidence (evidence_id,research_id,source_id,experiment_id,payload_json,claim,polarity,source_type,source_ref,status,provenance_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (science.evidence_id, result.research_id, None, science.experiment_id, to_json(science),
                     science.claim, science.polarity, "artifact", science.stats_artifact_id,
                     "PENDING", to_json(science.numeric_provenance)),
                )
        _emit("EXPERIMENT_STAGED" if payload.scientific else "mutation_staged", mutation_id=mutation_id, base_state_version=version)
        self.research_slice.stage_science(mutation_id, payload)
        return mutation_id

    def state_version(self, research_id: str) -> int:
        return self._one("SELECT state_version FROM research_runs WHERE research_id=?", (research_id,))["state_version"]

    def artifact(self, artifact_id: str) -> dict | None:
        row = self._db.execute("SELECT payload_json FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        return from_json(row["payload_json"]) if row else None

    def mutation_status(self, mutation_id: str) -> str:
        return self._one("SELECT status FROM staged_mutations WHERE mutation_id=?", (mutation_id,))["status"]

    def _evaluate(self, row: sqlite3.Row) -> VerificationResult:
        checks: list[VerificationCheck] = []

        def check(name: str, ok: bool, message: str) -> None:
            checks.append(VerificationCheck(check_id=name, passed=bool(ok), message=message))

        contract_row = self._db.execute("SELECT * FROM contracts WHERE contract_id=?", (row["contract_id"],)).fetchone()
        try:
            contract = ResearchContract.model_validate_json(contract_row["contract_json"]) if contract_row else None
            payload = StagedResult.model_validate_json(row["payload_json"])
            schema_ok = contract is not None
        except ValidationError:
            contract = None
            payload = None
            schema_ok = False
        check("schema", schema_ok, "contract and staged payload parse as Pydantic models")
        check("contract_exists", contract_row is not None, "contract exists")
        research = self._db.execute("SELECT state_version FROM research_runs WHERE research_id=?", (row["research_id"],)).fetchone()
        check("research_exists", research is not None, "research exists")
        task = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (contract_row["task_id"],)).fetchone() if contract_row else None
        check("task_exists", task is not None, "contract task exists")
        check("pending", row["status"] == "PENDING", "mutation has not been committed or rolled back")
        check("duplicate", self._db.execute("SELECT 1 FROM state_events WHERE mutation_id=?", (row["mutation_id"],)).fetchone() is None, "no prior commit event")
        check("state_version", research is not None and research["state_version"] == row["base_state_version"], "base state version is current")

        if contract and payload:
            agent = payload.agent_result
            slice_policy = agent.provenance.get("research_slice_policy")
            if slice_policy is not None:
                saved_policy = self.runtime_step(row["research_id"], "research_slice_config")
                check("RESEARCH_SLICE_POLICY", saved_policy is not None and saved_policy["status"] == "COMPLETED"
                      and saved_policy["output"] == slice_policy, "staged opt-in policy is frozen across verification and commit")
            request = payload.tool_request
            tool = payload.tool_result
            check("contract_identity", agent.contract_id == contract.contract_id == row["contract_id"] and agent.research_id == contract.research_id == row["research_id"], "result belongs to contract and research")
            check("actor", agent.actor_role == contract.assigned_role and request.actor_id == agent.actor_role and task is not None and task["assigned_role"] == agent.actor_role, "assigned actor submitted result")
            check("task_identity", task is not None and request.task_id == task["task_id"] and request.research_id == row["research_id"], "tool request belongs to task")
            check("allowed_tool", request.tool_name in contract.allowed_tools, "tool is allowed by contract")
            check("tool_success", tool.ok, "tool reports success")
            check("tool_result", tool.request_id == request.request_id and tool.tool_name == request.tool_name and bool(tool.result), "matching tool result is present")
            if payload.scientific is None:
                replay = MockAnalysisTool().run(request) if request.tool_name == MockAnalysisTool.name else None
                check("deterministic_output", replay is not None and replay.ok and tool.ok and replay.result == tool.result, "mock tool result matches deterministic replay")
            call = self._db.execute("SELECT tc.*,ar.contract_id,ar.actor_role FROM tool_calls tc JOIN agent_runs ar USING(agent_run_id) WHERE tc.request_id=?", (request.request_id,)).fetchone()
            check("execution_trace", call is not None and call["contract_id"] == contract.contract_id and call["actor_role"] == agent.actor_role and call["request_json"] == to_json(request) and call["result_json"] == to_json(tool), "recorded execution matches staged result")
            run_calls = self._db.execute("SELECT tool_name FROM tool_calls WHERE agent_run_id=?", (call["agent_run_id"],)).fetchall() if call else []
            check("tool_budget", call is not None and 0 < len(run_calls) <= contract.constraints.max_tool_calls and all(item["tool_name"] in contract.allowed_tools for item in run_calls), "agent run used only allowed tools within its call budget")
            refs = agent.artifact_refs + tool.artifacts
            resolved = all(ref.type.value == "artifact" and self._db.execute("SELECT 1 FROM artifacts WHERE artifact_id=? AND research_id=?", (ref.id, row["research_id"])).fetchone() is not None for ref in refs)
            check("artifact_refs", resolved, "declared artifact references resolve")
            check("result_output", agent.status == "completed" and agent.output == tool.result, "completed result matches tool output")
            acceptance = {"tool_ok": tool.ok, "result_present": bool(agent.output)}
            for item in contract.acceptance:
                if item.required:
                    ok = item.kind == "automatic" and item.id in acceptance and acceptance[item.id]
                    check(f"acceptance:{item.id}", ok, "required automatic acceptance check passes")
            if payload.scientific is not None:
                from .scientific_verifier import verify_scientific
                verify_scientific(self, row, payload, contract, check)
                persisted_repair = self.runtime_step(row["research_id"], "verification_repair_config")
                repair_config = (persisted_repair["output"] or {}) if persisted_repair else {}
                repair_required = (repair_config.get("enabled", False) or
                                   getattr(self, "verification_repair_enabled", False))
                if repair_required:
                    from .verification_repair import REPAIR_POLICY_VERSION
                    check("F3P_POLICY_VERSION",
                          persisted_repair is not None and persisted_repair["status"] == "COMPLETED" and
                          repair_config.get("enabled") is True and repair_config.get("version") == REPAIR_POLICY_VERSION,
                          "recorded repair policy is available and compatible")
                if repair_required and request.tool_name == "analysis.skill":
                    from .agent_schemas import AnalysisPlan
                    from .verification_repair import qualify_association_from_state, ridge_arithmetic_from_state
                    saved_plan = self.runtime_step(row["research_id"], f"worker_plan:{contract.contract_id}")
                    check("F3P_FROZEN_PLAN_AVAILABLE",
                          saved_plan is not None and saved_plan["status"] == "COMPLETED",
                          "runtime frozen plan is available for required F3-P checks")
                    if saved_plan is not None and saved_plan["status"] == "COMPLETED":
                        frozen = AnalysisPlan.model_validate(saved_plan["output"])
                        if frozen.skill_plan and frozen.skill_plan.skill_id == "tabular_association_v1":
                            try:
                                qualification = qualify_association_from_state(
                                    self, row["research_id"], frozen, tool.result)
                            except (ValueError, KeyError, OSError, DatasetIntegrityError):
                                qualification = "UNRESOLVED_DISAGREEMENT"
                            check("F3P_INDEPENDENT_ASSOCIATION",
                                  qualification == "NUMERICAL_CHECKS_PASSED_FOR_SCOPE",
                                  f"independent association qualification: {qualification}")
                        if repair_config.get("ridge_arithmetic_check", False) and frozen.skill_plan and frozen.skill_plan.skill_id == "tabular_regression_evaluation_v1":
                            try:
                                arithmetic = ridge_arithmetic_from_state(
                                    self, row["research_id"], frozen, tool.result)
                                check("F3P_RIDGE_ARITHMETIC", arithmetic.status == "passed",
                                      f"experimental Ridge check: {arithmetic.status}: {arithmetic.reason}")
                            except (ValueError, KeyError, OSError, DatasetIntegrityError):
                                check("F3P_RIDGE_ARITHMETIC", False, "needs_reference_check: input unavailable")
        else:
            check("payload_checks", False, "cannot inspect invalid payload")

        errors = [item.check_id for item in checks if not item.passed]
        return VerificationResult(
            verification_id=new_id("VER"), research_id=row["research_id"],
            subject_type="staged_mutation", subject_id=row["mutation_id"],
            verdict=Verdict.FAIL if errors else Verdict.PASS,
            checks=checks, errors=errors,
        )

    def verify(self, mutation_id: str) -> VerificationResult:
        row = self._one("SELECT * FROM staged_mutations WHERE mutation_id=?", (mutation_id,))
        result = self._evaluate(row)
        if row["status"] == "PENDING":
            with self._db:
                self._db.execute("UPDATE staged_mutations SET verification_json=? WHERE mutation_id=?", (to_json(result), mutation_id))
                self.research_slice.record_verification(row, result)
        _emit("verification", mutation_id=mutation_id, verdict=result.verdict.value)
        return result

    def commit(self, mutation_id: str) -> int:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._one("SELECT * FROM staged_mutations WHERE mutation_id=?", (mutation_id,))
            if row["status"] == "COMMITTED":
                raise DuplicateCommitError(mutation_id)
            if row["status"] != "PENDING":
                raise InvalidStateTransitionError(row["status"])
            if row["verification_json"] is None or VerificationResult.model_validate_json(row["verification_json"]).verdict != Verdict.PASS:
                raise VerificationRequiredError(mutation_id)
            fresh = self._evaluate(row)
            if fresh.verdict != Verdict.PASS:
                raise StateConflictError(fresh.errors)
            self.research_slice.catalog_gate(row, fresh)
            payload = StagedResult.model_validate_json(row["payload_json"])
            artifact_id = payload.agent_result.result_id
            after_json = to_json(payload.agent_result)
            self._db.execute(
                "INSERT INTO artifacts (artifact_id,research_id,contract_id,kind,payload_json,artifact_type,created_at,status) VALUES (?,?,?,?,?,?,?,?)",
                (artifact_id, row["research_id"], row["contract_id"], "agent_result", after_json,
                 "SCIENTIFIC_RESULT" if payload.scientific else None, utc_now().isoformat(), "VERIFIED"),
            )
            if payload.scientific is not None:
                science = payload.scientific
                artifact_ids = [science.profile_artifact_id, science.stats_artifact_id, science.figure_artifact_id]
                if science.plan_artifact_id:
                    artifact_ids.append(science.plan_artifact_id)
                self._db.execute("UPDATE artifacts SET status='VERIFIED' WHERE artifact_id IN (" +
                                 ",".join("?" for _ in artifact_ids) + ")", artifact_ids)
                self._db.execute("UPDATE experiments SET status='VERIFIED' WHERE experiment_id=?", (science.experiment_id,))
                self._db.execute("UPDATE evidence SET status='VERIFIED' WHERE evidence_id=?", (science.evidence_id,))
            new_version = row["base_state_version"] + 1
            changed = self._db.execute(
                "UPDATE research_runs SET state_version=? WHERE research_id=? AND state_version=?",
                (new_version, row["research_id"], row["base_state_version"]),
            ).rowcount
            if changed != 1:
                raise StateConflictError("state version changed")
            self._db.execute(
                "INSERT INTO state_events (research_id,entity_type,entity_id,operation,before_json,after_json,state_version,created_at,mutation_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (row["research_id"], "experiment" if payload.scientific else "artifact",
                 payload.scientific.experiment_id if payload.scientific else artifact_id, "CREATE", None,
                 to_json({"result": payload.agent_result, "scientific": payload.scientific}) if payload.scientific else after_json,
                 new_version, utc_now().isoformat(), mutation_id),
            )
            self._db.execute("UPDATE staged_mutations SET status='COMMITTED' WHERE mutation_id=?", (mutation_id,))
            self.research_slice.commit_science(mutation_id, payload)
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        _emit("CANONICAL_COMMIT", mutation_id=mutation_id, state_version=new_version)
        return new_version

    def rollback(self, mutation_id: str, *, preserve_shared_profile: bool = False) -> None:
        row = self._one("SELECT status,payload_json FROM staged_mutations WHERE mutation_id=?", (mutation_id,))
        if row["status"] != "PENDING":
            raise InvalidStateTransitionError(row["status"])
        with self._db:
            self._db.execute("UPDATE staged_mutations SET status='ROLLED_BACK' WHERE mutation_id=?", (mutation_id,))
            payload = StagedResult.model_validate_json(row["payload_json"])
            if payload.scientific is not None:
                science = payload.scientific
                self._db.execute("UPDATE experiments SET status='INVALIDATED' WHERE experiment_id=?", (science.experiment_id,))
                self._db.execute("UPDATE evidence SET status='INVALIDATED' WHERE evidence_id=?", (science.evidence_id,))
                artifact_ids = [science.stats_artifact_id, science.figure_artifact_id]
                if not preserve_shared_profile:
                    artifact_ids.append(science.profile_artifact_id)
                if science.plan_artifact_id:
                    artifact_ids.append(science.plan_artifact_id)
                self._db.execute("UPDATE artifacts SET status='INVALIDATED' WHERE artifact_id IN (" +
                                 ",".join("?" for _ in artifact_ids) + ")", artifact_ids)
                for artifact_id in artifact_ids:
                    self.research_slice.invalidate(payload.agent_result.research_id,
                                                   "artifact", artifact_id, "rollback/F3-P revision")
        _emit("rollback", mutation_id=mutation_id)

    def checkpoint(self, research_id: str, reason: str, cursor: dict | None = None) -> str:
        version = self.state_version(research_id)
        checkpoint_id = new_id("CHK")
        with self._db:
            self._db.execute("INSERT INTO checkpoints(checkpoint_id,research_id,state_version,created_at,reason,cursor_json) VALUES (?,?,?,?,?,?)",
                             (checkpoint_id, research_id, version, utc_now().isoformat(), reason,
                              to_json(cursor) if cursor is not None else None))
        self.runtime_event(research_id, "CHECKPOINT_CREATED", {"checkpoint_id": checkpoint_id, "stage": (cursor or {}).get("stage")})
        return checkpoint_id

    def latest_checkpoint(self, research_id: str) -> dict:
        self.state_version(research_id)
        row = self._one("SELECT * FROM checkpoints WHERE research_id=? ORDER BY rowid DESC LIMIT 1", (research_id,))
        result = dict(row)
        result["cursor"] = from_json(row["cursor_json"]) if row["cursor_json"] else None
        return result

    def runtime_step(self, research_id: str, step_key: str) -> dict | None:
        row = self._db.execute("SELECT * FROM runtime_steps WHERE research_id=? AND step_key=?",
                               (research_id, step_key)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["output"] = from_json(row["output_json"]) if row["output_json"] else None
        return result

    def begin_runtime_step(self, research_id: str, step_key: str,
                           contract_id: str | None = None) -> dict:
        self.state_version(research_id)
        existing = self.runtime_step(research_id, step_key)
        if existing is not None and existing["status"] == "COMPLETED":
            return existing
        now = utc_now().isoformat()
        with self._db:
            self._db.execute(
                "INSERT INTO runtime_steps(research_id,step_key,status,contract_id,attempt,updated_at) VALUES (?,?, 'RUNNING',?,1,?) ON CONFLICT(research_id,step_key) DO UPDATE SET status='RUNNING',contract_id=COALESCE(excluded.contract_id,runtime_steps.contract_id),attempt=runtime_steps.attempt+1,updated_at=excluded.updated_at",
                (research_id, step_key, contract_id, now))
        return self.runtime_step(research_id, step_key)

    def finish_runtime_step(self, research_id: str, step_key: str, output: dict,
                            contract_id: str | None = None) -> None:
        existing = self.runtime_step(research_id, step_key)
        if existing is not None and existing["status"] == "COMPLETED":
            if existing["output"] != output:
                raise StateConflictError("completed runtime step changed")
            return
        with self._db:
            self._db.execute(
                "INSERT INTO runtime_steps(research_id,step_key,status,output_json,contract_id,attempt,updated_at) VALUES (?,?,'COMPLETED',?,?,1,?) ON CONFLICT(research_id,step_key) DO UPDATE SET status='COMPLETED',output_json=excluded.output_json,contract_id=COALESCE(excluded.contract_id,runtime_steps.contract_id),updated_at=excluded.updated_at",
                (research_id, step_key, to_json(output), contract_id, utc_now().isoformat()))

    def save_runtime_cursor(self, cursor) -> None:
        from .recovery import ResearchRuntimeCursor

        record = ResearchRuntimeCursor.model_validate(cursor)
        if record.state_version != self.state_version(record.research_id):
            raise StateConflictError("runtime cursor version is stale")
        with self._db:
            self._db.execute(
                "INSERT INTO research_runtime_state(research_id,cursor_json,base_state_version,last_event_seq,last_planning_seq,updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(research_id) DO UPDATE SET cursor_json=excluded.cursor_json,base_state_version=excluded.base_state_version,last_event_seq=excluded.last_event_seq,last_planning_seq=excluded.last_planning_seq,updated_at=excluded.updated_at",
                (record.research_id, to_json(record), record.state_version, record.last_event_seq,
                 record.last_planning_seq, utc_now().isoformat()))

    def load_runtime_cursor(self, research_id: str):
        from .recovery import ResearchRuntimeCursor

        row = self._db.execute("SELECT cursor_json FROM research_runtime_state WHERE research_id=?",
                               (research_id,)).fetchone()
        return ResearchRuntimeCursor.model_validate_json(row["cursor_json"]) if row else None

    def committed_contract_result(self, research_id: str, contract_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT mutation_id,payload_json FROM staged_mutations WHERE research_id=? AND contract_id=? AND status='COMMITTED' ORDER BY rowid DESC LIMIT 1",
            (research_id, contract_id)).fetchone()
        if row is None:
            return None
        payload = StagedResult.model_validate_json(row["payload_json"])
        return {"mutation_id": row["mutation_id"], "result_artifact_id": payload.agent_result.result_id,
                "result": payload.agent_result.output, "state_version": self.state_version(research_id),
                "verdict": "PASS"}

    def _planning_commit(self, research_id: str, event_type: str, entity_type: str,
                         entity_id: str, payload: dict, write) -> int:
        """계획 변경을 원자적으로 검증·저장하고 연구 버전을 올린다."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._one("SELECT state_version,run_status FROM research_runs WHERE research_id=?", (research_id,))
            if row["run_status"] != "ACTIVE" and event_type not in {"EXPERIMENT_INVALIDATED", "SOURCE_INVALIDATED", "CLAIM_INVALIDATED", "CLAIM_REVALIDATED", "VERIFIER_QUALIFICATION_CHANGED", "DATASET_INVALIDATED", "CYCLE5_REVISION", "QUALIFIED_SOURCE_REVIEWED", "QUALIFIED_QUESTION_AMENDED", "RESEARCH_DESIGN_AMENDED"}:
                raise InvalidStateTransitionError("research is stopped")
            write()
            version = row["state_version"] + 1
            self._db.execute("UPDATE research_runs SET state_version=? WHERE research_id=? AND state_version=?",
                             (version, research_id, row["state_version"]))
            self._db.execute("INSERT INTO planning_events(research_id,event_type,entity_type,entity_id,payload_json,state_version,created_at) VALUES (?,?,?,?,?,?,?)",
                             (research_id, event_type, entity_type, entity_id, to_json(payload), version,
                              utc_now().isoformat()))
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        _emit(event_type, research_id=research_id, entity_id=entity_id, state_version=version)
        return version

    def set_research_question(self, research_id: str, question: str) -> int:
        if not question.strip():
            raise ValueError("research question is required")
        def write():
            self._db.execute("UPDATE research_runs SET research_question=? WHERE research_id=?", (question, research_id))
            self._invalidate_qualified_result(research_id, "QUESTION_INTERPRETATION_CHANGED")
        return self._planning_commit(research_id, "QUESTION_REFINED", "research", research_id,
                                     {"question": question}, write)

    def _invalidate_qualified_result(self, research_id, reason):
        from .qualified_profiles import current_record
        record = current_record(self, research_id)
        if not record:
            return
        saved = self._one("SELECT payload_json FROM staged_mutations WHERE research_id=? AND mutation_id=?", (research_id, record["mutation_id"]))
        science = StagedResult.model_validate_json(saved[0]).scientific
        self.research_slice.invalidate(research_id, "artifact", science.stats_artifact_id, reason, within_transaction=True)
        self._db.execute("UPDATE experiments SET status='INVALIDATED' WHERE research_id=? AND experiment_id=?", (research_id, science.experiment_id))
        self._db.execute("UPDATE evidence SET status='INVALIDATED' WHERE research_id=? AND experiment_id=?", (research_id, science.experiment_id))
        self._db.execute("UPDATE artifacts SET status='INVALIDATED' WHERE research_id=? AND artifact_id IN (?,?)",
                         (research_id, science.stats_artifact_id, science.figure_artifact_id))
        self._db.execute("UPDATE artifacts SET status='INVALIDATED' WHERE research_id=? AND artifact_id=?",
                         (research_id, StagedResult.model_validate_json(saved[0]).agent_result.result_id))

    def record_qualified_revision(self, research_id, event_type, step_key, document, *,
                                  expected_version, question=None, affected=False, qualified_authority=None):
        """소유자 절차에서 호출하며 자료·질문 수정과 의존 무효화를 한 트랜잭션에 저장한다."""
        if event_type not in {"QUALIFIED_SOURCE_REVIEWED", "QUALIFIED_QUESTION_AMENDED", "RESEARCH_DESIGN_AMENDED"}:
            raise ContractViolationError("qualified revision authority is required")
        if question is not None and event_type not in {"QUALIFIED_QUESTION_AMENDED", "RESEARCH_DESIGN_AMENDED"}:
            raise ContractViolationError("question amendment authority is required")
        if qualified_authority and event_type != "RESEARCH_DESIGN_AMENDED":
            raise ContractViolationError("design amendment authority is required")
        encoded = to_json(document)
        encoded.encode("utf-8", errors="strict")
        def write():
            if self.state_version(research_id) != expected_version:
                raise StateConflictError("qualified revision is stale")
            if self.runtime_step(research_id, step_key):
                raise StateConflictError("qualified revision already exists")
            self._db.execute("INSERT INTO runtime_steps(research_id,step_key,status,output_json,attempt,updated_at) VALUES (?,?,'COMPLETED',?,1,?)",
                             (research_id, step_key, encoded, utc_now().isoformat()))
            if question is not None:
                self._db.execute("UPDATE research_runs SET research_question=? WHERE research_id=?", (question, research_id))
            if qualified_authority:
                key = "qualified_authority:" + str(qualified_authority["plan"]["question_revision"])
                self._db.execute("INSERT INTO runtime_steps(research_id,step_key,status,output_json,attempt,updated_at) VALUES (?,?,'COMPLETED',?,1,?)",
                                 (research_id, key, to_json(qualified_authority), utc_now().isoformat()))
            if affected:
                self._invalidate_qualified_result(research_id, event_type)
                if event_type == "RESEARCH_DESIGN_AMENDED":
                    for row in self._db.execute("SELECT experiment_id FROM experiments WHERE research_id=? AND status='VERIFIED'", (research_id,)).fetchall():
                        self.research_slice.invalidate(research_id, "experiment", row[0], event_type, within_transaction=True)
                    self._db.execute("UPDATE experiments SET status='INVALIDATED' WHERE research_id=? AND status='VERIFIED'", (research_id,))
                    self._db.execute("UPDATE evidence SET status='INVALIDATED' WHERE research_id=? AND experiment_id IS NOT NULL AND status='VERIFIED'", (research_id,))
        return self._planning_commit(research_id, event_type, "research", research_id,
                                     {"step_key": step_key, "affected": affected, "question": question}, write)

    def search_cache_get(self, research_id: str, cache_key: str) -> dict | None:
        row = self._db.execute("SELECT result_json FROM scholarly_search_cache WHERE research_id=? AND cache_key=?",
                               (research_id, cache_key)).fetchone()
        return from_json(row["result_json"]) if row else None

    def search_cache_put(self, research_id: str, cache_key: str, provider: str, request, result) -> None:
        self._one("SELECT research_id FROM research_runs WHERE research_id=?", (research_id,))
        with self._db:
            self._db.execute("INSERT OR IGNORE INTO scholarly_search_cache VALUES (?,?,?,?,?,?)",
                             (research_id, cache_key, provider, to_json(request), to_json(result), utc_now().isoformat()))

    def upsert_source(self, research_id: str, candidate) -> tuple[str, bool]:
        from .scholarly import NormalizedSource, metadata_digest, source_from_row

        source = NormalizedSource.model_validate(candidate.model_dump() if isinstance(candidate, NormalizedSource) else candidate)
        if source.source_id or (source.research_id and source.research_id != research_id):
            raise ContractViolationError("provider cannot choose canonical source identity")
        source.metadata_hash = metadata_digest(source)
        key = re.sub(r"\W+", " ", source.title.casefold()).strip()
        existing = None
        if source.doi:
            existing = self._db.execute("SELECT * FROM sources WHERE research_id=? AND doi=?",
                                        (research_id, source.doi)).fetchone()
        if existing is None and source.openalex_id:
            existing = self._db.execute("SELECT * FROM sources WHERE research_id=? AND openalex_id=?",
                                        (research_id, source.openalex_id)).fetchone()
            if existing and source.doi and existing["doi"] and existing["doi"] != source.doi:
                raise StateConflictError("OpenAlex identity maps to conflicting DOIs")
        collision_warning = None
        if existing is None:
            for row in self._db.execute("SELECT * FROM sources WHERE research_id=? AND publication_year IS ?",
                                        (research_id, source.publication_year)):
                if (re.sub(r"\W+", " ", row["title"].casefold()).strip() == key
                        and (not row["doi"] or not source.doi) and (not row["openalex_id"] or not source.openalex_id)):
                    existing = row
                    collision_warning = "TITLE_YEAR_FALLBACK_COLLISION_POSSIBLE"
                    break
        if existing:
            identity = existing["source_id"]
            if (source.doi and existing["doi"] == source.doi and
                    (re.sub(r"\W+", " ", existing["title"].casefold()).strip() != key or
                     (source.publication_year and existing["publication_year"] and
                      source.publication_year != existing["publication_year"]))):
                collision_warning = "DOI_METADATA_CONFLICT"
            if existing["status"] not in {"DISCOVERED", "RELEVANT", "IRRELEVANT"}:
                return identity, False
            provider_ids = {**from_json(existing["provider_ids_json"]), **source.provider_ids}
            merged = source_from_row(existing).model_copy(update={
                "doi": existing["doi"] or source.doi,
                "openalex_id": existing["openalex_id"] or source.openalex_id,
                "authors": from_json(existing["authors_json"]) or source.authors,
                "source_name": existing["source_name"] or source.source_name,
                "abstract": existing["abstract"] or source.abstract,
                "url": existing["url"] or source.url,
                "is_open_access": bool(existing["is_open_access"]) if existing["is_open_access"] is not None else source.is_open_access,
                "cited_by_count": existing["cited_by_count"] if existing["cited_by_count"] is not None else source.cited_by_count,
                "referenced_works": from_json(existing["referenced_works_json"]) or source.referenced_works,
                "related_works": from_json(existing["related_works_json"]) or source.related_works,
                "provider_ids": provider_ids})
            if (provider_ids == from_json(existing["provider_ids_json"])
                    and (not source.doi or existing["doi"] == source.doi)
                    and (not source.openalex_id or existing["openalex_id"] == source.openalex_id)
                    and metadata_digest(merged) == existing["metadata_hash"]
                    and (not collision_warning or existing["collision_warning"])):
                return identity, False
            def update():
                self.research_slice.invalidate(research_id, "source", identity, "source snapshot revision", within_transaction=True)
                self._db.execute("UPDATE sources SET doi=?,openalex_id=?,authors_json=?,source_name=?,abstract=?,url=?,is_open_access=?,cited_by_count=?,referenced_works_json=?,related_works_json=?,provider_ids_json=?,metadata_hash=?,collision_warning=COALESCE(collision_warning,?) WHERE source_id=?",
                                 (merged.doi, merged.openalex_id, to_json(merged.authors), merged.source_name,
                                  merged.abstract, merged.url, merged.is_open_access, merged.cited_by_count,
                                  to_json(merged.referenced_works), to_json(merged.related_works),
                                  to_json(provider_ids), metadata_digest(merged), collision_warning, identity))
            self._planning_commit(research_id, "SOURCE_DEDUPLICATED", "source", identity,
                                  {"provider": source.provider, "collision_warning": collision_warning}, update)
            return identity, False
        identity = new_id("SRC")
        def insert():
            self._db.execute("INSERT INTO sources(source_id,research_id,source_type,title,authors_json,publication_year,doi,openalex_id,source_name,abstract,url,is_open_access,cited_by_count,referenced_works_json,related_works_json,provider,provider_ids_json,retrieved_at,metadata_hash,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (identity, research_id, "LITERATURE", source.title, to_json(source.authors),
                              source.publication_year, source.doi, source.openalex_id, source.source_name,
                              source.abstract, source.url, source.is_open_access, source.cited_by_count,
                              to_json(source.referenced_works), to_json(source.related_works),
                              source.provider, to_json(source.provider_ids), source.retrieved_at,
                              source.metadata_hash, "DISCOVERED"))
        self._planning_commit(research_id, "SOURCE_DISCOVERED", "source", identity,
                              {"title": source.title, "metadata_hash": source.metadata_hash}, insert)
        return identity, True

    def set_source_relevance(self, research_id: str, result) -> None:
        from .literature import RelevanceResult

        relevance = RelevanceResult.model_validate(
            result.model_dump() if isinstance(result, RelevanceResult) else result)
        row = self._one("SELECT status,relevance_json FROM sources WHERE research_id=? AND source_id=?",
                        (research_id, relevance.source_id))
        target = "IRRELEVANT" if relevance.relevance == "IRRELEVANT" else "RELEVANT"
        if row["status"] == target and row["relevance_json"] == to_json(relevance):
            return
        if row["status"] != "DISCOVERED":
            raise InvalidStateTransitionError("source relevance is already decided")
        for hypothesis_id in relevance.target_hypotheses:
            self._one("SELECT hypothesis_id FROM hypotheses WHERE research_id=? AND hypothesis_id=?",
                      (research_id, hypothesis_id))
        self._planning_commit(research_id, "SOURCE_SCREENED", "source", relevance.source_id,
                              relevance.model_dump(mode="json"),
                              lambda: self._db.execute("UPDATE sources SET status=?,relevance_json=? WHERE source_id=?",
                                                       (target, to_json(relevance), relevance.source_id)))

    def verify_literature_evidence(self, research_id: str, proposed, reviewer=None) -> str:
        from .literature import ExtractedEvidence, verify_alignment

        item = ExtractedEvidence.model_validate(
            proposed.model_dump() if isinstance(proposed, ExtractedEvidence) else proposed)
        source = self._one("SELECT * FROM sources WHERE research_id=? AND source_id=?",
                           (research_id, item.source_id))
        existing = self._db.execute("SELECT evidence_id,payload_json FROM evidence WHERE research_id=? AND source_id=? AND claim=? AND status='VERIFIED'",
                                    (research_id, item.source_id, item.claim)).fetchone()
        if existing:
            if from_json(existing["payload_json"]) != item.model_dump(mode="json"):
                raise ContractViolationError("verified literature evidence cannot be replaced")
            return existing["evidence_id"]
        verdict = verify_alignment(source, item, reviewer)
        if not verdict["passed"]:
            raise ContractViolationError("literature evidence alignment failed: " + verdict["reason"])
        if item.target_hypothesis_id:
            self._one("SELECT hypothesis_id FROM hypotheses WHERE research_id=? AND hypothesis_id=?",
                      (research_id, item.target_hypothesis_id))
        identity = new_id("E")
        text_hash = hashlib.sha256(item.evidence_text.encode("utf-8", errors="strict")).hexdigest()
        def insert():
            self._db.execute("INSERT INTO evidence(evidence_id,research_id,source_id,experiment_id,payload_json,claim,polarity,source_type,source_ref,status,provenance_json,confidence,source_metadata_hash,text_field,text_hash,evidence_text,evidence_location,limitations_json,target_hypothesis_id,verification_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (identity, research_id, item.source_id, None, to_json(item), item.claim,
                              item.polarity, "LITERATURE", item.source_id, "VERIFIED",
                              to_json({"source_id": item.source_id, "source_metadata_hash": source["metadata_hash"],
                                       "text_field": item.text_field, "text_hash": text_hash}), item.confidence,
                              source["metadata_hash"], item.text_field, text_hash, item.evidence_text,
                              item.evidence_location, to_json(item.limitations), item.target_hypothesis_id,
                              to_json(verdict)))
            self._db.execute("UPDATE sources SET status='VERIFIED' WHERE source_id=?", (item.source_id,))
            self.research_slice.literature_claim(research_id, identity)
        self._planning_commit(research_id, "LITERATURE_EVIDENCE_VERIFIED", "evidence", identity,
                              {"source_id": item.source_id, "polarity": item.polarity, "text_hash": text_hash}, insert)
        return identity

    def mark_source_extracted(self, research_id: str, source_id: str) -> None:
        row = self._one("SELECT status FROM sources WHERE research_id=? AND source_id=?",
                        (research_id, source_id))
        if row["status"] in {"EVIDENCE_EXTRACTED", "VERIFIED"}:
            return
        if row["status"] != "RELEVANT":
            raise InvalidStateTransitionError("only relevant sources can yield evidence")
        self._planning_commit(research_id, "SOURCE_EVIDENCE_EXTRACTED", "source", source_id, {},
                              lambda: self._db.execute("UPDATE sources SET status='EVIDENCE_EXTRACTED' WHERE source_id=?",
                                                       (source_id,)))

    def invalidate_source(self, research_id: str, source_id: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("invalidation reason is required")
        row = self._one("SELECT status FROM sources WHERE research_id=? AND source_id=?",
                        (research_id, source_id))
        if row["status"] == "INVALIDATED":
            return
        def write():
            self.research_slice.invalidate(research_id, "source", source_id, reason, within_transaction=True)
            self._db.execute("UPDATE sources SET status='INVALIDATED' WHERE source_id=?", (source_id,))
            self._db.execute("UPDATE evidence SET status='INVALIDATED' WHERE research_id=? AND source_id=?",
                             (research_id, source_id))
            self._db.execute("UPDATE research_runs SET run_status='ACTIVE',stop_reason=NULL,conclusion_json=NULL WHERE research_id=? AND run_status!='ACTIVE'",
                             (research_id,))
        self._planning_commit(research_id, "SOURCE_INVALIDATED", "source", source_id,
                              {"reason": reason}, write)
        from .literature import synthesize_literature
        self.save_literature_synthesis(research_id, synthesize_literature(self, research_id))

    def save_literature_synthesis(self, research_id: str, synthesis) -> None:
        from .literature import LiteratureSynthesis

        result = LiteratureSynthesis.model_validate(
            synthesis.model_dump() if isinstance(synthesis, LiteratureSynthesis) else synthesis)
        if result.research_id != research_id:
            raise ContractViolationError("synthesis research identity mismatch")
        for polarity, references in (("SUPPORT", result.support_evidence_refs),
                                     ("CONTRADICT", result.contradiction_evidence_refs),
                                     ("NEUTRAL", result.neutral_evidence_refs)):
            for identity in references:
                row = self._db.execute("SELECT e.polarity,e.status,s.status AS source_status FROM evidence e JOIN sources s ON s.source_id=e.source_id WHERE e.research_id=? AND e.evidence_id=? AND e.source_type='LITERATURE'",
                                       (research_id, identity)).fetchone()
                if row is None or row["polarity"] != polarity or row["status"] != "VERIFIED" or row["source_status"] != "VERIFIED":
                    raise ContractViolationError("synthesis contains unresolved literature evidence")
            expected = {row[0] for row in self._db.execute(
                "SELECT e.evidence_id FROM evidence e JOIN sources s ON s.source_id=e.source_id "
                "WHERE e.research_id=? AND e.source_type='LITERATURE' AND e.status='VERIFIED' "
                "AND s.status='VERIFIED' AND e.polarity=?", (research_id, polarity))}
            if set(references) != expected or len(references) != len(expected):
                raise ContractViolationError("synthesis omits or duplicates literature evidence")
        for identity in result.active_hypothesis_ids:
            self._one("SELECT hypothesis_id FROM hypotheses WHERE research_id=? AND hypothesis_id=? AND status='ACTIVE'",
                      (research_id, identity))
        previous = self._db.execute("SELECT synthesis_json FROM literature_syntheses WHERE research_id=?",
                                    (research_id,)).fetchone()
        if previous and previous["synthesis_json"] == to_json(result):
            return
        def write():
            self._db.execute("INSERT INTO literature_syntheses(research_id,synthesis_json,created_at) VALUES (?,?,?) ON CONFLICT(research_id) DO UPDATE SET synthesis_json=excluded.synthesis_json,created_at=excluded.created_at",
                             (research_id, to_json(result), utc_now().isoformat()))
        self._planning_commit(research_id, "LITERATURE_SYNTHESIZED", "research", research_id,
                              result.model_dump(mode="json"), write)

    def record_research_decision(self, research_id: str, rationale: str, *,
                                 visibility: str = "all") -> str:
        if not rationale.strip():
            raise ValueError("decision rationale is required")
        decision_id = new_id("DEC")
        def write():
            self._db.execute("INSERT INTO decisions(decision_id,research_id,payload_json,status,created_at,state_version,visibility) VALUES (?,?,?,?,?,?,?)",
                             (decision_id, research_id, to_json({"rationale": rationale}), "ACTIVE",
                              utc_now().isoformat(), self.state_version(research_id) + 1, visibility))
        self._planning_commit(research_id, "DECISION_RECORDED", "decision", decision_id,
                              {"rationale": rationale}, write)
        return decision_id

    def supersede_decision(self, research_id: str, decision_id: str, replacement_id: str) -> int:
        def write():
            old = self._one("SELECT status FROM decisions WHERE decision_id=? AND research_id=?",
                            (decision_id, research_id))
            replacement = self._one("SELECT status FROM decisions WHERE decision_id=? AND research_id=?",
                                    (replacement_id, research_id))
            if old["status"] != "ACTIVE" or replacement["status"] != "ACTIVE" or decision_id == replacement_id:
                raise InvalidStateTransitionError("decision cannot be superseded")
            self._db.execute("UPDATE decisions SET status='SUPERSEDED' WHERE decision_id=?", (decision_id,))
        return self._planning_commit(research_id, "DECISION_SUPERSEDED", "decision", decision_id,
                                     {"replacement_id": replacement_id}, write)

    def create_hypothesis(self, research_id: str, proposal, *, created_by: str,
                          hard_cap: int = 5, visibility: str = "all") -> str:
        from .research_schemas import HypothesisProposal
        proposal = HypothesisProposal.model_validate(proposal)
        if hard_cap > 5 or hard_cap < 1:
            raise ValueError("hypothesis hard cap must be 1..5")
        hypothesis_id = new_id("H")
        score = {**proposal.score.model_dump(), "aggregate": proposal.score.aggregate()}
        def write():
            count = self._db.execute("SELECT COUNT(*) FROM hypotheses WHERE research_id=?", (research_id,)).fetchone()[0]
            if count >= hard_cap:
                raise ContractViolationError("hypothesis hard cap exceeded")
            if proposal.parent_hypothesis_id:
                parent = self._one("SELECT research_id FROM hypotheses WHERE hypothesis_id=?", (proposal.parent_hypothesis_id,))
                if parent["research_id"] != research_id:
                    raise ContractViolationError("parent hypothesis belongs to another research")
            duplicate = self._db.execute("SELECT 1 FROM hypotheses WHERE research_id=? AND lower(statement)=lower(?)",
                                         (research_id, proposal.statement)).fetchone()
            if duplicate:
                raise ContractViolationError("duplicate hypothesis")
            now = utc_now().isoformat()
            self._db.execute("INSERT INTO hypotheses VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (hypothesis_id, research_id, proposal.parent_hypothesis_id,
                              proposal.statement, proposal.rationale, "PROPOSED", to_json(score),
                              created_by, visibility, now, now, 1))
        self._planning_commit(research_id, "HYPOTHESIS_CREATED", "hypothesis", hypothesis_id,
                              {"statement": proposal.statement, "score": score}, write)
        return hypothesis_id

    def shortlist_hypotheses(self, research_id: str, proposals, *, created_by: str,
                             max_shortlist: int = 3, hard_cap: int = 5) -> list[str]:
        from .research_schemas import HypothesisShortlist
        shortlist = HypothesisShortlist.model_validate(proposals)
        if not 1 <= max_shortlist <= hard_cap <= 5 or len(shortlist.hypotheses) > max_shortlist:
            raise ContractViolationError("shortlist cap exceeded")
        ids = [new_id("H") for _ in shortlist.hypotheses]
        def write():
            existing = self._db.execute("SELECT COUNT(*) FROM hypotheses WHERE research_id=?",
                                        (research_id,)).fetchone()[0]
            if existing + len(ids) > hard_cap:
                raise ContractViolationError("hypothesis hard cap exceeded")
            statements = [item.statement.strip().lower() for item in shortlist.hypotheses]
            if len(statements) != len(set(statements)):
                raise ContractViolationError("duplicate hypothesis")
            for hypothesis_id, proposal in zip(ids, shortlist.hypotheses):
                if proposal.parent_hypothesis_id:
                    parent = self._one("SELECT research_id FROM hypotheses WHERE hypothesis_id=?",
                                       (proposal.parent_hypothesis_id,))
                    if parent["research_id"] != research_id:
                        raise ContractViolationError("parent hypothesis belongs to another research")
                if self._db.execute("SELECT 1 FROM hypotheses WHERE research_id=? AND lower(statement)=?",
                                    (research_id, proposal.statement.strip().lower())).fetchone():
                    raise ContractViolationError("duplicate hypothesis")
                score = {**proposal.score.model_dump(), "aggregate": proposal.score.aggregate()}
                now = utc_now().isoformat()
                self._db.execute("INSERT INTO hypotheses VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (hypothesis_id, research_id, proposal.parent_hypothesis_id,
                                  proposal.statement, proposal.rationale, "SHORTLISTED", to_json(score),
                                  created_by, "all", now, now, 1))
        self._planning_commit(research_id, "HYPOTHESIS_SHORTLIST", "research", research_id,
                              {"hypothesis_ids": ids, "rationale": shortlist.rationale}, write)
        return ids

    def set_hypothesis_status(self, research_id: str, hypothesis_id: str, status: str, *,
                              decided_by: str, rationale: str,
                              evidence_refs: list | None = None, max_active: int = 2) -> int:
        from .schemas import ContextRef, RefType
        transitions = {"PROPOSED": {"SHORTLISTED", "INVALIDATED"},
                       "SHORTLISTED": {"ACTIVE", "INVALIDATED"},
                       "ACTIVE": {"SUPPORTED", "WEAKENED", "REJECTED", "INCONCLUSIVE", "INVALIDATED"},
                       "SUPPORTED": {"INCONCLUSIVE", "INVALIDATED"},
                       "WEAKENED": {"ACTIVE", "INCONCLUSIVE", "INVALIDATED"},
                       "INCONCLUSIVE": {"ACTIVE", "INVALIDATED"},
                       "REJECTED": {"INVALIDATED"}}
        evidence_refs = [ContextRef.model_validate(ref) for ref in (evidence_refs or [])]
        if not rationale.strip() or decided_by not in {"manager", "experiment_coordinator", "system"}:
            raise ContractViolationError("status change requires an authorized reason")
        def write():
            row = self._one("SELECT * FROM hypotheses WHERE hypothesis_id=? AND research_id=?",
                            (hypothesis_id, research_id))
            if status not in transitions.get(row["status"], set()):
                raise InvalidStateTransitionError(f"{row['status']} -> {status}")
            if status == "ACTIVE":
                count = self._db.execute("SELECT COUNT(*) FROM hypotheses WHERE research_id=? AND status='ACTIVE'",
                                         (research_id,)).fetchone()[0]
                if count >= max_active:
                    raise ContractViolationError("active hypothesis cap exceeded")
            if status in {"SUPPORTED", "WEAKENED", "REJECTED", "INCONCLUSIVE"}:
                if not evidence_refs or any(ref.type != RefType.evidence or not self.resolve_ref(research_id, ref)
                                            for ref in evidence_refs):
                    raise ContractViolationError("status change requires verified evidence")
                verified = [self._one("SELECT status FROM evidence WHERE evidence_id=? AND research_id=?",
                                      (ref.id, research_id))["status"] == "VERIFIED" for ref in evidence_refs]
                if not all(verified) or (status in {"SUPPORTED", "REJECTED"} and len({r.id for r in evidence_refs}) < 2):
                    raise ContractViolationError("insufficient verified evidence for conclusion")
                if any(self._db.execute("SELECT 1 FROM entity_edges WHERE research_id=? AND from_type='hypothesis' AND from_id=? AND edge_type='supports' AND to_type='evidence' AND to_id=? AND status='ACTIVE'",
                                        (research_id, hypothesis_id, ref.id)).fetchone() is None
                       for ref in evidence_refs):
                    raise ContractViolationError("evidence is not linked to hypothesis")
            self._db.execute("UPDATE hypotheses SET status=?,updated_at=?,version=version+1 WHERE hypothesis_id=?",
                             (status, utc_now().isoformat(), hypothesis_id))
        return self._planning_commit(research_id, "HYPOTHESIS_STATUS", "hypothesis", hypothesis_id,
                                     {"status": status, "decided_by": decided_by,
                                      "rationale": rationale, "evidence_refs": evidence_refs}, write)

    def add_entity_edge(self, research_id: str, from_ref, edge_type: str, to_ref) -> str:
        from .schemas import ContextRef
        source, target = ContextRef.model_validate(from_ref), ContextRef.model_validate(to_ref)
        if edge_type not in {"depends_on", "supports", "contradicts", "generated_from", "uses_dataset",
                             "produced_by", "supersedes", "invalidates", "tests"}:
            raise ValueError("invalid edge type")
        for ref in (source, target):
            if not self._entity_exists(research_id, ref):
                raise EntityNotFoundError(ref.id)
        existing = self._db.execute(
            "SELECT edge_id FROM entity_edges WHERE research_id=? AND from_type=? AND from_id=? AND edge_type=? AND to_type=? AND to_id=?",
            (research_id, source.type.value, source.id, edge_type, target.type.value, target.id)).fetchone()
        if existing is not None:
            return existing["edge_id"]
        edge_id = new_id("EDGE")
        with self._db:
            self._db.execute("INSERT INTO entity_edges VALUES (?,?,?,?,?,?,?,?,?)",
                             (edge_id, research_id, source.type.value, source.id, edge_type,
                              target.type.value, target.id, utc_now().isoformat(), "ACTIVE"))
        return edge_id

    def _entity_exists(self, research_id: str, ref) -> bool:
        names = {"hypothesis": ("hypotheses", "hypothesis_id"), "evidence": ("evidence", "evidence_id"),
                 "experiment": ("experiments", "experiment_id"), "dataset": ("datasets", "dataset_id"),
                 "artifact": ("artifacts", "artifact_id"), "decision": ("decisions", "decision_id"),
                 "contract": ("contracts", "contract_id"), "task": ("tasks", "task_id"),
                 "summary": ("milestone_summaries", "summary_id"), "source": ("sources", "source_id")}
        entry = names.get(ref.type.value)
        if entry is None:
            return False
        table, key = entry
        return self._db.execute(f"SELECT 1 FROM {table} WHERE {key}=? AND research_id=?",
                                (ref.id, research_id)).fetchone() is not None

    def record_context_metrics(self, bundle) -> None:
        metrics = bundle.metrics
        if metrics is None:
            raise ValueError("context metrics are required")
        with self._db:
            self._db.execute("INSERT INTO context_metrics VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (bundle.bundle_id, bundle.research_id, bundle.target_role,
                              metrics.estimated_tokens, metrics.mandatory_count, metrics.dependency_count,
                              metrics.semantic_count, metrics.recent_event_count,
                              metrics.invalidated_warning_count, bundle.state_version,
                              metrics.estimator, utc_now().isoformat()))

    def record_milestone_summary(self, research_id: str, kind: str, summary_text: str,
                                 covered_refs: list, *, generated_by: str) -> str:
        from .schemas import ContextRef
        refs = [ContextRef.model_validate(ref) for ref in covered_refs]
        if not summary_text.strip() or not refs:
            raise ValueError("summary and source references are required")
        if any(not self._entity_exists(research_id, ref) for ref in refs):
            raise EntityNotFoundError("summary source is missing")
        summary_id = new_id("SUM")
        with self._db:
            self._db.execute("INSERT INTO milestone_summaries VALUES (?,?,?,?,?,?,?,?,?)",
                             (summary_id, research_id, kind, summary_text, to_json(refs),
                              self.state_version(research_id), generated_by,
                              utc_now().isoformat(), "ACTIVE"))
        return summary_id

    def record_action(self, research_id: str, action_type: str, objective: str, *,
                      input_refs: list | None = None, method: str | None = None,
                      details: dict | None = None, max_actions: int = 30,
                      logical_key: str | None = None, output_refs: list | None = None,
                      contract_id: str | None = None, task_id: str | None = None) -> str:
        from .research_schemas import ResearchAction
        action = ResearchAction(action_type)
        normalized = re.sub(r"\s+", " ", objective.strip().lower())
        refs = sorted((ref.type.value, ref.id) for ref in (input_refs or []))
        fingerprint = hashlib.sha256(to_json([action.value, normalized, refs, method]).encode("utf-8", errors="strict")).hexdigest()
        if logical_key is not None:
            existing = self._db.execute("SELECT action_id FROM research_actions WHERE research_id=? AND logical_key=?",
                                        (research_id, logical_key)).fetchone()
            if existing is not None:
                return existing["action_id"]
        count = self._db.execute("SELECT COUNT(*) FROM research_actions WHERE research_id=?", (research_id,)).fetchone()[0]
        if count >= max_actions:
            self.runtime_event(research_id, "ACTION_LIMIT_REACHED")
            raise ActionLimitError(action.value)
        action_id = new_id("ACT")
        now = utc_now().isoformat()
        with self._db:
            self._db.execute("INSERT INTO research_actions(action_id,research_id,action_type,fingerprint,details_json,created_at,action_index,logical_key,status,task_id,contract_id,input_refs_json,output_refs_json,attempt,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (action_id, research_id, action.value, fingerprint,
                              to_json(details or {}), now, count + 1, logical_key, "COMPLETED",
                              task_id, contract_id, to_json(input_refs or []), to_json(output_refs or []),
                              1, now, now))
        repeats = self._db.execute("SELECT COUNT(*) FROM research_actions WHERE research_id=? AND fingerprint=?",
                                   (research_id, fingerprint)).fetchone()[0]
        if repeats >= 3:
            self.runtime_event(research_id, "LOOP_DETECTED", {"fingerprint": fingerprint})
            raise LoopDetectedError(fingerprint)
        return action_id

    def record_critic_review(self, research_id: str, experiment_id: str,
                             contract_id: str, review) -> str:
        from .research_schemas import CriticResult
        output = CriticResult.model_validate(review)
        existing = self._db.execute("SELECT review_id,output_json FROM critic_reviews WHERE research_id=? AND experiment_id=?",
                                    (research_id, experiment_id)).fetchone()
        if existing is not None:
            if from_json(existing["output_json"]) != output.model_dump(mode="json"):
                raise StateConflictError("critic review already exists with different output")
            return existing["review_id"]
        experiment = self._one("SELECT status FROM experiments WHERE experiment_id=? AND research_id=?",
                               (experiment_id, research_id))
        contract, _ = self.contract(contract_id)
        if experiment["status"] != "VERIFIED" or contract.research_id != research_id or contract.assigned_role != "verification_coordinator":
            raise ContractViolationError("critic review must concern a verified experiment")
        review_id = new_id("CRIT")
        with self._db:
            self._db.execute("INSERT INTO critic_reviews VALUES (?,?,?,?,?,?)",
                             (review_id, research_id, experiment_id, contract_id,
                              to_json(output), utc_now().isoformat()))
        return review_id

    def invalidate_experiment(self, research_id: str, experiment_id: str, reason: str) -> int:
        if not reason.strip():
            raise ValueError("invalidation reason required")
        def write():
            experiment = self._one("SELECT status,result_artifact_id,task_id FROM experiments WHERE experiment_id=? AND research_id=?",
                                   (experiment_id, research_id))
            if experiment["status"] != "VERIFIED":
                raise InvalidStateTransitionError(experiment["status"])
            self.research_slice.invalidate(research_id, "artifact", experiment["result_artifact_id"], reason, within_transaction=True)
            evidence_rows = self._db.execute("SELECT evidence_id FROM evidence WHERE experiment_id=? AND research_id=?",
                                             (experiment_id, research_id)).fetchall()
            self._db.execute("UPDATE experiments SET status='INVALIDATED' WHERE experiment_id=?", (experiment_id,))
            self._db.execute("UPDATE evidence SET status='INVALIDATED',quality_flags_json=? WHERE experiment_id=?",
                             (to_json([reason]), experiment_id))
            self._db.execute("UPDATE artifacts SET status='INVALIDATED' WHERE artifact_id=?",
                             (experiment["result_artifact_id"],))
            mutation = self._db.execute(
                "SELECT payload_json FROM staged_mutations WHERE research_id=? AND status='COMMITTED' AND json_extract(payload_json,'$.scientific.experiment_id')=?",
                (research_id, experiment_id)).fetchone()
            if mutation is not None:
                science = StagedResult.model_validate_json(mutation["payload_json"]).scientific
                if science is not None:
                    artifact_ids = [science.stats_artifact_id, science.figure_artifact_id]
                    if science.plan_artifact_id:
                        artifact_ids.append(science.plan_artifact_id)
                    self._db.execute("UPDATE artifacts SET status='INVALIDATED' WHERE artifact_id IN (" +
                                     ",".join("?" for _ in artifact_ids) + ")", artifact_ids)
            if experiment["task_id"]:
                self._db.execute("UPDATE artifacts SET status='INVALIDATED' WHERE kind='agent_result' AND contract_id IN (SELECT contract_id FROM contracts WHERE task_id=?)",
                                 (experiment["task_id"],))
            for item in evidence_rows:
                linked = self._db.execute("SELECT from_id FROM entity_edges WHERE research_id=? AND edge_type='supports' AND to_type='evidence' AND to_id=? AND from_type='hypothesis'",
                                          (research_id, item["evidence_id"])).fetchall()
                for hypothesis in linked:
                    self._db.execute("UPDATE hypotheses SET status='INCONCLUSIVE',version=version+1,updated_at=? WHERE hypothesis_id=? AND status='SUPPORTED'",
                                     (utc_now().isoformat(), hypothesis["from_id"]))
            run = self._one("SELECT conclusion_json FROM research_runs WHERE research_id=?", (research_id,))
            if run["conclusion_json"]:
                used = {ref["id"] for ref in from_json(run["conclusion_json"]).get("evidence_refs", [])}
                if used & {item["evidence_id"] for item in evidence_rows}:
                    self._db.execute("UPDATE research_runs SET run_status='ACTIVE',stop_reason=NULL,conclusion_json=NULL WHERE research_id=?",
                                     (research_id,))
        return self._planning_commit(research_id, "EXPERIMENT_INVALIDATED", "experiment", experiment_id,
                                     {"reason": reason}, write)

    def stop_research(self, research_id: str, reason, conclusion=None) -> int:
        from .research_schemas import ConclusionCandidate, StopReason
        from .schemas import RefType
        reason = StopReason(reason)
        candidate = ConclusionCandidate.model_validate(conclusion) if conclusion is not None else None
        def write():
            row = self._one("SELECT run_status FROM research_runs WHERE research_id=?", (research_id,))
            if row["run_status"] != "ACTIVE":
                raise InvalidStateTransitionError("research already stopped")
            if reason == StopReason.QUALIFIED_PROCEDURE_COMPLETED:
                from .qualified_profiles import conclusion_card
                card = conclusion_card(self, research_id)
                if not card["available"] or not card["current"]:
                    raise ContractViolationError("qualified procedure requires a current verified claim")
            if reason == StopReason.GOAL_ANSWERED:
                if candidate is None or not candidate.evidence_refs or candidate.support_level == "NONE":
                    raise ContractViolationError("answered goal requires supported conclusion")
                if re.search(r"\d", candidate.statement):
                    raise ContractViolationError("conclusion contains an unproven numeric claim")
                for ref in candidate.evidence_refs:
                    if ref.type != RefType.evidence or not self.resolve_ref(research_id, ref):
                        raise ContractViolationError("conclusion uses stale evidence")
                    status = self._one("SELECT status FROM evidence WHERE evidence_id=?", (ref.id,))["status"]
                    if status != "VERIFIED":
                        raise ContractViolationError("conclusion evidence is unverified")
                evidence_ids = [ref.id for ref in candidate.evidence_refs]
                supported = self._db.execute("SELECT hypothesis_id FROM hypotheses WHERE research_id=? AND status='SUPPORTED'",
                                             (research_id,)).fetchall()
                if not any(all(self._db.execute(
                        "SELECT 1 FROM entity_edges WHERE research_id=? AND from_type='hypothesis' AND from_id=? AND edge_type='supports' AND to_type='evidence' AND to_id=? AND status='ACTIVE'",
                        (research_id, row["hypothesis_id"], evidence_id)).fetchone() is not None
                        for evidence_id in evidence_ids) for row in supported):
                    raise ContractViolationError("conclusion lacks a supported hypothesis")
            self._db.execute("UPDATE research_runs SET run_status=?,stop_reason=?,conclusion_json=? WHERE research_id=?",
                             ("COMPLETED" if reason in {StopReason.GOAL_ANSWERED, StopReason.QUALIFIED_PROCEDURE_COMPLETED} else "STOPPED",
                              reason.value, to_json(candidate) if candidate is not None else None, research_id))
            self._db.execute("UPDATE tasks SET status='CANCELLED' WHERE research_id=? AND status IN ('ISSUED','RUNNING','WAITING_RETRY','WAITING_ESCALATION')",
                             (research_id,))
            self._db.execute("UPDATE contracts SET status='CANCELLED' WHERE research_id=? AND status IN ('ISSUED','RUNNING','WAITING_RETRY','WAITING_ESCALATION')",
                             (research_id,))
        return self._planning_commit(research_id, "RESEARCH_STOPPED", "research", research_id,
                                     {"reason": reason.value, "conclusion": candidate}, write)
