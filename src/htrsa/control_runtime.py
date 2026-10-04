"""확정된 정책과 지속되는 요청 원장을 사용하는 기존 연구 실행기."""
from __future__ import annotations

import asyncio
from hashlib import sha256
from datetime import datetime
import json
import os
from pathlib import Path
import time

import httpx
from pydantic import BaseModel

from .agent_policy import BudgetExceededError
from .agent_runtime import RuntimeFailure
from .cycle5 import SemanticReviewRequired
from .autonomous_loop import AutonomousResearchLoop, LoopConfig
from .control_plane import (Connection, ControlBoundary, ControlError, ControlStore, Credentials,
                            DEPTHS, ModelProfile, PinnedTransport, admitted_cost, classify_http, safe_source, resolved_depth)
from .database import initialize, to_json
from .final_report import export_final_report
from .providers.base import ModelProviderError, ModelRunResult
from .service import StateService
from .storage import Workspace, sha256_file


class RoutedGateway:
    """GUI·설정의 모든 모델 요청을 중개한다. 불확실한 사용량·수락 상태는 원장에 남기고 자동 재요청하지 않는다."""
    name = "control_broker"

    def __init__(self, store: ControlStore, credentials: Credentials, rid: str, snapshot: dict,
                 *, purpose="research", client_factory=None, observer=None):
        self.store, self.credentials, self.rid, self.snapshot = store, credentials, rid, snapshot
        self.purpose, self.client_factory = purpose, client_factory
        self.dispatch_count = 0
        self.observer = observer

    def observe(self, phase, **value):
        if self.observer is not None:
            self.observer(phase, value)

    async def run_structured(self, *, role, instructions, input_text, output_type, model, metadata=None):
        from .providers.normalized import GenerationRequest
        profile = ModelProfile.model_validate(self.snapshot["models"][role])
        request = GenerationRequest(request_id="structured", research_id=self.rid, role=role,
            model_profile_id=profile.profile_id, system_instructions=instructions, input_text=input_text,
            structured_output_schema=output_type.model_json_schema(), max_output_tokens=profile.output_limit,
            reasoning_policy=profile.reasoning_policy, temperature=profile.temperature, top_p=profile.top_p,
            stop=profile.stop, seed=profile.seed, stream=profile.stream, store_preference=profile.store_preference,
            timeout=profile.timeout_sec, metadata=metadata or {}, data_egress_policy=self.snapshot["egress"])
        result, output, dispatched = await self.generate(request, output_type=output_type)
        return ModelRunResult(provider=self.name, model=result.model_id, output=output, request_count=dispatched,
            input_tokens=result.usage.input_tokens or 0, output_tokens=result.usage.output_tokens or 0,
            latency_ms=result.latency_ms, provider_run_id=result.response_id)

    async def generate(self, request, *, output_type=None):
        from .resource_policy import low_spec
        from .resource_queue import ResourcePool, resource_key
        profile = ModelProfile.model_validate(self.snapshot['models'][request.role])
        connection = Connection.model_validate(self.snapshot['connections'][profile.connection_id])
        network_limit = 1 if low_spec(self.store) else 4
        shared_limits = {'remote-network':network_limit} if connection.endpoint_class!='loopback' else {'local-inference':1}
        async with ResourcePool(self.store).lease(self.rid, request.role, resource_key(connection),
                capacity=1, purpose=self.purpose, timeout=min(request.timeout, profile.timeout_sec),shared_limits=shared_limits) as job:
            result = await self._generate(request, output_type=output_type)
            job['resolved_model_id'] = result[0].model_id
            return result

    async def _generate(self, request, *, output_type=None):
        from decimal import Decimal
        from .providers.native import REGISTRY, compact
        from .providers.normalized import GenerationError, GenerationResult, GenerationUsage
        profile = ModelProfile.model_validate(self.snapshot["models"][request.role])
        connection = Connection.model_validate(self.snapshot["connections"][profile.connection_id])
        if not connection.enabled or not connection.destination_approved or self.snapshot["egress"] == "none":
            raise ControlError("EGRESS_BLOCKED")
        if self.snapshot.get("question_only") and request.role != "manager":
            raise ControlError("DATA_EGRESS_APPROVAL_REQUIRED")
        from .product_policy import workflow_compatible
        compatible = workflow_compatible(profile) if self.snapshot.get("settings_version", 1) >= 2 else profile.capability_status == "supported"
        if not compatible and self.purpose != "settings_smoke":
            raise ControlError("CAPABILITY_NOT_VALIDATED")
        current = Connection.model_validate(self.store.config("connection", connection.connection_id))
        if current != connection:
            raise ControlError("CONNECTION_CHANGED_REAPPROVAL_REQUIRED")
        key = self.credentials.get(connection.credential_env_name)
        needs_key = connection.adapter_id != "openai_compatible" or connection.auth_strategy in {"bearer", "api_key"} or (connection.auth_strategy == "auto" and connection.endpoint_class == "cloud")
        if not key and needs_key: raise ControlError("CREDENTIAL_UNCONFIGURED")
        adapter = REGISTRY.get(connection.adapter_id)
        request = request.model_copy(update={"data_egress_policy": self.snapshot["egress"]})
        if request.max_output_tokens > profile.output_limit: raise ControlError("OUTPUT_LIMIT")
        url, payload, mapping = adapter.serialize(request, profile, connection)
        count = len(compact(payload).encode("utf-8", errors="strict")) + 4096
        if profile.max_input_tokens is not None and count > profile.max_input_tokens: raise ControlError("CONTEXT_LIMIT_BLOCKED")
        references = [c.get("credential_env_name") for c in self.store.configs("connection")]
        secrets = self.credentials.active_secrets(references)
        if any(value in compact(payload) for value in secrets): raise ControlError("SECRET_IN_MODEL_INPUT")
        excluded = {"budget_reservation_id"}
        if not request.native_schema_strict: excluded.add("native_schema_strict")
        if not request.explicit_parallel_tool_control: excluded.add("explicit_parallel_tool_control")
        request_key = sha256(compact({"rid": self.rid, "request": request.model_dump(mode="json", exclude=excluded),
            "profile": profile.model_dump(mode="json"), "adapter_version": adapter.version}).encode("utf-8", errors="strict")).hexdigest()
        cached = self.store.db.execute("SELECT * FROM control_model_cache WHERE key=?", (request_key,)).fetchone()
        if cached:
            if cached["error"]: raise ModelProviderError(cached["error"], retryable=False)
            value = json.loads(cached["output_json"])
            if output_type:
                output = output_type.model_validate(value)
                result = GenerationResult(model_id=cached["model"], output_text=compact(value), structured_output=value,
                    usage=GenerationUsage(input_tokens=0, output_tokens=0), provider_metadata={"replay": True})
            else:
                result, output = GenerationResult.model_validate(value), None
                result.usage = GenerationUsage(input_tokens=0, output_tokens=0)
                result.provider_metadata["replay"] = True
                if result.tool_calls: raise ControlError("PROVIDER_CONTINUITY_UNAVAILABLE")
            return result, output, 0
        if profile.local_api_unmetered and connection.endpoint_class == "loopback":
            if count > profile.input_byte_limit or count + request.max_output_tokens > profile.context_limit: raise ControlError("CONTEXT_LIMIT_BLOCKED")
            bound = 0
        else: bound = admitted_cost(profile, count)
        timeout = min(profile.timeout_sec, request.timeout)
        if self.purpose == "research":
            run = self.store.db.execute("SELECT started_at FROM control_runs WHERE research_id=?", (self.rid,)).fetchone()
            if run and run[0]:
                remaining = snapshot_remaining(self.snapshot, run[0])
                if remaining <= 0: raise ControlError("TIME_LIMIT")
                timeout = min(timeout, remaining)
        effective_profile = profile.model_copy(update={"timeout_sec": timeout})
        self.observe("prepared", request=request, profile=effective_profile, connection=connection,
                     payload=payload, mapping=mapping, input_bound=count, bound=str(bound))
        defaults = self.store.defaults()
        from .product_policy import completion_budget, effective_cap
        reserve = 0
        if self.purpose == "research" and self.snapshot.get("performance_profile") and self.snapshot.get("adaptive_budget"):
            budget = completion_budget(self.store, self.rid, self.snapshot, exclude_current=True)
            if budget["status"] == "UNKNOWN":
                raise ControlBoundary("COMPLETION_BUDGET_INCOMPLETE")
            reserve = budget["completion_reserve_usd"]
            if Decimal(budget["available_usd"]) < Decimal(str(bound)) + Decimal(reserve):
                self.store.audit(self.rid, "COMPLETION_BUDGET_INCOMPLETE", budget)
                raise ControlBoundary("COMPLETION_BUDGET_INCOMPLETE")
        reservation = self.store.reserve(rid=self.rid, connection=connection.connection_id, model=profile.model_id,
            role=request.role, purpose=self.purpose, bound=bound, run_limit=effective_cap(self.store, self.rid, self.snapshot), completion_reserve=reserve,
            monthly_limit=min(defaults.monthly_limit_usd, Decimal(self.snapshot["monthly_limit_usd"])),
            request_limit=min(defaults.request_limit_usd, Decimal(self.snapshot["request_limit_usd"])),
            attempts=self.snapshot["depth_limits"]["attempts"], revision=profile.price.revision if profile.price else "local-unmetered")
        request = request.model_copy(update={"budget_reservation_id": reservation})
        try:
            self.observe("reserved", reservation_id=reservation)
        except BaseException:
            self.store.transition(reservation, "RELEASED")
            raise
        trace = {"reservation_id": reservation, "requested_model_id": profile.model_id, "model_alias": profile.model_alias,
            "profile_id": profile.profile_id, "profile_revision": self.snapshot.get("profile_revisions", {}).get(profile.profile_id),
            "capability_evidence": {k: v.model_dump(mode="json") for k,v in profile.capabilities.items()},
            "requested_parameters": request.model_dump(mode="json", exclude={"input_text","system_instructions","developer_instructions","messages","tools","metadata"}),
            "effective_parameters": mapping, "price_revision": profile.price.revision if profile.price else None}
        self.store.audit(self.rid, "NORMALIZED_REQUEST_PREPARED", trace)
        self.store.transition(reservation, "DISPATCHED")
        self.dispatch_count += 1
        try:
            self.observe("dispatched", reservation_id=reservation)
            result = await adapter.create_response(request, effective_profile, connection, key,
                client_factory=self.client_factory, secrets=secrets)
            self.observe("response", result=result)
            usage = result.usage
            if usage.input_tokens is None or usage.output_tokens is None: raise ControlError("USAGE_MISSING")
            if usage.output_tokens > request.max_output_tokens or usage.input_tokens > count: raise ControlError("USAGE_BOUND_EXCEEDED")
            price = profile.price
            cost = Decimal(0)
            if price:
                cached_tokens, write = usage.cached_input_tokens or 0, usage.cache_write_tokens or 0
                if cached_tokens + write > usage.input_tokens: raise ControlError("USAGE_INVALID")
                if cached_tokens and price.cached_input_per_million is None or write and price.cache_write_per_million is None:
                    raise ControlError("PRICE_CATEGORY_UNKNOWN")
                cost = ((usage.input_tokens-cached_tokens-write)*price.input_per_million + cached_tokens*(price.cached_input_per_million or 0)
                    + write*(price.cache_write_per_million or 0) + usage.output_tokens*price.output_per_million)/1000000
            trace.update(response_id=result.response_id, provider_request_id=result.provider_request_id,
                resolved_model_id=result.model_id, provider_model_revision=result.model_revision, resolved_at=datetime.now().astimezone().isoformat(),
                usage=usage.model_dump(mode="json"), latency_ms=result.latency_ms, raw_response_ref=result.raw_response_ref)
            error, output = None, None
            if result.status != "COMPLETED" and output_type: error = "REFUSAL" if result.status == "REFUSED" else "INCOMPLETE_RESPONSE"
            elif output_type:
                try: output = output_type.model_validate_json(result.output_text)
                except Exception: error = "STRUCTURED_OUTPUT_INVALID"
            trace["effective_parameters"]["validation_result"] = "FAIL" if error else "PASS" if output_type else "NOT_REQUESTED"
            self.store.settle_output(reservation, request_key, result.model_id, output if output_type else result,
                cost, error=error, response_id=result.response_id, trace=trace)
            self.observe("settled", cost=str(cost), validation=error or "PASS")
            if error: raise ModelProviderError(error, retryable=False)
            return result, output, 1
        except BaseException as exc:
            row = self.store.db.execute("SELECT status FROM spend_ledger WHERE id=?", (reservation,)).fetchone()
            if row[0] == "DISPATCHED": self.store.transition(reservation, "UNRESOLVED")
            code = exc.code if isinstance(exc, ModelProviderError) else "PROTOCOL_INCOMPATIBLE"
            self.store.audit(self.rid, "NORMALIZED_REQUEST_FAILED", {"reservation_id": reservation, "code": code})
            self.observe("failed", code=code, http_status=getattr(exc, "http_status", None),
                         provider_request_id=getattr(exc, "provider_request_id", None), provider_error_code=getattr(exc, "provider_error_code", None))
            if isinstance(exc, GenerationError) and code == "SECRET_IN_PROVIDER_RESPONSE": raise ControlError(code) from None
            if isinstance(exc, (ControlError, ModelProviderError)): raise
            if not isinstance(exc, Exception): raise
            raise ModelProviderError(code, retryable=False) from None


