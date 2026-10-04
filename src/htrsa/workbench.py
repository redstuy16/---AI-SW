"""동일 출처의 한국어 연구 작업대. python -m htrsa.workbench로 실행한다."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

from pydantic import ValidationError

from .control_plane import (Connection, ConnectionRegistration, ControlError, ControlStore, CredentialInput, Credentials, Defaults,
                            DEPTHS, ModelProfile, NewResearch, RoutingProfile, ROLES, source_snapshot, safe_source, process_alive, resolved_depth)
from .dashboard import APIResponse, DashboardReadAPI, project_overview, _stats_payload
from .database import to_json
from .preflight import environment_status
from .providers.base import ModelProviderError
from .release import export_release, ReleaseExportError
from .schemas import StrictModel, new_id, utc_now
from .storage import sha256_file
from .storage_errors import storage_error_code
from .local_auth import MemorySecrets, contains_auth_material, redact_auth_material


STATIC = Path(__file__).with_name("workbench_static")
STATUS_KO = {
    "ACTIVE": "진행 중", "RUNNING": "진행 중", "DRAFT": "설정 대기", "STARTING": "시작 요청됨",
    "RESUMING": "계속 요청됨", "PAUSE_REQUESTED": "일시정지 요청됨", "PAUSED": "일시정지됨",
    "STOP_REQUESTED": "중지 요청됨", "STOPPED": "중지됨", "COMPLETED": "완료", "FAILED": "문제 발생",
    "PREFLIGHT_BLOCKED": "실행 환경 미충족", "BUDGET_BLOCKED": "예산으로 중단", "NEEDS_RECONCILIATION": "과금 정산 확인 필요",
    "INSUFFICIENT_DATA": "자료 부족으로 종료", "LIMIT_REACHED": "작업 한도로 종료",
    "ANALYSIS_CHECK_FAILED": "분석 검증 실패", "CHECKER_QUALIFICATION_FAILED": "검증기 확인 실패",
    "ORIGINAL_CONTRACT_MISMATCH": "원래 분석 계약과 불일치", "REPAIR_CONTRACT_MUTATION_BLOCKED": "새 분석 계획 필요",
    "UNRESOLVED_DISAGREEMENT": "분석 결과와 검증 결과 불일치 미해결", "REPAIR_UNRESOLVED": "분석 결과와 검증 결과 불일치 미해결",
    "NUMERICAL_CHECKS_PASSED_FOR_SCOPE": "지원 범위 수치 검사 통과", "REPAIR_NOT_STARTED_BUDGET": "복구 검증 예산 부족",
    "REPAIR_INCOMPLETE_BUDGET": "복구 미완료", "REPAIR_INCOMPLETE": "복구 미완료", "VALIDATION_INCOMPLETE": "재검증 미완료",
    "VERIFIED": "검증 통과", "PASS": "지정 검사 통과", "FAIL": "검증 실패", "INVALIDATED": "무효화됨", "SUPERSEDED": "이전 수정본",
    "NOT_VALIDATED": "검증하지 않음", "UNCONFIGURED": "미설정", "VALIDATED": "실제 검사 통과",
    "passed": "검사 통과", "failed": "검사 실패", "not_applicable": "적용되지 않음", "needs_reference_check": "참조 검사 필요",
}


def redact(value):
    protected = [v for k, v in os.environ.items() if v and (k.endswith("_KEY") or "TOKEN" in k or "SECRET" in k)]
    if isinstance(value, str):
        value = redact_auth_material(value)
        for secret in protected:
            value = value.replace(secret, "[비밀 제거됨]")
        return re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[비밀 제거됨]", value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if k.lower() not in {"api_key", "authorization", "password", "session", "secret_value"}}
    if isinstance(value, (tuple, list)):
        return [redact(v) for v in value]
    return value


class SmokeOutput(StrictModel):
    status: str


class WorkbenchAPI:
    def __init__(self, database, workspace, *, mode=None, launch=True, credential_file=None):
        self.read = DashboardReadAPI(database, workspace, mode=mode)
        self.database, self.workspace, self.mode = Path(database).resolve(), Path(workspace).resolve(), mode
        db = self.read._db
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='control_schema'").fetchone():
            backup = self.database.with_suffix(".pre-workbench.sqlite")
            if self.database.is_file() and not backup.exists():
                target = sqlite3.connect(backup)
                try:
                    db.backup(target)
                finally:
                    target.close()
        self.store = ControlStore(db)
        self.credentials = Credentials(Path(__file__).resolve().parents[2], self.workspace, credential_file)
        self.launch = launch
        self.children = []
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "inputs").mkdir(exist_ok=True)
        from .input_upload import Attachments
        Attachments(self.store, self.workspace).cleanup()
        self._recover()
        from .research_lifecycle import cleanup
        cleanup(self)

    def _recover(self):
        from .resource_queue import ResourcePool
        with self.store.transaction():
            ResourcePool(self.store).recover()
        from .productization import recover_qualifications
        recover_qualifications(self)
        from .live_api_test import recover_sessions
        recover_sessions(self)
        for row in self.store.db.execute("SELECT research_id,pid FROM control_runs WHERE status IN ('RUNNING','STARTING','RESUMING','PAUSE_REQUESTED','STOP_REQUESTED')").fetchall():
            alive = bool(row["pid"] and process_alive(row["pid"]))
            if alive:
                continue
            self.store.recover_ledger(row["research_id"])
            uncertain = self.store.db.execute("SELECT 1 FROM spend_ledger WHERE research_id=? AND status='UNRESOLVED'", (row["research_id"],)).fetchone()
            self.store.db.execute("UPDATE control_runs SET status=?,pid=NULL,version=version+1 WHERE research_id=?",
                                  ("NEEDS_RECONCILIATION" if uncertain else "PAUSED", row["research_id"]))

    def close(self):
        self.read.close()

    def connections(self):
        return [dict(c, credential=self.credentials.metadata(c["credential_env_name"]), compatibility="연구 적합성 미평가") for c in self.store.configs("connection")]

    def environment(self):
        result = environment_status()
        versions = {}
        for package in ("pydantic", "numpy", "scipy", "matplotlib", "httpx", "openai-agents"):
            try:
                versions[package] = {"version": importlib.metadata.version(package), "status": "정상"}
            except importlib.metadata.PackageNotFoundError:
                versions[package] = {"version": None, "status": "설치 필요"}
        result.update(packages=versions, python=sys.version.split()[0], free_bytes=shutil.disk_usage(self.workspace).free,
                      generated_code="격리 검증 전 생성 코드 실행 차단", local_egress="외부 전송 차단 미검증",
                      skill_live_efficacy="NOT_VALIDATED", f3p_live_efficacy="NOT_VALIDATED")
        result['checked_at'] = datetime.now().astimezone().isoformat()
        return result

    def research_list(self):
        from .workbench_pages import research_list
        return research_list(self)

    def repair_projection(self, rid):
        state, db = self.read._state, self.store.db
        self.read._research(rid)
        artifacts = []
        skills = []
        for row in db.execute("SELECT * FROM artifacts WHERE research_id=? AND (artifact_type LIKE 'F3P_%' OR artifact_type IN ('SKILL_PLAN','SKILL_RESULT')) ORDER BY rowid", (rid,)):
            value = {"artifact_id": row["artifact_id"], "type": row["artifact_type"], "contract_id": row["contract_id"], "integrity": "VALID", "artifact_status": row["status"]}
            try:
                path = self.read.workspace.path(rid, row["relative_path"])
                if path.stat().st_size > 2000000 or sha256_file(path) != row["sha256"]:
                    raise ValueError()
                value["data"] = json.loads(path.read_text(encoding="utf-8", errors="strict"))
                if row["artifact_type"].startswith("SKILL_"):
                    value["metadata"] = value["data"].get("result", value["data"].get("plan", {}))
            except Exception:
                value.update(integrity="INVALID", data={}, reason="ARTIFACT_INTEGRITY_FAILED")
            (skills if row["artifact_type"].startswith("SKILL_") else artifacts).append(value)
        events = [dict(event_type=r["event_type"], details=json.loads(r["details_json"]), timestamp=r["created_at"]) for r in db.execute(
            "SELECT * FROM runtime_events WHERE research_id=? AND (event_type LIKE 'REPAIR_%' OR event_type LIKE '%QUALIFICATION%' OR event_type LIKE '%VALIDATION%') ORDER BY seq", (rid,))]
        chains = []
        for failure in [a for a in artifacts if a["type"] == "F3P_FAILURE_EVIDENCE"]:
            data, contract = failure["data"], failure["contract_id"]
            next_step = state.runtime_step(rid, "repair_next:" + contract)
            next_data = next_step["output"] if next_step else {}
            revision = (next_data.get("active_contract_ids") or [None])[-1]
            parent = next_data
            related = [a for a in artifacts if a["contract_id"] in {contract, revision}]
            decisions = [a for a in related if a["type"] == "F3P_REPAIR_DECISION"]
            revalidations = [a for a in related if a["type"] == "F3P_REVALIDATION"]
            checks = [check for a in revalidations for check in a["data"].get("verification", {}).get("checks", [])]
            complete = {c["check_id"] for c in checks if c.get("passed")}
            required = data.get("required_post_repair_checks", [])
            committed = any(db.execute("SELECT 1 FROM staged_mutations WHERE mutation_id=? AND status='COMMITTED'", (a["data"].get("mutation_id"),)).fetchone() for a in revalidations)
            final = "VERIFIED" if committed and required and set(required) <= complete and all(a["integrity"] == "VALID" for a in related) else "VALIDATION_INCOMPLETE"
            if final != "VERIFIED":
                for event in events:
                    if event["event_type"] in STATUS_KO and (event["details"].get("contract_id") in {None, contract, revision}):
                        final = event["event_type"]
            chains.append({"failure": failure, "original_contract": state.contract(contract)[0].model_dump(mode="json"),
                           "new_revision": revision, "revision_detail": parent, "decisions": decisions,
                           "revalidations": revalidations, "required_checks": required, "completed_checks": sorted(complete),
                           "remaining_checks": sorted(set(required) - complete), "final_disposition": final,
                           "qualification": [a for a in related if a["type"] == "F3P_CHECKER_QUALIFICATION"],
                           "procedure_approved_for_reuse": False})
        flags = {}
        for key in ("verified_analysis_skills_config", "verification_repair_config"):
            step = state.runtime_step(rid, key)
            flags[key] = step["output"] if step else {"enabled": False, "source": "no opt-in snapshot"}
        ridge_checks = []
        for record in db.execute("SELECT verification_json FROM staged_mutations WHERE research_id=? AND verification_json IS NOT NULL", (rid,)):
            for check in json.loads(record[0]).get("checks", []):
                if check["check_id"] == "F3P_RIDGE_ARITHMETIC":
                    text = check.get("message", "")
                    status = "not_applicable" if "not_applicable" in text else "needs_reference_check" if "needs_reference_check" in text else "passed" if check["passed"] else "failed"
                    ridge_checks.append({**check, "arithmetic_status": status})
        return {"chains": chains, "artifacts": artifacts, "skills": skills, "events": events, "flags": flags,
                "ridge": {"enabled": flags["verification_repair_config"].get("ridge_arithmetic_check", False),
                          "checks": ridge_checks,
                          "limitations": "고정 Ridge 정합성 범위만 확인 · 과학적 타당성이나 모든 데이터 누수는 증명하지 않습니다."}}

    def preflight(self, snapshot):
        reasons = []
        price_required_profiles = []
        if self.mode == "DEMO":
            reasons.append("DEMO_EXECUTION_SEPARATE")
        if snapshot["egress"] == "none":
            reasons.append("LOCAL_EGRESS_NOT_VALIDATED")
        if set(snapshot["models"]) != set(ROLES):
            reasons.append("ROLE_MODELS_REQUIRED")
        for role, raw in snapshot["models"].items():
            model = ModelProfile.model_validate(raw)
            conn = Connection.model_validate(snapshot["connections"][model.connection_id])
            if not conn.enabled or not conn.destination_approved:
                reasons.append("DESTINATION_APPROVAL_REQUIRED")
            from .product_policy import workflow_compatible
            if not (workflow_compatible(model) if snapshot.get("settings_version", 1) >= 2 else model.capability_status == "supported"):
                reasons.append("CAPABILITY_NOT_VALIDATED")
            try:
                from .providers.native import REGISTRY
                from .providers.normalized import GenerationRequest
                REGISTRY.get(conn.adapter_id).serialize(GenerationRequest(request_id="preflight", research_id="preflight",role=role,
                    model_profile_id=model.profile_id,max_output_tokens=model.output_limit,reasoning_policy=model.reasoning_policy,
                    temperature=model.temperature,top_p=model.top_p,stop=model.stop,seed=model.seed,stream=model.stream,store_preference=model.store_preference), model, conn)
            except ModelProviderError as exc: reasons.append(exc.code)
            if conn.endpoint_class == "cloud" and conn.auth_strategy != "none" and not self.credentials.metadata(conn.credential_env_name)["configured"]:
                reasons.append("CREDENTIAL_UNCONFIGURED")
            if not (model.local_api_unmetered and conn.endpoint_class == "loopback"):
                from .control_plane import admitted_cost
                try:
                    admitted_cost(model, min(4096, model.input_byte_limit))
                except ControlError as exc:
                    reasons.append(exc.code)
                    if exc.code == "PRICE_REQUIRED" and model.profile_id not in price_required_profiles:
                        price_required_profiles.append(model.profile_id)
        try:
            from .input_upload import Attachments
            Attachments(self.store, self.workspace).validate(snapshot.get("attachments", []), snapshot.get("draft_id"), snapshot.get("analysis_attachment_id"))
            if snapshot.get("source_relative") is None:
                if snapshot.get("source") is not None:
                    reasons.append("SOURCE_INVALID")
                source = None
            else:
                source = safe_source(self.workspace / "inputs", snapshot["source_relative"])
            if source is not None and sha256_file(source) != snapshot["source"]["sha256"]:
                reasons.append("SOURCE_HASH_MISMATCH")
        except ControlError as exc:
            reasons.append(exc.code)
        except OSError:
            reasons.append("SOURCE_INVALID")
        from .search_policy import decision
        search_status = decision(snapshot)
        if (snapshot.get("ai_report_enabled") and snapshot.get("research_profile_mode") == "AUTO"
                and not snapshot.get("source_relative") and snapshot.get("design_resolution", {}).get("profile", {}).get("status") == "SUPPORTED"):
            search_status = "SEARCH_NOT_NEEDED"
        report_search_enabled = snapshot.get("ai_report_enabled") and snapshot.get("search_policy") != "DISABLED"
        if (report_search_enabled or snapshot.get('search_required') and (snapshot.get("settings_version", 1) < 2 or snapshot.get("ai_report_enabled"))) and search_status not in {'SEARCH_ALLOWED', 'SEARCH_NOT_NEEDED'}:
            reasons.append(search_status)
        search_fields = {"SEARCH_EGRESS_DENIED": "public_search_consent", "SEARCH_QUERY_REQUIRED": "public_search_query",
                         "SEARCH_PRIVATE_QUERY_BLOCKED": "public_search_query", "SEARCH_DISABLED": "search_policy",
                         "SEARCH_REQUIRED_BUT_DISABLED": "search_policy", "SEARCH_ATTEMPT_LIMIT": "search_attempt_limit", "COMPLETION_RESERVE_BLOCKED": "run_limit_usd", "PRICE_UNKNOWN": "search_policy"}
        if snapshot.get("ai_report_enabled"):
            from .product_policy import completion_budget
            budget = completion_budget(self.store, "preflight", snapshot)
            if not budget["can_complete"]:
                reasons.append("COMPLETION_RESERVE_BLOCKED")
            if report_search_enabled and search_status == "SEARCH_ALLOWED":
                from .search_policy import default_search_price, search_allocation
                price, price_source = default_search_price()
                allocation = search_allocation(self.store, "preflight", snapshot, price=price, price_source=price_source, view=budget)
                if not allocation["allowed_attempts"]:
                    reasons.append(allocation["status"])
        value = {"search_status": search_status, "ready": not reasons, "reasons": list(dict.fromkeys(reasons)),
                "price_required_profiles": price_required_profiles,
                "repair_fields": [search_fields[r] for r in reasons if r in search_fields],
                "first_blocker": next(iter(reasons), None), "field_sources": snapshot.get("field_sources", {}),
                "effective_routing": snapshot["routing"], "effective_settings": {k:snapshot.get(k) for k in
                    ("performance_profile","advanced_performance_profile","model_reasoning","sampling_mode","search_required","search_attempt_limit","report_format","selected_model_pool","draft_revision")},
                "price_status": "가격 확인 필요" if "PRICE_REQUIRED" in reasons else "예약 시 계산",
                "generated_code": False, "egress_verified": False}
        from .beginner_policy import classify
        return classify(snapshot, value)

    def prepare(self, request):
        if contains_auth_material(to_json(request)):
            raise ControlError("SECRET_IN_CONFIG")
        if any(value in to_json(request) for value in self.credentials.active_secrets(c.get("credential_env_name") for c in self.store.configs("connection"))):
            raise ControlError("SECRET_IN_CONFIG")
        requested = dict(request)
        from .product_policy import prepare_snapshot, apply_reasoning, resolve_effective_settings
        resolved, field_sources = resolve_effective_settings(self.store, requested)
        prepared = prepare_snapshot(self.store, resolved)
        request = NewResearch.model_validate({k: v for k, v in prepared.items() if k in NewResearch.model_fields})
        from .input_upload import Attachments
        attachment_store = Attachments(self.store, self.workspace)
        attachment_items, analysis_source = attachment_store.validate(request.attachments, request.draft_id, request.analysis_attachment_id)
        from .research_design import authorize_bindings
        authorize_bindings(self.store, self.workspace, request.detailed_design, request.draft_id, request.attachments)
        if analysis_source:
            if request.source_relative and request.source_relative != analysis_source:
                raise ControlError("ANALYSIS_ATTACHMENT_CONFLICT")
            request.source_relative = analysis_source
        routing_revision = None
        if request.routing_profile_id and request.settings_version == 1:
            routing = RoutingProfile.model_validate(self.store.config("routing", request.routing_profile_id))
            request.routing = dict(routing.routing)
            if routing.reviewer_profile_id: request.routing["verification_coordinator"] = routing.reviewer_profile_id
            routing_revision = next(x["revision"] for x in self.store.configs("routing") if x["profile_id"] == routing.profile_id)
        snapshot = request.model_dump(mode="json")
        snapshot["source"] = source_snapshot(self.workspace / "inputs", request.source_relative) if request.source_relative else None
        snapshot["field_sources"] = field_sources
        snapshot["requested_settings"] = requested
        from .research_design import resolve_design
        snapshot["design_resolution"] = resolve_design(snapshot["question"], snapshot.get("detailed_design"))
        snapshot["attachment_snapshots"] = [attachment_store.public(item) for item in attachment_items]
        snapshot["models"] = {role: self.store.config("model", profile) for role, profile in request.routing.items()}
        snapshot["profile_revisions"] = {x["profile_id"]:x["revision"] for x in self.store.configs("model") if x["profile_id"] in request.routing.values()}
        snapshot["routing_revision"] = routing_revision
        snapshot["fallback_policy"] = "NONE"
        from .providers.native import REGISTRY
        snapshot["adapter_versions"] = {role:REGISTRY.get(self.store.config("connection", m["connection_id"])["adapter_id"]).version for role,m in snapshot["models"].items()}
        snapshot["connections"] = {m["connection_id"]: self.store.config("connection", m["connection_id"]) for m in snapshot["models"].values()}
        snapshot.update(depth_limits=DEPTHS[request.research_depth], **self.store.defaults().model_dump(mode="json"),
                        application_revision=sha256(Path(__file__).read_bytes()).hexdigest(), environment_fingerprint=sha256(to_json(self.environment()["gates"]).encode()).hexdigest(),
                        tool_policy={"generated_code": False, "web_search": request.search_policy != "DISABLED", "telemetry": False, "tools": "existing contract allowlist"})
        # 기본값으로 사용자가 선택한 깊이를 바꾸지 않는다.
        snapshot["research_depth"] = request.research_depth
        snapshot["search_attempt_limit"] = request.search_attempt_limit
        snapshot["requested_depth_limits"] = DEPTHS[request.research_depth]
        snapshot["depth_limits"] = resolved_depth(request.research_depth)
        if request.max_followups is not None:
            snapshot["depth_limits"]["reviews"] = min(snapshot["depth_limits"]["reviews"], request.max_followups)
        apply_reasoning(snapshot)
        return snapshot

    def create(self, request):
        snapshot = self.prepare(request)
        key = snapshot.get("submission_key")
        digest = sha256(to_json(request).encode("utf-8", errors="strict")).hexdigest()
        rid = None
        if key:
            with self.store.transaction():
                row = self.store.db.execute("SELECT payload FROM control_configs WHERE kind='research_submission' AND id=?", (key,)).fetchone()
                if row:
                    prior = json.loads(row[0])
                    if prior["payload_hash"] != digest:
                        raise ControlError("IDEMPOTENCY_CONFLICT")
                    rid = prior["research_id"]
                else:
                    rid = new_id("R")
                    self.store.db.execute("INSERT INTO control_configs VALUES('research_submission',?,1,?)",
                        (key, to_json({"research_id":rid, "payload_hash":digest})))
            if self.store.db.execute("SELECT 1 FROM control_runs WHERE research_id=?", (rid,)).fetchone():
                current = self.store.run(rid)["snapshot"]
                return {"research_id":rid, "snapshot":current, "preflight":self.preflight(current)}
        rid = self.read._state.create_research(snapshot["question"], research_id=rid)
        from .research_design import initialize_design
        design = initialize_design(self.read._state, rid, snapshot)
        if design and not design["issues"] and design["effective_question"] != snapshot["question"]:
            self.read._state.set_research_question(rid, design["effective_question"])
        self.read.workspace.prepare(rid)
        with self.store.transaction():
            self.store.db.execute("INSERT INTO control_runs(research_id,title,status,snapshot,created_at) VALUES(?,?,?,?,?)",
                                  (rid, snapshot["title"], "DRAFT", to_json(snapshot), utc_now().isoformat()))
            self.store.audit(rid, "RUN_SNAPSHOT_CREATED", {"research_depth": snapshot["research_depth"]})
        return {"research_id": rid, "snapshot": snapshot, "preflight": self.preflight(snapshot)}

    def command(self, rid, action, body):
        key = body.get("idempotency_key", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", key):
            raise ControlError("IDEMPOTENCY_REQUIRED")
        digest = sha256(to_json({"rid": rid, "action": action, "version": body.get("expected_version")}).encode()).hexdigest()
        with self.store.transaction():
            old = self.store.db.execute("SELECT * FROM control_commands WHERE key=?", (key,)).fetchone()
            if old:
                if old["payload_hash"] != digest:
                    raise ControlError("IDEMPOTENCY_CONFLICT")
                return json.loads(old["result"])
            run = self.store.run(rid)
            if run["version"] != body.get("expected_version"):
                raise ControlError("STATE_STALE")
            if action in {"start", "resume"}:
                if run["status"] not in ({"DRAFT", "PREFLIGHT_BLOCKED"} if action == "start" else {"PAUSED"}):
                    raise ControlError("STATE_COMMAND_BLOCKED")
                from .product_policy import effective_snapshot
                check = self.preflight(effective_snapshot(self.store, rid))
                if not check["ready"]:
                    raise ControlError("PREFLIGHT_BLOCKED:" + ",".join(check["reasons"]))
                if self.store.db.execute("SELECT 1 FROM spend_ledger WHERE research_id=? AND status IN ('DISPATCHED','RESERVED','UNRESOLVED')", (rid,)).fetchone():
                    raise ControlError("NEEDS_RECONCILIATION")
                from .resource_policy import low_spec
                if low_spec(self.store) and self.store.db.execute("SELECT 1 FROM control_runs WHERE research_id<>? AND status IN ('RUNNING','STARTING','RESUMING','PAUSE_REQUESTED','STOP_REQUESTED')", (rid,)).fetchone():
                    raise ControlError('LOW_SPEC_BUSY')
                status = "STARTING" if action == "start" else "RESUMING"
            elif action in {"pause", "stop"}:
                if run["status"] not in {"RUNNING", "STARTING", "RESUMING", "PAUSED"}:
                    raise ControlError("STATE_COMMAND_BLOCKED")
                status = "PAUSE_REQUESTED" if action == "pause" else "STOP_REQUESTED"
                if run["status"] in {"PAUSED", "STARTING", "RESUMING"}:
                    status = "PAUSED" if action == "pause" else "STOPPED"
            else:
                raise ControlError("COMMAND_UNKNOWN")
            self.store.db.execute("UPDATE control_runs SET status=?,version=version+1 WHERE research_id=?", (status, rid))
            if status in {"PAUSED", "STOPPED"}:
                self.store.db.execute("UPDATE control_runs SET pid=NULL WHERE research_id=?", (rid,))
            if status in {"STARTING", "RESUMING"}:
                self.store.db.execute("UPDATE control_runs SET pid=? WHERE research_id=?", (os.getpid(), rid))
            result = {"research_id": rid, "status": status, "version": run["version"] + 1}
            self.store.db.execute("INSERT INTO control_commands VALUES(?,?,?,?,?)", (key, rid, action, digest, to_json(result)))
            self.store.audit(rid, action.upper() + "_REQUESTED", result)
        if status in {"STARTING", "RESUMING"} and run["snapshot"].get("attachments"):
            from .input_upload import Attachments
            Attachments(self.store, self.workspace).reference(run["snapshot"]["attachments"], rid)
        if action in {"start", "resume"} and self.launch:
            args = [sys.executable, "-m", "htrsa.workbench", str(self.database), str(self.workspace), "--worker", rid]
            # 분석 도구·컨테이너에 키를 마운트하지 않는다.
            try:
                child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except OSError:
                self.store.db.execute("UPDATE control_runs SET status='FAILED',error='WORKER_START_FAILED',pid=NULL,version=version+1 WHERE research_id=?", (rid,))
                raise ControlError("WORKER_START_FAILED") from None
            self.store.db.execute("UPDATE control_runs SET pid=? WHERE research_id=? AND status IN ('STARTING','RESUMING')", (child.pid, rid))
            self.children.append(child)
        if status == "STOPPED":
            state = self.read._state
            if state._one("SELECT run_status FROM research_runs WHERE research_id=?", (rid,))["run_status"] == "ACTIVE":
                state.stop_research(rid, "USER_STOP")
            try:
                from .final_report import export_final_report
                export_final_report(state, rid)
            except Exception:
                self.store.audit(rid, "REPORT_NOT_AVAILABLE", {"reason": "EXISTING_REPORT_GATE_BLOCKED"})
        return result

    async def check_model(self, profile_id, body):
        from .provider_checks import check_model
        return await check_model(self, profile_id, body)

    def request(self, method, path, body=None):
        response = self._request(method, path, body)
        try:
            protected = self.credentials.active_secrets(c.get("credential_env_name") for c in self.store.configs("connection"))
            def scrub(value):
                if isinstance(value, bytes):
                    if contains_auth_material(value):
                        raise ControlError("SECRET_IN_ARTIFACT")
                    if any(s.encode("utf-8") in value for s in protected):
                        raise ControlError("SECRET_IN_ARTIFACT")
                    return value
                if isinstance(value, str):
                    value = redact_auth_material(value)
                    for secret in protected:
                        value = value.replace(secret, "[비밀 제거됨]")
                    return value
                if isinstance(value, dict):
                    return {scrub(k): scrub(v) for k, v in value.items()}
                if isinstance(value, (list, tuple)):
                    return [scrub(v) for v in value]
                return value
            return APIResponse(response.status, scrub(response.body), response.content_type)
        except sqlite3.Error as exc:
            return APIResponse(409, {"error": storage_error_code(exc)})
        except (ControlError, OSError):
            return APIResponse(409, {"error": "SECRET_STORAGE_OR_ARTIFACT_BLOCKED"})

    def _request(self, method, path, body=None):
        parts = [p for p in urlsplit(path).path.split("/") if p]
        body = body or {}
        try:
            if method != "GET" and contains_auth_material(to_json(body)):
                raise ControlError("SECRET_IN_CONFIG")
            if method not in {"GET", "POST"}:
                return APIResponse(405, {"error": "METHOD_NOT_ALLOWED"})
            if parts[-1:] == ["credential"] and (method != "POST" or urlsplit(path).query or urlsplit(path).fragment):
                return APIResponse(400, {"error": "CREDENTIAL_ROUTE_INVALID"})
            if method != "GET" and parts[-1:] != ["credential"]:
                config_body = {k: v for k, v in body.items() if k != "api_key"} if parts == ["api", "control", "connections", "register"] else body
                if any(value in to_json(config_body) for value in self.credentials.active_secrets(c.get("credential_env_name") for c in self.store.configs("connection"))):
                    raise ControlError("SECRET_IN_CONFIG")
            if method == "GET":
                if parts == ["api", "control", "research", "design-catalog"]:
                    from .research_design import catalog
                    return APIResponse(200, catalog())
                if len(parts) == 5 and parts[:3] == ["api", "control", "research"] and parts[4] == "design":
                    from .research_lifecycle import ensure_visible
                    from .research_design import current_design, summary
                    ensure_visible(self, parts[3])
                    return APIResponse(200, {"current": current_design(self.read._state, parts[3]),
                        "summary": summary(self.read._state, parts[3]), "state_version": self.read._state.state_version(parts[3])})
                if parts == ["api", "control", "research"]:
                    self._recover()
                    value = self.research_list()
                    from .workbench_pages import list_page
                    from urllib.parse import parse_qs
                    query = parse_qs(urlsplit(path).query)
                    if query.get('trash', ['0'])[0] == '1':
                        from .research_lifecycle import cleanup
                        cleanup(self)
                        value = [r for r in self.research_list() if r['lifecycle']['status'] in {'TRASH', 'PURGING'}]
                    else:
                        value = [r for r in value if r['lifecycle']['status'] == 'ACTIVE']
                    if 'limit' in query:
                        value = list_page(value,query)
                elif parts == ["api", "control", "settings"]:
                    from .product_policy import catalog, PRESETS
                    value = {"connections": self.connections(), "models": self.store.configs("model"),
                             "defaults": self.store.defaults().model_dump(mode="json"), "depths": DEPTHS, "statuses": STATUS_KO,
                             "resolved_depths": {k: resolved_depth(k) for k in DEPTHS},
                             "providers": __import__("htrsa.providers.native", fromlist=["DEFINITIONS"]).DEFINITIONS,
                             "routing_profiles": self.store.configs("routing"),
                             "defaults_revision": next((x["revision"] for x in self.store.configs("defaults")), 0),
                             "catalog": catalog(self.store), "performance_profiles": PRESETS,
                             "credential_storage": "환경변수 → OS 자격 증명 관리자 → 보호된 평문 파일 · 저장 키 조회/복사 불가",
                             "openalex_credential": self.credentials.metadata('OPENALEX_API_KEY')}
                    from .resource_policy import preferences, low_spec
                    value["preferences"] = preferences(self.store)
                    from .qualified_profiles import registry
                    value["research_profiles"] = registry().public()
                    value["low_spec"] = low_spec(self.store)
                    value['resource_policy'] = {'local_inference':1,'heavy_analysis':1,'remote_network':1 if value['low_spec'] else 4,'queue_limit':32,
                        'free_bytes':shutil.disk_usage(self.workspace).free,'disk_warning':shutil.disk_usage(self.workspace).free < 512 * 1024**2}
                    from .productization import onboarding
                    value["onboarding"] = onboarding(self, value["catalog"]["models"])
                elif parts == ["api", "control", "onboarding"]:
                    from .productization import onboarding
                    value = onboarding(self)
                elif parts == ["api", "control", "research", "draft"]:
                    row = self.store.db.execute("SELECT revision,payload FROM control_configs WHERE kind='research_draft' AND id='owner'").fetchone()
                    value = {"revision":row["revision"], "draft":json.loads(row["payload"])} if row else {"revision":0,"draft":None}
                elif parts == ["api", "control", "live-api-tests"]:
                    value = [{"api_test_session_id": r["api_test_session_id"], "status": r["status"],
                              "started_at": r["started_at"], "execution": r["execution"],
                              "stop_reason": r.get("stop_reason")} for r in self.store.configs("live_api_session")]
                elif len(parts) == 4 and parts[:3] == ["api", "control", "live-api-tests"]:
                    from .live_api_test import summarize, verify_manifest
                    record = self.store.config("live_api_session", parts[3])
                    verify_manifest(self, record)
                    value = summarize(self, record)
                elif parts == ["api", "control", "inputs"]:
                    value = [{"name": p.name, "size_bytes": p.stat().st_size} for p in sorted((self.workspace / "inputs").glob("*.csv"))
                             if p.is_file() and not p.is_symlink() and p.stat().st_size <= 5000000]
                elif parts == ["api", "control", "attachments"]:
                    from .input_upload import Attachments
                    from urllib.parse import parse_qs
                    draft = parse_qs(urlsplit(path).query).get("draft_id", [None])[0]
                    if not draft:
                        raise ControlError("ATTACHMENT_DRAFT_REQUIRED")
                    manager = Attachments(self.store, self.workspace)
                    value = [manager.public(item) for item in self.store.configs("attachment")
                             if item["draft_id"] == draft and item["status"] not in {"DELETED","RETAINED"}]
                elif parts == ['api','control','library']:
                    from .workbench_pages import library_page
                    from urllib.parse import parse_qs
                    query = parse_qs(urlsplit(path).query)
                    value = library_page(self,limit=int(query.get('limit',['50'])[0]),offset=int(query.get('offset',['0'])[0]))
                elif parts == ["api", "control", "environment"]:
                    value = self.environment()
                elif parts == ["api", "control", "usage"]:
                    from .workbench_pages import usage_page
                    value = usage_page(self,path)
                elif len(parts) == 5 and parts[:3] == ["api", "control", "models"] and parts[4] == "pricing":
                    from .product_policy import model_pricing
                    value = model_pricing(self.store, parts[3])
                elif len(parts) == 5 and parts[:3] == ["api", "control", "research"]:
                    rid, endpoint = parts[3:]
                    self.read._research(rid)
                    from urllib.parse import parse_qs
                    query = parse_qs(urlsplit(path).query)
                    if endpoint == 'conclusion-card':
                        from .qualified_profiles import conclusion_card
                        value = conclusion_card(self.read._state, rid)
                    elif endpoint == 'source-inspection':
                        from .qualified_profiles import source_inspection
                        value = source_inspection(self.read._state, rid, include_rows=query.get('rows', ['0'])[0] == '1')
                    elif endpoint == 'beginner-progress':
                        from .beginner_controls import progress
                        value = progress(self, rid)
                    elif endpoint == 'execution-summary':
                        from .research_report import execution_summary
                        value = execution_summary(self, rid)
                    elif endpoint == 'screen':
                        from .research_screen import screen_summary
                        value = screen_summary(self, rid)
                    elif endpoint in {'source-document', 'source-document.pdf'}:
                        from .source_documents import checked_document
                        source_id = query.get('source_id', [''])[0]
                        record, pages = checked_document(self.read._state, rid, source_id)
                        from .release import _secret_free
                        from .storage import sha256_bytes
                        data = self.read._state.workspace.path(rid, record['pdf']['relative_path']).read_bytes()
                        secrets = self.credentials.active_secrets([c.get('credential_env_name') for c in self.store.configs('connection')] + ['OPENALEX_API_KEY'])
                        if (not _secret_free('source.pdf', data) or sha256_bytes(data) != record['pdf']['sha256']
                                or any(s in to_json(pages) for s in secrets)):
                            raise ControlError('SOURCE_DOCUMENT_CHANGED')
                        if endpoint == 'source-document.pdf':
                            return APIResponse(200, data, 'application/pdf')
                        value = {'source_id': source_id, 'status': record['status'], 'pages': record['extracted_pages'],
                                 'total_pages': record['total_pages'], 'truncated': record['truncated'], 'pdf_sha256': record['pdf']['sha256']}
                    elif endpoint == 'flow':
                        from .research_flow import project_flow
                        value = project_flow(self,rid,view=query.get('view',['current'])[0],limit=int(query.get('limit',['100'])[0]),offset=int(query.get('offset',['0'])[0]),selected=query.get('selected',[None])[0],lane=query.get('lane',[None])[0])
                    elif endpoint == 'flow-node':
                        from .research_flow import flow_node
                        value = flow_node(self,rid,query.get('id',[''])[0])
                    elif endpoint == 'items':
                        from .workbench_pages import item_page
                        value = item_page(self,rid,query.get('kind',[''])[0],limit=int(query.get('limit',['50'])[0]),offset=int(query.get('offset',['0'])[0]))
                    elif endpoint == 'resources':
                        from .resource_queue import ResourcePool
                        value = ResourcePool(self.store).summary(rid)
                    elif endpoint == 'semantic-summary':
                        from .workbench_pages import semantic_summary
                        value = semantic_summary(self, rid)
                    elif endpoint == "activity":
                        from .resource_policy import activity_page
                        from urllib.parse import parse_qs
                        query = parse_qs(urlsplit(path).query)
                        value = activity_page(self.read._state, rid, limit=int(query.get('limit', ['100'])[0]), offset=int(query.get('offset', ['0'])[0]),summary=query.get('summary',['0'])[0]=='1')
                    elif endpoint == 'activity-event':
                        from .resource_policy import activity_detail
                        value = activity_detail(self.read._state,rid,query.get('id',[''])[0])
                    elif endpoint == "repair":
                        value = self.repair_projection(rid)
                    elif endpoint == "settings":
                        from .research_settings import projection
                        value = projection(self.store, rid)
                    elif endpoint == "completion-budget":
                        from .product_policy import effective_snapshot, completion_budget
                        value = completion_budget(self.store, rid, effective_snapshot(self.store, rid))
                    elif endpoint == "report-view":
                        from .report_ux import friendly_report
                        value = friendly_report(self.read._state, rid)
                    elif endpoint in {"report.pdf", "report-preview"}:
                        from .report_pdf import render_pdf
                        secrets = self.credentials.active_secrets(c.get("credential_env_name") for c in self.store.configs("connection"))
                        output = render_pdf(self.read._state, rid, protected_values=secrets, include_preview=endpoint == "report-preview")
                        if endpoint == "report-preview":
                            return APIResponse(200, {"view": output["preview"], "state_version": output["state_version"], "pdf_sha256": output["sha256"]})
                        return APIResponse(200, output["data"], "application/pdf")
                    elif endpoint == "usage":
                        from .workbench_pages import usage_page
                        value = usage_page(self,path,rid)
                    elif endpoint == "control":
                        self._recover()
                        if query.get('summary',['0'])[0]=='1':
                            row = self.store.db.execute('SELECT status,version FROM control_runs WHERE research_id=?',(rid,)).fetchone()
                            return APIResponse(200,{'status':row['status'] if row else 'LEGACY_INSPECTION','version':row['version'] if row else None})
                        try:
                            value = self.store.run(rid)
                            from .product_policy import effective_snapshot
                            value["effective_snapshot"] = effective_snapshot(self.store, rid)
                            value["preflight"] = self.preflight(value["effective_snapshot"])
                            checkpoint = self.store.db.execute("SELECT updated_at FROM research_runtime_state WHERE research_id=?", (rid,)).fetchone()
                            value["last_checkpoint"] = checkpoint[0] if checkpoint else None
                            try:
                                value["effective_depth"] = self.store.config("depth", rid)
                            except ControlError:
                                value["effective_depth"] = {"research_depth": value["snapshot"]["research_depth"], "applied": True}
                        except ControlError:
                            value = {"status": "LEGACY_INSPECTION", "snapshot": None}
                    else:
                        raise ControlError("NOT_FOUND")
                else:
                    result = self.read.request(path)
                    if result.status == 200 and len(parts) == 4 and parts[:2] == ["api", "research"] and parts[3] == "experiments":
                        for item in result.body:
                            record = self.store.db.execute("SELECT * FROM experiments WHERE research_id=? AND experiment_id=?", (parts[2], item["experiment_id"])).fetchone()
                            item["parameters"] = json.loads(record["payload_json"])
                            item["measured_values"] = _stats_payload(self.read._state, parts[2], record["result_artifact_id"])
                            item["environment_revision"] = "existing fixed tool provenance; artifact trace"
                    return APIResponse(result.status, redact(result.body), result.content_type)
                return APIResponse(200, redact(value))
            if parts == ["api", "control", "inputs", "upload"]:
                from .input_upload import upload_csv
                protected = self.credentials.active_secrets(c.get('credential_env_name') for c in self.store.configs('connection'))
                return APIResponse(201, upload_csv(self.workspace / 'inputs', body, protected_values=protected))
            if parts == ["api", "control", "search-suggestion"]:
                from .research_report import search_suggestion
                if set(body) - {"title", "question"} or any(not isinstance(v, str) or len(v) > 3000 for v in body.values()):
                    raise ControlError("INVALID_REQUEST")
                return APIResponse(200, search_suggestion(body.get("title", ""), body.get("question", "")))
            if parts == ["api", "control", "attachments", "begin"]:
                from .input_upload import Attachments
                return APIResponse(201, Attachments(self.store, self.workspace).begin(body))
            if len(parts) == 5 and parts[:3] == ["api", "control", "attachments"] and parts[4] == "delete":
                from .input_upload import Attachments
                return APIResponse(200, Attachments(self.store, self.workspace).delete(parts[3], body.get("draft_id")))
            if parts == ["api", "control", "preferences"]:
                from .resource_policy import save_preferences
                return APIResponse(200, save_preferences(self.store, body))
            if parts == ["api", "control", "preferences", "reset"]:
                from .resource_policy import save_preferences, UIPreferences
                return APIResponse(200, save_preferences(self.store, UIPreferences().model_dump(mode="json")))
            if len(parts) == 5 and parts[:3] == ["api", "control", "research"] and parts[4] in {"rename", "trash", "restore", "purge"}:
                from . import research_lifecycle
                if parts[4] == "purge" and body.get("confirm") is not True:
                    raise ControlError("PERMANENT_DELETE_CONFIRMATION_REQUIRED")
                result = research_lifecycle.rename(self, parts[3], body.get("title")) if parts[4] == "rename" else getattr(research_lifecycle, parts[4])(self, parts[3])
                return APIResponse(200, result)
            if len(parts) == 5 and parts[:3] == ["api", "control", "research"]:
                from .research_lifecycle import ensure_visible
                ensure_visible(self, parts[3])
                if parts[4] == "rewrite-report":
                    from .research_report import rewrite_report
                    return APIResponse(200, asyncio.run(rewrite_report(self, parts[3], body)))
                if parts[4] == "finish-current-budget":
                    from .research_lifecycle import _idle
                    from .research_schemas import StopReason
                    from .final_report import export_final_report
                    rid = parts[3]
                    _idle(self, rid)
                    if self.store.run(rid)["status"] not in {"PAUSED","BUDGET_BLOCKED"}:
                        raise ControlError("BUDGET_FINISH_REQUIRES_PAUSE")
                    if not self.store.db.execute("SELECT 1 FROM research_budgets WHERE research_id=?", (rid,)).fetchone():
                        raise ControlError("RESEARCH_NOT_STARTED")
                    if self.store.db.execute("SELECT run_status FROM research_runs WHERE research_id=?", (rid,)).fetchone()[0] == "ACTIVE":
                        self.read._state.stop_research(rid, StopReason.BUDGET_EXHAUSTED)
                    export_final_report(self.read._state, rid)
                    self.store.db.execute("UPDATE control_runs SET status='STOPPED',error='FINISHED_WITH_CURRENT_BUDGET',version=version+1 WHERE research_id=?", (rid,))
                    return APIResponse(200, {"status":"FINISHED_WITH_CURRENT_BUDGET","paid_calls":0,"monthly_limit_changed":False})
                if parts[4] in {"recalculate", "refresh-source", "amend-question"}:
                    from .research_lifecycle import _idle
                    from .qualified_workflow import execute_profile, fetch_source, fetch_secondary, update_source, amend_question
                    from .autonomous_loop import AutonomousResearchLoop
                    from .providers.fake import FakeProvider
                    _idle(self, parts[3])
                    rid = parts[3]
                    snapshot = self.store.run(rid)["snapshot"]
                    if parts[4] == "amend-question":
                        if set(body) != {"question", "expected_version"} or type(body["expected_version"]) is not int:
                            raise ControlError("PROFILE_OWNER_AMENDMENT_REQUIRED")
                        result = amend_question(self.read._state, rid, body["question"], expected_version=body["expected_version"])
                    elif parts[4] == "refresh-source":
                        text = asyncio.run(fetch_source(snapshot))
                        alternate = asyncio.run(fetch_secondary(self.read._state, rid, snapshot, credentials=self.credentials))
                        result = update_source(self.read._state, rid, text, secondary_text=alternate)
                        if not result["affected"] and result["status"] == "SUPPORTED" and self.read._state._one("SELECT run_status FROM research_runs WHERE research_id=?", (rid,))[0] != "ACTIVE":
                            from .qualified_workflow import archive_report
                            from .final_report import export_final_report
                            archive_report(self.read._state, rid)
                            export_final_report(self.read._state, rid)
                    else:
                        runtime = AutonomousResearchLoop(self.read._state, FakeProvider([]),
                            models={role: m["model_id"] for role, m in snapshot["models"].items()},
                            verified_analysis_skills_enabled=snapshot["verified_analysis_skills"],
                            verification_repair_enabled=snapshot["verification_repair"],
                            ridge_arithmetic_check_enabled=snapshot["ridge_arithmetic_check"])
                        result = asyncio.run(execute_profile(runtime, rid, snapshot, replay=True))
                        from .research_report import rebase_local_report
                        rebase_local_report(self.read._state, self.store, rid)
                        from .final_report import export_final_report
                        export_final_report(self.read._state, rid)
                    return APIResponse(200, redact(result))
            if parts == ["api", "control", "catalog", "resolve"]:
                from .product_policy import resolve_catalog_profile
                identity = resolve_catalog_profile(self.store, body)
                return APIResponse(200, {"profile_id": identity, "inference_executed": False})
            if parts == ["api", "control", "research", "effective-settings"]:
                from .product_policy import resolve_effective_settings
                config, sources = resolve_effective_settings(self.store, body, task_intent="preview")
                return APIResponse(200, {"effective": config, "field_sources": sources, "paid_calls": 0})
            if parts == ["api", "control", "research", "draft"]:
                from .product_policy import resolve_effective_settings
                draft = dict(body["draft"])
                if "detailed_design" not in draft:
                    previous = self.store.db.execute("SELECT payload FROM control_configs WHERE kind='research_draft' AND id='owner'").fetchone()
                    if previous:
                        saved_draft = json.loads(previous[0])
                        if saved_draft.get("draft_id") == draft.get("draft_id") and "detailed_design" in saved_draft:
                            draft["detailed_design"] = saved_draft["detailed_design"]
                if "detailed_design" in draft:
                    from .research_design import normalize_design
                    draft["detailed_design"] = normalize_design(draft["detailed_design"])
                    from .research_design import authorize_bindings
                    authorize_bindings(self.store, self.workspace, draft["detailed_design"], draft.get("draft_id"), draft.get("attachments", []))
                resolve_effective_settings(self.store, draft, task_intent="preview")
                return APIResponse(200, self.store.put("research_draft", "owner", draft, body["expected_revision"]))
            if parts == ["api", "control", "research", "design-review"]:
                from .research_design import resolve_design, organize
                if re.search(r"(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{35}|ghp_[A-Za-z0-9]{36})", to_json(body)):
                    raise ControlError("SECRET_IN_CONFIG")
                if body.get("action") == "organize":
                    return APIResponse(200, organize(body.get("question", ""), body.get("draft_revision", 0)))
                return APIResponse(200, resolve_design(body.get("question", ""), body.get("detailed_design")))
            if parts == ["api", "control", "research", "design-columns"]:
                import csv
                from .input_upload import Attachments
                manager = Attachments(self.store, self.workspace)
                item = manager.get(body["attachment_id"], body["draft_id"])
                manager.validate([item["attachment_id"]], body["draft_id"])
                if item["supported_parser"] != "csv":
                    raise ControlError("ANALYSIS_ATTACHMENT_INVALID")
                with manager.path(item).open(encoding="utf-8-sig", newline="") as stream:
                    columns = next(csv.reader(stream))
                return APIResponse(200, {"columns": columns, "sha256": item["sha256"], "rows_loaded": False, "paid_calls": 0})
            if len(parts) == 5 and parts[:3] == ["api", "control", "research"] and parts[4] == "design":
                from .research_lifecycle import ensure_visible, _idle
                from .research_design import amend_design
                ensure_visible(self, parts[3])
                _idle(self, parts[3])
                from .research_design import authorize_bindings
                run = self.store.run(parts[3])["snapshot"]
                authorize_bindings(self.store, self.workspace, body["detailed_design"], run.get("draft_id"), run.get("attachments", []),
                    state=self.read._state, rid=parts[3])
                return APIResponse(200, amend_design(self.read._state, parts[3], body["detailed_design"],
                    expected_version=body["expected_version"]))
            if parts == ["api", "control", "research", "preflight"]:
                snapshot = self.prepare(body)
                value = self.preflight(snapshot)
                from .control_plane import admitted_cost
                from decimal import Decimal
                required = Decimal(0)
                try:
                    qualified = value.get('research_profile', {})
                    paid_roles = qualified.get('paid_roles', ROLES) if qualified.get('status') == 'SUPPORTED' else ROLES
                    for role in paid_roles:
                        raw = snapshot['models'][role]
                        model = ModelProfile.model_validate(raw)
                        if not model.local_api_unmetered:
                            bound = admitted_cost(model, min(4096, model.input_byte_limit))
                            required += bound
                            if bound > min(Decimal(snapshot['request_limit_usd']), self.store.defaults().request_limit_usd):
                                value['reasons'].append('REQUEST_BUDGET_BLOCKED')
                    ledger = self.store.ledger()
                    available = min(Decimal(snapshot['run_limit_usd']), self.store.defaults().monthly_limit_usd - Decimal(ledger['monthly_exposure']))
                    if required > available:
                        value['reasons'].append('COMPLETION_RESERVE_BLOCKED')
                    if Decimal(ledger['unresolved']) > 0:
                        value['reasons'].append('NEEDS_RECONCILIATION')
                except ControlError as exc:
                    value['reasons'].append(exc.code)
                value['ready'] = not value['reasons']
                value['minimum_role_call_bound_usd'] = str(required)
                value['paid_calls'] = 0
                from .beginner_policy import classify
                value = classify(snapshot, value)
                return APIResponse(200, value)
            if parts == ["api", "control", "research"]:
                return APIResponse(201, redact(self.create(body)))
            if parts == ["api", "control", "catalog", "select"]:
                from .product_policy import catalog
                provider = body.get("provider", "openai")
                entry = next((m for m in catalog(self.store)["models"] if m["provider"] == provider and m["model_id"] == body.get("model_id") and not m.get("profile_id")), None)
                if not entry or entry.get("catalog_expired"):
                    raise ControlError("CATALOG_RECHECK_REQUIRED")
                if body.get("approve_destination") is not True or (entry.get("price_candidate") and body.get("approve_price") is not True):
                    raise ControlError("CATALOG_OWNER_APPROVAL_REQUIRED")
                from .providers.normalized import CapabilityEvidence
                from .control_plane import PriceRecord
                from .providers.native import DEFINITIONS
                if provider not in DEFINITIONS or provider == "openai_compatible":
                    raise ControlError("CATALOG_RECHECK_REQUIRED")
                connection_id = body.get("connection_id") or ("gpt-default" if provider == "openai" else provider + "-default")
                try:
                    conn = Connection.model_validate(self.store.config("connection", connection_id))
                except ControlError:
                    if body.get("connection_id"):
                        raise ControlError("CONFIG_MISSING") from None
                    conn = Connection(connection_id=connection_id, display_name=DEFINITIONS[provider]["name"], adapter_id=provider, destination_approved=True)
                    self.store.put("connection", connection_id, conn)
                if conn.adapter_id != provider or not conn.enabled or not conn.destination_approved:
                    raise ControlError("CATALOG_CONNECTION_MISMATCH")
                profile_id = new_id("GPT")
                profile = ModelProfile(profile_id=profile_id, connection_id=connection_id, model_id=entry["model_id"],
                    display_name=entry["display_name"], protocol=DEFINITIONS[provider]["protocols"][0], reasoning_levels=entry["reasoning_levels"],
                    capabilities={k: CapabilityEvidence(status="SUPPORTED", source="STATIC_ADAPTER_RULE", checked_at=utc_now().isoformat(), details=entry["source"]) for k in entry.get("documented_capabilities", ("text", "structured_output", "reasoning", "usage_reporting"))},
                    price=PriceRecord(**entry["price_candidate"], checked_at=datetime.fromisoformat(entry["catalog_checked_at"]),
                        revision="catalog-owner-" + entry["catalog_checked_at"][:10], source=entry.get("price_source", entry["source"]), owner_verified=True) if entry.get("price_candidate") else None)
                self.store.put("model", profile_id, profile)
                return APIResponse(201, {"profile_id": profile_id, "status": "UNTESTED", "paid_calls": 0})
            if parts == ["api", "control", "connections", "register"]:
                registration = ConnectionRegistration.model_validate(body)
                conn = registration.value
                key = registration.api_key.get_secret_value() if registration.api_key is not None else None
                if key is not None and not conn.credential_env_name:
                    raise ControlError("SECRET_INPUT_INVALID")
                if key and key in to_json(conn):
                    raise ControlError("SECRET_IN_CONFIG")
                result = {}
                def save_key():
                    if key is not None:
                        result.update(self.credentials.save(conn.credential_env_name, key))
                        from .productization import revision
                        credential_revision = revision(self.store, "credential_change", conn.connection_id)
                        payload = {"changed_at": utc_now().isoformat()}
                        self.store.db.execute("INSERT OR REPLACE INTO control_configs VALUES(?,?,?,?)", ("credential_change", conn.connection_id, credential_revision + 1, to_json(payload)))
                        self.store.audit(None, "CREDENTIAL_CHANGED", {"connection_id": conn.connection_id, "revision": credential_revision + 1})
                saved = self.store.put("connection", conn.connection_id, conn, registration.expected_revision, before_commit=save_key)
                return APIResponse(200, {**saved, "connection_id": conn.connection_id, "credential": result or self.credentials.metadata(conn.credential_env_name), "paid_calls": 0})
            if parts == ["api", "control", "defaults"]:
                value = Defaults.model_validate(body["value"])
                return APIResponse(200, self.store.put("defaults", "global", value, body.get("expected_revision", 0)))
            if parts == ["api", "control", "connections"]:
                value = Connection.model_validate(body["value"])
                return APIResponse(200, self.store.put("connection", value.connection_id, value, body.get("expected_revision", 0)))
            if parts == ["api", "control", "routing"]:
                value = RoutingProfile.model_validate(body["value"])
                for identity in list(value.routing.values()) + ([value.reviewer_profile_id] if value.reviewer_profile_id else []): self.store.config("model", identity)
                return APIResponse(200, self.store.put("routing", value.profile_id, value, body.get("expected_revision", 0)))
            if parts == ["api", "control", "models"]:
                value = ModelProfile.model_validate(body["value"])
                from .providers.normalized import merge_evidence
                if any(v.source != "USER_DECLARED" for v in value.capabilities.values()): raise ControlError("CAPABILITY_SOURCE_FORBIDDEN")
                try:
                    prior = ModelProfile.model_validate(self.store.config("model", value.profile_id))
                except ControlError: prior = None
                if prior and prior.model_id == value.model_id and prior.connection_id == value.connection_id:
                    value.capabilities = merge_evidence(prior.capabilities, value.capabilities)
                value.capability_status, value.capability_source, value.capability_checked_at = "unknown", None, None
                self.store.config("connection", value.connection_id)
                return APIResponse(200, self.store.put("model", value.profile_id, value, body.get("expected_revision", 0)))
            if parts == ['api', 'control', 'search', 'credential']:
                secret = None if body.get('delete') is True else CredentialInput.model_validate(body).value.get_secret_value()
                result = self.credentials.save('OPENALEX_API_KEY', secret)
                self.store.audit(None, 'SEARCH_CREDENTIAL_CHANGED', {'provider': 'OpenAlex', 'deleted': secret is None})
                return APIResponse(200, result)
            if len(parts) == 5 and parts[:3] == ["api", "control", "connections"] and parts[4] == "credential":
                conn = self.store.config("connection", parts[3])
                secret = None if body.get("delete") is True else CredentialInput.model_validate(body).value.get_secret_value()
                result = self.credentials.save(conn["credential_env_name"], secret)
                from .productization import revision
                self.store.put("credential_change", parts[3], {"changed_at": utc_now().isoformat()}, revision(self.store, "credential_change", parts[3]))
                return APIResponse(200, result)
            if len(parts) == 5 and parts[:3] == ["api", "control", "connections"] and parts[4] == "delete":
                from .beginner_controls import delete_connection
                return APIResponse(200, delete_connection(self, parts[3], confirm=body.get("confirm"), expected_revision=body.get("expected_revision")))
            if len(parts) == 5 and parts[:3] == ["api", "control", "models"] and parts[4] == "qualify":
                from .productization import qualify
                return APIResponse(200, asyncio.run(qualify(self, parts[3], body)))
            if len(parts) == 5 and parts[:3] == ["api", "control", "models"] and parts[4] == "live-test":
                from .live_api_test import run_session, export_session, summarize
                record = asyncio.run(run_session(self, parts[3], body))
                package = export_session(self, record["api_test_session_id"])
                return APIResponse(200, {**summarize(self, record), "package": package})
            if len(parts) == 5 and parts[:3] == ["api", "control", "live-api-tests"] and parts[4] == "export":
                from .live_api_test import export_session
                return APIResponse(200, export_session(self, parts[3]))
            if len(parts) == 5 and parts[:3] == ["api", "control", "models"] and parts[4] == "pricing":
                from .product_policy import apply_model_pricing
                return APIResponse(200, apply_model_pricing(self.store, parts[3], body))
            if len(parts) == 5 and parts[:3] == ["api", "control", "models"] and parts[4] == "check":
                return APIResponse(200, asyncio.run(self.check_model(parts[3], body)))
            if len(parts) == 5 and parts[:3] == ["api", "control", "research"]:
                rid, action = parts[3:]
                if action == "cycle5":
                    from .research_slice_schemas import ResearchSliceConfig
                    cycle = self.read._state.cycle5
                    operation = body.get("operation")
                    if operation == "enable":
                        if not self.read._state.runtime_step(rid, "research_slice_config"):
                            self.read._state.configure_research_slice(rid, ResearchSliceConfig(claim_evidence_provenance=True, verifier_dependency_catalog=True))
                        cycle.enable(rid, actor_role="owner")
                    elif operation in {"source", "goal", "lineage"}:
                        raw = body["record"]
                        if raw.get("research_id") != rid:
                            raise ValueError("CROSS_RESEARCH_SEMANTIC_RECORD")
                        {"source": cycle.propose_source, "goal": cycle.set_goal, "lineage": cycle.declare_lineage}[operation](raw, actor_role="owner")
                    elif operation == "review":
                        cycle.review_source(rid, body["record_id"], body["expected_revision"], actor_role="owner", approve=body.get("approve") is True)
                    elif operation == "repair":
                        from .verification_repair import repair_transformation
                        result = repair_transformation(self.read._state, rid, body["record_id"], body["expected_revision"], actor_role="owner")
                        return APIResponse(200, redact(result))
                    else:
                        raise ValueError("UNSUPPORTED_SEMANTIC_OPERATION")
                    return APIResponse(200, redact(cycle.snapshot(rid)))
                if action == "settings":
                    from .research_settings import queue
                    return APIResponse(200, queue(self.store, rid, body))
                if action == "analysis-plan-revision":
                    # 원래 계약의 의미는 보존하고 새 연구에서 Manager/Worker 계획 승인을 다시 받는다.
                    from .agent_schemas import AnalysisPlan
                    from .product_policy import effective_snapshot
                    old = self.store.run(rid)
                    if old["version"] != body.get("expected_version"):
                        raise ControlError("STATE_STALE")
                    plan = AnalysisPlan.model_validate(body["analysis_plan"])
                    question = body.get("question", old["snapshot"]["question"])
                    if not isinstance(question, str) or not 0 < len(question) <= 3000:
                        raise ControlError("INVALID_REQUEST")
                    parent = effective_snapshot(self.store, rid)
                    request = {k: v for k, v in parent.items() if k in NewResearch.model_fields}
                    request.update(question=question + "\n소유자 요청 새 계획(별도 검증 필요): " + to_json(plan), title=old["title"][:160] + " · 새 분석 계획")
                    created = self.create(request)
                    revision_id = new_id("PLAN")
                    record = {"parent_research_id": rid, "child_research_id": created["research_id"], "plan": plan.model_dump(mode="json"),
                              "status": "NEW_PLAN_REQUIRES_APPROVAL", "previous_snapshot_hash": sha256(to_json(old["snapshot"]).encode("utf-8")).hexdigest(),
                              "approved_for_execution": False, "created_at": utc_now().isoformat()}
                    self.store.put("analysis_plan_revision", revision_id, record)
                    self.read._state.finish_runtime_step(created["research_id"], "owner_analysis_plan_request",
                        {"revision_id": revision_id, "plan": plan.model_dump(mode="json"), "parent_research_id": rid,
                         "input_sha256": created["snapshot"]["source"]["sha256"]})
                    self.store.audit(rid, "ANALYSIS_PLAN_REVISION_CREATED", record)
                    return APIResponse(201, {**record, "revision_id": revision_id})
                if action == "depth":
                    depth = body.get("research_depth")
                    if depth not in DEPTHS:
                        raise ControlError("DEPTH_INVALID")
                    with self.store.transaction():
                        run = self.store.run(rid)
                        if run["status"] not in {"RUNNING", "PAUSED"} or run["version"] != body.get("expected_version"):
                            raise ControlError("STATE_STALE")
                        old = self.store.db.execute("SELECT revision,payload FROM control_configs WHERE kind='depth' AND id=?", (rid,)).fetchone()
                        old_depth = json.loads(old["payload"])["research_depth"] if old else run["snapshot"]["research_depth"]
                        update = {"research_depth": depth, "previous_depth": old_depth, "applied": False,
                                  "approved_at": utc_now().isoformat(), "money_and_egress_unchanged": True}
                        self.store.db.execute("INSERT OR REPLACE INTO control_configs VALUES('depth',?,?,?)", (rid, old["revision"] + 1 if old else 1, to_json(update)))
                        self.store.db.execute("UPDATE control_runs SET version=version+1 WHERE research_id=?", (rid,))
                        self.store.audit(rid, "DEPTH_CHANGE_REQUESTED", update)
                    return APIResponse(200, update)
                if action == "export":
                    root = self.workspace / "exports" / rid / new_id("RELEASE")
                    if root.is_symlink() or not root.resolve().is_relative_to(self.workspace):
                        raise ControlError("EXPORT_PATH_UNSAFE")
                    protected = self.credentials.active_secrets(c.get("credential_env_name") for c in self.store.configs("connection"))
                    exported = export_release(self.read._state, rid, root, protected_values=protected)
                    return APIResponse(200, {"manifest_sha256": exported["manifest_sha256"], "files": exported["files"],
                                             "relative_path": root.relative_to(self.workspace).as_posix(), "release_ready": exported["release_ready"]})
                return APIResponse(200, self.command(rid, action, body))
            raise ControlError("NOT_FOUND")
        except (ControlError, ModelProviderError) as exc:
            return APIResponse(409, {"error": exc.code})
        except (ValidationError, KeyError, ValueError, TypeError):
            return APIResponse(400, {"error": "INVALID_REQUEST"})
        except sqlite3.Error as exc:
            return APIResponse(409, {"error": storage_error_code(exc)})
        except OSError as exc:
            code = storage_error_code(exc)
            return APIResponse(409, {"error": "STORAGE_OR_EXPORT_BLOCKED" if code == "STORAGE_UNAVAILABLE" else code})
        except ReleaseExportError:
            return APIResponse(409, {"error": "STORAGE_OR_EXPORT_BLOCKED"})
        except Exception:
            return APIResponse(409, {"error": "OPERATION_UNAVAILABLE"})


class OwnerSession:
    ticket_lifetime = 60
    session_lifetime = 43200

    def __init__(self, *, allow_pairing=False, clock=None):
        self.instance_id = secrets.token_hex(16)
        self.cookie_name = "htrsa_owner_" + self.instance_id
        self.allow_pairing = allow_pairing
        self.pairing = secrets.token_urlsafe(24) if allow_pairing else None
        self.cookie = None
        self.csrf = None
        self.paired_at = None
        self.attempts = []
        self.requests = []
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._secrets = MemorySecrets()
        if self.pairing:
            self._secrets.remember(self.pairing)
        self.ticket_digest = None
        self.issued_at = self.expires_at = None
        self.used = False
        self.closed = False

    def issue_bootstrap(self):
        with self._lock:
            if self.closed or self.ticket_digest is not None or self.cookie is not None:
                raise ControlError("BOOTSTRAP_ALREADY_ISSUED")
            ticket = secrets.token_urlsafe(32)
            self.ticket_digest = self._secrets.remember(ticket)
            self.issued_at = self._clock()
            self.expires_at = self.issued_at + self.ticket_lifetime
            return ticket

    def _attempt(self, code):
        now = self._clock()
        self.attempts = [t for t in self.attempts if now - t < 60]
        if len(self.attempts) >= 5:
            raise ControlError(code)
        self.attempts.append(now)

    def _mint(self):
        self.cookie, self.csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self._secrets.remember(self.cookie)
        self._secrets.remember(self.csrf)
        self.pairing = None
        self.paired_at = self._clock()

    def exchange(self, ticket):
        with self._lock:
            self._attempt("BOOTSTRAP_RATE_LIMIT")
            if self.closed or not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket):
                raise ControlError("BOOTSTRAP_DENIED")
            digest = sha256(ticket.encode("ascii")).hexdigest()
            if not self.ticket_digest or not secrets.compare_digest(digest, self.ticket_digest):
                raise ControlError("BOOTSTRAP_DENIED")
            if self.used:
                raise ControlError("BOOTSTRAP_ALREADY_USED")
            if self._clock() >= self.expires_at:
                raise ControlError("BOOTSTRAP_EXPIRED")
            # 세션 발급이 실패해도 티켓을 다시 사용할 수 없다.
            self.used = True
            self._mint()

    def close(self):
        with self._lock:
            self.closed = True
            self.cookie = self.csrf = self.pairing = self.ticket_digest = None
            self._secrets.clear()

    def admitted(self):
        now = self._clock()
        self.requests = [t for t in self.requests if now - t < 60]
        if len(self.requests) >= 180:
            return False
        self.requests.append(now)
        return True

    def pair(self, code):
        with self._lock:
            self._attempt("PAIR_RATE_LIMIT")
            if self.closed or not self.allow_pairing or not self.pairing or not isinstance(code, str):
                raise ControlError("PAIR_DENIED")
            if not secrets.compare_digest(code.encode("utf-8"), self.pairing.encode("ascii")):
                raise ControlError("PAIR_DENIED")
            if self.ticket_digest:
                self.used = True
            self._mint()

    def authorized(self, cookie):
        try:
            values = SimpleCookie(cookie or "")
            token = values[self.cookie_name].value
        except Exception:
            return False
        return bool(not self.closed and self.cookie and self.paired_at is not None and
                    self._clock() - self.paired_at < self.session_lifetime and
                    secrets.compare_digest(token.encode("utf-8"), self.cookie.encode("ascii")))


class Handler(BaseHTTPRequestHandler):
    api: WorkbenchAPI
    session: OwnerSession
    authority: str

    def reply(self, response, cookie=False):
        data = response.body if isinstance(response.body, bytes) else response.body.encode("utf-8", errors="strict") if isinstance(response.body, str) else to_json(response.body).encode("utf-8", errors="strict")
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; worker-src 'self'; style-src 'self'; font-src 'self' blob:; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        if cookie:
            self.send_header("Set-Cookie", self.session.cookie_name + "=" + self.session.cookie + "; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200")
        self.end_headers()
        self.wfile.write(data)

    def boundary_valid(self, write=False):
        if self.headers.get_all("Host", []) != [self.authority]:
            return False
        origin = self.headers.get("Origin")
        if len(self.headers.get_all("Origin", [])) > 1:
            return False
        if origin and origin != "http://" + self.authority:
            return False
        if write and origin != "http://" + self.authority:
            return False
        return True

    def do_GET(self):
        if not self.boundary_valid():
            return self.reply(APIResponse(403, {"error": "ORIGIN_HOST_DENIED"}))
        path = urlsplit(self.path).path
        if path == '/favicon.ico':
            return self.reply(APIResponse(204,b'', 'image/x-icon'))
        if path in {"/", "/dashboard", "/api/session", "/auth/bootstrap", "/auth/status"} and (urlsplit(self.path).query or urlsplit(self.path).fragment):
            return self.reply(APIResponse(400, {"error": "AUTH_ROUTE_INVALID"}))
        if path == "/auth/bootstrap":
            return self.reply(APIResponse(405, {"error": "METHOD_NOT_ALLOWED"}))
        if path == "/auth/status":
            return self.reply(APIResponse(200, {"manual_pairing": self.session.allow_pairing}))
        if path in {"/", "/dashboard"}:
            return self.reply(APIResponse(200, (STATIC / "index.html").read_text(encoding="utf-8"), "text/html; charset=utf-8"))
        if path in {"/assets/workbench.js", "/assets/provider_settings.js", "/assets/product_ux.js", "/assets/bootstrap.js", "/assets/cycle5_ui.js", "/assets/live_api_test.js", "/assets/workbench.css", '/assets/research_flow.js', '/assets/beginner_ux.js', '/assets/tutorial_content.js', '/assets/tutorial.js', '/assets/research_design.js', '/assets/research_workspace.js', '/assets/research_workspace.css'}:
            filename = path.rsplit("/", 1)[-1]
            return self.reply(APIResponse(200, (STATIC / filename).read_bytes(), "text/javascript; charset=utf-8" if filename.endswith(".js") else "text/css; charset=utf-8"))
        if path.startswith('/assets/pdfjs/'):
            relative = path[len('/assets/pdfjs/'):]
            vendor = STATIC / 'pdfjs'
            manifest = json.loads((vendor / 'manifest.json').read_text(encoding='utf-8', errors='strict'))
            if manifest['version'] != '6.4.299' or relative not in manifest['files']:
                return self.reply(APIResponse(404, {'error':'NOT_FOUND'}))
            asset = (vendor / relative).resolve()
            if not asset.is_relative_to(vendor.resolve()):
                return self.reply(APIResponse(404, {'error':'NOT_FOUND'}))
            data = asset.read_bytes()
            if sha256(data).hexdigest() != manifest['files'][relative]:
                return self.reply(APIResponse(409, {'error':'PDF_ASSET_CHANGED'}))
            mime = 'text/javascript; charset=utf-8' if relative.endswith('.mjs') else 'application/wasm' if relative.endswith('.wasm') else 'application/octet-stream'
            return self.reply(APIResponse(200, data, mime))
        if not self.session.authorized(self.headers.get("Cookie")):
            return self.reply(APIResponse(401, {"error": "OWNER_SESSION_REQUIRED"}))
        if not self.session.admitted():
            return self.reply(APIResponse(429, {"error": "LOCAL_RATE_LIMIT"}))
        if path == "/api/session":
            return self.reply(APIResponse(200, {"csrf": self.session.csrf}))
        self.reply(self.api.request("GET", self.path))

    def do_POST(self):
        admitted_origin = self.boundary_valid(True)
        upload_path = re.fullmatch(r"/api/control/attachments/(ATT-[a-f0-9]{32})/upload", self.path)
        if upload_path:
            authorized = self.session.authorized(self.headers.get("Cookie")) and secrets.compare_digest(
                self.headers.get("X-CSRF-Token", ""), self.session.csrf or "invalid")
            if not admitted_origin or not authorized:
                self.close_connection = True
                return self.reply(APIResponse(403, {"error":"OWNER_CSRF_REQUIRED"}))
            if not self.session.admitted():
                self.close_connection = True
                return self.reply(APIResponse(429, {"error":"LOCAL_RATE_LIMIT"}))
            try:
                from .input_upload import Attachments, MAX_BYTES
                length = int(self.headers.get("Content-Length", "-1"))
                if self.headers.get("Content-Type") != "application/octet-stream" or not 0 <= length <= MAX_BYTES or self.headers.get("Transfer-Encoding"):
                    raise ControlError("UPLOAD_SIZE_OR_CONTENT")
                protected = self.api.credentials.active_secrets(c.get("credential_env_name") for c in self.api.store.configs("connection"))
                value = Attachments(self.api.store, self.api.workspace, protected_values=protected).receive(upload_path[1], self.rfile, length)
                return self.reply(APIResponse(201, value))
            except (ControlError, OSError, ValueError, sqlite3.Error) as exc:
                self.close_connection = True
                code = exc.code if isinstance(exc, ControlError) else storage_error_code(exc)
                return self.reply(APIResponse(409, {"error": "UPLOAD_STORAGE_FAILED" if code == "STORAGE_UNAVAILABLE" else code}))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            upload = (self.path == '/api/control/inputs/upload' and admitted_origin
                      and self.session.authorized(self.headers.get('Cookie'))
                      and secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), self.session.csrf or 'invalid'))
            if not 0 < length <= (6_700_000 if upload else 64000) or self.headers.get("Content-Type") != "application/json":
                raise ValueError()
            body = json.loads(self.rfile.read(length).decode("utf-8", errors="strict"))
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            if not admitted_origin:
                return self.reply(APIResponse(403, {"error": "ORIGIN_HOST_DENIED"}))
            return self.reply(APIResponse(400, {"error": "INVALID_REQUEST"}))
        # Windows에서 403 전달 전 연결이 초기화되지 않도록 제한된 본문을 읽는다.
        
        if not admitted_origin:
            return self.reply(APIResponse(403, {"error": "ORIGIN_HOST_DENIED"}))
        path = urlsplit(self.path)
        if path.path in {"/auth/bootstrap", "/api/session"} and (path.query or path.fragment):
            return self.reply(APIResponse(400, {"error": "AUTH_ROUTE_INVALID"}))
        if self.path == "/auth/bootstrap":
            if set(body) != {"ticket"} or self.headers.get_all("X-H-TRSA-Bootstrap", []) != ["1"]:
                return self.reply(APIResponse(403, {"error": "BOOTSTRAP_DENIED"}))
            try:
                self.session.exchange(body["ticket"])
                return self.reply(APIResponse(200, {"authenticated": True, "csrf": self.session.csrf}), cookie=True)
            except ControlError as exc:
                code = exc.code
                status = {"BOOTSTRAP_ALREADY_USED": 409, "BOOTSTRAP_EXPIRED": 410, "BOOTSTRAP_RATE_LIMIT": 429}.get(code, 403)
                return self.reply(APIResponse(status, {"error": code}))
        if self.path == "/api/session":
            try:
                self.session.pair(body.get("pairing_code"))
                return self.reply(APIResponse(200, {"csrf": self.session.csrf}), cookie=True)
            except ControlError:
                return self.reply(APIResponse(403, {"error": "PAIR_DENIED"}))
        if not self.session.authorized(self.headers.get("Cookie")) or not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.session.csrf or "invalid"):
            return self.reply(APIResponse(403, {"error": "OWNER_CSRF_REQUIRED"}))
        if not self.session.admitted():
            return self.reply(APIResponse(429, {"error": "LOCAL_RATE_LIMIT"}))
        self.reply(self.api.request("POST", self.path, body))

    def log_message(self, *_args):
        return None

    def do_OPTIONS(self):
        if not self.boundary_valid(True):
            return self.reply(APIResponse(403, {"error": "ORIGIN_HOST_DENIED"}))
        return self.reply(APIResponse(405, {"error": "METHOD_NOT_ALLOWED"}))


class LocalWorkbenchServer(HTTPServer):
    # 새로고침 때 한꺼번에 들어오는 화면 파일 요청을 순서대로 처리한다.
    request_queue_size = 64


def create_server(api, port=0, *, session=None):
    session = session if session is not None else OwnerSession()
    handler = type("WorkbenchHandler", (Handler,), {"api": api, "session": session, "authority": ""})
    server = LocalWorkbenchServer(("127.0.0.1", port), handler)
    handler.authority = f"127.0.0.1:{server.server_port}"
    return server


def open_browser(session, origin, *, no_browser=False, opener=None, fallback=None):
    ticket = session.issue_bootstrap()
    url = origin + "/#bootstrap=" + ticket
    ticket = None
    opened = False
    if not no_browser:
        try:
            opened = bool((opener or webbrowser.open)(url, new=2, autoraise=True))
        except Exception:
            pass
    if not opened:
        if fallback is not None:
            fallback(url)
        else:
            print("H-TRSA 열기 · 60초 유효 · 1회용 링크:", flush=True)
            print(url, flush=True)
    url = None
    return opened


def main(argv=None, *, fallback=None, quiet=False):
    parser = argparse.ArgumentParser(description="자동 로컬 인증 H-TRSA 연구 작업대")
    parser.add_argument("database", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--port", type=int, default=0, help="기본값 0: OS가 배정하는 loopback 포트")
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 열지 않고 1회용 링크 출력")
    parser.add_argument("--manual-pairing", action="store_true", help="개발자 전용 기존 수동 연결 사용")
    parser.add_argument("--mode", choices=["LIVE", "DEMO", "OFFLINE-CACHED"])
    parser.add_argument("--worker")
    args = parser.parse_args(argv)
    if args.worker:
        from .control_runtime import execute
        asyncio.run(execute(args.database, args.workspace, args.worker))
        return
    if not 0 <= args.port <= 65535:
        parser.error("포트는 0부터 65535까지 지정하세요.")
    api, session = WorkbenchAPI(args.database, args.workspace, mode=args.mode), OwnerSession(allow_pairing=args.manual_pairing)
    server = None
    try:
        server = create_server(api, args.port, session=session)
        origin = "http://" + server.RequestHandlerClass.authority
        if not quiet:
            print("H-TRSA: " + origin, flush=True)
        if args.manual_pairing:
            print("개발자 수동 연결 코드: " + session.pairing, flush=True)
        elif args.no_browser:
            open_browser(session, origin, no_browser=True)
        else:
            def desktop_link(url):
                try:
                    keep = fallback(url)
                except Exception:
                    keep = False
                if keep is False:
                    server.shutdown()
            threading.Thread(target=open_browser, args=(session, origin), kwargs={"fallback": desktop_link if fallback else None}, daemon=True, name="local-browser-handoff").start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        session.close()
        if server:
            server.server_close()
        api.close()


if __name__ == "__main__":
    main()
