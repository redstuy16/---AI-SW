"""연구별 소유자 변경을 저장 경계에서 적용하고 이전 설정을 보존한다."""
from __future__ import annotations

from decimal import Decimal
import json
from typing import Literal

from pydantic import Field, StrictInt, field_validator

from .control_plane import ControlError, ModelProfile, ROLES, resolved_depth
from .database import to_json
from .product_policy import PRESETS, apply_reasoning, effective_snapshot, workflow_compatible
from .providers.normalized import ReasoningPolicy
from .schemas import StrictModel, utc_now


class ResearchSettings(StrictModel):
    performance_profile: Literal["FAST", "BALANCED", "DEEP", "MAX"] | None = None
    adaptive_budget: bool = True
    model_profile_id: str | None = None
    manual_role_override: bool | None = None
    role_reasoning: dict[str, ReasoningPolicy] = Field(default_factory=dict)
    routing: dict[str, str] = Field(default_factory=dict)
    run_limit_usd: Decimal = Field(gt=0, le=100, allow_inf_nan=False)
    max_elapsed_sec: int = Field(ge=10, le=3600)
    egress: Literal["none", "selected", "research"]
    report_style: Literal["friendly", "technical"] = "friendly"
    max_followups: int = Field(default=1, ge=0, le=2)
    approved_worker_profiles: list[str] = Field(default_factory=list, max_length=8)
    search_policy: Literal['AUTO', 'DISABLED', 'ALLOWED'] | None = None
    public_search_query: str | None = Field(default=None, max_length=500)
    public_search_consent: bool | None = None
    search_required: bool | None = None
    search_attempt_limit: StrictInt | None = Field(default=None, ge=0, le=20)
    fulltext_enabled: bool | None = None
    openalex_archive_enabled: bool | None = None
    searxng_url: str | None = Field(default=None, max_length=500)
    advanced_performance_profile: Literal["FAST", "BALANCED", "DEEP", "MAX"] | None = None
    model_reasoning: ReasoningPolicy | None = None
    sampling_mode: Literal["provider_default", "profile"] | None = None
    report_format: Literal["pdf"] = "pdf"

    @field_validator('searxng_url')
    @classmethod
    def public_search_server(cls, value):
        if value:
            from .source_documents import validate_public_url
            return validate_public_url(value.strip(), allow_loopback=True, root=True).rstrip('/')
        return None


def projection(store, rid):
    run = store.run(rid)
    effective = effective_snapshot(store, rid)
    value = {"expected_version": run["version"], "effective": effective, "pending": None,
             "history": [], "analysis_plan_revisions": []}
    try:
        value["pending"] = store.config("research_settings", rid)
    except ControlError:
        pass
    value["history"] = [json.loads(r[0]) for r in store.db.execute(
        "SELECT payload FROM control_audit WHERE research_id=? AND kind IN ('RESEARCH_SETTINGS_REQUESTED','RESEARCH_SETTINGS_APPLIED','ADAPTIVE_MODEL_SELECTED') ORDER BY seq", (rid,))]
    value["analysis_plan_revisions"] = [x for x in store.configs("analysis_plan_revision") if x["parent_research_id"] == rid]
    return value


