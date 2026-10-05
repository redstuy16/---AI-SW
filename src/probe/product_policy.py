"""소유자 화면의 모델 목록, 실행 정책과 완료 비용 예약."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path

from .control_plane import Connection, ControlError, ModelProfile, ROLES, admitted_cost
from .providers.normalized import ReasoningPolicy


SETTINGS_VERSION = 2


def workflow_compatible(profile):
    """실행 전 기능 근거와 실제 응답 검증은 서로 다른 상태다."""
    if any(profile.capabilities.get(key) and profile.capabilities[key].status == "UNSUPPORTED"
           for key in ("text", "structured_output")):
        return False
    return profile.capability_status == "supported" or all(
        profile.capabilities.get(key) and profile.capabilities[key].status == "SUPPORTED"
        for key in ("text", "structured_output"))


def resolve_catalog_profile(store, selection):
    """승인된 기존 연결에만 비밀 값 없는 모델 구성을 연결한다."""
    from hashlib import sha256
    from .providers.native import DEFINITIONS
    from .providers.normalized import CapabilityEvidence
    from .schemas import utc_now
    if set(selection) - {"provider", "model_id", "connection_id", "profile_id"}:
        raise ControlError("MODEL_SELECTION_INVALID")
    if selection.get("profile_id"):
        identity = selection["profile_id"]
        store.config("model", identity)
        return identity
    if set(selection) - {"provider", "model_id", "connection_id", "profile_id"}:
        raise ControlError("MODEL_SELECTION_INVALID")
    provider, model_id = selection.get("provider"), selection.get("model_id")
    connections = [c for c in store.configs("connection")
                   if c["adapter_id"] == provider and c["enabled"] and c["destination_approved"]]
    connection_id = selection.get("connection_id")
    if connection_id:
        connections = [c for c in connections if c["connection_id"] == connection_id]
    if not connections:
        raise ControlError("CONNECTION_REQUIRED")
    if len(connections) != 1:
        raise ControlError("CONNECTION_SELECTION_REQUIRED")
    connection_id = connections[0]["connection_id"]
    existing = [m for m in store.configs("model") if m["connection_id"] == connection_id and m["model_id"] == model_id]
    if len(existing) > 1:
        raise ControlError("MODEL_PROFILE_SELECTION_REQUIRED")
    if existing:
        return existing[0]["profile_id"]
    entry = next((m for m in catalog(store)["models"] if m["provider"] == provider
                  and m["model_id"] == model_id and not m.get("profile_id")), None)
    if not entry or entry.get("catalog_expired"):
        raise ControlError("CATALOG_RECHECK_REQUIRED")
    protocol = DEFINITIONS[provider]["protocols"][0]
    identity = "AUTO-" + sha256((connection_id + "\0" + protocol + "\0" + model_id).encode("utf-8", errors="strict")).hexdigest()[:32]
    evidence = {key: CapabilityEvidence(status="SUPPORTED", source="STATIC_ADAPTER_RULE",
                checked_at=utc_now().isoformat(), details=entry["source"])
                for key in entry.get("documented_capabilities", ())}
    profile = ModelProfile(profile_id=identity, connection_id=connection_id, model_id=model_id,
        protocol=protocol, display_name=entry["display_name"], context_limit=65536, output_limit=32768, timeout_sec=120,
        task_output_limits={"planning": 16384, "report": 32768},
        capabilities=evidence, reasoning_levels=entry.get("reasoning_levels", []))
    try:
        store.put("model", identity, profile)
    except ControlError as exc:
        if exc.code != "CONFIG_STALE":
            raise
        current = store.config("model", identity)
        if (current["connection_id"], current["model_id"], current["protocol"]) != (connection_id, model_id, protocol):
            raise ControlError("MODEL_SELECTION_CONFLICT") from None
    return identity


def model_pricing(store, profile_id):
    """단가가 빠진 모델의 공식 가격 후보를 읽기 전용으로 제공한다."""
    from hashlib import sha256
    from .control_plane import PriceRecord
    from .providers.native import DEFINITIONS
    profile = ModelProfile.model_validate(store.config("model", profile_id))
    connection = Connection.model_validate(store.config("connection", profile.connection_id))
    revision = next(m["revision"] for m in store.configs("model") if m["profile_id"] == profile_id)
    result = {"profile_id": profile_id, "expected_revision": revision, "required": False,
              "candidate": None, "quote_id": None}
    if profile.local_api_unmetered and connection.endpoint_class == "loopback":
        return result
    try:
        admitted_cost(profile, 0)
        return result
    except ControlError as exc:
        if exc.code != "PRICE_REQUIRED":
            raise
    result["required"] = True
    rules = json.loads(Path(__file__).with_name("product_catalog.json").read_text(encoding="utf-8"))
    if datetime.now(timezone.utc) > datetime.fromisoformat(rules["expires_at"]):
        return result
    provider = connection.adapter_id
    if provider == "openai_compatible" or connection.base_url.rstrip("/") != DEFINITIONS[provider]["base_url"]:
        return result
    rule = next((m for m in rules["models"] if m["provider"] == provider and m["model_id"] == profile.model_id), None)
    if not rule or not rule.get("price_candidate"):
        return result
    candidate = PriceRecord(**rule["price_candidate"], checked_at=datetime.fromisoformat(rules["checked_at"]),
        source=rule.get("price_source", rule["source"]), revision="catalog-owner-" + rules["checked_at"][:10],
        owner_verified=False)
    try:
        admitted_cost(profile.model_copy(update={"price": candidate.model_copy(update={"owner_verified": True})}), 0)
    except ControlError:
        return result
    quoted = {"price": candidate.model_dump(mode="json"), "model_id": profile.model_id,
              "connection_id": connection.connection_id, "expected_revision": revision}
    result.update(candidate=quoted["price"], quote_id=sha256(json.dumps(quoted, sort_keys=True).encode("utf-8", errors="strict")).hexdigest())
    return result


def apply_model_pricing(store, profile_id, body):
    """소유자가 확인한 현재 후보만 적용한다. 호출·연구 기록은 만들지 않는다."""
    if body.get("approve_price") is not True:
        raise ControlError("CATALOG_OWNER_APPROVAL_REQUIRED")
    quote = model_pricing(store, profile_id)
    if body.get("expected_revision") != quote["expected_revision"]:
        raise ControlError("CONFIG_STALE")
    if not quote["required"]:
        return {"profile_id": profile_id, "status": "PRICE_ALREADY_READY", "paid_calls": 0}
    if not quote["candidate"]:
        raise ControlError("PRICE_REQUIRED")
    if body.get("quote_id") != quote["quote_id"]:
        raise ControlError("CONFIG_STALE")
    from .control_plane import PriceRecord
    profile = ModelProfile.model_validate(store.config("model", profile_id))
    profile.price = PriceRecord.model_validate(quote["candidate"])
    profile.price.owner_verified = True
    store.put("model", profile_id, profile, quote["expected_revision"])
    return {"profile_id": profile_id, "status": "PRICE_APPLIED", "paid_calls": 0}


def resolve_effective_settings(store, draft, *, task_intent="research"):
    """초안의 명시 값·상속·소유자 기본값을 필드별로 한 번 해석한다."""
    from .control_plane import NewResearch
    from .resource_policy import preferences
    raw = dict(draft)
    if raw.get("execution_mode") == "SCIENCE_AUTO":
        from .research_report import requested_report_sections
        extended = len(requested_report_sections(raw.get("question", ""))) >= 8
        raw.setdefault("science_max_decisions", 20 if extended else 6)
        raw.setdefault("science_no_progress_limit", 3 if extended else 2)
        raw.setdefault("search_attempt_limit", 3)
        raw.setdefault("ai_report_enabled", True)
    owner = preferences(store)
    if raw.get("beginner_mode") is True:
        if raw.get("run_limit_usd") in {None, ""}:
            raw["run_limit_usd"] = str(min(Decimal("0.10"), store.defaults().request_limit_usd))
        raw.setdefault("research_profile_mode", "DISABLED" if raw.get("execution_mode") == "SCIENCE_AUTO" else "AUTO")
        raw.setdefault("egress", "selected")
    default_version = SETTINGS_VERSION if raw.get("execution_mode") == "SCIENCE_AUTO" else (1 if raw.get("source_relative") else SETTINGS_VERSION)
    version = raw.get("settings_version", default_version)
    raw["settings_version"] = version
    sources = {}
    for key in ("performance_profile", "adaptive_budget", "search_policy", "search_required", "search_attempt_limit",
                "fulltext_enabled", "openalex_archive_enabled", "searxng_url"):
        if key in raw and raw[key] is not None:
            sources[key] = "main"
        elif version == SETTINGS_VERSION:
            raw[key] = owner.get(key, {"performance_profile":"BALANCED", "adaptive_budget":True,
                "search_policy":"AUTO", "search_required":False, "search_attempt_limit":10,
                "fulltext_enabled":False, "openalex_archive_enabled":False, "searxng_url":None}[key])
            sources[key] = "owner" if key in owner else "application"
    raw["search_attempt_limit"] = min(raw.get("search_attempt_limit", 5), store.defaults().search_attempt_limit) if type(raw.get("search_attempt_limit", 5)) is int else raw.get("search_attempt_limit")
    pool = list(dict.fromkeys(raw.get("selected_model_pool", [])))
    for selection in raw.pop("model_selections", []):
        identity = resolve_catalog_profile(store, selection)
        if identity not in pool:
            pool.append(identity)
    selected = raw.get("model_profile_id")
    if pool and selected not in pool:
        selected = pool[0]
    if not selected and version == SETTINGS_VERSION:
        candidate = owner.get("model_profile_id")
        if candidate and any(m["profile_id"] == candidate for m in store.configs("model")):
            selected, sources["model_profile_id"] = candidate, "owner"
        else:
            selected = next((m["profile_id"] for m in catalog(store)["models"]
                if m.get("profile_id") and m.get("operational") and m["provider"] == "openai"), None)
            if not selected and raw.get("beginner_mode") is True:
                candidates = [m for m in catalog(store)["models"] if m.get("profile_id") and m.get("operational")]
                if len({m["connection_id"] for m in candidates}) == 1 and candidates:
                    selected = candidates[0]["profile_id"]
    if selected:
        store.config("model", selected)
        if not pool:
            pool = [selected]
        raw["model_profile_id"] = selected
        sources.setdefault("model_profile_id", "main")
    elif version == SETTINGS_VERSION and task_intent == "research" and not raw.get("routing_profile_id"):
        raise ControlError("PRIMARY_MODEL_REQUIRED")
    for identity in pool:
        store.config("model", identity)
    raw["selected_model_pool"] = pool
    advanced = raw.get("advanced_performance_profile")
    if advanced is not None:
        raw["performance_profile"] = advanced
        sources["performance_profile"] = "explicit advanced"
    if version == SETTINGS_VERSION:
        raw["report_format"] = "pdf"
        raw.setdefault("attachments", [])
        raw.setdefault("max_followups", 1)
        raw.setdefault("title", str(raw.get("question", "")).strip()[:100])
        if "run_limit_usd" not in raw:
            cap = str(store.defaults().request_limit_usd) if store.configs("defaults") else None
            if cap is None and task_intent == "research":
                raise ControlError("BUDGET_CAP_REQUIRED")
            if cap is not None:
                raw["run_limit_usd"] = cap
                sources["run_limit_usd"] = "owner"
        if "sampling_mode" not in raw and selected:
            model = store.config("model", selected)
            raw["sampling_mode"] = "profile" if any(model.get(k) is not None for k in ("temperature","top_p","stop","seed")) else "provider_default"
    if selected:
        overrides = raw.get("routing", {}) if raw.get("manual_role_override") else {}
        if raw.get("routing_profile_id") and version == SETTINGS_VERSION:
            from .control_plane import RoutingProfile
            profile = RoutingProfile.model_validate(store.config("routing", raw["routing_profile_id"]))
            inherited = dict(profile.routing)
            if profile.reviewer_profile_id:
                inherited["verification_coordinator"] = profile.reviewer_profile_id
            overrides = inherited | overrides
            raw["manual_role_override"] = True
        raw["routing"] = {role:selected for role in ROLES} | overrides
        for role in ROLES:
            sources["routing." + role] = "explicit role" if role in overrides else sources["model_profile_id"]
    if task_intent == "preview":
        if not str(raw.get("question", "")).strip():
            raw["question"] = "설정 미리보기"
            raw["title"] = "설정 미리보기"
        if raw.get("run_limit_usd") in {None, ""}:
            raw["run_limit_usd"] = "0.01"
    config = NewResearch.model_validate(raw).model_dump(mode="json")
    if task_intent == "preview":
        if not str(draft.get("question", "")).strip():
            config["question"] = ""
            config["title"] = draft.get("title", "")
        if draft.get("run_limit_usd") in {None, ""} and not store.configs("defaults"):
            config["run_limit_usd"] = None
    for key in NewResearch.model_fields:
        sources.setdefault(key, "main" if key in draft else "application")
    return config, sources


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


def task_profile(profile, purpose):
    limit = min(profile.output_limit, profile.max_output_tokens or profile.output_limit,
                profile.task_output_limits.get(purpose, profile.output_limit))
    return profile.model_copy(update={"output_limit": limit})


def catalog(store):
    rules = json.loads(Path(__file__).with_name("product_catalog.json").read_text(encoding="utf-8"))
    fresh = datetime.now(timezone.utc) <= datetime.fromisoformat(rules["expires_at"])
    rows = []
    connections = {c["connection_id"]: c for c in store.configs("connection")}
    profiles = store.configs("model")
    for raw in profiles:
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
        text_evidence = raw.get("capabilities", {}).get("text", {})
        response_verified = (text_evidence.get("status") == "SUPPORTED"
                             and text_evidence.get("source") == "LIVE_CAPABILITY_TEST")
        operational = (status != "UNAVAILABLE" and workflow_compatible(profile)
                       and (status == "SUPPORTED" or response_verified)
                       and conn.get("enabled", False) and conn.get("destination_approved", False))
        rows.append({"profile_id": profile.profile_id, "provider": conn.get("adapter_id"), "model_id": profile.model_id,
            "display_name": profile.display_name or profile.model_alias or profile.model_id, "status": status,
            "purpose": "연구용 구조화 응답", "capabilities": raw.get("capabilities", {}),
            "reasoning_levels": raw.get("reasoning_levels", []), "cost_class": "OWNER_PRICE" if profile.price else "UNKNOWN",
            "context_class": str(profile.context_limit), "verified_at": profile.capability_checked_at,
            "source": profile.capability_source or "USER_DECLARED", "operational": operational,
            "connection_id": profile.connection_id, "reasoning_mapping": mappings})
    for rule in rules["models"]:
        missing_connection = any(c["enabled"] and c["adapter_id"] == rule["provider"]
            and not any(m["connection_id"] == c["connection_id"] and m["model_id"] == rule["model_id"]
                        for m in profiles) for c in connections.values())
        if missing_connection or not any(r["provider"] == rule["provider"] and r["model_id"] == rule["model_id"] for r in rows):
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
    if not preset and snapshot.get("settings_version", 1) < SETTINGS_VERSION:
        return
    custom = snapshot.get("role_reasoning", {})
    for role, raw in snapshot["models"].items():
        profile = ModelProfile.model_validate(raw)
        if snapshot.get("settings_version", 1) >= SETTINGS_VERSION:
            requested = custom.get(role, snapshot.get("model_reasoning") or profile.reasoning_policy.value)
            # 토큰 여유와 추론 깊이는 분리하며 명시 설정은 그대로 유지한다.
            evidence = profile.capabilities.get("reasoning")
            if (role not in custom and not snapshot.get("model_reasoning")
                    and profile.reasoning_policy == ReasoningPolicy.AUTO
                    and ReasoningPolicy.LOW in profile.reasoning_levels
                    and evidence and evidence.status == "SUPPORTED"):
                requested = "LOW"
            if snapshot.get("sampling_mode") == "provider_default":
                for key in ("temperature", "top_p", "stop", "seed"):
                    raw[key] = None
        else:
            requested = custom.get(role, PRESETS[preset]["reasoning"])
        if requested != "AUTO" and ReasoningPolicy(requested) not in profile.reasoning_levels:
            if role in custom or snapshot.get("model_reasoning") not in {None, "AUTO"}:
                raise ControlError("REASONING_UNSUPPORTED")
            requested = "AUTO"
        raw["reasoning_policy"] = requested
    snapshot["preset_customized"] = bool(custom or snapshot.get("advanced_performance_profile") or snapshot.get("model_reasoning"))


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
    if snapshot.get("settings_version", 1) >= 2 and (not snapshot.get("source") or snapshot.get("question_only")):
        roles = ["manager"] if stage == "START" else []
    purposes = ["planning"] * len(roles)
    science = snapshot.get("execution_mode") == "SCIENCE_AUTO"
    if science:
        roles = [] if stage == "STOPPED" else ["manager"]
        purposes = ["report"] * len(roles)
    if snapshot.get("ai_report_enabled") and not science:
        saved = store.db.execute("SELECT payload FROM control_configs WHERE kind='ai_report' AND id=?", (rid,)).fetchone()
        if not saved or json.loads(saved[0]).get("status") == "RUNNING":
            roles = roles + ["manager"]
            purposes.append("report")
    if exclude_current and roles:
        roles = roles[1:]
        purposes = purposes[1:]
    amounts = []
    try:
        for role, purpose in zip(roles, purposes):
            profile = task_profile(ModelProfile.model_validate(snapshot["models"][role]), purpose)
            conn = snapshot["connections"][profile.connection_id]
            amounts.append(Decimal(0) if profile.local_api_unmetered and conn["endpoint_class"] == "loopback"
                           else admitted_cost(profile, min(8192, profile.input_byte_limit, profile.context_limit - profile.output_limit)) if science else profile_bound(profile))
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
            "mandatory_calls": roles, "local_report_export_usd": "0", "ai_narrative": "PLANNED" if snapshot.get("ai_report_enabled") else "NOT_RUN",
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
        from .science_policy import explicit_model_limits
        proposal["explicit_model_limits"] = {**snapshot.get("explicit_model_limits", {}),
            **explicit_model_limits(store, {"analysis_planner_worker": raw})}
        if not completion_budget(store, rid, proposal)["can_complete"]:
            continue
        record = {"previous_model": old.model_id, "new_model": candidate.model_id, "role": "analysis_planner_worker",
                  "reason": "OWNER_APPROVED_SAME_PROVIDER_COMPLETION_RESERVE", "approved_profile_id": identity}
        snapshot.update(models=proposal["models"], connections=proposal["connections"], explicit_model_limits=proposal["explicit_model_limits"])
        snapshot["profile_revisions"][identity] = next(x["revision"] for x in store.configs("model") if x["profile_id"] == identity)
        with store.transaction():
            store.audit(rid, "ADAPTIVE_MODEL_SELECTED", record)
            previous = store.db.execute("SELECT revision FROM control_configs WHERE kind='research_effective' AND id=?", (rid,)).fetchone()
            try:
                settings = store.config("research_effective", rid)["settings"]
            except ControlError:
                settings = {}
            settings.update({k: snapshot[k] for k in ("models", "connections", "profile_revisions", "run_limit_usd", "explicit_model_limits")})
            effective = {"applied": True, "settings": settings}
            store.db.execute("INSERT OR REPLACE INTO control_configs VALUES('research_effective',?,?,?)", (rid, previous[0] + 1 if previous else 1, json.dumps(effective, ensure_ascii=False)))
        return True
    return False
