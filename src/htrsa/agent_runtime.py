"""고정 도구와 StateService를 사용하는 제한된 Agent 실행."""
from __future__ import annotations

import asyncio
from hashlib import sha256
import os
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from .agent_context import compile_context, input_text, instructions
from .agent_policy import BudgetController, BudgetExceededError, PricingRegistry, decide_escalation, validate_contract
from .agent_schemas import AnalysisPlan, CoordinatorDecision, FailureEvent, ManagerDecision
from .providers.base import ModelProvider, ModelProviderError
from .real_tools import DataImportTool, DataProfileTool, EvidenceTool, StatsTool, ToolRegistry, VisualizationTool, VerifiedAnalysisSkillTool
from .analysis_skills import SKILL_VERSIONS, SkillRequest
from .database import to_json
from .verification_repair import (LOCAL_RECOVERY_RESERVE_USD, MAX_REPAIR_ATTEMPTS,
                                  REPAIR_POLICY_VERSION, RepairDecision, RepairRequest,
                                  VerificationFailureEvidence, build_failure,
                                  eligible_for_reexecution, frozen_plan_matches, plan_hash,
                                  qualify_association_from_state, persist_audit_artifact)
from .verification_repair import CheckerQualification
from .schemas import (AgentResult, Constraints, ContextPolicy, ContextRef, NumericProvenance,
                      RefType, ResearchContract, ScientificMutation, StagedResult, ToolRequest, new_id)
from .scientific_verifier import numeric_fields
from .service import ContractViolationError, EntityNotFoundError, StateConflictError, StateService


class RuntimeFailure(Exception):
    def __init__(self, code: str, level: int):
        super().__init__(code)
        self.code, self.level = code, level


def configured_models() -> dict[str, str]:
    names = {"manager": "HTRSA_MANAGER_MODEL", "experiment_coordinator": "HTRSA_COORDINATOR_MODEL",
             "analysis_planner_worker": "HTRSA_WORKER_MODEL",
             "verification_coordinator": "HTRSA_VERIFICATION_MODEL",
             "literature_verification_coordinator": "HTRSA_LITERATURE_REVIEW_MODEL"}
    configured = {role: os.environ.get(variable, "") for role, variable in names.items()}
    configured["literature_verification_coordinator"] = (
        configured["literature_verification_coordinator"] or configured["verification_coordinator"])
    return configured


