"""기본 화면의 저장 상태 요약과 연결 삭제."""
import json

from .control_plane import ControlError
from .research_lifecycle import ACTIVE


def delete_connection(api, identity, *, confirm, expected_revision):
    if confirm is not True:
        raise ControlError("CONNECTION_DELETE_CONFIRMATION_REQUIRED")
    store = api.store
    with store.transaction():
        conn = store.config("connection", identity)
        row = store.db.execute("SELECT revision FROM control_configs WHERE kind='connection' AND id=?", (identity,)).fetchone()
        if expected_revision != row[0]:
            raise ControlError("CONFIG_STALE")
        if store.db.execute("SELECT 1 FROM spend_ledger WHERE connection_id=? AND status IN ('RESERVED','DISPATCHED','UNRESOLVED')", (identity,)).fetchone():
            raise ControlError("NEEDS_RECONCILIATION")
        model_ids = {m["profile_id"] for m in store.configs("model") if m["connection_id"] == identity}
        for run in store.db.execute("SELECT status,snapshot FROM control_runs"):
            if run["status"] in ACTIVE | {"PAUSED","NEEDS_RECONCILIATION"} and any(m in model_ids for m in json.loads(run["snapshot"]).get("routing", {}).values()):
                raise ControlError("CONNECTION_IN_USE")
        for routing in store.configs("routing"):
            if any(m in model_ids for m in list(routing.get("routing", {}).values()) + [routing.get("reviewer_profile_id")]):
                store.db.execute("DELETE FROM control_configs WHERE kind='routing' AND id=?", (routing["profile_id"],))
        for model in model_ids:
            store.db.execute("DELETE FROM control_configs WHERE kind='model' AND id=?", (model,))
        store.db.execute("DELETE FROM control_configs WHERE kind='connection' AND id=?", (identity,))
        name = conn.get("credential_env_name")
        shared = name and any(c.get("credential_env_name") == name for c in store.configs("connection"))
        credential = api.credentials.save(name, None) if name and not shared else None
        store.audit(None, "CONNECTION_DELETED", {"connection_id":identity,"models_removed":sorted(model_ids),"shared_key_preserved":bool(shared)})
    return {"connection_id":identity,"deleted":True,"credential":credential,"shared_key_preserved":bool(shared),"paid_calls":0}


def progress(api, rid):
    state, db = api.read._state, api.store.db
    from .qualified_profiles import conclusion_card
    card = conclusion_card(state, rid)
    tools = {r[0] for r in db.execute("SELECT tool_name FROM tool_dispatches WHERE research_id=? AND status='FINISHED'", (rid,))}
    api.read._research(rid)
    report_ready = False
    try:
        from .release import validate_report_snapshot, ReleaseExportError
        validate_report_snapshot(state, rid)
        report_ready = True
    except (ControlError, ReleaseExportError, ValueError, OSError, KeyError):
        pass
    if not card["available"] and report_ready:
        from .report_ux import friendly_report
        view = friendly_report(state, rid)
        sources = [r[0] for r in db.execute("SELECT title FROM sources WHERE research_id=? AND status='VERIFIED'", (rid,))]
        datasets = [r[0] for r in db.execute("SELECT DISTINCT d.original_name FROM datasets d JOIN experiments e USING(dataset_id) WHERE d.research_id=? AND d.status!='INVALID' AND e.status='VERIFIED'", (rid,))]
        card = {"available":True,"current":True,"question":view["question"],
                "source":" · ".join(sources + datasets) or "검증된 출처가 없습니다.",
                "calculation":view["conclusion"],"scope":"현재 검증된 분석·근거의 범위에 한정합니다.",
                "unconfirmed":["인과관계", "별도로 검증하지 않은 모집단·지역으로의 일반화"] + ([] if view["complete"] else ["질문에 대한 최종 결론"]),
                "currentness":"현재 검증된 기록" if view["complete"] else "현재 부분 기록 · 결론 미확정",
                "message":"","history":[]}
    current = card["current"] if card["available"] else db.execute("SELECT 1 FROM experiments WHERE research_id=? AND status='VERIFIED'", (rid,)).fetchone() is not None
    labels = ["질문","자료 찾기","자료 확인","분석","결과 확인","보고서"]
    done = [True, bool(tools & {"data.import","literature.search"}), "data.profile" in tools,
            "stats.run" in tools, bool(current), report_ready]
    from .research_lifecycle import lifecycle
    title = lifecycle(api, rid).get("title")
    if not title:
        control = db.execute("SELECT title FROM control_runs WHERE research_id=?", (rid,)).fetchone()
        title = control[0] if control else state._one("SELECT goal FROM research_runs WHERE research_id=?", (rid,))[0]
    return {"title":title,"steps":[{"label":label,"done":passed} for label,passed in zip(labels, done)],"report_ready":report_ready,
            "ledger":api.store.ledger(rid),"card":card}
