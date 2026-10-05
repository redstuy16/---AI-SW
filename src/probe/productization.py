"""기존 설정·기능 검사·비용 원장으로 제품 연결 준비 상태를 기록한다."""
from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import os

from pydantic import Field

from .control_plane import Connection, ControlError, ModelProfile
from .providers.base import ModelProviderError
from .schemas import StrictModel, new_id, utc_now


class QualificationRequest(StrictModel):
    consent: bool = False
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    max_total_usd: Decimal = Field(default=Decimal("0.10"), gt=0, le=Decimal("0.25"), allow_inf_nan=False)
    discovery: bool = True
    approve_discovery: bool = False
    retest: bool = False


def revision(store, kind, identity):
    row = store.db.execute("SELECT revision FROM control_configs WHERE kind=? AND id=?", (kind, identity)).fetchone()
    return row[0] if row else 0


def credential_version(app, connection):
    meta = app.credentials.metadata(connection.credential_env_name)
    return {"active_source": meta["active_source"], "last_changed_at": meta.get("last_changed_at"),
            "configured": meta["configured"], "owner_revision": revision(app.store, "credential_change", connection.connection_id),
            "environment_process": os.getpid() if meta["active_source"] == "environment" else None}


def qualification_view(app, profile_id):
    try:
        record = app.store.config("qualification", profile_id)
        profile = ModelProfile.model_validate(app.store.config("model", profile_id))
        connection = Connection.model_validate(app.store.config("connection", profile.connection_id))
    except ControlError:
        return {"status": "NOT_VALIDATED", "research_efficacy": "NOT_VALIDATED"}
    fresh = (record.get("profile_revision") == revision(app.store, "model", profile_id)
             and record.get("connection_revision") == revision(app.store, "connection", profile.connection_id)
             and record.get("credential_version") == credential_version(app, connection))
    return {**record, "status": record["status"] if fresh else "STALE", "current": fresh,
            "research_efficacy": "NOT_VALIDATED"}


def recover_qualifications(app):
    from .control_plane import process_alive
    for record in app.store.configs("qualification_run"):
        if record["status"] != "RUNNING" or record.get("owner_pid") and process_alive(record["owner_pid"]):
            continue
        app.store.recover_ledger(record["run_id"])
        record.update(status="NEEDS_RECONCILIATION", automatic_retry=False, ledger=app.store.ledger(record["run_id"]))
        identity, expected = record.pop("request_key"), record.pop("revision")
        record["request_key"] = identity
        app.store.put("qualification_run", identity, record, expected)


def picker_label(row, rules):
    rule = next((r for r in rules if r["provider"] == row["provider"] and r["model_id"] == row["model_id"]), None)
    if row["provider"] == "openai":
        group = {"LOW": "빠른 GPT", "HIGH": "고성능 GPT"}.get((rule or row).get("cost_class"), "권장 GPT")
        return group + (" · " + rule["display_name"] if rule else " · 저장 모델")
    return "로컬 모델" if row["provider"] == "openai_compatible" else "고급 제공사 모델"


def onboarding(app, rows=None):
    from .preflight import productization_status
    if rows is None:
        from .product_policy import catalog
        rows = catalog(app.store)["models"]
    candidates = [r for r in rows if r["provider"] == "openai" and r.get("profile_id")]
    models = []
    for row in candidates:
        connection = Connection.model_validate(app.store.config("connection", row["connection_id"]))
        models.append({"profile_id": row["profile_id"], "label": row.get("picker_label", row["display_name"]),
                       "credential": app.credentials.metadata(connection.credential_env_name),
                       "qualification": qualification_view(app, row["profile_id"])})
    connected = [m for m in models if m["qualification"]["status"] == "VALIDATED"]
    key_ready = any(m["credential"]["configured"] for m in models)
    steps = [
        {"id": "key", "title": "API 키 등록", "complete": key_ready},
        {"id": "connection", "title": "GPT 연결 확인", "complete": bool(connected)},
        {"id": "model", "title": "모델 선택", "complete": bool(candidates)},
        {"id": "budget", "title": "예산 설정", "complete": revision(app.store, "defaults", "global") > 0},
        {"id": "research", "title": "첫 연구", "complete": bool(app.store.db.execute("SELECT 1 FROM control_runs LIMIT 1").fetchone())},
    ]
    return {"steps": steps, "models": models, "gpt_connectivity": "VALIDATED" if connected else "NOT_VALIDATED",
            "platform_checks": productization_status(),
            "research_efficacy": "NOT_VALIDATED", "view_paid_calls": 0,
            "notice": "연결 검증은 연구 정확성 검증이 아닙니다. 저장·목록·화면 조회로 유료 검사하지 않습니다."}


