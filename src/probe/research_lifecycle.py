"""연구 이름과 휴지통. 지출 원장과 공유 입력은 보존한다."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3

from .control_plane import ControlError, process_alive
from .database import to_json

ACTIVE = {"RUNNING", "STARTING", "RESUMING", "PAUSE_REQUESTED", "STOP_REQUESTED"}


def lifecycle(api, rid):
    try:
        return api.store.config("research_lifecycle", rid)
    except ControlError:
        return {"research_id": rid, "status": "ACTIVE", "deleted_at": None, "purge_after": None}


def _save(api, rid, value):
    row = api.store.db.execute("SELECT revision FROM control_configs WHERE kind='research_lifecycle' AND id=?", (rid,)).fetchone()
    api.store.put("research_lifecycle", rid, value, row[0] if row else 0)


def _inactive(api, rid):
    row = api.store.db.execute("SELECT status,pid FROM control_runs WHERE research_id=?", (rid,)).fetchone()
    if row and (row["status"] in ACTIVE or row["pid"] and process_alive(row["pid"])):
        raise ControlError("RESEARCH_PAUSE_BEFORE_DELETE")


def _idle(api, rid):
    # 과거 미정산 비용은 작업의 실행 여부와 분리한다.
    _inactive(api, rid)
    if api.store.db.execute("SELECT 1 FROM spend_ledger WHERE research_id=? AND status IN ('RESERVED','DISPATCHED')", (rid,)).fetchone():
        raise ControlError("NEEDS_RECONCILIATION")


def _tree(path, root):
    """재분석 지점까지 확인한 절대 경로만 이동·삭제 대상으로 사용한다."""
    root = Path(root).resolve()
    path = Path(path)
    for ancestor in [path, *path.parents]:
        if ancestor == root:
            break
        if ancestor.is_symlink() or ancestor.is_junction():
            raise ControlError("DELETE_PATH_UNSAFE")
    if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(root) or path.resolve() == root:
        raise ControlError("DELETE_PATH_UNSAFE")
    if path.exists():
        for base, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                item = Path(base) / name
                if item.is_symlink() or item.is_junction() or getattr(item.lstat(), "st_file_attributes", 0) & 0x400:
                    raise ControlError("DELETE_PATH_UNSAFE")
                if not item.resolve().is_relative_to(root):
                    raise ControlError("DELETE_PATH_UNSAFE")
    return path.resolve()


def ensure_visible(api, rid):
    if lifecycle(api, rid)["status"] != "ACTIVE":
        raise ControlError("RESEARCH_IN_TRASH")


def rename(api, rid, title):
    ensure_visible(api, rid)
    api.read._research(rid)
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ControlError("TITLE_INVALID")
    title = title.strip()
    title.encode("utf-8", errors="strict")
    value = lifecycle(api, rid) | {"title": title, "updated_at": datetime.now(timezone.utc).isoformat()}
    _save(api, rid, value)
    return value


def trash(api, rid, *, now=None):
    api.read._research(rid)
    # 휴지통 이동은 원문·응답·비용 원장을 보존하므로 정산 완료를 요구하지 않는다.
    _inactive(api, rid)
    value = lifecycle(api, rid)
    if value["status"] == "ACTIVE":
        now = now or datetime.now(timezone.utc)
        value.update(status="TRASH", deleted_at=now.isoformat(), purge_after=(now + timedelta(days=30)).isoformat())
        _save(api, rid, value)
    return value


def restore(api, rid, *, now=None):
    value = lifecycle(api, rid)
    now = now or datetime.now(timezone.utc)
    if value["status"] != "TRASH":
        raise ControlError("RESEARCH_NOT_RESTORABLE")
    if now >= datetime.fromisoformat(value["purge_after"]):
        purge(api, rid)
        raise ControlError("RESEARCH_RETENTION_EXPIRED")
    api.read._research(rid)
    value.update(status="ACTIVE", deleted_at=None, purge_after=None)
    _save(api, rid, value)
    return value


def purge(api, rid):
    value = lifecycle(api, rid)
    if value["status"] not in {"TRASH", "PURGING", "PURGED"} or not re.fullmatch(r"R-[0-9a-f]{32}", rid):
        raise ControlError("RESEARCH_NOT_IN_TRASH")
    if value["status"] == "PURGED":
        return value
    _inactive(api, rid)
    # 과거 미정산 비용은 원장에 남기되 아직 예약·전송 중인 요청은 보호한다.
    if api.store.db.execute("SELECT 1 FROM spend_ledger WHERE research_id=? AND status IN ('RESERVED','DISPATCHED')", (rid,)).fetchone():
        raise ControlError("NEEDS_RECONCILIATION")
    root = api.workspace.resolve()
    owned = _tree(root / rid, root)
    quarantine = _tree(root / ".research-trash" / rid, root)
    if owned.exists():
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        _tree(quarantine.parent, root)
        if quarantine.exists():
            raise ControlError("DELETE_PATH_CONFLICT")
        owned.rename(quarantine)
    try:
        db = api.store.db
        with api.store.transaction():
            db.execute("PRAGMA defer_foreign_keys=ON")
            # 응답 본문을 삭제하기 전에 미정산 요청의 사용량·응답 식별자만 보존한다.
            checkpoints = db.execute("SELECT c.id,c.payload FROM control_configs c JOIN spend_ledger l ON l.id=c.id WHERE c.kind='provider_response_checkpoint' AND l.research_id=? AND l.status='UNRESOLVED'", (rid,)).fetchall()
            for checkpoint in checkpoints:
                saved = json.loads(checkpoint["payload"])
                result = saved.get("result", saved)
                details = {key: result[key] for key in ("response_id", "model_id", "usage") if key in result}
                details.update({key: saved[key] for key in ("profile_id", "request_key") if key in saved})
                details["reservation_id"] = checkpoint["id"]
                api.store.audit(rid, "PURGED_BILLING_EVIDENCE", details)
            db.execute("DELETE FROM tool_calls WHERE agent_run_id IN (SELECT a.agent_run_id FROM agent_runs a JOIN contracts c USING(contract_id) WHERE c.research_id=?)", (rid,))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='slice_staging'").fetchone():
                db.execute("DELETE FROM slice_staging WHERE mutation_id IN (SELECT mutation_id FROM staged_mutations WHERE research_id=?)", (rid,))
            db.execute("DELETE FROM agent_runs WHERE contract_id IN (SELECT contract_id FROM contracts WHERE research_id=?)", (rid,))
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                if table in {"spend_ledger", "control_audit", "research_runs"}:
                    continue
                columns = {r[1] for r in db.execute('PRAGMA table_info("' + table.replace('"', '""') + '")')}
                if "research_id" in columns:
                    db.execute('DELETE FROM "' + table.replace('"', '""') + '" WHERE research_id=?', (rid,))
            db.execute("DELETE FROM research_runs WHERE research_id=?", (rid,))
            db.execute("DELETE FROM control_configs WHERE (id=? OR json_extract(payload,'$.research_id')=?) AND kind != 'research_lifecycle'", (rid, rid))
            value["status"] = "PURGING"
            db.execute("UPDATE control_configs SET revision=revision+1,payload=? WHERE kind='research_lifecycle' AND id=?", (to_json(value), rid))
    except BaseException:
        if quarantine.exists() and not owned.exists():
            quarantine.rename(owned)
        raise
    # DB 반영 후 중단되더라도 시작 시 PURGING 기록으로 파일 제거를 재개한다.
    quarantine = _tree(quarantine, root)
    if quarantine.exists():
        shutil.rmtree(quarantine)
    value["status"] = "PURGED"
    _save(api, rid, value)
    return value


def cleanup(api, *, now=None):
    now = now or datetime.now(timezone.utc)
    results = []
    for value in api.store.configs("research_lifecycle"):
        if value["status"] == "PURGING" or value["status"] == "TRASH" and now >= datetime.fromisoformat(value["purge_after"]):
            try:
                results.append(purge(api, value["research_id"]))
            except (ControlError, OSError, sqlite3.Error):
                api.store.audit(value["research_id"], "TRASH_CLEANUP_BLOCKED", {"status": value["status"]})
    return results
