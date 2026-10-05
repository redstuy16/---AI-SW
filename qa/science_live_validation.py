"""원본 설정은 읽기 전용으로 사용하고 격리 원장에서 과학 탐구를 비교한다."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from probe.control_plane import Connection, ControlError, ControlStore, Credentials, Defaults, ModelProfile, PriceRecord, ROLES
from probe.control_runtime import RoutedGateway, execute
from probe.database import initialize, to_json
from probe.providers.base import ModelRunResult
from probe.release import _secret_free
from probe.schemas import utc_now
from probe.service import StateService
from probe.storage import Workspace
from science_visual_quality import inspect_report
from science_validation_budget import LiveValidationBudget
from science_single_luna import execute_direct_single, DirectSingleMockGateway, offline_no_search

KST = timezone(timedelta(hours=9), "Asia/Seoul")
GLOBAL_CAP = Decimal("3")
RUN_CAP = Decimal("0.10")
MODEL_ID = "gpt-6-luna"
CASES = (
    {"id": "principle", "question": "고등학생을 위한 빛의 세기와 광합성 탐구 설계안을 작성하세요. 이미 알려진 안정적인 원리와 통제변인, 측정 절차를 설명하고 실측 결과를 만들지 마세요. 최신 정보나 외부 인용은 요구하지 않습니다."},
    {"id": "literature", "question": "고등학생의 빛의 세기와 광합성 탐구를 위해 실제 공개 과학 문헌을 AI 검색으로 확인하고 출처와 정확한 인용을 포함해 탐구 설계를 작성하세요. 상관과 인과를 구분하고 실제 측정하지 않은 수치 결과는 만들지 마세요."},
    {"id": "csv", "question": "제공한 교육용 합성 CSV의 온도와 생장 수치 사이 연관성을 검증하세요. 단위와 관측의 독립성은 제공되지 않았으므로 확인 한계를 명시하세요. Pearson과 Spearman 두 방법으로 비선형성에 대한 민감도를 검사하고 자체 검토·수정을 수행하며, 인과를 주장하지 마세요.", "source": "tests/fixtures/monotonic_nonlinear.csv"},
)


def _validation_source_state():
    """제품 소스와 모든 QA 실행 코드의 경로·해시를 같은 실행의 근거로 고정한다."""
    from probe.preflight import _source_fingerprint
    paths = sorted(path for path in (ROOT / "qa").rglob("*") if path.is_file() and path.suffix in {".py", ".cjs"})
    files = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    encoded = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8", errors="strict")
    return {"product_source_fingerprint": _source_fingerprint(), "qa_fingerprint": hashlib.sha256(encoded).hexdigest(), "qa_files": files}


class ValidationSourceGuard:
    """변경·읽기 실패를 감지하면 이 세션의 후속 모델 호출과 출시 증빙을 차단한다."""
    def __init__(self):
        self.baseline, self.failure = None, None
        try:
            self.baseline = _validation_source_state()
        except Exception as exc:
            self.failure = {"boundary": "SESSION_START", "reason": "SOURCE_GUARD_UNAVAILABLE", "error_type": type(exc).__name__}

    def check(self, boundary):
        if self.failure:
            raise ValueError("SOURCE_CHANGED")
        try:
            current = _validation_source_state()
        except Exception as exc:
            self.failure = {"boundary": boundary, "reason": "SOURCE_GUARD_UNAVAILABLE", "error_type": type(exc).__name__}
            raise ValueError("SOURCE_CHANGED") from None
        if current != self.baseline:
            self.failure = {"boundary": boundary, "reason": "SOURCE_CHANGED",
                "product_source_fingerprint": current["product_source_fingerprint"], "qa_fingerprint": current["qa_fingerprint"]}
            raise ValueError("SOURCE_CHANGED")

    def metadata(self):
        return {"status": "SOURCE_CHANGED" if self.failure else "UNCHANGED", "baseline": self.baseline, "failure": self.failure}

    def install(self, cleanup):
        original, dispatch = RoutedGateway.generate, RoutedGateway._generate
        async def guarded_generate(gateway, *args, **kwargs):
            self.check("MODEL_GENERATE")
            return await original(gateway, *args, **kwargs)
        async def guarded_dispatch(gateway, *args, **kwargs):
            self.check("MODEL_DISPATCH_READY")
            return await dispatch(gateway, *args, **kwargs)
        cleanup.enter_context(patch.object(RoutedGateway, "generate", guarded_generate))
        cleanup.enter_context(patch.object(RoutedGateway, "_generate", guarded_dispatch))


def _source_changed(summary, guard):
    summary.update(status="SOURCE_CHANGED", blocked_reason="SOURCE_CHANGED", native_search_validation="NOT_VALIDATED")
    summary["source_guard"] = guard.metadata()


def write_text(path, text):
    text.encode("utf-8", errors="strict")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", errors="strict", newline="")


def write_json(path, value):
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False, default=str) + "\n"
    if not _secret_free(path.name, text.encode("utf-8", errors="strict")):
        raise ValueError("RESULT_SECRET_BLOCKED")
    write_text(path, text)
    return hashlib.sha256(text.encode("utf-8", errors="strict")).hexdigest()


def log(folder, message):
    line = datetime.now(KST).isoformat() + " " + message + "\n"
    line.encode("utf-8", errors="strict")
    with (folder / "session.log").open("a", encoding="utf-8", errors="strict") as handle:
        handle.write(line)


def source_settings(source):
    if not source.is_file():
        raise ValueError("SOURCE_SETTINGS_UNAVAILABLE")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    db = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT kind,id,payload FROM control_configs WHERE kind IN ('model','connection') ORDER BY id").fetchall()
        models = [json.loads(row[2]) for row in rows if row[0] == "model"]
        connections = {row[1]: json.loads(row[2]) for row in rows if row[0] == "connection"}
        model = next((item for item in models if item.get("model_id") == MODEL_ID and item.get("connection_id") in connections), None)
        if model is None:
            raise ValueError("LUNA_PROFILE_UNCONFIGURED")
        profile = ModelProfile.model_validate(model)
        connection = Connection.model_validate(connections[profile.connection_id])
        # ControlStore 생성자의 스키마 쓰기 없이 운영 앱과 같은 상한 출처 판정을 읽는다.
        def config(kind, identity):
            row = db.execute("SELECT payload FROM control_configs WHERE kind=? AND id=?", (kind, identity)).fetchone()
            if row is None:
                raise ControlError("CONFIG_MISSING")
            return json.loads(row[0])
        from probe.science_policy import explicit_model_limits
        owner_limits = explicit_model_limits(SimpleNamespace(db=db, config=config), {"manager": model})
        return connection, profile, {"mode": "ro", "sha256_before": before,
            "explicit_model_limits": owner_limits, "model_limit_policy": "OWNER_SIDECAR_OR_HISTORICAL_AUDIT"}
    finally:
        db.close()


def mock_settings():
    connection = Connection(connection_id="science_mock", display_name="오프라인 검증", destination_approved=True)
    profile = ModelProfile(profile_id="AUTO-science_mock", connection_id=connection.connection_id, model_id=MODEL_ID,
        protocol="responses", context_limit=32768, input_byte_limit=32000, output_limit=8192,
        price=PriceRecord(input_per_million="1", output_per_million="2", cached_input_per_million="1",
            source="offline-simulation", checked_at=utc_now(), revision="mock-only", owner_verified=True))
    return connection, profile, {"mode": "NOT_READ_IN_SMOKE"}


class OfflineCredentials:
    """모의 검증은 환경·파일·운영체제 자격 증명 저장소를 조회하지 않는다."""
    def get(self, name):
        return None

    def active_secrets(self, references=()):
        return ()


class MockGateway:
    name = "control_broker"
    def __init__(self, case):
        self.case = case

    async def run_structured(self, *, role, instructions, input_text, output_type, model, metadata=None):
        bundle = json.loads(input_text)
        active = bundle["active_state"]
        if output_type.__name__ == "CriticResult":
            method = active["verified_experiments"][0]["method"]
            value = {"verdict": "FOLLOW_UP_REQUIRED" if method == "pearson_correlation" else "ACCEPT_WITH_LIMITATION",
                "issues": [{"code": "NONLINEARITY" if method == "pearson_correlation" else "CAUSALITY", "severity": "MEDIUM" if method == "pearson_correlation" else "LOW",
                    "detail": "합성 관측의 비선형성·단위·독립성과 인과 해석의 한계를 확인합니다."}],
                "requested_followups": [{"method": "spearman_correlation", "rationale": "순위 상관으로 민감도를 확인합니다.",
                    "hypothesis_id": active["active_hypotheses"][0]["hypothesis_id"]}] if method == "pearson_correlation" else [],
                "conclusion_strength": "NONE" if method == "pearson_correlation" else "WEAK"}
            return ModelRunResult(provider=self.name, model=MODEL_ID, output=output_type.model_validate(value),
                request_count=0, input_tokens=0, output_tokens=0, estimated_cost_usd=0, cost_status="KNOWN")
        context = json.loads(active["contract"]["objective"].split("\n", 1)[1])
        observations = context["observations"]
        if self.case["id"] == "csv" and len(observations) in {0, 2}:
            value = {"action": "ANALYZE", "rationale": "교육용 합성 자료의 연관성을 실제 도구로 계산합니다.",
                "analysis_plan": {"dataset_id": context["dataset"]["dataset_id"], "selected_variables": ["temperature", "growth"],
                    "method": "pearson_correlation" if not observations else "spearman_correlation", "justification": "수치 열과 요청한 민감도 검사를 확인했습니다.",
                    "requested_tools": ["stats.run", "visualization.render", "evidence.record"]}}
        elif self.case["id"] == "csv" and len(observations) in {1, 3}:
            value = {"action": "DELEGATE", "rationale": "검증된 결과의 한계를 다시 검토합니다.", "delegate_role": "verification_coordinator"}
        elif self.case["id"] == "literature" and not observations:
            value = {"action": "SEARCH", "rationale": "검색 선택과 관찰 저장만 오프라인으로 점검합니다.", "search_queries": ["photosynthesis light scientific evidence"]}
        else:
            value = {"action": "COMPLETE", "rationale": "오프라인 모의 자료에 대한 설계 초안을 완성했습니다.",
                "report_draft": {"report_type": "analysis" if self.case["id"] == "csv" else "design", "summary": "교육용 합성 자료의 검증된 연관성을 확인했습니다. 단위와 관측의 독립성은 제공되지 않았고 인과를 주장하지 않습니다." if self.case["id"] == "csv" else "직접 측정할 때 사용할 과학 탐구 설계안입니다.",
                    "explanation": "안정적인 원리 또는 실제 계산을 직접 측정한 결과와 구분합니다.",
                    "variables": [{"name": "온도" if self.case["id"] == "csv" else "빛의 조건", "role": "독립변인"}, {"name": "생장" if self.case["id"] == "csv" else "관찰 변화", "role": "종속변인"}],
                    "procedure": ["통제 조건을 일정하게 유지합니다.", "조건별 관찰을 같은 절차로 기록합니다."],
                    "measurement": "같은 시간과 도구로 관찰합니다.", "figure_refs": context.get("figure_refs", [])}}
        return ModelRunResult(provider=self.name, model=MODEL_ID, output=output_type.model_validate(value),
            request_count=0, input_tokens=0, output_tokens=0, estimated_cost_usd=0, cost_status="KNOWN")


def make_snapshot(case, variant, output_limit, connection, original, source_info, reference_dir=None, explicit_limits=None):
    data = original.model_dump(mode="json")
    automatic = original.profile_id.startswith("AUTO-") and (explicit_limits or {}).get(original.profile_id) is not True
    maximum = original.max_output_tokens or output_limit
    effective_output = min(output_limit, maximum) if automatic else min(output_limit, original.output_limit, maximum)
    data.update(profile_id="science_luna", output_limit=effective_output, max_retries=0)
    if automatic:
        data.update(context_limit=max(original.context_limit, 131072),
            max_input_tokens=max(original.max_input_tokens or 0, 128000),
            task_output_limits={"planning": min(2048, effective_output), "report": effective_output})
    else:
        # 비교 요청은 소유자가 정한 하드캡을 낮출 수만 있고 높일 수는 없다.
        data["task_output_limits"] = {key: min(value, effective_output) for key, value in original.task_output_limits.items()}
    profile = ModelProfile.model_validate(data)
    from probe.control_plane import NewResearch, resolved_depth
    snapshot = NewResearch(question=case["question"], execution_mode="SCIENCE_AUTO", settings_version=2,
        model_profile_id=profile.profile_id, performance_profile="BALANCED", research_profile_mode="DISABLED",
        ai_report_enabled=True, egress="research", public_search_consent=True, search_policy="AUTO",
        search_required=case["id"] == "literature", run_limit_usd=RUN_CAP, max_elapsed_sec=300,
        search_attempt_limit=3, report_style="friendly").model_dump(mode="json")
    snapshot.update(models={role: profile.model_dump(mode="json") for role in ROLES},
        connections={connection.connection_id: connection.model_dump(mode="json")},
        routing={role: profile.profile_id for role in ROLES}, depth_limits={**resolved_depth("standard"), "attempts": 20},
        monthly_limit_usd=str(GLOBAL_CAP), request_limit_usd=str(RUN_CAP),
        profile_revisions={profile.profile_id: 1}, validation_variant=variant, source=source_info)
    snapshot.update(explicit_model_limits={} if automatic else {profile.profile_id: True},
        validation_original_profile_id=original.profile_id,
        validation_limit_policy="AUTOMATIC_DEFAULT_COMPARISON" if automatic else "EXPLICIT_OR_MANUAL_LIMITS_PRESERVED",
        validation_requested_output_limit=output_limit, validation_effective_output_limit=effective_output)
    if reference_dir is not None:
        snapshot["reference_examples_dir"] = str(reference_dir)
    if source_info:
        snapshot["source_relative"] = "comparison.csv"
    return snapshot


def hosted_context_preflight(profile):
    """서버 검색 문맥과 실제 전송 바이트 경계가 혼동되지 않는지 오프라인으로 확인한다."""
    from probe.science_policy import request_cost_bound
    output_limit = min(2048, profile.output_limit, profile.task_output_limits.get("planning", 2048))
    result = {"context_boundary_passed": False, "sample_wire_bytes": 1024,
        "wire_byte_limit": profile.input_byte_limit, "hosted_input_token_bound": 128000,
        "search_output_limit": output_limit, "context_limit": profile.context_limit,
        "max_input_tokens": profile.max_input_tokens, "model_cost_upper_bound_usd": None,
        "budget_admission_is_separate": True}
    try:
        cost = request_cost_bound(profile, 1024, 128000, output_limit)
    except ControlError as exc:
        result["blocked_reason"] = exc.code
    else:
        result.update(context_boundary_passed=True, model_cost_upper_bound_usd=str(cost))
    return result


def metrics(db, rid, elapsed, case, variant, repetition, output_limit, execution):
    store = ControlStore(db)
    ledger = store.ledger(rid)
    run = store.run(rid)
    models = [dict(row) for row in db.execute("SELECT actor_role,status,started_at,finished_at,input_tokens,output_tokens,latency_ms,estimated_cost_usd,error_code FROM agent_runs WHERE research_id=? AND provider IS NOT NULL", (rid,))]
    audits = [(row["kind"], json.loads(row["payload"]), row["created_at"]) for row in db.execute(
        "SELECT kind,payload,created_at FROM control_audit WHERE research_id=? AND kind IN ('NORMALIZED_RESPONSE_SETTLED','SEARCH_COMPLETED') ORDER BY seq", (rid,))]
    settled = [(value, timestamp) for kind, value, timestamp in audits if kind == "NORMALIZED_RESPONSE_SETTLED"]
    searches = [value for kind, value, _ in audits if kind == "SEARCH_COMPLETED"]
    usages = [value.get("usage", {}) for value, _ in settled]
    report = db.execute("SELECT payload FROM control_configs WHERE kind='ai_report' AND id=?", (rid,)).fetchone()
    record = json.loads(report[0]) if report else {}
    figure_records = [dict(row) for row in db.execute("SELECT artifact_id,relative_path,sha256 FROM artifacts WHERE research_id=? AND status='VERIFIED' AND artifact_type='FIGURE'", (rid,))]
    figures = len(figure_records)
    from probe.report_ux import report_visual_specs
    visual_specs = report_visual_specs(record.get("draft")) if record.get("status") == "READY" else []
    single_roles = [json.loads(row[0]) for row in db.execute("SELECT payload FROM control_audit WHERE research_id=? AND kind='SCIENCE_SINGLE_ROLE' ORDER BY seq", (rid,))]
    observations = [json.loads(row[0]) for row in db.execute("SELECT output_json FROM runtime_steps WHERE research_id=? AND step_key LIKE 'science:observation:%' AND status='COMPLETED' ORDER BY rowid", (rid,))]
    methods = [row[0] for row in db.execute("SELECT method FROM experiments WHERE research_id=? AND status='VERIFIED' ORDER BY rowid", (rid,))]
    reviews = db.execute("SELECT COUNT(*) FROM critic_reviews WHERE research_id=?", (rid,)).fetchone()[0]
    citations = record.get("draft", {}).get("claims", [])
    literature_ids = {row[0] for row in db.execute("SELECT evidence_id FROM evidence WHERE research_id=? AND source_type='LITERATURE' AND status='VERIFIED'", (rid,))}
    literature_citations = sum(claim.get("evidence_id") in literature_ids for claim in citations)
    paid_requests = [item for item in ledger["requests"] if item["status"] in {"DISPATCHED", "SETTLED", "UNRESOLVED"}]
    search_calls = sum(value.get("billed_search_calls") if value.get("billed_search_calls") is not None else len(value.get("hosted_tool_calls", [])) for value in searches)
    runtime_finished = run["status"] == "COMPLETED" and record.get("status") == "READY"
    requirements_met = runtime_finished and (not search_calls and len(models) == 1 if case["id"] == "principle" else
        search_calls > 0 and literature_citations > 0 if case["id"] == "literature" else
        {"pearson_correlation", "spearman_correlation"}.issubset(methods) and reviews > 0 and figures > 0)
    def since_start(timestamp):
        return round(max(0, (datetime.fromisoformat(timestamp)-datetime.fromisoformat(run["started_at"])).total_seconds()), 3) if timestamp and run["started_at"] else None
    responses = [timestamp for _, timestamp in settled] + [row["finished_at"] for row in models if row["status"] == "COMPLETED" and row["finished_at"]]
    report_event = db.execute("SELECT created_at FROM runtime_events WHERE research_id=? AND event_type='REPORT_WRITING_COMPLETED' ORDER BY seq LIMIT 1", (rid,)).fetchone()
    raw = {"research_id": rid, "execution": execution, "requirements_met": requirements_met,
           "observations": observations, "model_runs": models,
           "ledger": [{key: row[key] for key in ("id", "role", "purpose", "status", "reserved", "settled")} for row in ledger["requests"]],
           "provider_measurements": [{key: value[key] for key in ("reservation_id", "response_id", "resolved_model_id", "usage", "latency_ms", "finish_reason") if key in value} for value, _ in settled],
           "search_measurements": [{**{key: value[key] for key in ("provider", "response_id", "billed_search_calls", "usage", "status") if key in value},
                "hosted_tool_actions": len(value.get("hosted_tool_calls", []))} for value in searches],
           "analysis_methods": methods, "critic_reviews": reviews, "single_role_mapping": single_roles,
           "figure_artifacts": figure_records, "diagram_specs": visual_specs, "report": record}
    return {"research_id": rid, "case": case["id"], "variant": variant, "repetition": repetition,
        "output_limit": output_limit, "execution": execution, "elapsed_sec": round(elapsed, 3),
        "model_calls": len(paid_requests), "model_invocations": len(models), "model_roles": [row["actor_role"] for row in models],
        "effective_model_roles": [value["effective_role"] for value in single_roles] if single_roles else [row["actor_role"] for row in models],
        "search_calls": search_calls, "search_model_requests": sum(bool(value.get("usage", {}).get("web_search_calls")) for value, _ in settled),
        "tool_calls": db.execute("SELECT COUNT(*) FROM tool_calls tc JOIN agent_runs ar USING(agent_run_id) WHERE ar.research_id=? OR ar.contract_id IN (SELECT contract_id FROM contracts WHERE research_id=?)", (rid, rid)).fetchone()[0],
        "input_tokens": sum(value.get("input_tokens") or 0 for value in usages), "output_tokens": sum(value.get("output_tokens") or 0 for value in usages),
        "model_latency_ms": sum(value.get("latency_ms") or 0 for value, _ in settled),
        "first_model_result_sec": min((since_start(timestamp) for timestamp in responses), default=None),
        "first_report_sec": since_start(report_event[0]) if report_event else None,
        "spent_usd": ledger["spent"], "unsettled_usd": ledger["unresolved"], "reserved_usd": ledger["reserved"],
        "finished": runtime_finished, "case_requirements_met": requirements_met, "status": run["status"], "stop_reason": run["error"],
        "report_status": record.get("status", "NOT_CREATED"), "figure_count": figures, "figure_artifacts": figure_records,
        "diagram_spec_count": len(visual_specs),
        "citation_count": len(citations), "literature_citation_count": literature_citations,
        "analysis_methods": methods, "critic_review_count": reviews,
        "citation_validation": "EXISTING_REPORT_GATE" if record.get("status") == "READY" else "NOT_VERIFIED",
        "unit_validation": "NOT_PROVIDED_IN_CSV" if case["id"] == "csv" else "PLANNED_VARIABLES_ONLY",
        "visual_quality": "RENDERED_ARTIFACTS_ONLY_NOT_HUMAN_REVIEWED", "quality_superiority": "NOT_ESTABLISHED"}, raw


def save_summary(folder, summary):
    rows = summary.get("runs", [])
    lines = ["# 과학 탐구 비교 검증", "", f"상태: {summary['status']}",
        f"실행: {summary['execution']} · 전체 한도 ${GLOBAL_CAP} · 연구 한도 ${RUN_CAP} · 연구 시간 300초", "",
        "원본 앱 DB는 mode=ro로 설정만 읽습니다. 저장된 Windows 자격 증명을 재등록하지 않습니다. "
        "비교 대상은 동일 LUNA·자료·추론 설정·결정론적 도구·검증·자체 검토 및 수정 기회를 공유합니다.", "",
        "단일 LUNA는 앱의 판단 루프를 호출하지 않는 독립 실행기에서 행동·도구 결과·자체 검토·수정을 수행합니다. "
        "원장·검색·결정론적 도구·보고서 검증은 공통이며 같은 사례의 참고 예시는 첫 선정 내용으로 고정합니다. 실제 실행 전에는 속도·품질 우위를 판정하지 않습니다. "
        "관측 단위가 없는 합성 CSV의 단위·독립성을 확인했다고 기록하지 않습니다.", ""]
    if summary.get("cross_session_budget"):
        previous = summary["cross_session_budget"]
        lines += ["이전 실제 검증 누적 지출: $" + str(previous.get("previous_spent_usd")) +
                  " · 이번 세션 가능 상한: $" + str(summary.get("session_available_usd", "미확인")),
                  "중복 실행은 운영체제 파일 잠금으로 차단하며 이전 미정산 요청이 있으면 실행하지 않습니다.", ""]
    if summary.get("blocked_reason"):
        lines += ["차단 사유: " + summary["blocked_reason"], f"유료 전송 요청: {summary['live_paid_calls']}회", ""]
    if rows:
        lines += ["| 사례 | 방식 | 반복 | 요청/적용 출력 한도 | 시간(초) | 모델 API | 검색 | 도구 | 비용($) | 실행 완료 | 요구 충족 | 그림 | 인용 |", "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|"]
        for row in rows:
            lines.append(f"| {row['case']} | {row['variant']} | {row['repetition']} | {row['output_limit']}/{row.get('effective_output_limit', row['output_limit'])} | {row['elapsed_sec']} | {row['model_calls']} | {row['search_calls']} | {row['tool_calls']} | {row['spent_usd']} | {row['finished']} | {row['case_requirements_met']} | {row['figure_count']} | {row['citation_count']} |")
        lines += ["", "1024/4096/8192 실험에서는 출력 상한만 바꿉니다. 같은 합계·연구 한도를 유지하므로 예산 차단과 출력 부족을 각각 기록합니다. "
                  "소유자가 명시한 상한은 넘기지 않으며 요청보다 작은 적용값과 planning/report별 상한은 원시 결과에 기록합니다. "
                  "세 번의 비교는 분산 관찰용이며 통계적 품질 우위의 증거로 해석하지 않습니다."]
        lines += ["", "원시 초안과 실제 검증 그림은 다음 링크에서 확인할 수 있습니다. 연구별 PDF·페이지 PNG·텍스트와 객관 검사 결과를 visual-quality 폴더에 보존합니다. 도식 사양은 raw 파일에 있으며 내용·의미·시각 설명력은 사람 검토가 필요합니다.", ""]
        for row in rows:
            identity = row["research_id"]
            lines.append(f"- {row['case']} · {row['variant']} · {row['repetition']}: [원시 결과](raw/{identity}.json) · 도식 사양 {row['diagram_spec_count']}개")
            if row.get("first_rendered_pdf_sec") is not None:
                lines.append(f"  [PDF](visual-quality/{identity}/report.pdf) · [객관 검사](visual-quality/{identity}/visual-quality.json) · {row['visual_objective_status']} · 사람 검토 필요")
            for figure in row["figure_artifacts"]:
                lines += ["", f"![검증된 분석 그림](workspace/{identity}/{figure['relative_path'].replace(chr(92), '/')})", ""]
    if summary.get("execution") == "MOCK_SIMULATION":
        lines += ["", "모의 문헌 검색은 네트워크 없이 검색 행동과 미확보 근거에 따른 중단만 확인합니다. "
                  "실제 문헌·검색 속도·유료 모델 성능은 검증하지 않았습니다."]
    summary_hash = write_json(folder / "summary.json", summary)
    write_text(folder / "report.md", "\n".join(lines) + "\n")
    write_json(folder / "sha256.json", {"summary.json": summary_hash,
        "report.md": hashlib.sha256((folder / "report.md").read_bytes()).hexdigest(),
        **{row["research_id"] + ".json": row["raw_sha256"] for row in rows},
        **({"blocked.json": summary["blocked_raw_sha256"]} if summary.get("blocked_raw_sha256") else {})})


def record_completed_native_search(folder, summary, *, source_guard=None):
    """같은 소스에서 실제 문헌 비교가 모두 정산·검증된 경우에만 출시 표식을 갱신한다."""
    summary["native_search_validation"] = "NOT_VALIDATED"
    if source_guard is None:
        return
    try:
        source_guard.check("RELEASE_EVIDENCE")
    except ValueError:
        _source_changed(summary, source_guard)
        return
    literature = [row for row in summary.get("runs", []) if row["case"] == "literature" and row["category"] == "core"]
    ledger = summary.get("ledger", {})
    if (summary.get("execution") != "LIVE_USER_SESSION" or summary.get("live_paid_calls", 0) <= 0
            or len(literature) != 6 or not all(row["case_requirements_met"] for row in literature)
            or Decimal(ledger.get("reserved", "0")) != 0 or Decimal(ledger.get("unresolved", "0")) != 0
            or Decimal(ledger.get("spent", "0")) <= 0 or summary.get("blocked_reason")):
        return
    runs = [{"research_id": row["research_id"], "artifact_path": (folder / "raw" / (row["research_id"] + ".json")).relative_to(ROOT).as_posix(),
             "artifact_sha256": row["raw_sha256"]} for row in literature]
    from probe.preflight import record_search_validation
    try:
        source_guard.check("RELEASE_MARKER_WRITE")
        marker = record_search_validation(status="VALIDATED", provider="openai.web_search",
            evidence={"execution": "LIVE_USER_SESSION", "runs": runs})
    except (ValueError, OSError):
        if source_guard.failure:
            _source_changed(summary, source_guard)
            return
        summary["native_search_validation"] = "EVIDENCE_GATE_NOT_PASSED"
        return
    summary["native_search_validation"] = "VALIDATED" if marker["validated"] else "NOT_VALIDATED"


async def run_validation(folder, source, *, smoke=False):
    # 종료 결과 저장이나 원장 조회가 실패해도 전체 실행 경계에서 잠금을 해제한다.
    with ExitStack() as cleanup:
        return await _run_validation(folder, source, smoke=smoke, cleanup=cleanup)


async def _run_validation(folder, source, *, smoke=False, cleanup):
    execution = "MOCK_SIMULATION" if smoke else "LIVE_USER_SESSION"
    summary = {"status": "STARTING", "execution": execution, "model_id": MODEL_ID, "global_cap_usd": str(GLOBAL_CAP),
               "run_cap_usd": str(RUN_CAP), "live_paid_calls": 0, "runs": [], "comparison_limit": "INDEPENDENT_SINGLE_LUNA_SHARED_TOOLS_AND_REPORT_GATE"}
    db = store = live_budget = source_guard = None
    session_cap = GLOBAL_CAP
    try:
        source_guard = ValidationSourceGuard()
        summary["source_guard"] = source_guard.metadata()
        source_guard.check("SESSION_START")
        source_guard.install(cleanup)
        connection, original, source_meta = mock_settings() if smoke else source_settings(source)
        summary["source_settings"] = source_meta
        reference_dir = (ROOT / "build/workbench" if smoke else source.parent) / "reference_examples"
        fixed_examples = {}
        summary["reference_examples_policy"] = "같은 사례의 첫 선정 내용을 모든 방식·반복에서 고정 사용"
        owner_limits = source_meta.get("explicit_model_limits", {})
        prepared = make_snapshot(CASES[0], "app", 8192, connection, original, None, reference_dir, owner_limits)
        comparison_profile = ModelProfile.model_validate(prepared["models"]["manager"])
        summary["inference_settings"] = {"reasoning_policy": original.reasoning_policy.value, "temperature": original.temperature,
            "top_p": original.top_p, "seed": original.seed, "input_byte_limit": original.input_byte_limit,
            "price_revision": original.price.revision if original.price else None,
            "context_limit_original": original.context_limit, "context_limit_comparison": comparison_profile.context_limit,
            "max_input_tokens_original": original.max_input_tokens, "max_input_tokens_comparison": comparison_profile.max_input_tokens,
            "context_adjustment": prepared["validation_limit_policy"], "wire_byte_limit_changed": False,
            "explicit_model_limits": prepared["explicit_model_limits"],
            "output_limit_original": original.output_limit, "output_limit_comparison": comparison_profile.output_limit,
            "task_output_limits_original": original.task_output_limits, "task_output_limits_comparison": comparison_profile.task_output_limits}
        credentials = OfflineCredentials() if smoke else Credentials(ROOT, folder / "workspace")
        if not smoke:
            try:
                configured = bool(credentials.get(connection.credential_env_name))
            except Exception:
                configured = False
            if not configured:
                raise ValueError("CREDENTIAL_UNCONFIGURED")
            if not connection.enabled or not connection.destination_approved:
                raise ValueError("EGRESS_BLOCKED")
            if original.price is None or not original.price.owner_verified:
                raise ValueError("PRICE_UNKNOWN")
            live_budget = LiveValidationBudget(ROOT / "build/science-live", folder, GLOBAL_CAP)
            cleanup.callback(live_budget.close)
            live_budget.acquire()
            session_cap = live_budget.available_usd
            summary["cross_session_budget"] = live_budget.metadata
            summary["session_available_usd"] = str(session_cap)
        summary["hosted_context_preflight"] = hosted_context_preflight(comparison_profile)
        database, workspace = folder / "state.sqlite", folder / "workspace"
        db = initialize(database)
        cleanup.callback(db.close)
        store = ControlStore(db)
        store.put("defaults", "global", Defaults(monthly_limit_usd=session_cap, request_limit_usd=RUN_CAP, search_attempt_limit=3))
        store.put("connection", connection.connection_id, connection)
        repetitions = 1 if smoke else 3
        schedule = [(case, variant, repetition, 8192, "core") for repetition in range(1, repetitions+1)
                    for case in CASES for variant in (("app", "strong_single") if repetition % 2 else ("strong_single", "app"))]
        if not smoke:
            schedule += [(CASES[0], variant, 1, limit, "output_ablation") for limit in (1024, 4096, 8192) for variant in ("app", "strong_single")]
        for case, variant, repetition, limit, category in schedule:
            source_guard.check("RESEARCH_START")
            ledger = store.ledger()
            exposure = sum(Decimal(ledger[key]) for key in ("spent", "reserved", "unresolved"))
            if Decimal(ledger["unresolved"]) > 0 or exposure + RUN_CAP > session_cap:
                summary["status"] = "NEEDS_RECONCILIATION" if Decimal(ledger["unresolved"]) > 0 else "GLOBAL_BUDGET_STOPPED"
                break
            if case["id"] == "literature" and not summary["hosted_context_preflight"]["context_boundary_passed"]:
                raise ValueError("OWNER_LIMIT_SEARCH_PREFLIGHT_BLOCKED")
            source_info = None
            if case.get("source"):
                content = (ROOT / case["source"]).read_text(encoding="utf-8", errors="strict")
                destination = workspace / "inputs" / "comparison.csv"
                if not destination.is_file():
                    write_text(destination, content)
                source_info = {"sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
            snapshot = make_snapshot(case, variant, limit, connection, original, source_info, reference_dir, owner_limits)
            rid = StateService(db, Workspace(workspace)).create_research(case["question"])
            Workspace(workspace).prepare(rid)
            db.execute("INSERT INTO control_runs(research_id,title,status,snapshot,created_at) VALUES(?,?,'STARTING',?,?)",
                       (rid, case["id"] + " · " + variant, to_json(snapshot), utc_now().isoformat()))
            from probe.research_examples import freeze_reference_examples
            from probe.research_report import _save
            if case["id"] not in fixed_examples:
                fixed_examples[case["id"]] = freeze_reference_examples(StateService(db, Workspace(workspace)), store, rid, snapshot)
            else:
                _save(store, "reference_examples", rid, fixed_examples[case["id"]])
            log(folder, f"시작 {case['id']} {variant} 반복 {repetition} 출력 {limit}")
            started = perf_counter()
            factory = (lambda *_: (DirectSingleMockGateway(case) if variant == "strong_single" else MockGateway(case))) if smoke else None
            if variant == "strong_single":
                await execute_direct_single(database, workspace, rid, credentials=credentials,
                    provider_factory=factory, search_runner=offline_no_search if smoke else None)
            elif smoke:
                async def no_network(*args, **kwargs):
                    return {"status": "OFFLINE_SEARCH_NOT_TESTED"}
                with patch("probe.search_policy.run_search", no_network):
                    await execute(database, workspace, rid, provider_factory=factory)
            else:
                await execute(database, workspace, rid, provider_factory=factory)
            row, raw = metrics(db, rid, perf_counter()-started, case, variant, repetition, limit, execution)
            source_changed = False
            try:
                source_guard.check("RESEARCH_COMPLETED")
            except ValueError:
                source_changed = True
                _source_changed(summary, source_guard)
                row.update(finished=False, case_requirements_met=False)
                raw["requirements_met"] = False
            row["source_guard_status"] = source_guard.metadata()["status"]
            raw["source_guard"] = source_guard.metadata()
            row["requested_output_limit"] = limit
            row["effective_output_limit"] = snapshot["validation_effective_output_limit"]
            row["effective_task_output_limits"] = snapshot["models"]["manager"]["task_output_limits"]
            row["model_limit_policy"] = snapshot["validation_limit_policy"]
            raw["comparison_model_limits"] = {key: row[key] for key in ("requested_output_limit", "effective_output_limit", "effective_task_output_limits", "model_limit_policy")}
            row["category"] = category
            row["reference_examples_fingerprint"] = fixed_examples[case["id"]]["fingerprint"]
            row["reference_examples_count"] = len(fixed_examples[case["id"]]["examples"])
            raw["reference_examples"] = fixed_examples[case["id"]]
            raw["comparison_runner"] = "DIRECT_SINGLE_LUNA" if variant == "strong_single" else "SCIENCE_AUTO"
            visual_started = perf_counter()
            if row["report_status"] == "READY" and not source_changed:
                try:
                    protected = () if smoke else credentials.active_secrets([connection.credential_env_name])
                    visual = inspect_report(StateService(db, Workspace(workspace)), rid,
                        folder / "visual-quality" / rid, protected_values=protected)
                except Exception as exc:
                    visual = {"status": "INSPECTION_UNAVAILABLE", "objective_status": "INCOMPLETE",
                        "visual_and_scientific_quality": "HUMAN_REVIEW_REQUIRED", "content_superiority": "NOT_ASSESSED",
                        "errors": [{"code": type(exc).__name__, "scope": "LOCAL_VISUAL_INSPECTION_ONLY"}]}
            else:
                visual = {"status": "NO_READY_REPORT", "objective_status": "INCOMPLETE",
                    "visual_and_scientific_quality": "HUMAN_REVIEW_REQUIRED", "content_superiority": "NOT_ASSESSED"}
            row["visual_quality"] = visual["visual_and_scientific_quality"]
            row["visual_objective_status"] = visual["objective_status"]
            row["visual_validation_elapsed_sec"] = round(perf_counter() - visual_started, 3)
            row["total_elapsed_sec"] = round(perf_counter() - started, 3)
            row["first_valid_report_sec"] = row["first_report_sec"]
            row["first_rendered_pdf_sec"] = row["total_elapsed_sec"] if visual.get("artifacts", {}).get("pdf") else None
            raw["visual_quality"] = visual
            row["question_sha256"] = hashlib.sha256(case["question"].encode("utf-8", errors="strict")).hexdigest()
            row["source_sha256"] = source_info["sha256"] if source_info else None
            row["raw_sha256"] = write_json(folder / "raw" / (rid + ".json"), raw)
            summary["runs"].append(row)
            summary["live_paid_calls"] = 0 if smoke else sum(item["status"] in {"DISPATCHED", "SETTLED", "UNRESOLVED"} for item in store.ledger()["requests"])
            save_summary(folder, summary)
            log(folder, f"종료 {case['id']} {variant}: {row['status']} 비용 {row['spent_usd']}")
            if source_changed:
                break
        summary["ledger"] = {key: store.ledger()[key] for key in ("spent", "reserved", "unresolved", "monthly_exposure")}
        if summary["status"] == "STARTING":
            if smoke:
                checks = {"known_principle_zero_search": all(row["finished"] and row["model_invocations"] == 1 and row["search_calls"] == 0 for row in summary["runs"] if row["case"] == "principle"),
                    "csv_two_verified_methods_with_review_and_figure": all(row["case_requirements_met"] for row in summary["runs"] if row["case"] == "csv"),
                    "unavailable_literature_does_not_finish": all(not row["finished"] and row["stop_reason"] == "UNRESOLVED_VERIFICATION" for row in summary["runs"] if row["case"] == "literature"),
                    "independent_single_luna_with_self_review": all(set(row["model_roles"]) == {"manager"} and (row["critic_review_count"] >= 2 if row["case"] == "csv" else True) for row in summary["runs"] if row["variant"] == "strong_single"),
                    "same_fixed_reference_examples": all(len({row["reference_examples_fingerprint"] for row in summary["runs"] if row["case"] == case["id"]}) == 1 for case in CASES),
                    "hosted_search_context_boundary": summary["hosted_context_preflight"]["context_boundary_passed"],
                    "zero_paid_requests": summary["live_paid_calls"] == 0,
                    "completed_reports_pass_objective_visual_checks": all(row["visual_objective_status"] == "PASS" for row in summary["runs"] if row["finished"])}
                summary["smoke_checks"] = checks
                summary["status"] = "MOCK_SIMULATION_COMPLETED" if all(checks.values()) else "MOCK_SIMULATION_FAILED"
            else:
                summary["status"] = "LIVE_COMPARISON_COMPLETED" if all(row["case_requirements_met"] and row["visual_objective_status"] == "PASS" for row in summary["runs"] if row["category"] == "core") else "LIVE_COMPARISON_PARTIAL"
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,80}", str(exc)) else getattr(exc, "code", type(exc).__name__)
        summary.update(status="SOURCE_CHANGED" if code == "SOURCE_CHANGED" else "BLOCKED", blocked_reason=code)
        log(folder, "차단 " + code)
    finally:
        if source_guard is not None:
            summary["source_guard"] = source_guard.metadata()
        if store is not None:
            ledger = store.ledger()
            summary["ledger"] = {key: ledger[key] for key in ("spent", "reserved", "unresolved", "monthly_exposure")}
            summary["live_paid_calls"] = 0 if smoke else sum(item["status"] in {"DISPATCHED", "SETTLED", "UNRESOLVED"} for item in ledger["requests"])
        if db is not None:
            db.close()
        if not smoke and summary.get("source_settings") and source.is_file():
            summary["source_settings"]["sha256_after"] = hashlib.sha256(source.read_bytes()).hexdigest()
        if summary.get("blocked_reason"):
            summary["blocked_raw_sha256"] = write_json(folder / "raw" / "blocked.json", {
                "status": summary["status"], "reason": summary["blocked_reason"], "live_paid_calls": summary["live_paid_calls"],
                "ledger": summary.get("ledger"), "source_settings": summary.get("source_settings")})
    try:
        if live_budget is not None:
            summary["cross_session_budget"] = live_budget.metadata
        record_completed_native_search(folder, summary, source_guard=source_guard)
        save_summary(folder, summary)
        return summary
    finally:
        if live_budget is not None:
            live_budget.close()


def main():
    parser = argparse.ArgumentParser(description="동일 LUNA 과학 탐구 비교 검증")
    parser.add_argument("--live", action="store_true", help="사용자 세션에서 최대 전체 $3의 실제 API 검증")
    parser.add_argument("--smoke", action="store_true", help="키 조회와 네트워크 없이 오프라인 모의 검증")
    parser.add_argument("--open-results", action="store_true")
    parser.add_argument("--settings-db", type=Path, default=ROOT / "build/workbench/state.sqlite")
    args = parser.parse_args()
    if args.live == args.smoke:
        parser.error("--live 또는 --smoke 중 하나만 지정하세요.")
    folder = ROOT / "build/science-live" / (datetime.now(KST).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6])
    folder.mkdir(parents=True, exist_ok=False)
    log(folder, "검증 세션 시작 · 원본 설정 읽기 전용")
    if args.open_results and os.name == "nt":
        os.startfile(str(folder))
    summary = asyncio.run(run_validation(folder, args.settings_db, smoke=args.smoke))
    if sys.stdout is not None:
        print(json.dumps({"status": summary["status"], "results": str(folder), "live_paid_calls": summary["live_paid_calls"]}, ensure_ascii=False))
    return 0 if summary["status"] in {"MOCK_SIMULATION_COMPLETED", "LIVE_COMPARISON_COMPLETED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