class AgentRuntime:
    def __init__(self, state: StateService, provider: ModelProvider,
                 models: dict[str, str] | None = None, pricing: PricingRegistry | None = None,
                 verified_analysis_skills_enabled: bool | None = None,
                 verification_repair_enabled: bool | None = None,
                 ridge_arithmetic_check_enabled: bool | None = None,
                 claim_evidence_provenance_enabled: bool | None = None,
                 verifier_dependency_catalog_enabled: bool | None = None):
        if state.workspace is None:
            raise ValueError("workspace is required")
        self.state = state
        self.provider = provider
        self.models = models or configured_models()
        self.pricing = pricing or PricingRegistry()
        self.budget = BudgetController(state, self.pricing)
        self.context_budget = 4096
        self.verified_analysis_skills_enabled = (os.environ.get("HTRSA_VERIFIED_ANALYSIS_SKILLS_ENABLED", "0") == "1"
                                                  if verified_analysis_skills_enabled is None
                                                  else verified_analysis_skills_enabled)
        self.state.verified_analysis_skills_enabled = self.verified_analysis_skills_enabled
        self.verification_repair_enabled = (os.environ.get("HTRSA_VERIFICATION_REPAIR_ENABLED", "0") == "1"
                                            if verification_repair_enabled is None else verification_repair_enabled)
        self.ridge_arithmetic_check_enabled = (
            os.environ.get("HTRSA_F3P_RIDGE_ARITHMETIC_CHECK", "0") == "1"
            if ridge_arithmetic_check_enabled is None else ridge_arithmetic_check_enabled)
        if self.ridge_arithmetic_check_enabled and not self.verification_repair_enabled:
            raise ValueError("Ridge arithmetic check requires verification repair")
        self.state.verification_repair_enabled = self.verification_repair_enabled
        self.state.ridge_arithmetic_check_enabled = self.ridge_arithmetic_check_enabled
        from .research_slice_schemas import ResearchSliceConfig
        self.research_slice_config = ResearchSliceConfig(
            claim_evidence_provenance=(os.environ.get("HTRSA_CLAIM_EVIDENCE_PROVENANCE", "0") == "1"
                                      if claim_evidence_provenance_enabled is None else claim_evidence_provenance_enabled),
            verifier_dependency_catalog=(os.environ.get("HTRSA_VERIFIER_DEPENDENCY_CATALOG", "0") == "1"
                                         if verifier_dependency_catalog_enabled is None else verifier_dependency_catalog_enabled))

    def _skill_config(self) -> dict:
        return {"enabled": self.verified_analysis_skills_enabled,
                "versions": SKILL_VERSIONS if self.verified_analysis_skills_enabled else {}}

    def _check_skill_config(self, research_id: str) -> None:
        saved_slice = self.state.runtime_step(research_id, "research_slice_config")
        if saved_slice:
            if saved_slice["status"] != "COMPLETED" or saved_slice["output"] != self.research_slice_config.model_dump(mode="json"):
                raise StateConflictError("RECOVERY_RESEARCH_SLICE_POLICY_MISMATCH")
        elif self.research_slice_config.claim_evidence_provenance or self.research_slice_config.verifier_dependency_catalog:
            raise StateConflictError("RECOVERY_RESEARCH_SLICE_POLICY_MISSING")
        saved = self.state.runtime_step(research_id, "verified_analysis_skills_config")
        if saved is None:
            if self.verified_analysis_skills_enabled:
                raise StateConflictError("RECOVERY_SKILL_CONFIG_MISSING")
            return
        if saved["status"] != "COMPLETED" or saved["output"] != self._skill_config():
            raise StateConflictError("RECOVERY_SKILL_VERSION_OR_CONFIG_MISMATCH")

    def _repair_config(self) -> dict:
        return {"enabled": self.verification_repair_enabled,
                "version": REPAIR_POLICY_VERSION if self.verification_repair_enabled else None,
                "ridge_arithmetic_check": self.ridge_arithmetic_check_enabled}

    def _check_repair_config(self, research_id: str) -> None:
        saved = self.state.runtime_step(research_id, "verification_repair_config")
        if saved is None:
            if self.verification_repair_enabled:
                raise StateConflictError("RECOVERY_REPAIR_CONFIG_MISSING")
            return
        if saved["status"] != "COMPLETED" or saved["output"] != self._repair_config():
            raise StateConflictError("RECOVERY_REPAIR_VERSION_OR_CONFIG_MISMATCH")

    def _remaining_runtime(self, contract: ResearchContract) -> float:
        agent_ms = self.state._db.execute(
            "SELECT COALESCE(SUM(latency_ms),0) FROM agent_runs WHERE contract_id=?",
            (contract.contract_id,)).fetchone()[0]
        tool_ms = self.state._db.execute(
            "SELECT COALESCE(SUM(tc.latency_ms),0) FROM tool_calls tc JOIN agent_runs ar USING(agent_run_id) WHERE ar.contract_id=?",
            (contract.contract_id,)).fetchone()[0]
        remaining = contract.constraints.max_runtime_sec - (agent_ms + tool_ms) / 1000
        if remaining <= 0:
            raise RuntimeFailure("RETRY_EXHAUSTED", 2)
        return remaining

    def _issue(self, contract: ResearchContract, *, runtime_key: str | None = None) -> str:
        validate_contract(self.state, contract, expected_research_id=contract.research_id,
                          remaining_usd=self.state.budget(contract.research_id)["remaining_usd"])
        self.state.runtime_event(contract.research_id, "CONTRACT_VALIDATED", {"contract_id": contract.contract_id})
        task_id = self.state.issue_contract(contract, runtime_key=runtime_key)
        self.state.runtime_event(contract.research_id, "CONTRACT_CREATED", {"contract_id": contract.contract_id,
                                                                          "task_id": task_id})
        self.state.set_task_status(contract.contract_id, "RUNNING")
        return task_id

    async def _invoke(self, contract: ResearchContract, output_type: type,
                      *, recent_failure: str | None = None):
        if getattr(self, "control_boundary", None):
            self.control_boundary()
        research_id, role = contract.research_id, contract.assigned_role
        model = self.models.get(role, "")
        if not model:
            raise ModelProviderError("MODEL_NOT_CONFIGURED")
        attempts = contract.constraints.max_retries + 1
        failed = self.state._db.execute(
            "SELECT COUNT(*) FROM agent_runs WHERE contract_id=? AND status='FAILED' AND provider IS NOT NULL",
            (contract.contract_id,)).fetchone()[0]
        if failed >= attempts:
            raise RuntimeFailure("RETRY_EXHAUSTED", 2)
        for attempt in range(failed + 1, attempts + 1):
            remaining = self._remaining_runtime(contract)
            self.budget.before_call(research_id, self.provider.name, model, contract)
            base_version = self.state.state_version(research_id)
            bundle = compile_context(self.state, contract, recent_failure=recent_failure)
            self.state.record_context_metrics(bundle)
            run_id = self.state.begin_model_run(contract.contract_id, role, self.provider.name, model, base_version)
            invocation_started = time.perf_counter()
            usage: dict = {}
            cost = None
            try:
                role_instructions = instructions(role)
                if getattr(output_type, "INSTRUCTIONS", None):
                    role_instructions += "\n" + output_type.INSTRUCTIONS
                if output_type is RepairDecision:
                    role_instructions += (
                        "\nChoose a RepairDecision from the structured failure evidence. "
                        "REPAIR authorizes only trusted reexecution of the unchanged frozen plan. "
                        "Do not change data, methods, variables, split, preprocessing, alpha, lags, "
                        "checks, thresholds, tools, policy, or research question. "
                        "Missing information or scientific changes require a new plan or unresolved outcome.\n")
                if self.verified_analysis_skills_enabled and role == "analysis_planner_worker":
                    role_instructions += ("\nWhen an eligible dataset has documented sampling, units, and identity, "
                                          "you may choose analysis.skill with a frozen SkillPlan instead of stats.run. "
                                          "Use only a listed skill and method. Set requested_tools to analysis.skill "
                                          "and evidence.record. Return needs_review through the Skill for uncertain "
                                          "assumptions; never invent independence or time regularity.\n")
                if self.verified_analysis_skills_enabled and role == "experiment_coordinator":
                    role_instructions += ("\nFor an eligible analysis, you may delegate analysis.skill "
                                          "and evidence.record to the worker. The three bounded options are "
                                          "numeric pair association, documented IID regression holdout, and "
                                          "regular one-step time-series backtest. Require documented units, "
                                          "observation identity and sampling. Missing assumptions require review.\n")
                result = await asyncio.wait_for(self.provider.run_structured(
                    role=role, instructions=role_instructions, input_text=input_text(bundle),
                    output_type=output_type, model=model,
                    metadata={"contract_id": contract.contract_id, "base_state_version": base_version}),
                    timeout=remaining)
                usage = result.usage()
                usage["latency_ms"] = max(usage.get("latency_ms") or 0,
                                          (time.perf_counter() - invocation_started) * 1000)
                cost = self.pricing.estimate(self.provider.name, model, usage)
                output = output_type.model_validate(result.output)
                if self.state.state_version(research_id) != base_version:
                    self.state.finish_model_run(run_id, "STALE", usage, "STALE_AGENT_RESULT", cost)
                    self.state.runtime_event(research_id, "STALE_AGENT_RESULT", {"agent_run_id": run_id})
                    raise RuntimeFailure("STALE_AGENT_RESULT", 2)
                self.state.finish_model_run(run_id, "COMPLETED", usage, estimated_cost_usd=cost)
                return output
            except RuntimeFailure:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                error = ModelProviderError("PROVIDER_TIMEOUT", retryable=True)
            except ModelProviderError as exc:
                error = exc
            except ValidationError:
                error = ModelProviderError("STRUCTURED_OUTPUT_INVALID", retryable=True)
            except Exception:
                error = ModelProviderError("PROVIDER_ERROR", retryable=True)
            usage["latency_ms"] = max(usage.get("latency_ms") or 0,
                                      (time.perf_counter() - invocation_started) * 1000)
            self.state.finish_model_run(run_id, "FAILED", usage, error.code, cost)
            failure = FailureEvent(code=error.code, severity="MEDIUM", source_role=role,
                                   contract_id=contract.contract_id, attempt=attempt,
                                   impact="agent output unavailable")
            escalation = decide_escalation(failure, contract.constraints.max_retries)
            if attempt < attempts and error.retryable and escalation.level <= 1:
                self.state.set_task_status(contract.contract_id, "WAITING_RETRY")
                self.state.runtime_event(research_id, "RETRY_SCHEDULED", {"code": error.code, "attempt": attempt})
                if escalation.level == 1:
                    self.state.runtime_event(research_id, "ESCALATION_L1", {"code": error.code})
                self.state.set_task_status(contract.contract_id, "RUNNING")
                recent_failure = error.code
                continue
            self.state.set_task_status(contract.contract_id, "WAITING_ESCALATION")
            self.state.runtime_event(research_id, "ESCALATION_L2", {"code": error.code, "attempt": attempt})
            raise RuntimeFailure(error.code, 2)
        raise AssertionError("unreachable retry state")

    def _registry(self, contract_id: str, *, import_source_paths=None) -> ToolRegistry:
        registry = ToolRegistry(self.state)
        try:
            contract, _ = self.state.contract(contract_id)
        except EntityNotFoundError:
            import_source_paths = []
        else:
            if contract.task_type == "QualifiedProfileImport" and import_source_paths is None:
                import_source_paths = []
        for tool in (DataImportTool(self.state, allowed_source_paths=import_source_paths), DataProfileTool(self.state, contract_id),
                     StatsTool(self.state, contract_id), VisualizationTool(self.state, contract_id),
                     EvidenceTool(self.state)):
            registry.register(tool)
        if self.verified_analysis_skills_enabled:
            registry.register(VerifiedAnalysisSkillTool(self.state, contract_id))
        return registry

    def _worker_contract(self, coordinator: ResearchContract, coordinator_task: str,
                         draft: CoordinatorDecision, dataset_id: str, profile_id: str,
                         additional_refs: list[ContextRef] | None = None) -> ResearchContract:
        required_refs = {("dataset", dataset_id), ("artifact", profile_id)}
        offered_refs = {(ref.type.value, ref.id) for ref in draft.input_refs}
        offered_tools = set(draft.allowed_tools)
        legacy_tools = {"stats.run", "visualization.render", "evidence.record"}
        skill_tools = {"analysis.skill", "evidence.record"}
        if not required_refs <= offered_refs or not (legacy_tools <= offered_tools or
                (self.verified_analysis_skills_enabled and skill_tools <= offered_tools)):
            raise ContractViolationError("CONTRACT_INVALID: required worker inputs or tools absent")
        refs = list(draft.input_refs)
        for ref in additional_refs or []:
            if ref not in refs:
                refs.append(ref)
        return ResearchContract(contract_id=new_id("C"), research_id=coordinator.research_id,
                                parent_task_id=coordinator_task, task_type="analysis_plan",
                                issued_by="experiment_coordinator", assigned_role=draft.assigned_role,
                                objective=draft.objective, inputs=refs,
                                context_policy=ContextPolicy(must_preserve=refs,
                                                             max_context_tokens=self.context_budget),
                                allowed_tools=draft.allowed_tools,
                                constraints=Constraints(max_tool_calls=draft.max_tool_calls,
                                                        max_retries=draft.max_retries,
                                                        max_runtime_sec=draft.max_runtime_sec,
                                                        max_cost_usd=draft.max_cost_usd),
                                output_schema_id="AnalysisPlan")

    def _dispatch(self, registry: ToolRegistry, contract: ResearchContract, task_id: str,
                  name: str, args: dict):
        if getattr(self, "control_boundary", None):
            self.control_boundary()
        if name not in contract.allowed_tools:
            raise RuntimeFailure("TOOL_NOT_ALLOWED", 2)
        for attempt in range(1, contract.constraints.max_retries + 2):
            self._remaining_runtime(contract)
            key = f"{contract.contract_id}:{name}:{attempt}"
            request = ToolRequest(request_id=f"TREQ-{uuid5(NAMESPACE_URL, key).hex}",
                                  research_id=contract.research_id, task_id=task_id,
                                  actor_id=contract.assigned_role, idempotency_key=key,
                                  tool_name=name, args=args)
            replay = self.state.replay_tool(key, request)
            result = replay if replay is not None else registry.dispatch(contract.contract_id, request)
            self._remaining_runtime(contract)
            if result.ok:
                return request, result
            if result.error == 'RESOURCE_EXHAUSTED':
                raise RuntimeFailure('RESOURCE_EXHAUSTED', 2)
            if name == "analysis.skill" and result.error and result.error.startswith("SKILL_"):
                raise RuntimeFailure(result.error, 2)
            failure = FailureEvent(code="TOOL_FAILURE", severity="MEDIUM", source_role=contract.assigned_role,
                                   contract_id=contract.contract_id, attempt=attempt,
                                   deterministic_signals=[name], impact="tool output unavailable")
            escalation = decide_escalation(failure, contract.constraints.max_retries)
            if escalation.level == 0:
                self.state.runtime_event(contract.research_id, "RETRY_SCHEDULED", {"tool": name, "attempt": attempt})
                continue
            self.state.runtime_event(contract.research_id, "ESCALATION_L2", {"tool": name})
            raise RuntimeFailure("TOOL_FAILURE", 2)
        raise AssertionError("unreachable tool retry state")

    async def prepare(self, goal: str, csv_source: str | Path, *, target_usd: float = 0.25,
                      soft_limit_usd: float = 0.75, hard_limit_usd: float = 1.0) -> dict:
        research_id = self.state.create_research(goal)
        self.state.workspace.prepare(research_id)
        self.state.configure_research_slice(research_id, self.research_slice_config)
        self.state.finish_runtime_step(research_id, "verified_analysis_skills_config", self._skill_config())
        self.state.finish_runtime_step(research_id, "verification_repair_config", self._repair_config())
        self.state.configure_budget(research_id, target_usd, soft_limit_usd, hard_limit_usd)
        manager = ResearchContract(contract_id=new_id("C"), research_id=research_id,
                                   task_type="initial_plan", issued_by="system", assigned_role="manager",
                                   objective=goal, allowed_tools=[],
                                   constraints=Constraints(max_tool_calls=0, max_retries=1,
                                                           max_runtime_sec=90, max_cost_usd=hard_limit_usd),
                                   output_schema_id="ManagerDecision")
        manager_task = self._issue(manager)
        decision: ManagerDecision = await self._invoke(manager, ManagerDecision)
        if decision.decision_type not in {"INITIAL_PLAN", "DELEGATE"}:
            raise RuntimeFailure("CONTRACT_INVALID", 2)
        coordinator = ResearchContract(contract_id=new_id("C"), research_id=research_id,
                                       parent_task_id=manager_task, task_type="experiment_coordination",
                                       issued_by="manager", assigned_role=decision.coordinator_role,
                                       objective=decision.objective,
                                       allowed_tools=["data.import", "data.profile"],
                                       constraints=Constraints(max_tool_calls=2, max_retries=1,
                                                               max_runtime_sec=120,
                                                               max_cost_usd=self.state.budget(research_id)["remaining_usd"]),
                                       output_schema_id="CoordinatorDecision")
        coordinator_task = self._issue(coordinator)
        self.state.set_task_status(manager.contract_id, "COMPLETED")
        registry = self._registry(coordinator.contract_id)
        _, imported = self._dispatch(registry, coordinator, coordinator_task, "data.import",
                                     {"source_path": str(csv_source), "research_id": research_id})
        dataset_id = imported.result["dataset_id"]
        _, profile = self._dispatch(registry, coordinator, coordinator_task, "data.profile",
                                    {"dataset_id": dataset_id})
        profile_id = profile.result["artifact_id"]
        draft: CoordinatorDecision = await self._invoke(coordinator, CoordinatorDecision)
        if draft.decision_type != "DELEGATE":
            await self.escalate(research_id, "COORDINATOR_CONFLICT")
            raise RuntimeFailure("COORDINATOR_CONFLICT", 3)
        worker = self._worker_contract(coordinator, coordinator_task, draft, dataset_id, profile_id)
        worker_task = self._issue(worker)
        cursor = {"stage": "WORKER_READY", "active_task_ids": [coordinator_task, worker_task],
                  "active_contract_ids": [coordinator.contract_id, worker.contract_id],
                  "dataset_id": dataset_id, "profile_artifact_id": profile_id}
        checkpoint_id = self.state.checkpoint(research_id, "worker ready", cursor)
        return {"research_id": research_id, "checkpoint_id": checkpoint_id, **cursor}

    async def resume(self, research_id: str) -> dict:
        self._check_skill_config(research_id)
        self._check_repair_config(research_id)
        self.state.cycle5.reconcile_review_checkpoint(research_id)
        self.state.cycle5.validate_completed(research_id)
        self.state.runtime_event(research_id, "RESUME_STARTED")
        checkpoint = self.state.latest_checkpoint(research_id)
        cursor = checkpoint["cursor"]
        if not cursor or checkpoint["research_id"] != research_id:
            raise StateConflictError("checkpoint state version mismatch")
        if cursor["stage"] == "COMPLETED":
            return {"research_id": research_id, "already_completed": True,
                    "state_version": checkpoint["state_version"]}
        if cursor["stage"] != "WORKER_READY" or len(cursor["active_task_ids"]) != 2 or len(cursor["active_contract_ids"]) != 2:
            raise StateConflictError("invalid checkpoint cursor")
        coordinator, coordinator_task = self.state.contract(cursor["active_contract_ids"][0])
        worker, worker_task = self.state.contract(cursor["active_contract_ids"][1])
        if [coordinator_task, worker_task] != cursor["active_task_ids"] or any(
                contract.research_id != research_id for contract in (coordinator, worker)):
            raise StateConflictError("checkpoint references a different research")
        committed = self.state.committed_contract_result(research_id, worker.contract_id)
        if committed is not None:
            if self.state.task_status(worker_task) != "COMPLETED":
                self.state.set_task_status(worker.contract_id, "COMPLETED")
            if self.state.task_status(coordinator_task) != "COMPLETED":
                self.state.set_task_status(coordinator.contract_id, "COMPLETED")
            self.state.checkpoint(research_id, "recovered committed task", {"stage": "COMPLETED",
                                  "active_task_ids": [], "active_contract_ids": [],
                                  "mutation_id": committed["mutation_id"]})
            return {"research_id": research_id, "already_completed": True, **committed}
        if checkpoint["state_version"] != self.state.state_version(research_id):
            raise StateConflictError("checkpoint state version mismatch")
        if self.state.task_status(worker_task) == "COMPLETED":
            return {"research_id": research_id, "already_completed": True,
                    "state_version": self.state.state_version(research_id)}
        if self.state.task_status(worker_task) in {"FAILED", "CANCELLED"} and not (
                self.verification_repair_enabled and self.state.runtime_step(
                    research_id, f"repair_failure:{worker.contract_id}") is not None):
            raise StateConflictError("cannot resume invalidated task")
        self.state.dataset_record(cursor["dataset_id"], research_id)
        self.state.file_artifact(cursor["profile_artifact_id"], research_id)
        try:
            failure_step = self.state.runtime_step(research_id, f"repair_failure:{worker.contract_id}")
            if self.verification_repair_enabled and failure_step is not None:
                result = await self._repair_failed_worker(worker, worker_task, cursor)
            else:
                result = await self._run_worker(worker, worker_task, cursor)
        except RuntimeFailure as exc:
            if self.verification_repair_enabled and exc.code == "VERIFICATION_FAILED":
                return await self._repair_failed_worker(worker, worker_task, cursor)
            if self.verification_repair_enabled and exc.code.startswith(
                    ("REPAIR_", "CHECKER_", "VALIDATION_")):
                raise
            if exc.level == 2:
                await self._diagnose(coordinator, exc.code, cursor)
            raise
        self.state.runtime_event(research_id, "RESUME_COMPLETED", {"state_version": result["state_version"]})
        return result

    async def _diagnose(self, coordinator: ResearchContract, code: str, cursor: dict) -> None:
        self.state.runtime_event(coordinator.research_id, "ESCALATION_L2", {"code": code})
        old_worker_id = cursor["active_contract_ids"][1]
        if cursor.get("replans", 0) >= 1:
            self.state.set_task_status(old_worker_id, "FAILED")
            return
        try:
            diagnosis: CoordinatorDecision = await self._invoke(coordinator, CoordinatorDecision,
                                                                recent_failure=code)
            if diagnosis.decision_type == "ESCALATE":
                self.state.set_task_status(old_worker_id, "FAILED")
                await self.escalate(coordinator.research_id, "COORDINATOR_CONFLICT")
                return
            coordinator_task = cursor["active_task_ids"][0]
            new_worker = self._worker_contract(coordinator, coordinator_task, diagnosis,
                                               cursor["dataset_id"], cursor["profile_artifact_id"])
            new_task = self._issue(new_worker)
            self.state.set_task_status(old_worker_id, "FAILED")
            updated = {**cursor, "active_task_ids": [coordinator_task, new_task],
                       "active_contract_ids": [coordinator.contract_id, new_worker.contract_id],
                       "replans": cursor.get("replans", 0) + 1}
            self.state.checkpoint(coordinator.research_id, "coordinator replan", updated)
        except (RuntimeFailure, ContractViolationError, BudgetExceededError):
            self.state.set_task_status(old_worker_id, "FAILED")

    def _repair_fault(self, boundary: str) -> None:
        faults = getattr(self, "repair_faults", None)
        if faults is not None:
            faults.at(boundary)

    def _repair_preflight(self, research_id: str, coordinator: ResearchContract,
                          *, proposal_required: bool = True) -> float:
        if self.provider.name == "control_broker" and self.provider.snapshot.get("performance_profile") and self.provider.snapshot.get("adaptive_budget"):
            from .product_policy import completion_budget
            view = completion_budget(self.provider.store, research_id, self.provider.snapshot)
            if not view["can_complete"]:
                self.state.runtime_event(research_id, "REPAIR_NOT_STARTED_BUDGET", view)
                raise RuntimeFailure("REPAIR_NOT_STARTED_BUDGET", 2)
        available = self.state.budget(research_id)["remaining_usd"]
        model = self.models.get("experiment_coordinator", "")
        reserve = self.pricing.reservation(self.provider.name, model) if model else None
        required = LOCAL_RECOVERY_RESERVE_USD + ((reserve or 0.0) if proposal_required else 0.0)
        limits = self.budget.unknown_limits
        budget = self.state.budget(research_id)
        unknown_exhausted = proposal_required and reserve is None and (
            budget["unknown_price_calls"] + 1 > limits.max_calls or
            budget["unknown_price_input_tokens"] + limits.reserve_input_tokens > limits.max_input_tokens or
            budget["unknown_price_output_tokens"] + limits.reserve_output_tokens > limits.max_output_tokens)
        if available < required or unknown_exhausted or (proposal_required and not model):
            self.state.runtime_event(research_id, "REPAIR_NOT_STARTED_BUDGET",
                                     {"required_usd": required, "available_usd": available,
                                      "unknown_price_cap": unknown_exhausted})
            raise RuntimeFailure("REPAIR_NOT_STARTED_BUDGET" if model or not proposal_required else "MODEL_NOT_CONFIGURED", 2)
        if proposal_required and coordinator.constraints.max_cost_usd < (reserve or 0.0):
            raise RuntimeFailure("REPAIR_NOT_STARTED_BUDGET", 2)
        if proposal_required:
            self.budget.before_call(research_id, self.provider.name, model, coordinator)
        self.state.runtime_event(research_id, "REPAIR_BUDGET_PREFLIGHT",
                                 {"required_usd": required, "available_usd": available,
                                  "includes": ["proposal", "execution", "full_revalidation",
                                               "artifacts", "state", "report"],
                                  "price_known": reserve is not None})
        return required

    async def _repair_failed_worker(self, worker: ResearchContract, task_id: str,
                                    cursor: dict) -> dict:
        research_id = worker.research_id
        failure_step = self.state.runtime_step(research_id, f"repair_failure:{worker.contract_id}")
        if failure_step is None or failure_step["status"] != "COMPLETED":
            raise StateConflictError("REPAIR_FAILURE_EVIDENCE_MISSING")
        failure = VerificationFailureEvidence.model_validate(failure_step["output"])
        if failure.contract_id != worker.contract_id or failure.task_id != task_id:
            raise StateConflictError("REPAIR_FAILURE_EVIDENCE_MISMATCH")
        self.state.file_artifact(failure.failure_id, research_id)
        if sha256(to_json(worker).encode("utf-8", errors="strict")).hexdigest() != failure.authoritative_contract_hash:
            raise StateConflictError("REPAIR_CONTRACT_HASH_MISMATCH")
        if self.state.dataset_record(cursor["dataset_id"], research_id)["sha256"] != failure.input_hashes[cursor["dataset_id"]]:
            raise StateConflictError("REPAIR_INPUT_HASH_MISMATCH")
        mutation = self.state._one("SELECT status FROM staged_mutations WHERE mutation_id=?",
                                   (failure.mutation_id,))
        if mutation["status"] == "PENDING":
            self.state.rollback(failure.mutation_id, preserve_shared_profile=True)
        elif mutation["status"] != "ROLLED_BACK":
            raise StateConflictError("REPAIR_ORIGINAL_NOT_FAILED")
        attempt = cursor.get("repair_attempt", 0)
        if attempt >= MAX_REPAIR_ATTEMPTS:
            self.state.runtime_event(research_id, "REPAIR_INCOMPLETE",
                                     {"reason": "attempt_limit", "attempts": attempt})
            raise RuntimeFailure("REPAIR_INCOMPLETE", 2)
        if not eligible_for_reexecution(failure.check_ids):
            self.state.runtime_event(research_id, "REPAIR_UNRESOLVED",
                                     {"check_ids": failure.check_ids})
            raise RuntimeFailure("REPAIR_UNRESOLVED", 2)
        coordinator, coordinator_task = self.state.contract(cursor["active_contract_ids"][0])
        original_step = self.state.runtime_step(research_id, f"worker_plan:{worker.contract_id}")
        if original_step is None or original_step["status"] != "COMPLETED":
            raise StateConflictError("REPAIR_FROZEN_PLAN_MISSING")
        plan = AnalysisPlan.model_validate(original_step["output"])
        if plan_hash(plan) != failure.frozen_plan_hash:
            raise StateConflictError("REPAIR_FROZEN_PLAN_MISMATCH")
        if "RESULT_FIELD_MATCH" in failure.check_ids:
            original = self.state._one("SELECT payload_json FROM staged_mutations WHERE mutation_id=?",
                                       (failure.mutation_id,))
            original_payload = StagedResult.model_validate_json(original["payload_json"])
            qualification = qualify_association_from_state(
                self.state, research_id, plan, original_payload.agent_result.output)
            qualified = CheckerQualification(
                status=("CHECKER_QUALIFICATION_FAILED" if qualification == "NUMERICAL_CHECKS_PASSED_FOR_SCOPE"
                        else qualification), frozen_plan_hash=plan_hash(plan),
                failure_id=failure.failure_id, input_hashes=failure.input_hashes)
            persist_audit_artifact(
                self.state, research_id, worker.contract_id,
                f"CQ-{uuid5(NAMESPACE_URL, worker.contract_id).hex}", "F3P_CHECKER_QUALIFICATION",
                qualified.model_dump(mode="json"))
            if qualification == "NUMERICAL_CHECKS_PASSED_FOR_SCOPE":
                self.state.runtime_event(research_id, "CHECKER_QUALIFICATION_FAILED",
                                         {"failure_id": failure.failure_id})
                raise RuntimeFailure("CHECKER_QUALIFICATION_FAILED", 2)
            if qualification == "UNRESOLVED_DISAGREEMENT":
                raise RuntimeFailure("REPAIR_UNRESOLVED", 2)
            if qualification == "ORIGINAL_CONTRACT_MISMATCH":
                raise RuntimeFailure("REPAIR_CONTRACT_MUTATION_BLOCKED", 2)
        decision_key = f"repair_decision:{worker.contract_id}"
        saved = self.state.runtime_step(research_id, decision_key)
        required = self._repair_preflight(
            research_id, coordinator, proposal_required=saved is None or saved["status"] != "COMPLETED")
        request = RepairRequest(
            original_contract_id=worker.contract_id, frozen_plan=plan,
            frozen_plan_hash=plan_hash(plan), current_revision=attempt, failure=failure,
            authorized_inputs=[ref.id for ref in worker.inputs],
            allowed_tools=worker.allowed_tools, allowed_repair_scope=failure.allowed_repair_scope,
            forbidden_semantic_changes=failure.forbidden_semantic_changes,
            remaining_budget_usd=self.state.budget(research_id)["remaining_usd"],
            max_repair_attempts=MAX_REPAIR_ATTEMPTS - attempt,
            mandatory_post_repair_checks=failure.required_post_repair_checks)
        request_key = f"repair_request:{worker.contract_id}"
        recorded_request = self.state.runtime_step(research_id, request_key)
        if recorded_request is None:
            self.state.finish_runtime_step(research_id, request_key,
                                           request.model_dump(mode="json"), worker.contract_id)
        else:
            request = RepairRequest.model_validate(recorded_request["output"])
        if saved is not None and saved["status"] == "COMPLETED":
            decision = RepairDecision.model_validate(saved["output"])
        else:
            proposal_id = f"C-{uuid5(NAMESPACE_URL, 'f3p-proposal:' + worker.contract_id).hex}"
            proposal_row = self.state._db.execute(
                "SELECT 1 FROM contracts WHERE contract_id=?", (proposal_id,)).fetchone()
            if proposal_row is None:
                proposal_coordinator = ResearchContract(
                    contract_id=proposal_id, research_id=research_id, parent_task_id=coordinator_task,
                    task_type="verification_repair_decision", issued_by="experiment_coordinator",
                    assigned_role="experiment_coordinator", objective=worker.objective,
                    inputs=worker.inputs,
                    context_policy=ContextPolicy(must_preserve=worker.inputs,
                                                 max_context_tokens=self.context_budget),
                    allowed_tools=[], constraints=coordinator.constraints.model_copy(
                        update={"max_tool_calls": 0, "max_retries": 0,
                                "max_cost_usd": min(coordinator.constraints.max_cost_usd,
                                                    self.state.budget(research_id)["remaining_usd"])}),
                    output_schema_id="RepairDecision")
                self._issue(proposal_coordinator)
            else:
                proposal_coordinator, _ = self.state.contract(proposal_id)
            self.state.begin_runtime_step(research_id, decision_key, proposal_id)
            if self.state.cycle5.enabled(research_id):
                from .verification_repair import repair_context_projection
                repair_context = repair_context_projection(request)
            else:
                repair_context = request
            decision = await self._invoke(proposal_coordinator, RepairDecision, recent_failure=to_json(repair_context))
            self.state.finish_runtime_step(research_id, decision_key,
                                           decision.model_dump(mode="json"), proposal_id)
            self.state.set_task_status(proposal_id, "COMPLETED")
        decision_artifact_id = f"RD-{uuid5(NAMESPACE_URL, worker.contract_id).hex}"
        persist_audit_artifact(self.state, research_id, worker.contract_id,
                               decision_artifact_id, "F3P_REPAIR_DECISION",
                               {"failure_id": failure.failure_id, "decision": decision.model_dump(mode="json")})
        self._repair_fault("AFTER_REPAIR_DECISION")
        if decision.action != "REPAIR":
            self.state.runtime_event(research_id, "REPAIR_UNRESOLVED",
                                     {"decision": decision.action, "failure_id": failure.failure_id})
            raise RuntimeFailure(f"REPAIR_{decision.action}", 2)
        if not frozen_plan_matches(plan, decision.proposed_plan):
            self.state.runtime_event(research_id, "REPAIR_CONTRACT_MUTATION_BLOCKED",
                                     {"failure_id": failure.failure_id})
            raise RuntimeFailure("REPAIR_CONTRACT_MUTATION_BLOCKED", 2)
        try:
            execution_reserve = self._repair_preflight(research_id, coordinator, proposal_required=False)
        except RuntimeFailure as exc:
            raise RuntimeFailure("REPAIR_INCOMPLETE_BUDGET", 2) from exc
        new_id_value = f"C-{uuid5(NAMESPACE_URL, 'f3p:' + worker.contract_id).hex}"
        existing = self.state._db.execute("SELECT 1 FROM contracts WHERE contract_id=?",
                                          (new_id_value,)).fetchone()
        if existing is None:
            repair_worker = worker.model_copy(deep=True, update={
                "contract_id": new_id_value, "parent_task_id": coordinator_task,
                "task_type": "analysis_repair", "issued_by": "experiment_coordinator",
                "constraints": worker.constraints.model_copy(update={
                    "max_cost_usd": min(worker.constraints.max_cost_usd,
                                        self.state.budget(research_id)["remaining_usd"] - execution_reserve)})
            })
            old_key = self.state._one("SELECT runtime_key FROM contracts WHERE contract_id=?",
                                      (worker.contract_id,))["runtime_key"]
            new_task = self._issue(repair_worker,
                                   runtime_key=f"auto:worker:f3p:{new_id_value}" if old_key else None)
        else:
            repair_worker, new_task = self.state.contract(new_id_value)
        self.state.finish_runtime_step(research_id, f"worker_plan:{new_id_value}",
                                       plan.model_dump(mode="json"), new_id_value)
        self.state.finish_runtime_step(research_id, f"repair_parent:{new_id_value}",
                                       {"contract_id": worker.contract_id,
                                        "required_checks": failure.required_post_repair_checks,
                                        "revision": attempt + 1}, new_id_value)
        updated = {**cursor, "active_task_ids": [coordinator_task, new_task],
                   "active_contract_ids": [coordinator.contract_id, new_id_value],
                   "repair_attempt": attempt + 1,
                   "repair_origin_contract_id": cursor.get("repair_origin_contract_id", worker.contract_id)}
        self.state.finish_runtime_step(research_id, f"repair_next:{worker.contract_id}", updated, worker.contract_id)
        self.state.set_task_status(worker.contract_id, "FAILED")
        self.state.checkpoint(research_id, "repair worker ready", updated)
        try:
            result = await self._run_worker(repair_worker, new_task, updated)
        except RuntimeFailure as exc:
            if exc.code == "VERIFICATION_FAILED":
                return await self._repair_failed_worker(repair_worker, new_task, updated)
            raise
        self.state.runtime_event(research_id, "REPAIR_REVALIDATED",
                                 {"failure_id": failure.failure_id, "mutation_id": result["mutation_id"],
                                  "revision": attempt + 1})
        return result

    async def escalate(self, research_id: str, code: str) -> ManagerDecision | None:
        failure = FailureEvent(code=code, severity="HIGH", source_role="experiment_coordinator",
                               contract_id="strategic", attempt=1, impact="research plan")
        decision = decide_escalation(failure, 0)
        if decision.level != 3:
            return None
        self.state.runtime_event(research_id, "ESCALATION_L3", {"code": code})
        contract = ResearchContract(contract_id=new_id("C"), research_id=research_id,
                                    task_type="strategic_review", issued_by="system", assigned_role="manager",
                                    objective=f"Review strategic failure: {code}", allowed_tools=[],
                                    constraints=Constraints(max_tool_calls=0, max_retries=0,
                                                            max_runtime_sec=90,
                                                            max_cost_usd=self.state.budget(research_id)["remaining_usd"]),
                                    output_schema_id="ManagerDecision")
        self._issue(contract)
        result = await self._invoke(contract, ManagerDecision, recent_failure=code)
        self.state.set_task_status(contract.contract_id, "COMPLETED")
        return result

    async def _run_worker(self, contract: ResearchContract, task_id: str, cursor: dict,
                          *, expected_method: str | None = None,
                          recovery_key: str | None = None, faults=None) -> dict:
        self._check_repair_config(contract.research_id)
        mutation = self.state._db.execute(
            "SELECT mutation_id,status,payload_json,verification_json FROM staged_mutations WHERE research_id=? AND contract_id=? AND status IN ('PENDING','COMMITTED') ORDER BY rowid DESC LIMIT 1",
            (contract.research_id, contract.contract_id)).fetchone()
        if mutation is not None:
            payload = StagedResult.model_validate_json(mutation["payload_json"])
            if mutation["status"] == "COMMITTED":
                self.state.runtime_event(contract.research_id, "COMMITTED_ACTION_RECONCILED",
                                         {"contract_id": contract.contract_id, "mutation_id": mutation["mutation_id"]})
                self._finish_worker_contracts(contract, cursor, mutation["mutation_id"])
                return self._worker_outcome(contract, cursor, mutation["mutation_id"], payload)
            self.state.runtime_event(contract.research_id, "STAGED_MUTATION_RECOVERED",
                                     {"mutation_id": mutation["mutation_id"]})
            return self._finish_staged_worker(contract, cursor, mutation["mutation_id"], payload,
                                              mutation["verification_json"], faults)
        self._check_skill_config(contract.research_id)
        recovery_key = recovery_key or f"worker_plan:{contract.contract_id}"
        plan_step = self.state.runtime_step(contract.research_id, recovery_key)
        self.state.cycle5.require_review(contract.research_id, cursor["dataset_id"])
        if plan_step is not None and plan_step["status"] == "COMPLETED":
            try:
                plan = AnalysisPlan.model_validate(plan_step["output"])
            except ValidationError as exc:
                raise StateConflictError("RECOVERY_SKILL_PLAN_INCOMPATIBLE") from exc
        else:
            if recovery_key:
                self.state.begin_runtime_step(contract.research_id, recovery_key, contract.contract_id)
            plan = await self._invoke(contract, AnalysisPlan)
            if recovery_key:
                self.state.finish_runtime_step(contract.research_id, recovery_key,
                                               plan.model_dump(mode="json"), contract.contract_id)
        canonical_plan_key = f"worker_plan:{contract.contract_id}"
        if recovery_key != canonical_plan_key:
            self.state.finish_runtime_step(contract.research_id, canonical_plan_key,
                                           plan.model_dump(mode="json"), contract.contract_id)
        dataset_id = cursor["dataset_id"]
        if plan.dataset_id != dataset_id or not set(plan.requested_tools) <= set(contract.allowed_tools) or (
                expected_method is not None and plan.method != expected_method):
            raise ContractViolationError("CONTRACT_INVALID: worker plan exceeds contract")
        if plan.skill_plan is not None and (not self.verified_analysis_skills_enabled
                                           or plan.skill_plan.method != plan.method
                                           or plan.skill_plan.dataset_id != dataset_id
                                           or "analysis.skill" not in contract.allowed_tools):
            raise ContractViolationError("CONTRACT_INVALID: incompatible or disabled Skill plan")
        if plan.skill_plan is None and plan.method in {"ridge_holdout", "ridge_rolling_origin"}:
            raise ContractViolationError("CONTRACT_INVALID: Skill method requires versioned plan")
        profile = self.state.dataset_record(dataset_id, contract.research_id)
        owner = self.state.runtime_step(contract.research_id, "owner_analysis_plan_request")
        if owner and (recovery_key == "worker_plan:0" or not self.state._db.execute("SELECT 1 FROM experiments WHERE research_id=? AND status='VERIFIED'", (contract.research_id,)).fetchone()):
            requested = AnalysisPlan.model_validate(owner["output"]["plan"])
            if plan.method != requested.method or plan.selected_variables != requested.selected_variables or profile["sha256"] != owner["output"]["input_sha256"]:
                raise ContractViolationError("OWNER_ANALYSIS_PLAN_MISMATCH")
            if requested.skill_plan is not None:
                actual = plan.skill_plan.model_dump(mode="json", exclude={"dataset_id"}) if plan.skill_plan else None
                expected = requested.skill_plan.model_dump(mode="json", exclude={"dataset_id"})
                if actual != expected:
                    raise ContractViolationError("OWNER_ANALYSIS_PLAN_MISMATCH")
        self.state.research_slice.precommit(contract.research_id, contract, plan, self.models)
        from .database import from_json
        columns = from_json(profile["schema_json"])
        x, y = plan.selected_variables
        required_tools = {"analysis.skill", "evidence.record"} if plan.skill_plan else {"stats.run", "visualization.render", "evidence.record"}
        if x == y or x not in columns or y not in columns or not required_tools <= set(plan.requested_tools):
            raise ContractViolationError("CONTRACT_INVALID: worker variables or tools")
        registry = self._registry(contract.contract_id)
        if faults:
            faults.at("BEFORE_TOOL")
        if plan.skill_plan:
            skill_request = SkillRequest(research_id=contract.research_id, task_id=task_id,
                                         contract_id=contract.contract_id, plan_ref=contract.contract_id,
                                         plan_version="1",
                                         plan_hash=plan.skill_plan.fingerprint(), plan=plan.skill_plan)
            stats_request, stats = self._dispatch(registry, contract, task_id, "analysis.skill",
                                                  skill_request.model_dump(mode="json"))
        else:
            stats_request, stats = self._dispatch(registry, contract, task_id, "stats.run",
                                                  {"dataset_id": dataset_id, "method": plan.method,
                                                   "variables": {"x": x, "y": y}, "parameters": {}})
        if faults:
            faults.at("AFTER_TOOL")
        if plan.skill_plan:
            figure_id = stats.provenance["figure_artifact_id"]
            estimate = stats.result.get("metrics", {}).get("estimate")
        else:
            _, figure = self._dispatch(registry, contract, task_id, "visualization.render",
                                       {"dataset_id": dataset_id, "plot_type": "scatter", "x": x, "y": y,
                                        "title": f"{x} and {y}"})
            figure_id = figure.result["artifact_id"]
            estimate = stats.result["estimate"]
        if estimate is None:
            delta = stats.result["metrics"]["candidate_minus_baseline_mae"]
            claim = f"Fixed {plan.method} candidate MAE differs from its baseline by {delta:.6g}; predictive evaluation is not causal evidence."
            polarity = "neutral"
        else:
            direction = "positive" if estimate > 0 else "negative" if estimate < 0 else "no monotonic"
            claim = f"{x} and {y} show a {direction} association in the imported sample; this does not establish causality."
            polarity = "support" if estimate != 0 else "neutral"
        _, evidence = self._dispatch(registry, contract, task_id, "evidence.record",
                                     {"claim": claim, "polarity": polarity,
                                      "source_type": "artifact", "source_ref": stats.provenance["stats_artifact_id"]})
        artifact_id = stats.provenance["stats_artifact_id"]
        provenance = {name: NumericProvenance(value=value, field=name, artifact_id=artifact_id,
                       tool_call_id=stats_request.request_id, dataset_id=dataset_id,
                       dataset_sha256=profile["sha256"])
                      for name, value in numeric_fields(stats.result).items()}
        science = ScientificMutation(dataset_id=dataset_id, profile_artifact_id=cursor["profile_artifact_id"],
                                     stats_artifact_id=artifact_id, figure_artifact_id=figure_id,
                                     experiment_id=new_id("EXP"), evidence_id=new_id("EV"), method=plan.method,
                                     claim=evidence.result["claim"], polarity=evidence.result["polarity"],
                                     numeric_provenance=provenance,
                                     plan_artifact_id=stats.provenance.get("plan_artifact_id"))
        agent_result = AgentResult(result_id=new_id("RES"), research_id=contract.research_id,
                                   contract_id=contract.contract_id, actor_role=contract.assigned_role,
                                   status="completed", output=stats.result,
                                   artifact_refs=[ContextRef(type=RefType.artifact, id=ref) for ref in
                                                  (science.profile_artifact_id, science.stats_artifact_id,
                                                   science.figure_artifact_id, *([science.plan_artifact_id] if science.plan_artifact_id else []))], confidence=0,
                                   provenance={"numeric_source": stats_request.tool_name,
                                               **({"repair_policy_version": REPAIR_POLICY_VERSION,
                                                   "analysis_revision": cursor.get("repair_attempt", 0),
                                                   "original_contract_id": cursor.get(
                                                       "repair_origin_contract_id", contract.contract_id),
                                                   "frozen_plan_hash": plan_hash(plan)}
                                                  if self.verification_repair_enabled else {})})
        payload = StagedResult(agent_result=agent_result, tool_request=stats_request,
                               tool_result=stats, scientific=science)
        mutation_id = self.state.stage(payload)
        if faults:
            faults.at("AFTER_STAGE")
        if cursor.get("repair_attempt", 0):
            self._repair_fault("AFTER_REPAIRED_EXECUTION")
        return self._finish_staged_worker(contract, cursor, mutation_id, payload, None, faults)

    def _worker_outcome(self, contract: ResearchContract, cursor: dict,
                        mutation_id: str, payload: StagedResult) -> dict:
        return {"research_id": contract.research_id, "dataset_id": cursor["dataset_id"],
                "mutation_id": mutation_id, "result_artifact_id": payload.agent_result.result_id,
                "result": payload.agent_result.output,
                "state_version": self.state.state_version(contract.research_id), "verdict": "PASS"}

    def _finish_worker_contracts(self, contract: ResearchContract, cursor: dict, mutation_id: str) -> None:
        for contract_id in (contract.contract_id, cursor["active_contract_ids"][0]):
            persisted, task_id = self.state.contract(contract_id)
            if self.state.task_status(task_id) != "COMPLETED":
                self.state.set_task_status(persisted.contract_id, "COMPLETED")
        prior = self.state._db.execute(
            "SELECT 1 FROM checkpoints WHERE research_id=? AND reason='committed' AND json_extract(cursor_json,'$.mutation_id')=? LIMIT 1",
            (contract.research_id, mutation_id)).fetchone()
        if prior is None:
            self.state.checkpoint(contract.research_id, "committed", {"stage": "COMPLETED",
                                  "active_task_ids": [], "active_contract_ids": [], "mutation_id": mutation_id})

    def _finish_staged_worker(self, contract: ResearchContract, cursor: dict,
                              mutation_id: str, payload: StagedResult,
                              verification_json: str | None, faults) -> dict:
        from .schemas import VerificationResult, Verdict

        if cursor.get("repair_attempt", 0) and not verification_json:
            self._repair_fault("DURING_REVALIDATION")
        if verification_json and VerificationResult.model_validate_json(verification_json).verdict == Verdict.PASS:
            verification = VerificationResult.model_validate_json(verification_json)
        else:
            verification = self.state.verify(mutation_id)
        if verification.verdict.value != "PASS":
            if self.verification_repair_enabled:
                key = f"repair_failure:{contract.contract_id}"
                saved = self.state.runtime_step(contract.research_id, key)
                if saved is None:
                    plan_step = self.state.runtime_step(contract.research_id,
                                                        f"worker_plan:{contract.contract_id}")
                    if plan_step is None or plan_step["status"] != "COMPLETED":
                        raise StateConflictError("REPAIR_FROZEN_PLAN_MISSING")
                    plan = AnalysisPlan.model_validate(plan_step["output"])
                    _, task_id = self.state.contract(contract.contract_id)
                    dataset = self.state._one("SELECT sha256 FROM datasets WHERE dataset_id=?",
                                              (cursor["dataset_id"],))
                    artifacts = ([ref.id for ref in payload.agent_result.artifact_refs]
                                 + [ref.id for ref in payload.tool_result.artifacts])
                    evidence = build_failure(
                        verification=verification, research_id=contract.research_id,
                        task_id=task_id, contract_id=contract.contract_id,
                        contract_hash=sha256(to_json(contract).encode("utf-8", errors="strict")).hexdigest(),
                        plan=plan, mutation_id=mutation_id,
                        experiment_id=payload.scientific.experiment_id if payload.scientific else None,
                        revision=cursor.get("repair_attempt", 0), dataset_hash=dataset["sha256"],
                        artifact_refs=artifacts, trace_refs=[payload.tool_request.request_id,
                                                              verification.verification_id],
                        available_usd=self.state.budget(contract.research_id)["remaining_usd"],
                        required_usd=LOCAL_RECOVERY_RESERVE_USD + (
                            self.pricing.reservation(self.provider.name,
                                                     self.models.get("experiment_coordinator", "")) or 0.0))
                    evidence.failure_id = f"VF-{uuid5(NAMESPACE_URL, mutation_id).hex}"
                    evidence.observed_value = payload.agent_result.output
                    evidence.affected_output_fields = list(numeric_fields(payload.agent_result.output))
                    evidence.tolerance_information = {"association_atol": 1e-10}
                    evidence.applicability_preconditions = ["unchanged frozen plan", "active dataset", "trusted registered tools"]
                    evidence.applicability_exclusions = ["dataset corruption", "stale state", "semantic plan revision"]
                    persist_audit_artifact(self.state, contract.research_id, contract.contract_id,
                                           evidence.failure_id, "F3P_FAILURE_EVIDENCE",
                                           evidence.model_dump(mode="json"))
                    self.state.finish_runtime_step(contract.research_id, key,
                                                   evidence.model_dump(mode="json"), contract.contract_id)
                    self.state.runtime_event(contract.research_id, "REPAIR_FAILURE_EVIDENCE",
                                             {"failure_id": evidence.failure_id, "mutation_id": mutation_id,
                                              "check_ids": evidence.check_ids})
                if faults:
                    faults.at("AFTER_FAILURE_EVIDENCE")
                self._repair_fault("AFTER_FAILURE_EVIDENCE")
                self.state.rollback(mutation_id, preserve_shared_profile=True)
            else:
                self.state.rollback(mutation_id)
            raise RuntimeFailure("VERIFICATION_FAILED", 2)
        if faults:
            faults.at("AFTER_VERIFY")
        if cursor.get("repair_attempt", 0):
            parent = self.state.runtime_step(contract.research_id, f"repair_parent:{contract.contract_id}")
            if parent is None or not set(parent["output"]["required_checks"]) <= {
                    check.check_id for check in verification.checks}:
                raise RuntimeFailure("VALIDATION_INCOMPLETE", 2)
            persist_audit_artifact(
                self.state, contract.research_id, contract.contract_id,
                f"RV-{uuid5(NAMESPACE_URL, mutation_id).hex}", "F3P_REVALIDATION",
                {"mutation_id": mutation_id, "verification": verification.model_dump(mode="json"),
                 "procedure_approved_for_reuse": False})
            self._repair_fault("BEFORE_REPAIR_COMMIT")
        self.state.commit(mutation_id)
        if faults:
            faults.at("AFTER_COMMIT")
        if cursor.get("repair_attempt", 0):
            self._repair_fault("AFTER_REPAIR_COMMIT")
        self._finish_worker_contracts(contract, cursor, mutation_id)
        return self._worker_outcome(contract, cursor, mutation_id, payload)

    async def run(self, goal: str, csv_source: str | Path, **budget_limits) -> dict:
        prepared = await self.prepare(goal, csv_source, **budget_limits)
        return await self.resume(prepared["research_id"])