def snapshot_remaining(snapshot, started_at):
    return snapshot["max_elapsed_sec"] - (datetime.now().astimezone() - datetime.fromisoformat(started_at)).total_seconds()


def terminal_status(stop_reason):
    return {"GOAL_ANSWERED": "COMPLETED", "BUDGET_EXHAUSTED": "BUDGET_BLOCKED",
            "INSUFFICIENT_DATA": "INSUFFICIENT_DATA", "UNRESOLVED_VERIFICATION": "VALIDATION_INCOMPLETE",
            "ACTION_LIMIT_REACHED": "LIMIT_REACHED"}.get(stop_reason, "FAILED")


def boundary(store: ControlStore, state: StateService, rid: str, runtime=None, snapshot=None):
    run = store.run(rid)
    from .research_settings import apply_pending
    from .product_policy import effective_snapshot
    if snapshot is not None:
        apply_pending(store, state, rid, snapshot, runtime)
        if runtime is not None:
            from .product_policy import maybe_select_worker
            if maybe_select_worker(store, rid, snapshot):
                runtime.models.update({r: v["model_id"] for r, v in snapshot["models"].items()})
                runtime.context_budget = min(4096, min(m["input_byte_limit"] for m in snapshot["models"].values()) // 4)
    if run["started_at"] and snapshot_remaining(snapshot or effective_snapshot(store, rid), run["started_at"]) <= 0:
        store.db.execute("UPDATE control_runs SET status='STOP_REQUESTED',error='TIME_LIMIT' WHERE research_id=?", (rid,))
        run["status"] = "STOP_REQUESTED"
    if run["status"] in {"PAUSE_REQUESTED", "STOP_REQUESTED"}:
        # 안전 경계에서 중단하며 실행 중인 도구와 전송된 과금 노출은 보존한다.
        
        from .schemas import ContextRef
        state.runtime_event(rid, "CONTROL_SAFE_BOUNDARY", {"request": run["status"]})
        raise ControlBoundary(run["status"])
    if runtime is not None:
        try:
            policy = store.config("depth", rid)
        except ControlError:
            return
        if policy.get("superseded_by_settings"):
            return
        limits = resolved_depth(policy["research_depth"])
        runtime.config = LoopConfig(max_hypotheses=min(5, limits["hypotheses"]), max_shortlist=min(3, limits["hypotheses"]),
                                    max_active_hypotheses=min(2, limits["hypotheses"]), max_branch_depth=min(2, limits["experiments"]),
                                    max_followups_per_hypothesis=min(2, limits["reviews"]))
        snapshot["depth_limits"] = limits
        if snapshot.get("performance_profile"):
            snapshot["preset_customized"] = True
        if not policy["applied"]:
            policy["applied"] = True
            with store.transaction():
                store.db.execute("UPDATE control_configs SET payload=? WHERE kind='depth' AND id=?", (to_json(policy), rid))
                store.audit(rid, "DEPTH_APPLIED_AT_SAFE_BOUNDARY", policy)
            state.runtime_event(rid, "CONTROL_DEPTH_APPLIED", policy)


async def execute(database: Path, workspace: Path, rid: str, *, credential_file=None, provider_factory=None):
    db = initialize(database)
    try:
        store = ControlStore(db)
        with store.transaction():
            changed = db.execute("UPDATE control_runs SET status='RUNNING',pid=?,started_at=COALESCE(started_at,?),version=version+1 WHERE research_id=? AND status IN ('STARTING','RESUMING')",
                                 (os.getpid(), datetime.now().astimezone().isoformat(), rid)).rowcount
            if not changed:
                return
        from .product_policy import effective_snapshot, optional_admission
        snapshot = effective_snapshot(store, rid)
        state = StateService(db, Workspace(workspace))
        if state.cycle5.enabled(rid):
            cycle5_config = state.research_slice.config(rid)
        else:
            cycle5_config = None
        source = safe_source(workspace / "inputs", snapshot["source_relative"]) if snapshot.get("source_relative") else None
        from .input_upload import Attachments
        Attachments(store, workspace).validate(snapshot.get("attachments", []), snapshot.get("draft_id"), snapshot.get("analysis_attachment_id"))
        if source is not None and sha256_file(source) != snapshot["source"]["sha256"]:
            raise ControlError("SOURCE_HASH_MISMATCH")
        provider = provider_factory(store, rid, snapshot) if provider_factory else RoutedGateway(
            store, Credentials(Path(__file__).resolve().parents[2], workspace, credential_file), rid, snapshot)
        from .search_policy import run_search
        boundary(store, state, rid, snapshot=snapshot)
        if snapshot.get("settings_version", 1) < 2:
            await run_search(state, store, Credentials(Path(__file__).resolve().parents[2], workspace, credential_file), rid, snapshot,
                             before_dispatch=lambda: boundary(store, state, rid, snapshot=snapshot))
        limits = snapshot["depth_limits"]
        runtime = AutonomousResearchLoop(
            state, provider, models={role: value["model_id"] for role, value in snapshot["models"].items()},
            config=LoopConfig(max_hypotheses=min(5, limits["hypotheses"]), max_shortlist=min(3, limits["hypotheses"]),
                              max_active_hypotheses=min(2, limits["hypotheses"]),
                              max_branch_depth=min(2, limits["experiments"]),
                              max_followups_per_hypothesis=min(2, limits["reviews"])),
            verified_analysis_skills_enabled=snapshot["verified_analysis_skills"],
            verification_repair_enabled=snapshot["verification_repair"],
            ridge_arithmetic_check_enabled=snapshot["ridge_arithmetic_check"])
        if cycle5_config:
            runtime.research_slice_config = cycle5_config
        runtime.control_boundary = lambda: boundary(store, state, rid, runtime, snapshot)
        runtime.optional_admission = lambda kind: optional_admission(store, rid, snapshot, kind=kind)
        if snapshot.get("research_profile_mode") == "AUTO":
            from .qualified_profiles import registry
            from .research_design import current_design
            design = current_design(state, rid)
            candidate = registry().choose(design["effective_question"] if design else snapshot["question"])
            if candidate["status"] == "SUPPORTED":
                from .qualified_workflow import execute_profile
                runtime.context_budget = min(4096, min(m["input_byte_limit"] for m in snapshot["models"].values()) // 4)
                await execute_profile(runtime, rid, snapshot)
                if state._one("SELECT run_status FROM research_runs WHERE research_id=?", (rid,))[0] == "ACTIVE":
                    state.stop_research(rid, "QUALIFIED_PROCEDURE_COMPLETED")
                export_final_report(state, rid)
                store.db.execute("UPDATE control_runs SET status='COMPLETED',pid=NULL,version=version+1,error=NULL WHERE research_id=?", (rid,))
                return
            if candidate["status"] != "GENERAL":
                raise ControlError("PROFILE_" + candidate["status"])
        if snapshot.get("settings_version", 1) >= 2:
            from .search_policy import qualified_literature
            async def acquire_and_assess():
                if snapshot.get("question_only") and source is not None:
                    state.runtime_event(rid, "DATA_EGRESS_APPROVAL_REQUIRED", {"question_only":True})
                    runtime.input_limitation = "DATA_EGRESS_APPROVAL_REQUIRED"
                    return False
                if not qualified_literature(state, rid):
                    try:
                        await run_search(state, store, Credentials(Path(__file__).resolve().parents[2], workspace, credential_file), rid, snapshot,
                            before_dispatch=lambda: boundary(store, state, rid, snapshot=snapshot))
                    except ControlError as exc:
                        if not exc.code.startswith("SEARCH_"):
                            raise
                        state.runtime_event(rid, "LITERATURE_ACQUISITION_LIMITATION", {"code": exc.code})
                if snapshot["search_required"] and not qualified_literature(state, rid):
                    state.runtime_event(rid, "LITERATURE_EVIDENCE_MISSING", {"stop_policy": True})
                    return False
                return True
            runtime.evidence_acquisition = acquire_and_assess
        runtime.context_budget = min(4096, min(m["input_byte_limit"] for m in snapshot["models"].values()) // 4)
        if state.load_runtime_cursor(rid) is None:
            state.configure_budget(rid, float(snapshot["run_limit_usd"]) * 0.25, float(snapshot["run_limit_usd"]) * 0.75, float(snapshot["run_limit_usd"]))
            state.finish_runtime_step(rid, "verified_analysis_skills_config", runtime._skill_config())
            state.finish_runtime_step(rid, "verification_repair_config", runtime._repair_config())
            state.finish_runtime_step(rid, "source", {"csv_source": str(source) if source is not None else None, "goal": snapshot["question"]})
            runtime._save_cursor(rid, "START")
            result = await runtime._run_with_stops(rid, source, snapshot["question"])
        else:
            result = await runtime.resume(rid)
        export_final_report(state, rid)
        store.db.execute("UPDATE control_runs SET status=?,pid=NULL,version=version+1,error=? WHERE research_id=?",
                         (terminal_status(result.get("stop_reason")), result.get("stop_reason"), rid))
    except (ControlBoundary, SemanticReviewRequired) as exc:
        incomplete = str(exc) == "COMPLETION_BUDGET_INCOMPLETE"
        store.db.execute("UPDATE control_runs SET status=?,pid=NULL,error=?,version=version+1 WHERE research_id=?",
                         ("BUDGET_BLOCKED" if incomplete else "STOPPED" if str(exc) == "STOP_REQUESTED" else "PAUSED", str(exc) if isinstance(exc, SemanticReviewRequired) else None, rid))
        if incomplete or str(exc) == "STOP_REQUESTED":
            if state._one("SELECT run_status FROM research_runs WHERE research_id=?", (rid,))["run_status"] == "ACTIVE":
                state.stop_research(rid, "BUDGET_EXHAUSTED" if incomplete else "USER_STOP")
            try:
                export_final_report(state, rid)
            except Exception:
                store.audit(rid, "REPORT_NOT_AVAILABLE", {"reason": "EXISTING_REPORT_GATE_BLOCKED"})
    except BaseException as exc:
        code = exc.code if isinstance(exc, (ControlError, ModelProviderError, RuntimeFailure)) else "BUDGET_BLOCKED" if isinstance(exc, BudgetExceededError) else "EXECUTION_FAILED"
        if "store" in locals():
            unsettled = store.db.execute("SELECT 1 FROM spend_ledger WHERE research_id=? AND status IN ('DISPATCHED','UNRESOLVED')", (rid,)).fetchone()
            status = "NEEDS_RECONCILIATION" if unsettled else "BUDGET_BLOCKED" if "BUDGET" in code or "PRICE" in code else "FAILED"
            store.db.execute("UPDATE control_runs SET status=?,error=?,pid=NULL,version=version+1 WHERE research_id=?", (status, code, rid))
            store.audit(rid, "EXECUTION_HALTED", {"code": code})
            if "snapshot" in locals() and snapshot.get("performance_profile") and "state" in locals():
                if state._one("SELECT run_status FROM research_runs WHERE research_id=?", (rid,))["run_status"] == "ACTIVE":
                    state.stop_research(rid, "BUDGET_EXHAUSTED" if status == "BUDGET_BLOCKED" else "FATAL_ERROR")
                try:
                    export_final_report(state, rid)
                except Exception:
                    store.audit(rid, "REPORT_NOT_AVAILABLE", {"reason": "EXISTING_REPORT_GATE_BLOCKED"})
    finally:
        db.close()
