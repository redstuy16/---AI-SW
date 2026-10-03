"""소유자 화면의 모델 목록, 실행 정책과 완료 비용 예약."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path

from .control_plane import Connection, ControlError, ModelProfile, ROLES, admitted_cost
from .providers.normalized import ReasoningPolicy


PRESETS = {
    "FAST": {"label": "low", "depth": "explore", "reasoning": "LOW"},
    "BALANCED": {"label": "medium", "depth": "standard", "reasoning": "MEDIUM"},
    "DEEP": {"label": "high", "depth": "deep", "reasoning": "HIGH"},
    "MAX": {"label": "max", "depth": "focused", "reasoning": "MAX"},
}
REDUCTION_ORDER = ["extra_literature", "hypotheses", "repeated_review", "sensitivity", "followups",
                   "noncritical_reasoning", "approved_worker_model", "preserve_verification", "finalize_current_evidence"]


def profile_bound(profile):
    return admitted_cost(profile, min(profile.input_byte_limit, profile.context_limit - profile.output_limit))


def catalog(store):
    rules = json.loads(Path(__file__).with_name("product_catalog.json").read_text(encoding="utf-8"))
    fresh = datetime.now(timezone.utc) <= datetime.fromisoformat(rules["expires_at"])
    rows = []
    connections = {c["connection_id"]: c for c in store.configs("connection")}
    for raw in store.configs("model"):
        conn = connections.get(raw["connection_id"], {})
        profile = ModelProfile.model_validate({k: v for k, v in raw.items() if k != "revision"})
        if "text" not in profile.output_modalities or "text" not in profile.input_modalities or (profile.capabilities.get("text") and profile.capabilities["text"].status == "UNSUPPORTED"):
            continue
        mappings = {}
        from .providers.native import REGISTRY
        from .providers.normalized import GenerationRequest
        from .providers.base import ModelProviderError
        adapter = REGISTRY.get(conn.get("adapter_id", "openai"))
        for level in [ReasoningPolicy.AUTO, *profile.reasoning_levels]:
            try:
                request = GenerationRequest(request_id="preview", research_id="preview", model_profile_id=profile.profile_id, role="manager", reasoning_policy=level)
                protocol = adapter.validate_profile(profile, Connection.model_validate({k: v for k, v in conn.items() if k != "revision"}))["protocol"]
                mappings[level.value] = adapter.reasoning(request, profile, protocol)[1]
            except (ModelProviderError, ValueError):
                continue
        status = "SUPPORTED" if profile.capability_status == "supported" else "UNAVAILABLE" if profile.capability_status == "unsupported" else "UNTESTED"
        rows.append({"profile_id": profile.profile_id, "provider": conn.get("adapter_id"), "model_id": profile.model_id,
            "display_name": profile.display_name or profile.model_alias or profile.model_id, "status": status,
            "purpose": "연구용 구조화 응답", "capabilities": raw.get("capabilities", {}),
            "reasoning_levels": raw.get("reasoning_levels", []), "cost_class": "OWNER_PRICE" if profile.price else "UNKNOWN",
            "context_class": str(profile.context_limit), "verified_at": profile.capability_checked_at,
            "source": profile.capability_source or "USER_DECLARED", "operational": status == "SUPPORTED" and conn.get("destination_approved", False),
            "connection_id": profile.connection_id, "reasoning_mapping": mappings})
    for rule in rules["models"]:
        if not any(r["provider"] == rule["provider"] and r["model_id"] == rule["model_id"] for r in rows):
            rows.append({**rule, "profile_id": None, "status": "UNTESTED", "operational": False,
                         "verified_at": None, "catalog_checked_at": rules["checked_at"], "catalog_expired": not fresh,
                         "capabilities": {k: {"status": "SUPPORTED" if fresh else "UNKNOWN", "source": "STATIC_DOCS"} for k in rule.get("documented_capabilities", ("structured_output",))},
                         "price_candidate": rule.get("price_candidate") if fresh else None})
    for discovered in store.configs("catalog_discovery"):
        conn = connections.get(discovered["connection_id"], {})
        for model in discovered["models"]:
            if any(r["model_id"] == model["model_id"] and r["provider"] == conn.get("adapter_id") for r in rows):
                continue
            if model.get("capabilities", {}).get("text", {}).get("status") != "SUPPORTED" or model.get("capabilities", {}).get("structured_output", {}).get("status") == "UNSUPPORTED":
                continue
            rows.append({"profile_id": None, "provider": conn.get("adapter_id"), "model_id": model["model_id"],
                "display_name": model.get("display_name") or model["model_id"], "status": "UNTESTED", "operational": False,
                "purpose": "제공사 목록에서 확인된 텍스트 모델 · 수동 검증 필요", "capabilities": model.get("capabilities", {}),
                "reasoning_levels": model.get("reasoning_levels", []), "cost_class": "UNKNOWN", "context_class": str(model.get("max_input_tokens") or "UNKNOWN"),
                "verified_at": None, "source": "PROVIDER_METADATA", "catalog_checked_at": discovered["checked_at"],
                "catalog_expired": True, "connection_id": discovered["connection_id"]})
    recommended = next((r for r in rows if r["provider"] == "openai" and r.get("operational")), None)
    from .productization import picker_label
    for row in rows:
        row["picker_label"] = picker_label(row, rules["models"])
        rule = next((r for r in rules["models"] if r["provider"] == row["provider"] and r["model_id"] == row["model_id"]), None)
        row["catalog_curated"] = rule is not None
        row["featured_order"] = rule.get("featured_order") if rule else None
    if recommended:
        recommended["status"] = "RECOMMENDED"
    return {"models": rows, "checked_at": rules["checked_at"], "expired": not fresh,
            "notice": "문서 확인은 실제 호출 검증이 아닙니다. 가격은 소유자 확인 후 적용합니다."}


def prepare_snapshot(store, snapshot):
    selected = snapshot.get("model_profile_id")
    if selected:
        snapshot["routing"] = {role: selected for role in ROLES} | (snapshot.get("routing", {}) if snapshot.get("manual_role_override") else {})
    performance = snapshot.get("performance_profile")
    if performance:
        snapshot["research_depth"] = PRESETS[performance]["depth"]
        snapshot["preset_label"] = PRESETS[performance]["label"]
    return snapshot


def apply_reasoning(snapshot):
    preset = snapshot.get("performance_profile")
    if not preset:
        return
    custom = snapshot.get("role_reasoning", {})
    for role, raw in snapshot["models"].items():
        profile = ModelProfile.model_validate(raw)
        requested = custom.get(role, PRESETS[preset]["reasoning"])
        if requested != "AUTO" and ReasoningPolicy(requested) not in profile.reasoning_levels:
            if role in custom:
                raise ControlError("REASONING_UNSUPPORTED")
            requested = "AUTO"
        raw["reasoning_policy"] = requested
    snapshot["preset_customized"] = bool(custom)


def effective_snapshot(store, rid, initial=None):
    value = json.loads(json.dumps(initial or store.run(rid)["snapshot"]))
    try:
        update = store.config("research_effective", rid)
        value.update(update["settings"])
    except ControlError:
        pass
    try:
        depth = store.config("depth", rid)
        if depth.get("applied") and not depth.get("superseded_by_settings"):
            from .control_plane import resolved_depth
            value.update(research_depth=depth["research_depth"], depth_limits=resolved_depth(depth["research_depth"]))
            if value.get("performance_profile"):
                value["preset_customized"] = True
    except ControlError:
        pass
    return value


def effective_cap(store, rid, snapshot):
    cap = Decimal(str(snapshot["run_limit_usd"]))
    try:
        queued = store.config("research_settings", rid)
        if not queued["applied"]:
            cap = min(cap, Decimal(str(queued["settings"]["run_limit_usd"])))
    except ControlError:
        pass
    return cap


def remaining_roles(stage):
    # 진행 커서는 마지막 저장 경계다. 후속 분기는 선택 작업이며 기본 완료 경로와 분리한다.
    order = ["manager", "manager", "experiment_coordinator", "analysis_planner_worker", "verification_coordinator", "experiment_coordinator", "manager"]
    start = {"START": 0, "QUESTION_READY": 1, "SHORTLIST_READY": 2, "HYPOTHESIS_READY": 2,
             "INTAKE_READY": 2, "WORKER_0_READY": 3, "EXPERIMENT_0_READY": 4,
             "CRITIC_0_READY": 5, "HYPOTHESIS_UPDATED": 6, "STOPPED": 7}.get(stage)
    if start is not None:
        return order[start:]
    if stage.startswith("WORKER_"):
        return ["analysis_planner_worker", "verification_coordinator", "experiment_coordinator", "manager"]
    if stage.startswith("EXPERIMENT_"):
        return ["verification_coordinator", "experiment_coordinator", "manager"]
    if stage.startswith("CRITIC_"):
        return ["experiment_coordinator", "manager"]
    return order


def completion_budget(store, rid, snapshot, *, exclude_current=False):
    ledger = store.ledger(rid)
    cursor = store.db.execute("SELECT cursor_json FROM research_runtime_state WHERE research_id=?", (rid,)).fetchone()
    stage = json.loads(cursor[0]).get("stage", "START") if cursor else "START"
    roles = remaining_roles(stage)
    if exclude_current and roles:
        roles = roles[1:]
    amounts = []
    try:
        for role in roles:
            profile = ModelProfile.model_validate(snapshot["models"][role])
            conn = snapshot["connections"][profile.connection_id]
            amounts.append(Decimal(0) if profile.local_api_unmetered and conn["endpoint_class"] == "loopback"
                           else profile_bound(profile))
        recovery = Decimal("0")
        if snapshot.get("verification_repair") and roles:
            from .verification_repair import LOCAL_RECOVERY_RESERVE_USD
            recovery = Decimal(str(LOCAL_RECOVERY_RESERVE_USD))
            profile = ModelProfile.model_validate(snapshot["models"]["experiment_coordinator"])
            conn = snapshot["connections"][profile.connection_id]
            recovery += Decimal(0) if profile.local_api_unmetered and conn["endpoint_class"] == "loopback" else profile_bound(profile)
        reserved = sum(amounts, Decimal(0)) + recovery
        status = "KNOWN"
    except (ControlError, KeyError):
        reserved, recovery, status = None, None, "UNKNOWN"
    used = sum(Decimal(ledger[x]) for x in ("spent", "reserved", "unresolved"))
    cap = effective_cap(store, rid, snapshot)
    monthly_cap = min(store.defaults().monthly_limit_usd, Decimal(snapshot["monthly_limit_usd"]))
    available = max(Decimal(0), min(cap - used, monthly_cap - Decimal(ledger["monthly_exposure"])))
    return {"adaptive_budget": snapshot.get("adaptive_budget", False), "stage": stage, "status": status,
            "hard_cap_usd": str(cap), "spent_usd": ledger["spent"], "active_reservations_usd": ledger["reserved"],
            "unsettled_exposure_usd": ledger["unresolved"], "available_usd": str(available),
            "completion_reserve_usd": str(reserved) if reserved is not None else None,
            "f3p_reserve_usd": str(recovery) if recovery is not None else None,
            "mandatory_calls": roles, "local_report_export_usd": "0", "ai_narrative": "NOT_RUN",
            "can_complete": reserved is not None and available >= reserved,
            "reduction_order": REDUCTION_ORDER, "environment_efficacy": "NOT_VALIDATED"}


def optional_admission(store, rid, snapshot, *, kind="followups"):
    if not snapshot.get("performance_profile") or not snapshot.get("adaptive_budget"):
        return True
    view = completion_budget(store, rid, snapshot)
    extra = Decimal(0)
    try:
        for role in ("experiment_coordinator", "analysis_planner_worker", "verification_coordinator"):
            p = ModelProfile.model_validate(snapshot["models"][role])
            c = snapshot["connections"][p.connection_id]
            extra += Decimal(0) if p.local_api_unmetered and c["endpoint_class"] == "loopback" else profile_bound(p)
    except (ControlError, KeyError):
        view["status"] = "UNKNOWN"
    admitted = view["status"] == "KNOWN" and Decimal(view["available_usd"]) >= Decimal(view["completion_reserve_usd"] or "0") + extra
    if not admitted:
        store.audit(rid, "ADAPTIVE_OPTIONAL_REDUCED", {"kind": kind, "reason": "COMPLETION_RESERVE", "budget": view})
    return admitted


def maybe_select_worker(store, rid, snapshot):
    """소유자가 승인한 같은 제공사의 Worker만 비용이 부족한 미래 호출에 선택한다."""
    if not snapshot.get("performance_profile") or not snapshot.get("adaptive_budget") or not snapshot.get("approved_worker_profiles"):
        return False
    budget = completion_budget(store, rid, snapshot)
    if budget["can_complete"]:
        return False
    old = ModelProfile.model_validate(snapshot["models"]["analysis_planner_worker"])
    original_connection = snapshot["connections"][old.connection_id]
    try:
        old_cost = profile_bound(old)
    except ControlError:
        return False
    for identity in snapshot["approved_worker_profiles"]:
        candidate = ModelProfile.model_validate(store.config("model", identity))
        conn = store.config("connection", candidate.connection_id)
        if (conn["adapter_id"] != original_connection["adapter_id"] or conn["base_url"] != original_connection["base_url"] or not conn["destination_approved"] or not conn["enabled"] or candidate.capability_status != "supported"):
            continue
        if any(candidate.capabilities.get(k) is None or candidate.capabilities[k].status != "SUPPORTED" for k in ("text", "structured_output")):
            continue
        try:
            if profile_bound(candidate) >= old_cost:
                continue
        except ControlError:
            continue
        if old.reasoning_policy != ReasoningPolicy.AUTO and old.reasoning_policy not in candidate.reasoning_levels:
            continue
        raw = candidate.model_dump(mode="json")
        raw["reasoning_policy"] = old.reasoning_policy.value
        proposal = json.loads(json.dumps(snapshot))
        proposal["models"]["analysis_planner_worker"] = raw
        proposal["connections"][candidate.connection_id] = conn
        if not completion_budget(store, rid, proposal)["can_complete"]:
            continue
        record = {"previous_model": old.model_id, "new_model": candidate.model_id, "role": "analysis_planner_worker",
                  "reason": "OWNER_APPROVED_SAME_PROVIDER_COMPLETION_RESERVE", "approved_profile_id": identity}
        snapshot.update(models=proposal["models"], connections=proposal["connections"])
        snapshot["profile_revisions"][identity] = next(x["revision"] for x in store.configs("model") if x["profile_id"] == identity)
        with store.transaction():
            store.audit(rid, "ADAPTIVE_MODEL_SELECTED", record)
            previous = store.db.execute("SELECT revision FROM control_configs WHERE kind='research_effective' AND id=?", (rid,)).fetchone()
            try:
                settings = store.config("research_effective", rid)["settings"]
            except ControlError:
                settings = {}
            settings.update({k: snapshot[k] for k in ("models", "connections", "profile_revisions", "run_limit_usd")})
            effective = {"applied": True, "settings": settings}
            store.db.execute("INSERT OR REPLACE INTO control_configs VALUES('research_effective',?,?,?)", (rid, previous[0] + 1 if previous else 1, json.dumps(effective, ensure_ascii=False)))
        return True
    return False
