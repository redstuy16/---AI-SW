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
        self._recover()

    def _recover(self):
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
        return result

    def research_list(self):
        rows = []
        for raw in self.store.db.execute("SELECT * FROM research_runs ORDER BY created_at DESC"):
            rid = raw["research_id"]
            overview = project_overview(self.read._state, rid, mode=self.mode).model_dump(mode="json")
            owned = self.store.db.execute("SELECT * FROM control_runs WHERE research_id=?", (rid,)).fetchone()
            snapshot = json.loads(owned["snapshot"]) if owned else {}
            event = self.store.db.execute("SELECT event_type,created_at FROM runtime_events WHERE research_id=? ORDER BY seq DESC LIMIT 1", (rid,)).fetchone()
            rows.append({**overview, "title": owned["title"] if owned else overview["question"],
                         "control_status": owned["status"] if owned else overview["status"], "control_version": owned["version"] if owned else None,
                         "research_depth": snapshot.get("research_depth"), "last_action": event[0] if event else overview["current_stage"],
                         "updated_at": event[1] if event else raw["created_at"], "controlled": bool(owned)})
        return rows

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
            if model.capability_status != "supported":
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
        try:
            source = safe_source(self.workspace / "inputs", snapshot["source_relative"])
            if sha256_file(source) != snapshot["source"]["sha256"]:
                reasons.append("SOURCE_HASH_MISMATCH")
        except (ControlError, OSError):
            reasons.append("SOURCE_INVALID")
        from .search_policy import decision
        search_status = decision(snapshot)
        if snapshot.get('search_required') and search_status != 'SEARCH_ALLOWED':
            reasons.append(search_status)
        return {"search_status": search_status, "ready": not reasons, "reasons": sorted(set(reasons)), "price_status": "가격 확인 필요" if "PRICE_REQUIRED" in reasons else "예약 시 계산",
                "generated_code": False, "egress_verified": False}

    def prepare(self, request):
        if contains_auth_material(to_json(request)):
            raise ControlError("SECRET_IN_CONFIG")
        if any(value in to_json(request) for value in self.credentials.active_secrets(c.get("credential_env_name") for c in self.store.configs("connection"))):
            raise ControlError("SECRET_IN_CONFIG")
        request = NewResearch.model_validate(request)
        from .product_policy import prepare_snapshot, apply_reasoning
        prepared = prepare_snapshot(self.store, request.model_dump(mode="json"))
        request = NewResearch.model_validate({k: v for k, v in prepared.items() if k in NewResearch.model_fields})
        routing_revision = None
        if request.routing_profile_id:
            routing = RoutingProfile.model_validate(self.store.config("routing", request.routing_profile_id))
            request.routing = dict(routing.routing)
            if routing.reviewer_profile_id: request.routing["verification_coordinator"] = routing.reviewer_profile_id
            routing_revision = next(x["revision"] for x in self.store.configs("routing") if x["profile_id"] == routing.profile_id)
        snapshot = request.model_dump(mode="json")
        snapshot["source"] = source_snapshot(self.workspace / "inputs", request.source_relative)
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
        snapshot["requested_depth_limits"] = DEPTHS[request.research_depth]
        snapshot["depth_limits"] = resolved_depth(request.research_depth)
        if request.max_followups is not None:
            snapshot["depth_limits"]["reviews"] = min(snapshot["depth_limits"]["reviews"], request.max_followups)
        apply_reasoning(snapshot)
        return snapshot

    def create(self, request):
        snapshot = self.prepare(request)
        rid = self.read._state.create_research(snapshot["question"])
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
                if parts == ["api", "control", "research"]:
                    self._recover()
                    value = self.research_list()
                elif parts == ["api", "control", "settings"]:
                    from .product_policy import catalog, PRESETS
                    value = {"connections": self.connections(), "models": self.store.configs("model"),
                             "defaults": self.store.defaults().model_dump(mode="json"), "depths": DEPTHS, "statuses": STATUS_KO,
                             "resolved_depths": {k: resolved_depth(k) for k in DEPTHS},
                             "providers": __import__("htrsa.providers.native", fromlist=["DEFINITIONS"]).DEFINITIONS,
                             "routing_profiles": self.store.configs("routing"),
                             "defaults_revision": next((x["revision"] for x in self.store.configs("defaults")), 0),
                             "catalog": catalog(self.store), "performance_profiles": PRESETS,
                             "credential_storage": "환경변수 → OS 자격 증명 관리자 → 보호된 평문 파일 · 저장 키 조회/복사 불가"}
                    from .resource_policy import preferences, low_spec
                    value["preferences"] = preferences(self.store)
                    value["low_spec"] = low_spec(self.store)
                    from .productization import onboarding
                    value["onboarding"] = onboarding(self, value["catalog"]["models"])
                elif parts == ["api", "control", "onboarding"]:
                    from .productization import onboarding
                    value = onboarding(self)
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
                elif parts == ["api", "control", "environment"]:
                    value = self.environment()
                elif parts == ["api", "control", "usage"]:
                    value = self.store.ledger()
                elif len(parts) == 5 and parts[:3] == ["api", "control", "research"]:
                    rid, endpoint = parts[3:]
                    self.read._research(rid)
                    if endpoint == "activity":
                        from .resource_policy import activity_page
                        from urllib.parse import parse_qs
                        query = parse_qs(urlsplit(path).query)
                        value = activity_page(self.read._state, rid, limit=int(query.get('limit', ['100'])[0]), offset=int(query.get('offset', ['0'])[0]))
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
                    elif endpoint == "usage":
                        value = self.store.ledger(rid)
                    elif endpoint == "control":
                        self._recover()
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
            if parts == ["api", "control", "preferences"]:
                from .resource_policy import save_preferences
                return APIResponse(200, save_preferences(self.store, body))
            if parts == ["api", "control", "research", "preflight"]:
                snapshot = self.prepare(body)
                value = self.preflight(snapshot)
                from .control_plane import admitted_cost
                from decimal import Decimal
                required = Decimal(0)
                try:
                    for raw in snapshot['models'].values():
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
            if len(parts) == 5 and parts[:3] == ["api", "control", "connections"] and parts[4] == "credential":
                conn = self.store.config("connection", parts[3])
                secret = None if body.get("delete") is True else CredentialInput.model_validate(body).value.get_secret_value()
                result = self.credentials.save(conn["credential_env_name"], secret)
                from .productization import revision
                self.store.put("credential_change", parts[3], {"changed_at": utc_now().isoformat()}, revision(self.store, "credential_change", parts[3]))
                return APIResponse(200, result)
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
        except (OSError, ReleaseExportError):
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
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
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
        if path in {"/", "/dashboard", "/api/session", "/auth/bootstrap", "/auth/status"} and (urlsplit(self.path).query or urlsplit(self.path).fragment):
            return self.reply(APIResponse(400, {"error": "AUTH_ROUTE_INVALID"}))
        if path == "/auth/bootstrap":
            return self.reply(APIResponse(405, {"error": "METHOD_NOT_ALLOWED"}))
        if path == "/auth/status":
            return self.reply(APIResponse(200, {"manual_pairing": self.session.allow_pairing}))
        if path in {"/", "/dashboard"}:
            return self.reply(APIResponse(200, (STATIC / "index.html").read_text(encoding="utf-8"), "text/html; charset=utf-8"))
        if path in {"/assets/workbench.js", "/assets/provider_settings.js", "/assets/product_ux.js", "/assets/bootstrap.js", "/assets/cycle5_ui.js", "/assets/live_api_test.js", "/assets/workbench.css"}:
            filename = path.rsplit("/", 1)[-1]
            return self.reply(APIResponse(200, (STATIC / filename).read_bytes(), "text/javascript; charset=utf-8" if filename.endswith(".js") else "text/css; charset=utf-8"))
        if not self.session.authorized(self.headers.get("Cookie")):
            return self.reply(APIResponse(401, {"error": "OWNER_SESSION_REQUIRED"}))
        if not self.session.admitted():
            return self.reply(APIResponse(429, {"error": "LOCAL_RATE_LIMIT"}))
        if path == "/api/session":
            return self.reply(APIResponse(200, {"csrf": self.session.csrf}))
        self.reply(self.api.request("GET", self.path))

    def do_POST(self):
        admitted_origin = self.boundary_valid(True)
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


def create_server(api, port=0, *, session=None):
    session = session if session is not None else OwnerSession()
    handler = type("WorkbenchHandler", (Handler,), {"api": api, "session": session, "authority": ""})
    server = HTTPServer(("127.0.0.1", port), handler)
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
