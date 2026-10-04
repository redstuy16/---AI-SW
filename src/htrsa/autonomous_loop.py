"""가설·실험·비판·후속 실험의 제한된 연구 순환."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .agent_runtime import AgentRuntime, RuntimeFailure
from .agent_policy import BudgetExceededError
from .agent_schemas import CoordinatorDecision, ManagerDecision
from .database import from_json
from .recovery import FaultInjector, ResearchRuntimeCursor
from .research_schemas import (ConclusionCandidate, CriticResult, FollowupDecision,
                               HypothesisShortlist, HypothesisStatusDecision,
                               ResearchAction, StopDecision, StopReason)
from .schemas import Constraints, ContextPolicy, ContextRef, RefType, ResearchContract, StagedResult, new_id
from .service import ActionLimitError, ContractViolationError, LoopDetectedError, StateConflictError


@dataclass(frozen=True)
class LoopConfig:
    max_actions_per_run: int = 30
    max_hypotheses: int = 5
    max_shortlist: int = 3
    max_active_hypotheses: int = 2
    max_branch_depth: int = 2
    max_followups_per_hypothesis: int = 2


class AutonomousResearchLoop(AgentRuntime):
    def __init__(self, state, provider, models=None, pricing=None, config: LoopConfig | None = None,
                 faults: FaultInjector | None = None, verified_analysis_skills_enabled: bool | None = None,
                 verification_repair_enabled: bool | None = None,
                 ridge_arithmetic_check_enabled: bool | None = None,
                 claim_evidence_provenance_enabled: bool | None = None,
                 verifier_dependency_catalog_enabled: bool | None = None):
        super().__init__(state, provider, models, pricing, verified_analysis_skills_enabled,
                         verification_repair_enabled, ridge_arithmetic_check_enabled,
                         claim_evidence_provenance_enabled, verifier_dependency_catalog_enabled)
        self.config = config or LoopConfig()
        self.faults = faults or FaultInjector()
        self.context_budget = 4096

    async def _after_hypothesis_selected(self, research_id: str, hypothesis_id: str) -> None:
        return None

    def _save_cursor(self, research_id: str, stage: str) -> None:
        db = self.state._db
        action_count = db.execute("SELECT COUNT(*) FROM research_actions WHERE research_id=?",
                                  (research_id,)).fetchone()[0]
        active = db.execute(
            "SELECT c.contract_id,c.task_id FROM contracts c WHERE c.research_id=? AND c.status IN ('ISSUED','RUNNING','WAITING_RETRY','WAITING_ESCALATION') ORDER BY c.rowid",
            (research_id,)).fetchall()
        hypotheses = [row[0] for row in db.execute(
            "SELECT hypothesis_id FROM hypotheses WHERE research_id=? AND status='ACTIVE' ORDER BY rowid",
            (research_id,))]
        branches = {row["hypothesis_id"]: row["depth"] for row in db.execute(
            "SELECT h.hypothesis_id,MAX(0,COUNT(DISTINCT x.from_id)-1) AS depth FROM hypotheses h LEFT JOIN entity_edges x ON x.research_id=h.research_id AND x.to_type='hypothesis' AND x.to_id=h.hypothesis_id AND x.from_type='experiment' AND x.edge_type='tests' WHERE h.research_id=? GROUP BY h.hypothesis_id",
            (research_id,))}
        retry = {row["contract_id"]: row["failures"] for row in db.execute(
            "SELECT c.contract_id,COUNT(ar.agent_run_id) AS failures FROM contracts c JOIN agent_runs ar ON ar.contract_id=c.contract_id WHERE c.research_id=? AND ar.status='FAILED' AND ar.provider IS NOT NULL GROUP BY c.contract_id",
            (research_id,))}
        fingerprints = {row["fingerprint"]: row["n"] for row in db.execute(
            "SELECT fingerprint,COUNT(*) AS n FROM research_actions WHERE research_id=? GROUP BY fingerprint",
            (research_id,))}
        cursor = ResearchRuntimeCursor(
            research_id=research_id, stage=stage, action_index=action_count,
            active_hypotheses=hypotheses, active_tasks=[row["task_id"] for row in active],
            active_contracts=[row["contract_id"] for row in active],
            active_experiments=[row[0] for row in db.execute(
                "SELECT experiment_id FROM experiments WHERE research_id=? AND status IN ('PENDING_VERIFICATION','VERIFIED') ORDER BY rowid",
                (research_id,))],
            pending_actions=[row[0] for row in db.execute(
                "SELECT step_key FROM runtime_steps WHERE research_id=? AND status!='COMPLETED' ORDER BY rowid",
                (research_id,))],
            retry_counters=retry, branch_depths=branches, loop_fingerprints=fingerprints,
            state_version=self.state.state_version(research_id),
            last_event_seq=db.execute("SELECT COALESCE(MAX(seq),0) FROM state_events WHERE research_id=?",
                                      (research_id,)).fetchone()[0],
            last_planning_seq=db.execute("SELECT COALESCE(MAX(seq),0) FROM planning_events WHERE research_id=?",
                                         (research_id,)).fetchone()[0])
        self.state.save_runtime_cursor(cursor)
        if getattr(self, "control_boundary", None):
            self.control_boundary()

    async def _model_once(self, contract: ResearchContract, output_type: type, step_key: str):
        previous = self.state.runtime_step(contract.research_id, step_key)
        if previous is not None and previous["status"] == "COMPLETED":
            self.state.runtime_event(contract.research_id, "ACTION_REPLAY_SKIPPED", {"step_key": step_key})
            return output_type.model_validate(previous["output"])
        _, task_id = self.state.contract(contract.contract_id)
        if self.state.task_status(task_id) == "WAITING_ESCALATION":
            self.state.runtime_event(contract.research_id, "ESCALATION_L2_RECOVERED",
                                     {"contract_id": contract.contract_id})
            raise RuntimeFailure("RETRY_EXHAUSTED", 2)
        self.state.begin_runtime_step(contract.research_id, step_key, contract.contract_id)
        output = await self._invoke(contract, output_type)
        self.state.finish_runtime_step(contract.research_id, step_key,
                                       output.model_dump(mode="json"), contract.contract_id)
        return output

    def _complete_task(self, contract_id: str) -> None:
        _, task_id = self.state.contract(contract_id)
        if self.state.task_status(task_id) != "COMPLETED":
            self.state.set_task_status(contract_id, "COMPLETED")

    def _action(self, research_id: str, action: ResearchAction, objective: str,
                *, refs: list[ContextRef] | None = None, method: str | None = None,
                details: dict | None = None, logical_key: str | None = None) -> None:
        self.state.record_action(research_id, action.value, objective, input_refs=refs,
                                 method=method, details=details,
                                 max_actions=self.config.max_actions_per_run,
                                 logical_key=logical_key)

    def _role_contract(self, research_id: str, role: str, objective: str,
                       output_schema: str, *, parent_task_id: str | None = None,
                       inputs: list[ContextRef] | None = None,
                       preserve: list[ContextRef] | None = None,
                       allowed_tools: list[str] | None = None,
                       max_tool_calls: int = 0,
                       runtime_key: str | None = None) -> tuple[ResearchContract, str]:
        if runtime_key:
            existing = self.state.runtime_contract(research_id, runtime_key)
            if existing is not None:
                contract, task_id = existing
                if (contract.assigned_role != role or contract.output_schema_id != output_schema or
                        self.state.task_status(task_id) in {"FAILED", "CANCELLED"}):
                    raise StateConflictError("persisted runtime contract conflicts with requested step")
                return existing
        contract = ResearchContract(contract_id=new_id("C"), research_id=research_id,
                                    parent_task_id=parent_task_id, task_type=output_schema,
                                    issued_by="system", assigned_role=role, objective=objective,
                                    inputs=inputs or [],
                                    context_policy=ContextPolicy(must_preserve=preserve or [],
                                                                 max_context_tokens=self.context_budget),
                                    allowed_tools=allowed_tools or [],
                                    constraints=Constraints(max_tool_calls=max_tool_calls,
                                                            max_retries=1, max_runtime_sec=180,
                                                            max_cost_usd=self.state.budget(research_id)["remaining_usd"]),
                                    output_schema_id=output_schema)
        return contract, self._issue(contract, runtime_key=runtime_key)

    def _science_ids(self, mutation_id: str) -> tuple[str, str]:
        row = self.state._one("SELECT payload_json FROM staged_mutations WHERE mutation_id=?", (mutation_id,))
        science = StagedResult.model_validate_json(row["payload_json"]).scientific
        if science is None:
            raise RuntimeFailure("VERIFICATION_FAILED", 2)
        return science.experiment_id, science.evidence_id

    def _link_experiment(self, research_id: str, hypothesis_id: str, dataset_id: str,
                         experiment_id: str, evidence_id: str) -> None:
        hypothesis = ContextRef(type=RefType.hypothesis, id=hypothesis_id)
        experiment = ContextRef(type=RefType.experiment, id=experiment_id)
        evidence = ContextRef(type=RefType.evidence, id=evidence_id)
        self.state.add_entity_edge(research_id, experiment, "tests", hypothesis)
        self.state.add_entity_edge(research_id, experiment, "uses_dataset",
                                   ContextRef(type=RefType.dataset, id=dataset_id))
        self.state.add_entity_edge(research_id, hypothesis, "supports", evidence)
        self.state.add_entity_edge(research_id, evidence, "generated_from", experiment)

    async def _critic(self, research_id: str, experiment_id: str, evidence_id: str,
                      hypothesis_id: str, decision_ref: ContextRef,
                      branch_index: int) -> CriticResult:
        refs = [ContextRef(type=RefType.experiment, id=experiment_id),
                ContextRef(type=RefType.evidence, id=evidence_id),
                ContextRef(type=RefType.hypothesis, id=hypothesis_id)]
        contract, _ = self._role_contract(research_id, "verification_coordinator",
                                          f"Critique verified experiment {experiment_id}", "CriticResult",
                                          inputs=refs, preserve=[decision_ref, refs[2]],
                                          runtime_key=f"auto:critic:{branch_index}")
        existing = self.state._db.execute(
            "SELECT output_json FROM critic_reviews WHERE research_id=? AND experiment_id=?",
            (research_id, experiment_id)).fetchone()
        if existing is not None:
            review = CriticResult.model_validate(from_json(existing["output_json"]))
            self.state.runtime_event(research_id, "ACTION_REPLAY_SKIPPED",
                                     {"step_key": f"critic:{branch_index}"})
        else:
            review = await self._model_once(contract, CriticResult, f"critic_output:{branch_index}")
            self.state.record_critic_review(research_id, experiment_id, contract.contract_id, review)
            self.faults.at("AFTER_CRITIC")
        self._complete_task(contract.contract_id)
        self._action(research_id, ResearchAction.CRITIQUE_RESULT, experiment_id,
                     refs=refs, details={"verdict": review.verdict},
                     logical_key=f"critic:{branch_index}")
        self._save_cursor(research_id, f"CRITIC_{branch_index}_READY")
        return review

    async def _experiment(self, research_id: str, hypothesis_id: str, dataset_id: str,
                          profile_id: str, decision_ref: ContextRef,
                          *, followup_method: str | None = None,
                          objective: str = "Test the active hypothesis",
                          branch_index: int = 0) -> dict | None:
        status = self.state._one("SELECT status FROM hypotheses WHERE hypothesis_id=? AND research_id=?",
                                 (hypothesis_id, research_id))["status"]
        existing_worker = self.state.runtime_contract(research_id, f"auto:worker:{branch_index}")
        repair_cursor = {}
        if self.verification_repair_enabled and existing_worker is not None:
            for _ in range(2):
                next_step = self.state.runtime_step(
                    research_id, f"repair_next:{existing_worker[0].contract_id}")
                if next_step is None:
                    break
                repair_cursor = next_step["output"]
                existing_worker = self.state.contract(repair_cursor["active_contract_ids"][1])
        if status != "ACTIVE" and not (status == "SUPPORTED" and existing_worker is not None and
                                       self.state.committed_contract_result(research_id, existing_worker[0].contract_id)):
            raise ContractViolationError("experiment requires an active hypothesis")
        hypothesis_ref = ContextRef(type=RefType.hypothesis, id=hypothesis_id)
        refs = [hypothesis_ref, ContextRef(type=RefType.dataset, id=dataset_id),
                ContextRef(type=RefType.artifact, id=profile_id)]
        coordinator, coordinator_task = self._role_contract(
            research_id, "experiment_coordinator", objective,
            "FollowupDecision" if followup_method else "CoordinatorDecision",
            inputs=refs, preserve=[decision_ref, hypothesis_ref],
            runtime_key=f"auto:coordinator:{branch_index}")
        if followup_method:
            approval: FollowupDecision = await self._model_once(
                coordinator, FollowupDecision, f"followup_decision:{branch_index}")
            if not approval.approve:
                self._complete_task(coordinator.contract_id)
                self.state.runtime_event(research_id, "FOLLOWUP_DECLINED", {"hypothesis_id": hypothesis_id})
                return None
            if approval.method != followup_method:
                raise ContractViolationError("approved follow-up method differs from critic request")
            self._action(research_id, ResearchAction.FOLLOWUP_EXPERIMENT, approval.objective,
                         refs=[hypothesis_ref], method=followup_method,
                         logical_key=f"followup:{branch_index}")
            draft = CoordinatorDecision(decision_type="DELEGATE", objective=approval.objective,
                                        rationale=approval.rationale, assigned_role="analysis_planner_worker",
                                        input_refs=refs[1:],
                                        allowed_tools=["stats.run", "visualization.render", "evidence.record"],
                                        max_tool_calls=3, max_retries=1, max_runtime_sec=120,
                                        max_cost_usd=min(0.5, self.state.budget(research_id)["remaining_usd"]))
        else:
            draft: CoordinatorDecision = await self._model_once(
                coordinator, CoordinatorDecision, f"coordinator_decision:{branch_index}")
            if draft.decision_type != "DELEGATE":
                raise RuntimeFailure("COORDINATOR_CONFLICT", 3)
        if existing_worker is None:
            worker = self._worker_contract(coordinator, coordinator_task, draft, dataset_id, profile_id,
                                           additional_refs=[hypothesis_ref, decision_ref])
            worker_task = self._issue(worker, runtime_key=f"auto:worker:{branch_index}")
            if branch_index:
                self.faults.at("AFTER_FOLLOWUP_CONTRACT")
        else:
            worker, worker_task = existing_worker
        self._action(research_id, ResearchAction.DESIGN_EXPERIMENT, objective,
                     refs=refs, method=followup_method,
                     details={"worker_contract_id": worker.contract_id},
                     logical_key=f"design:{branch_index}")
        cursor = {**repair_cursor, "stage": "WORKER_READY", "active_task_ids": [coordinator_task, worker_task],
                  "active_contract_ids": [coordinator.contract_id, worker.contract_id],
                  "dataset_id": dataset_id, "profile_artifact_id": profile_id}
        self.state.checkpoint(research_id, "autonomous worker ready", cursor)
        self._save_cursor(research_id, f"WORKER_{branch_index}_READY")
        try:
            if self.verification_repair_enabled and self.state.runtime_step(
                    research_id, f"repair_failure:{worker.contract_id}") is not None:
                outcome = await self._repair_failed_worker(worker, worker_task, cursor)
            else:
                outcome = await self._run_worker(
                    worker, worker_task, cursor, expected_method=followup_method,
                    recovery_key=f"worker_plan:{branch_index}", faults=self.faults)
        except RuntimeFailure as exc:
            if not self.verification_repair_enabled or exc.code != "VERIFICATION_FAILED":
                raise
            outcome = await self._repair_failed_worker(worker, worker_task, cursor)
        experiment_id, evidence_id = self._science_ids(outcome["mutation_id"])
        if self.state._one("SELECT status FROM experiments WHERE experiment_id=?", (experiment_id,))["status"] != "VERIFIED":
            raise StateConflictError("committed experiment was invalidated")
        self._link_experiment(research_id, hypothesis_id, dataset_id, experiment_id, evidence_id)
        method = outcome["result"]["method"]
        self._action(research_id, ResearchAction.RUN_EXPERIMENT, experiment_id,
                     refs=refs, method=method, details={"mutation_id": outcome["mutation_id"]},
                     logical_key=f"run:{branch_index}")
        self._action(research_id, ResearchAction.VERIFY_RESULT, experiment_id,
                     refs=[ContextRef(type=RefType.evidence, id=evidence_id)], method=method,
                     details={"verdict": outcome["verdict"]},
                     logical_key=f"verify:{branch_index}")
        self._save_cursor(research_id, f"EXPERIMENT_{branch_index}_READY")
        return {**outcome, "experiment_id": experiment_id, "evidence_id": evidence_id}

    async def run(self, goal: str, csv_source: str | Path, *, target_usd: float = 0.25,
                  soft_limit_usd: float = 0.75, hard_limit_usd: float = 1.0) -> dict:
        research_id = self.state.create_research(goal)
        self.state.workspace.prepare(research_id)
        self.state.configure_budget(research_id, target_usd, soft_limit_usd, hard_limit_usd)
        self.state.finish_runtime_step(research_id, "verified_analysis_skills_config", self._skill_config())
        self.state.finish_runtime_step(research_id, "verification_repair_config", self._repair_config())
        self.state.finish_runtime_step(research_id, "source", {"csv_source": str(csv_source), "goal": goal})
        self._save_cursor(research_id, "START")
        return await self._run_with_stops(research_id, csv_source, goal)

    async def resume(self, research_id: str) -> dict:
        self._check_skill_config(research_id)
        self._check_repair_config(research_id)
        self.state.cycle5.validate_completed(research_id)
        row = self.state._one("SELECT goal,run_status,stop_reason FROM research_runs WHERE research_id=?",
                              (research_id,))
        if row["run_status"] != "ACTIVE":
            return {"research_id": research_id, "already_terminal": True,
                    "stop_reason": row["stop_reason"], "outcome": "ALREADY_TERMINAL"}
        source = self.state.runtime_step(research_id, "source")
        if source is None or source["status"] != "COMPLETED":
            raise StateConflictError("autonomous source record is missing")
        self._reconcile(research_id)
        return await self._run_with_stops(research_id, source["output"]["csv_source"], row["goal"])

    def _reconcile(self, research_id: str) -> None:
        self.state.runtime_event(research_id, "PROCESS_RECOVERY_STARTED")
        cursor = self.state.load_runtime_cursor(research_id)
        if cursor is None:
            self.state.runtime_event(research_id, "RESUME_STATE_CONFLICT", {"reason": "missing cursor"})
            raise StateConflictError("autonomous runtime cursor is missing")
        self.state.runtime_event(research_id, "CHECKPOINT_LOADED", {"stage": cursor.stage})
        current = self.state.state_version(research_id)
        if cursor.state_version > current:
            self.state.runtime_event(research_id, "RESUME_STATE_CONFLICT", {"reason": "version regression"})
            raise StateConflictError("RESUME_STATE_CONFLICT: version regression")
        db = self.state._db
        scientific = db.execute(
            "SELECT se.state_version,se.mutation_id,sm.status,c.runtime_key,e.status AS experiment_status FROM state_events se LEFT JOIN staged_mutations sm ON sm.mutation_id=se.mutation_id LEFT JOIN contracts c ON c.contract_id=sm.contract_id LEFT JOIN experiments e ON e.experiment_id=se.entity_id WHERE se.research_id=? AND se.seq>? ORDER BY se.seq",
            (research_id, cursor.last_event_seq)).fetchall()
        planning = db.execute(
            "SELECT state_version,event_type,payload_json FROM planning_events WHERE research_id=? AND seq>? ORDER BY seq",
            (research_id, cursor.last_planning_seq)).fetchall()
        allowed = {"QUESTION_REFINED", "DECISION_RECORDED", "HYPOTHESIS_SHORTLIST",
                   "HYPOTHESIS_STATUS", "EXPERIMENT_INVALIDATED", "RESEARCH_STOPPED",
                   "SOURCE_DISCOVERED", "SOURCE_DEDUPLICATED", "SOURCE_SCREENED",
                   "SOURCE_EVIDENCE_EXTRACTED", "SOURCE_INVALIDATED",
                    "LITERATURE_EVIDENCE_VERIFIED", "LITERATURE_SYNTHESIZED", "SOURCE_DOCUMENT_SAVED"}
        for event in planning:
            if event['event_type'] != 'SOURCE_DOCUMENT_SAVED':
                continue
            payload = from_json(event['payload_json'])
            if payload.get('status') not in {'DOWNLOADED', 'READY', 'FAILED'}:
                raise StateConflictError('SOURCE_DOCUMENT_RECOVERY_CONFLICT')
            for key, kind in (('artifacts', 'SOURCE_PDF'), ('text', 'SOURCE_TEXT')):
                item = payload.get(key)
                if item:
                    artifact = self.state.file_artifact(item['artifact_id'], research_id)
                    if (artifact['artifact_type'] != kind or artifact['sha256'] != item['sha256']
                            or artifact['relative_path'] != item['relative_path']):
                        raise StateConflictError('SOURCE_DOCUMENT_RECOVERY_CONFLICT')
        if self.state.cycle5.enabled(research_id):
            self.state.cycle5.validate_planning_events(research_id, planning)
            allowed.update({"CYCLE5_REVISION", "CYCLE5_TRANSFORM_REPAIRED"})
        versions = [row["state_version"] for row in scientific] + [row["state_version"] for row in planning]
        conflict = (sorted(versions) != list(range(cursor.state_version + 1, current + 1)) or
                    any(row["status"] != "COMMITTED" or not (row["runtime_key"] or "").startswith("auto:worker:") or
                        row["experiment_status"] not in {"VERIFIED", "INVALIDATED"} for row in scientific) or
                    any(row["event_type"] not in allowed for row in planning))
        if conflict:
            self.state.runtime_event(research_id, "RESUME_STATE_CONFLICT", {"reason": "unexplained state event"})
            raise StateConflictError("RESUME_STATE_CONFLICT: unexplained state event")
        for row in scientific:
            self.state.runtime_event(research_id, "COMMITTED_ACTION_RECONCILED",
                                     {"mutation_id": row["mutation_id"]})
        for row in db.execute("SELECT step_key FROM runtime_steps WHERE research_id=? AND status='RUNNING'",
                              (research_id,)):
            self.state.runtime_event(research_id, "ORPHAN_ACTION_FOUND", {"step_key": row["step_key"]})
        for row in db.execute("SELECT agent_run_id FROM agent_runs WHERE research_id=? AND status='RUNNING' AND provider IS NOT NULL",
                              (research_id,)).fetchall():
            self.state.finish_model_run(row["agent_run_id"], "FAILED", {}, "PROCESS_INTERRUPTED")
        self._save_cursor(research_id, "RECONCILED")
        self.state.runtime_event(research_id, "RUNTIME_RECONCILED", {"state_version": current})

    async def _run_with_stops(self, research_id: str, csv_source: str | Path, goal: str) -> dict:
        try:
            return await self._run_bounded(research_id, csv_source, goal)
        except ActionLimitError:
            self.state.stop_research(research_id, StopReason.ACTION_LIMIT_REACHED)
        except LoopDetectedError:
            self.state.stop_research(research_id, StopReason.FATAL_ERROR)
        except BudgetExceededError:
            self.state.stop_research(research_id, StopReason.BUDGET_EXHAUSTED)
        except StateConflictError as exc:
            if str(exc) != "committed experiment was invalidated":
                raise
            self.state.runtime_event(research_id, "INVALIDATED_EVIDENCE_REJECTED")
            self.state.stop_research(research_id, StopReason.INSUFFICIENT_DATA)
        except RuntimeFailure:
            self.state.stop_research(research_id, StopReason.FATAL_ERROR)
            self._save_cursor(research_id, "STOPPED")
            raise
        result = {"research_id": research_id, "stop_reason": self.state._one(
            "SELECT stop_reason FROM research_runs WHERE research_id=?", (research_id,))["stop_reason"]}
        self._save_cursor(research_id, "STOPPED")
        return result

    async def _run_bounded(self, research_id: str, csv_source: str | Path, goal: str) -> dict:
        objective = goal
        if getattr(self, "approved_search_query", None):
            objective += "\n승인된 공개 검색어: " + self.approved_search_query + "\nsearch_queries에 같은 주제의 영어 검색 표현을 포함한다. 개인정보나 다른 주제를 추가하지 않는다."
        manager, manager_task = self._role_contract(research_id, "manager", objective, "ManagerDecision",
                                                    runtime_key="auto:manager:initial")
        decision: ManagerDecision = await self._model_once(manager, ManagerDecision, "manager_decision")
        if decision.decision_type not in {"INITIAL_PLAN", "DELEGATE"}:
            raise RuntimeFailure("CONTRACT_INVALID", 2)
        saved_question = self.state._one("SELECT research_question FROM research_runs WHERE research_id=?",
                                         (research_id,))["research_question"]
        if saved_question is None:
            self.state.set_research_question(research_id, decision.research_question)
        elif saved_question != decision.research_question:
            raise StateConflictError("persisted research question differs from manager decision")
        prior_decision = self.state._db.execute(
            "SELECT decision_id,payload_json FROM decisions WHERE research_id=? ORDER BY rowid LIMIT 1",
            (research_id,)).fetchone()
        if prior_decision is None:
            decision_id = self.state.record_research_decision(research_id, decision.rationale)
        else:
            if from_json(prior_decision["payload_json"]).get("rationale") != decision.rationale:
                raise StateConflictError("persisted research decision changed")
            decision_id = prior_decision["decision_id"]
        decision_ref = ContextRef(type=RefType.decision, id=decision_id)
        self._complete_task(manager.contract_id)
        self._action(research_id, ResearchAction.REFINE_QUESTION, decision.research_question,
                     refs=[decision_ref], logical_key="refine_question")
        self._save_cursor(research_id, "QUESTION_READY")

        acquire = getattr(self, "evidence_acquisition", None)
        self.planned_search_queries = decision.search_queries
        supported = await acquire() if acquire is not None else True
        if csv_source is None and getattr(self, "report_writer", None):
            self._save_cursor(research_id, "LITERATURE_READY")
            record = await self.report_writer()
            draft = record.get("draft", {})
            reason = StopReason.INSUFFICIENT_DATA
            if supported and record.get("status") == "READY":
                if draft.get("claims"):
                    reason = StopReason.LITERATURE_REVIEW_COMPLETED
                elif len(draft.get("procedure", [])) >= 2 and len(draft.get("variables", [])) >= 2 and draft.get("measurement"):
                    reason = StopReason.LITERATURE_DESIGN_COMPLETED
                    self.state.runtime_event(research_id, "RESEARCH_INPUT_LIMITATION", {"reason": "LITERATURE_EVIDENCE_MISSING", "design_only": True})
            self.state.runtime_event(research_id, "LITERATURE_DESIGN_READY", {"report_status": record["status"], "measured": False})
            if not supported:
                self.state.runtime_event(research_id, "RESEARCH_INPUT_LIMITATION", {"reason": getattr(self, "input_limitation", "LITERATURE_EVIDENCE_MISSING")})
            self.state.stop_research(research_id, reason)
            self._save_cursor(research_id, "STOPPED")
            return {"research_id": research_id, "stop_reason": reason.value}
        if not supported or csv_source is None:
            self.state.runtime_event(research_id, "RESEARCH_INPUT_LIMITATION",
                {"reason": getattr(self, "input_limitation", "LITERATURE_EVIDENCE_MISSING") if not supported else "ANALYSIS_DATA_REQUIRED"})
            self.state.stop_research(research_id, StopReason.INSUFFICIENT_DATA)
            self._save_cursor(research_id, "STOPPED")
            return {"research_id": research_id, "stop_reason": StopReason.INSUFFICIENT_DATA.value}

        shortlist_contract, _ = self._role_contract(
            research_id, "manager", "Generate up to three testable hypotheses for the research question",
            "HypothesisShortlist", parent_task_id=manager_task, inputs=[decision_ref], preserve=[decision_ref],
            runtime_key="auto:manager:shortlist")
        shortlist: HypothesisShortlist = await self._model_once(
            shortlist_contract, HypothesisShortlist, "hypothesis_shortlist")
        previous_hypotheses = self.state._db.execute(
            "SELECT hypothesis_id,statement FROM hypotheses WHERE research_id=? ORDER BY rowid",
            (research_id,)).fetchall()
        if previous_hypotheses:
            if [row["statement"] for row in previous_hypotheses] != [item.statement for item in shortlist.hypotheses]:
                raise StateConflictError("persisted hypothesis shortlist differs from manager output")
            hypothesis_ids = [row["hypothesis_id"] for row in previous_hypotheses]
        else:
            hypothesis_ids = self.state.shortlist_hypotheses(research_id, shortlist, created_by="manager",
                                                            max_shortlist=self.config.max_shortlist,
                                                            hard_cap=self.config.max_hypotheses)
        if self.state._db.execute(
                "SELECT 1 FROM milestone_summaries WHERE research_id=? AND kind='hypothesis_shortlist'",
                (research_id,)).fetchone() is None:
            self.state.record_milestone_summary(
                research_id, "hypothesis_shortlist", shortlist.rationale,
                [ContextRef(type=RefType.hypothesis, id=identity) for identity in hypothesis_ids],
                generated_by="manager")
        self._complete_task(shortlist_contract.contract_id)
        self._action(research_id, ResearchAction.GENERATE_HYPOTHESES, decision.research_question,
                     refs=[decision_ref], details={"hypothesis_ids": hypothesis_ids},
                     logical_key="generate_hypotheses")
        self._save_cursor(research_id, "SHORTLIST_READY")
        ranked = sorted(hypothesis_ids, key=lambda identity: -from_json(self.state._one(
            "SELECT score_json FROM hypotheses WHERE hypothesis_id=?", (identity,))["score_json"])["aggregate"])
        hypothesis_id = ranked[0]
        hypothesis_status = self.state._one("SELECT status FROM hypotheses WHERE hypothesis_id=?",
                                            (hypothesis_id,))["status"]
        if hypothesis_status == "SHORTLISTED":
            self.state.set_hypothesis_status(research_id, hypothesis_id, "ACTIVE", decided_by="manager",
                                             rationale="highest deterministic planning score",
                                             max_active=self.config.max_active_hypotheses)
        elif hypothesis_status not in {"ACTIVE", "SUPPORTED", "WEAKENED", "REJECTED", "INCONCLUSIVE"}:
            raise StateConflictError("selected hypothesis has an invalid persisted status")
        hypothesis_ref = ContextRef(type=RefType.hypothesis, id=hypothesis_id)
        self._action(research_id, ResearchAction.SELECT_HYPOTHESIS, hypothesis_id,
                     refs=[hypothesis_ref], logical_key="select_hypothesis")
        self._save_cursor(research_id, "HYPOTHESIS_READY")
        await self._after_hypothesis_selected(research_id, hypothesis_id)

        intake, intake_task = self._role_contract(research_id, "experiment_coordinator", "Import and profile the dataset",
                                                  "DatasetIntake", parent_task_id=manager_task,
                                                  inputs=[hypothesis_ref], preserve=[decision_ref, hypothesis_ref],
                                                  allowed_tools=["data.import", "data.profile"], max_tool_calls=2,
                                                  runtime_key="auto:intake")
        registry = self._registry(intake.contract_id)
        _, imported = self._dispatch(registry, intake, intake_task, "data.import",
                                     {"source_path": str(csv_source), "research_id": research_id})
        self.faults.at("AFTER_INTAKE_IMPORT")
        dataset_id = imported.result["dataset_id"]
        _, profile = self._dispatch(registry, intake, intake_task, "data.profile",
                                    {"dataset_id": dataset_id})
        self.faults.at("AFTER_INTAKE_PROFILE")
        profile_id = profile.result["artifact_id"]
        self._complete_task(intake.contract_id)
        self._save_cursor(research_id, "INTAKE_READY")

        outcomes = []
        first = await self._experiment(research_id, hypothesis_id, dataset_id, profile_id, decision_ref,
                                       branch_index=0)
        if first is None:
            raise RuntimeFailure("CONTRACT_INVALID", 2)
        outcomes.append(first)
        review = await self._critic(research_id, first["experiment_id"], first["evidence_id"],
                                    hypothesis_id, decision_ref, 0)
        followups = 0
        while review.verdict == "FOLLOW_UP_REQUIRED" and review.requested_followups:
            admission = getattr(self, "optional_admission", None)
            if admission is not None and not admission("followups"):
                self.state.runtime_event(research_id, "OPTIONAL_FOLLOWUP_BUDGET_REDUCED", {"mandatory_critic_preserved": True})
                break
            if followups >= self.config.max_followups_per_hypothesis or followups + 1 >= self.config.max_branch_depth:
                break
            request = review.requested_followups[0]
            if request.hypothesis_id != hypothesis_id:
                raise ContractViolationError("critic follow-up targets another hypothesis")
            if request.method == outcomes[-1]["result"]["method"]:
                self.state.runtime_event(research_id, "LOOP_DETECTED", {"method": request.method})
                raise LoopDetectedError(request.method)
            self._action(research_id, ResearchAction.REPLAN, request.rationale,
                         refs=[hypothesis_ref], method=request.method,
                         logical_key=f"replan:{followups + 1}")
            self.faults.at("AFTER_REPLAN")
            next_result = await self._experiment(research_id, hypothesis_id, dataset_id, profile_id,
                                                 decision_ref, followup_method=request.method,
                                                 objective=request.rationale,
                                                 branch_index=followups + 1)
            if next_result is None:
                break
            outcomes.append(next_result)
            followups += 1
            review = await self._critic(research_id, next_result["experiment_id"],
                                        next_result["evidence_id"], hypothesis_id, decision_ref, followups)

        evidence_refs = [ContextRef(type=RefType.evidence, id=item["evidence_id"]) for item in outcomes]
        if self.state._db.execute(
                "SELECT 1 FROM milestone_summaries WHERE research_id=? AND kind='experiment_branch'",
                (research_id,)).fetchone() is None:
            self.state.record_milestone_summary(
                research_id, "experiment_branch",
                "Verified methods: " + ", ".join(item["result"]["method"] for item in outcomes),
                evidence_refs, generated_by="system")
        status_contract, _ = self._role_contract(research_id, "experiment_coordinator",
                                                 "Synthesize verified evidence for the active hypothesis",
                                                 "HypothesisStatusDecision", inputs=[hypothesis_ref] + evidence_refs,
                                                 preserve=[decision_ref, hypothesis_ref],
                                                 runtime_key="auto:status")
        status: HypothesisStatusDecision = await self._model_once(
            status_contract, HypothesisStatusDecision, "hypothesis_status_decision")
        permitted = {(ref.type.value, ref.id) for ref in evidence_refs}
        submitted = {(ref.type.value, ref.id) for ref in status.evidence_refs}
        if status.hypothesis_id != hypothesis_id or not submitted <= permitted:
            raise ContractViolationError("hypothesis status references unrelated evidence")
        saved_status = self.state._one("SELECT status FROM hypotheses WHERE hypothesis_id=?",
                                       (hypothesis_id,))["status"]
        if saved_status == "ACTIVE":
            self.state.set_hypothesis_status(research_id, hypothesis_id, status.new_status,
                                             decided_by="experiment_coordinator", rationale=status.rationale,
                                             evidence_refs=status.evidence_refs)
            self.faults.at("AFTER_HYPOTHESIS")
        elif saved_status != status.new_status:
            raise StateConflictError("persisted hypothesis transition conflicts with decision")
        self._complete_task(status_contract.contract_id)
        self._action(research_id, ResearchAction.UPDATE_HYPOTHESIS, hypothesis_id,
                     refs=status.evidence_refs, details={"status": status.new_status},
                     logical_key="update_hypothesis")
        self._save_cursor(research_id, "HYPOTHESIS_UPDATED")

        stop_contract, _ = self._role_contract(research_id, "manager", "Decide whether the verified research goal is answered",
                                               "StopDecision", inputs=[hypothesis_ref] + evidence_refs,
                                               preserve=[decision_ref, hypothesis_ref],
                                               runtime_key="auto:stop")
        stop: StopDecision = await self._model_once(stop_contract, StopDecision, "stop_decision")
        self._complete_task(stop_contract.contract_id)
        if not stop.stop:
            stop_reason = StopReason.UNRESOLVED_VERIFICATION
            conclusion = None
        elif (stop.reason == "GOAL_ANSWERED" and status.new_status == "SUPPORTED"
              and review.verdict in {"ACCEPT", "ACCEPT_WITH_LIMITATION"}
              and not any(issue.severity == "HIGH" for issue in review.issues) and outcomes):
            stop_reason = StopReason.GOAL_ANSWERED
            conclusion = ConclusionCandidate(
                statement=f"Verified experiments support: {self.state._one('SELECT statement FROM hypotheses WHERE hypothesis_id=?', (hypothesis_id,))['statement']}",
                support_level="MODERATE" if review.conclusion_strength in {"MODERATE", "STRONG"} else "WEAK",
                evidence_refs=evidence_refs,
                unresolved_questions=review.alternative_explanations + review.confounders)
        else:
            stop_reason = StopReason.INSUFFICIENT_DATA if stop.reason == "INSUFFICIENT_DATA" else StopReason.UNRESOLVED_VERIFICATION
            conclusion = None
        self._action(research_id, ResearchAction.STOP, stop_reason.value,
                     refs=evidence_refs, details={"reason": stop_reason.value},
                     logical_key="stop")
        version = self.state.stop_research(research_id, stop_reason, conclusion)
        self._save_cursor(research_id, "STOPPED")
        self.state.runtime_event(research_id, "RECOVERY_COMPLETED", {"state_version": version})
        return {"research_id": research_id, "hypothesis_id": hypothesis_id,
                "experiment_ids": [item["experiment_id"] for item in outcomes],
                "evidence_ids": [item["evidence_id"] for item in outcomes],
                "first_result": outcomes[0]["result"], "last_result": outcomes[-1]["result"],
                "stop_reason": stop_reason.value, "conclusion": conclusion.model_dump(mode="json") if conclusion else None,
                "state_version": version,
                "action_count": self.state._db.execute("SELECT COUNT(*) FROM research_actions WHERE research_id=?",
                                                        (research_id,)).fetchone()[0]}
