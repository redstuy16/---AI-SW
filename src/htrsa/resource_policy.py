"""표시 설정과 제한된 기록 조회. 과학 검증 정책은 바꾸지 않는다."""
from __future__ import annotations

import json
import os
from typing import Literal

from .schemas import StrictModel
from .control_plane import ControlError


class UIPreferences(StrictModel):
    model_profile_id: str | None = None
    performance_profile: Literal["FAST", "BALANCED", "DEEP", "MAX"] = "BALANCED"
    search_policy: Literal["AUTO", "DISABLED", "ALLOWED"] = "AUTO"
    report_style: Literal["friendly", "technical"] = "friendly"
    low_spec_mode: Literal["AUTO", "ON", "OFF"] = "AUTO"


def preferences(store):
    try:
        return UIPreferences.model_validate(store.config('ui_preferences', 'owner')).model_dump(mode='json')
    except ControlError:
        return UIPreferences().model_dump(mode='json')


def low_spec(store):
    mode = preferences(store)['low_spec_mode']
    if mode != 'AUTO':
        return mode == 'ON'
    return (os.cpu_count() or 1) <= 4


def save_preferences(store, value):
    safe = UIPreferences.model_validate(value)
    if safe.model_profile_id:
        store.config('model', safe.model_profile_id)
    revision = next((v['revision'] for v in store.configs('ui_preferences')), 0)
    store.put('ui_preferences', 'owner', safe, revision)
    return safe.model_dump(mode='json')


def activity_page(state, rid, *, limit=100, offset=0):
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise ControlError('PAGE_INVALID')
    branches = []
    for table, kind, event, payload in (
        ('research_actions', 'action', 'action_type', 'details_json'),
        ('runtime_events', 'runtime', 'event_type', 'details_json'),
        ('planning_events', 'planning', 'event_type', 'payload_json'),
        ('state_events', 'commit', 'operation', "'{}'")):
        version = "state_version" if kind in {"planning", "commit"} else "NULL"
        branches.append(f"SELECT {version} AS state_version,rowid AS identity,created_at AS timestamp,'{kind}' AS kind,{event} AS event_type,{payload} AS details FROM {table} WHERE research_id=?")
    branches.append("SELECT NULL,tc.rowid,COALESCE(tc.finished_at,tc.started_at,''),'tool',tc.tool_name,json_object('status',tc.status,'latency_ms',tc.latency_ms) FROM tool_calls tc JOIN agent_runs ar ON ar.agent_run_id=tc.agent_run_id JOIN contracts c ON c.contract_id=ar.contract_id WHERE c.research_id=?")
    union = ' UNION ALL '.join(branches)
    rows = state._db.execute('SELECT * FROM (' + union + ') ORDER BY timestamp DESC,kind,identity DESC LIMIT ? OFFSET ?', (rid,) * 5 + (limit + 1, offset)).fetchall()
    items = [{"timestamp": r['timestamp'], "kind": r['kind'], "event_type": r['event_type'],
              "label": r['event_type'], "details": json.loads(r['details']), "state_version": r["state_version"],
              "low_level": r['kind'] in {'commit', 'tool'}} for r in rows[:limit]]
    return {"items": items, "offset": offset, "limit": limit,
            "next_offset": offset + limit if len(rows) > limit else None}