async def qualify(app, profile_id, body, *, client_factory=None):
    from .provider_checks import check_model
    request = QualificationRequest.model_validate(body)
    if not request.consent:
        raise ControlError("PAID_TEST_CONSENT_REQUIRED")
    profile = ModelProfile.model_validate(app.store.config("model", profile_id))
    connection = Connection.model_validate(app.store.config("connection", profile.connection_id))
    if connection.adapter_id != "openai" and not (connection.adapter_id == "openai_compatible" and connection.endpoint_class == "loopback"):
        raise ControlError("QUALIFICATION_PROVIDER_DENIED")
    if not connection.enabled or not connection.destination_approved:
        raise ControlError("EGRESS_BLOCKED")
    if connection.endpoint_class == "cloud" and not app.credentials.metadata(connection.credential_env_name)["configured"]:
        raise ControlError("CREDENTIAL_UNCONFIGURED")
    key = sha256((profile_id + ":" + request.idempotency_key).encode("utf-8", errors="strict")).hexdigest()
    fingerprint = sha256(request.model_dump_json().encode("utf-8", errors="strict")).hexdigest()
    try:
        cached = app.store.config("qualification_run", key)
    except ControlError:
        cached = None
    if cached:
        if cached["request_fingerprint"] != fingerprint:
            raise ControlError("IDEMPOTENCY_CONFLICT")
        status = cached["status"]
        if status == "RUNNING":
            status = "NEEDS_RECONCILIATION"
        elif status in {"VALIDATED", "OFFLINE_VALIDATED"} and (
                cached.get("profile_revision") != revision(app.store, "model", profile_id)
                or cached.get("connection_revision") != revision(app.store, "connection", connection.connection_id)
                or cached.get("credential_version") != credential_version(app, connection)):
            status = "STALE"
        return {**cached, "status": status, "replayed": True, "automatic_retry": False}
    if request.discovery and not connection.discovery_unmetered:
        if not request.approve_discovery:
            raise ControlError("DISCOVERY_COST_NOT_BOUNDED_MANUAL_ID_ALLOWED")
        connection.discovery_unmetered = True
        app.store.put("connection", connection.connection_id, connection, revision(app.store, "connection", connection.connection_id))
    rid = new_id("QUALIFICATION")
    limit = min(request.max_total_usd, app.store.defaults().request_limit_usd)
    record = {"profile_id": profile_id, "run_id": rid, "request_fingerprint": fingerprint, "status": "RUNNING",
              "request_key": key, "owner_pid": os.getpid(),
              "checked_at": utc_now().isoformat(), "max_total_usd": str(limit), "checks": [],
              "execution": "REAL_HTTP" if client_factory is None else "MOCK_HTTP", "automatic_retry": False,
              "research_efficacy": "NOT_VALIDATED"}
    app.store.put("qualification_run", key, record)
    scope = {"rid": rid, "limit": str(limit)}
    modes = (["discovery"] if request.discovery else []) + ["text", "structured", "tools"]
    if "LOW" in profile.reasoning_levels:
        modes.append("reasoning")
        scope["reasoning_policy"] = "LOW"
    try:
        for mode in modes:
            result = await check_model(app, profile_id, {"mode": mode, "consent": True, "retest": request.retest},
                                       client_factory=client_factory, scope=scope)
            record["checks"].append({"mode": mode, "status": "PASS", "usage": result.get("usage"),
                                     "ledger_reservations": result.get("ledger_reservations", 0)})
            app.store.put("qualification_run", key, record, revision(app.store, "qualification_run", key))
        record["status"] = "VALIDATED" if client_factory is None else "OFFLINE_VALIDATED"
    except ModelProviderError as exc:
        record.update(status="NOT_VALIDATED", error_code=exc.code)
    record.update(profile_revision=revision(app.store, "model", profile_id),
                  connection_revision=revision(app.store, "connection", profile.connection_id),
                  credential_version=credential_version(app, connection), ledger=app.store.ledger(rid))
    app.store.put("qualification_run", key, record, revision(app.store, "qualification_run", key))
    app.store.put("qualification", profile_id, record, revision(app.store, "qualification", profile_id))
    return record