def queue(store, rid, body):
    run = store.run(rid)
    if run["status"] not in {"DRAFT", "PREFLIGHT_BLOCKED", "RUNNING", "PAUSED", "STARTING", "RESUMING"}:
        raise ControlError("STATE_COMMAND_BLOCKED")
    settings = ResearchSettings.model_validate(body["value"])
    effective = effective_snapshot(store, rid)
    updated = settings.model_dump(mode="json", exclude_none=True, exclude_unset=True)
    if set(settings.role_reasoning) - set(ROLES):
        raise ControlError("REASONING_ROLE_INVALID")
    models, connections, revisions = effective["models"], effective["connections"], effective["profile_revisions"]
    if settings.model_profile_id:
        profile = store.config("model", settings.model_profile_id)
        conn = store.config("connection", profile["connection_id"])
        compatible = workflow_compatible(ModelProfile.model_validate(profile)) if effective.get("settings_version",1) >= 2 else profile["capability_status"] == "supported"
        if not conn["enabled"] or not conn["destination_approved"] or not compatible:
            raise ControlError("CAPABILITY_NOT_VALIDATED")
        models = {role: dict(profile) for role in ROLES}
        connections = {profile["connection_id"]: conn}
        revisions = {profile["profile_id"]: next(x["revision"] for x in store.configs("model") if x["profile_id"] == profile["profile_id"])}
    for role, identity in settings.routing.items():
        if role not in ROLES:
            raise ControlError("REASONING_ROLE_INVALID")
        profile = store.config("model", identity)
        conn = store.config("connection", profile["connection_id"])
        compatible = workflow_compatible(ModelProfile.model_validate(profile)) if effective.get("settings_version",1) >= 2 else profile["capability_status"] == "supported"
        if not conn["enabled"] or not conn["destination_approved"] or not compatible:
            raise ControlError("CAPABILITY_NOT_VALIDATED")
        models[role] = dict(profile)
        connections[profile["connection_id"]] = conn
        revisions[profile["profile_id"]] = next(x["revision"] for x in store.configs("model") if x["profile_id"] == profile["profile_id"])
    candidate = {**effective, **updated, "models": models, "connections": connections, "profile_revisions": revisions}
    if effective.get("settings_version", 1) >= 2:
        from .control_plane import NewResearch
        from .product_policy import resolve_effective_settings
        requested = {k:v for k,v in candidate.items() if k in NewResearch.model_fields}
        for key in ("advanced_performance_profile","model_reasoning"):
            if key in settings.model_fields_set:
                requested[key] = getattr(settings, key)
        resolved, sources = resolve_effective_settings(store, requested)
        candidate.update(resolved, field_sources=sources)
        models, connections, revisions = {}, {}, {}
        for role, identity in resolved["routing"].items():
            model = store.config("model", identity)
            connection = store.config("connection", model["connection_id"])
            if not connection["enabled"] or not connection["destination_approved"] or not workflow_compatible(ModelProfile.model_validate(model)):
                raise ControlError("CAPABILITY_NOT_VALIDATED")
            models[role] = dict(model)
            connections[model["connection_id"]] = connection
            revisions[identity] = next(item["revision"] for item in store.configs("model") if item["profile_id"] == identity)
        candidate.update(models=models, connections=connections, profile_revisions=revisions)
        updated.update({k:candidate[k] for k in ("performance_profile","advanced_performance_profile","model_reasoning","search_attempt_limit","report_format")})
    candidate["routing"] = {role: model["profile_id"] for role, model in models.items()}
    candidate["manual_role_override"] = settings.manual_role_override if settings.manual_role_override is not None else (
        True if settings.routing or settings.role_reasoning else effective.get("manual_role_override", False))
    if candidate.get("performance_profile"):
        candidate["research_depth"] = PRESETS[candidate["performance_profile"]]["depth"]
        candidate["depth_limits"] = resolved_depth(candidate["research_depth"])
    candidate["depth_limits"]["reviews"] = min(candidate["depth_limits"]["reviews"], settings.max_followups)
    apply_reasoning(candidate)
    from .science_policy import explicit_model_limits
    candidate["explicit_model_limits"] = explicit_model_limits(store, candidate["models"])
    for identity in settings.approved_worker_profiles:
        profile = ModelProfile.model_validate(store.config("model", identity))
        conn = store.config("connection", profile.connection_id)
        worker = candidate["models"].get("analysis_planner_worker", {})
        original = candidate["connections"].get(worker.get("connection_id"), {})
        if conn["adapter_id"] != original.get("adapter_id") or conn["base_url"] != original.get("base_url") or not conn["destination_approved"] or profile.capability_status != "supported":
            raise ControlError("ADAPTIVE_MODEL_NOT_APPROVED")
    keys = [*updated, "models", "connections", "profile_revisions", "depth_limits", "research_depth", "preset_customized", "explicit_model_limits"]
    with store.transaction():
        current = store.run(rid)
        if current["version"] != body.get("expected_version"):
            raise ControlError("STATE_STALE")
        prior = store.db.execute("SELECT revision FROM control_configs WHERE kind='research_settings' AND id=?", (rid,)).fetchone()
        revision = prior[0] + 1 if prior else 1
        record = {"revision": revision, "approved_at": utc_now().isoformat(), "applied": False,
                  "settings": {k: candidate[k] for k in keys if k in candidate},
                  "previous": {k: effective.get(k) for k in keys}, "owner_approved": True}
        store.db.execute("INSERT OR REPLACE INTO control_configs VALUES('research_settings',?,?,?)", (rid, revision, to_json(record)))
        store.db.execute("UPDATE control_runs SET version=version+1 WHERE research_id=?", (rid,))
        store.audit(rid, "RESEARCH_SETTINGS_REQUESTED", record)
    return record


def apply_pending(store, state, rid, snapshot, runtime=None):
    try:
        record = store.config("research_settings", rid)
    except ControlError:
        return
    if record["applied"]:
        return
    snapshot.update(record["settings"])
    if runtime:
        from .autonomous_loop import LoopConfig
        limits = snapshot["depth_limits"]
        runtime.models.update({r: v["model_id"] for r, v in snapshot["models"].items()})
        runtime.config = LoopConfig(max_hypotheses=min(5, limits["hypotheses"]), max_shortlist=min(3, limits["hypotheses"]),
            max_active_hypotheses=min(2, limits["hypotheses"]), max_branch_depth=min(2, limits["experiments"]),
            max_followups_per_hypothesis=min(2, limits["reviews"]))
        from .science_policy import prepare_science_profiles, context_budget
        prepare_science_profiles(snapshot)
        runtime.context_budget = context_budget(snapshot)
        if snapshot.get("execution_mode") == "SCIENCE_AUTO":
            runtime.science_settings = getattr(runtime, "science_settings", {})
            runtime.science_settings.update(search_required=snapshot.get("search_required", False),
                max_runtime_sec=snapshot.get("max_elapsed_sec", 300),
                max_decisions=snapshot.get("science_max_decisions", 6),
                no_progress_limit=snapshot.get("science_no_progress_limit", 5),
                audience=snapshot.get("audience", "student"), science_field=snapshot.get("science_field", "general"))
    if store.db.execute("SELECT 1 FROM research_budgets WHERE research_id=?", (rid,)).fetchone():
        state.update_owner_budget(rid, float(snapshot["run_limit_usd"]))
    record.update(applied=True, applied_at=utc_now().isoformat())
    with store.transaction():
        depth = store.db.execute("SELECT payload FROM control_configs WHERE kind='depth' AND id=?", (rid,)).fetchone()
        if depth:
            previous_depth = json.loads(depth[0])
            previous_depth["superseded_by_settings"] = record["revision"]
            store.db.execute("UPDATE control_configs SET payload=? WHERE kind='depth' AND id=?", (to_json(previous_depth), rid))
        store.db.execute("UPDATE control_configs SET payload=? WHERE kind='research_settings' AND id=?", (to_json(record), rid))
        store.db.execute("INSERT OR REPLACE INTO control_configs VALUES('research_effective',?,?,?)", (rid, record["revision"], to_json(record)))
        store.audit(rid, "RESEARCH_SETTINGS_APPLIED", record)
    state.runtime_event(rid, "CONTROL_SETTINGS_APPLIED", {"revision": record["revision"]})
