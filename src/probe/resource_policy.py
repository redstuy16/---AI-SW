"""표시 설정과 제한된 기록 조회. 과학 검증 정책은 바꾸지 않는다."""
from __future__ import annotations

import json
import os
from typing import Literal
from pydantic import Field, StrictInt, field_validator

from .schemas import StrictModel
from .control_plane import ControlError
from .tutorial_guide import TutorialProgress


class UIPreferences(StrictModel):
    model_profile_id: str | None = None
    performance_profile: Literal["FAST", "BALANCED", "DEEP", "MAX"] = "BALANCED"
    search_policy: Literal["AUTO", "DISABLED", "ALLOWED"] = "AUTO"
    report_style: Literal["friendly", "technical"] = "friendly"
    low_spec_mode: Literal["AUTO", "LOW_SPEC", "NORMAL", "ON", "OFF"] = "AUTO"
    settings_version: Literal[1, 2] = 2
    search_required: bool = False
    search_attempt_limit: StrictInt = Field(default=10, ge=0, le=20)
    fulltext_enabled: bool = False
    openalex_archive_enabled: bool = False
    searxng_url: str | None = Field(default=None, max_length=500)
    adaptive_budget: bool = True
    new_research_explanations: bool = False
    explanation_prompt_dismissed: bool = False
    tutorial_completed: bool = False
    tutorial_do_not_ask: bool = False
    tutorial_progress: TutorialProgress | None = None

    @field_validator('searxng_url')
    @classmethod
    def public_search_server(cls, value):
        if value:
            from .source_documents import validate_public_url
            return validate_public_url(value.strip(), allow_loopback=True, root=True).rstrip('/')
        return None


def preferences(store):
    try:
        return UIPreferences.model_validate(store.config('ui_preferences', 'owner')).model_dump(mode='json')
    except ControlError:
        return UIPreferences().model_dump(mode='json')


def low_spec(store):
    mode = preferences(store)['low_spec_mode']
    if mode != 'AUTO':
        return mode in {'ON', 'LOW_SPEC'}
    return coarse_capacity()['low_spec']


def coarse_capacity():
    """CPU 수와 총 메모리만 로컬에서 확인한다. 확인 실패 시 일반 모드다."""
    ram = None
    try:
        if os.name == 'nt':
            import ctypes
            class MemoryStatus(ctypes.Structure):
                _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong),
                            *[(k, ctypes.c_ulonglong) for k in ('total', 'available', 'page_total', 'page_available', 'virtual_total', 'virtual_available', 'extended')]]
            status = MemoryStatus();status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                ram = status.total / 1024**3
        else:
            ram = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 1024**3
    except (OSError, ValueError, AttributeError):
        pass
    cpu = os.cpu_count()
    return {'logical_cpu': cpu, 'ram_gb': round(ram, 1) if ram is not None else None,
            'low_spec': bool(cpu is not None and ram is not None and (cpu <= 4 or ram <= 8))}


def save_preferences(store, value):
    update = UIPreferences.model_validate(value).model_dump(mode='json', exclude_unset=True)
    previous = next(iter(store.configs('ui_preferences')), {})
    revision = previous.pop('revision', 0)
    safe = UIPreferences.model_validate({**previous, **update})
    if safe.model_profile_id:
        store.config('model', safe.model_profile_id)
    store.put('ui_preferences', 'owner', safe, revision)
    return safe.model_dump(mode='json')


def activity_page(state, rid, *, limit=100, offset=0, summary=False):
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise ControlError('PAGE_INVALID')
    branches = []
    for table, kind, event, payload in (
        ('research_actions', 'action', 'action_type', 'details_json'),
        ('runtime_events', 'runtime', 'event_type', 'details_json'),
        ('planning_events', 'planning', 'event_type', 'payload_json'),
        ('state_events', 'commit', 'operation', "'{}'")):
        version = "state_version" if kind in {"planning", "commit"} else "NULL"
        if summary:
            payload = "'{}'"
        branches.append(f"SELECT {version} AS state_version,rowid AS identity,created_at AS timestamp,'{kind}' AS kind,{event} AS event_type,{payload} AS details FROM {table} WHERE research_id=?")
    branches.append("SELECT NULL,tc.rowid,COALESCE(tc.finished_at,tc.started_at,''),'tool',tc.tool_name,json_object('status',tc.status,'latency_ms',tc.latency_ms) FROM tool_calls tc JOIN agent_runs ar ON ar.agent_run_id=tc.agent_run_id JOIN contracts c ON c.contract_id=ar.contract_id WHERE c.research_id=?")
    union = ' UNION ALL '.join(branches)
    rows = state._db.execute('SELECT * FROM (' + union + ') ORDER BY timestamp DESC,kind,identity DESC LIMIT ? OFFSET ?', (rid,) * 5 + (limit + 1, offset)).fetchall()
    items = [{"timestamp": r['timestamp'], "kind": r['kind'], "event_type": r['event_type'],
              "label": r['event_type'], "details": json.loads(r['details']), "state_version": r["state_version"],
              "low_level": r['kind'] in {'commit', 'tool'},'id':str(r['kind'])+':'+str(r['identity'])} for r in rows[:limit]]
    return {"items": items, "offset": offset, "limit": limit,
            "next_offset": offset + limit if len(rows) > limit else None}


def activity_detail(state, rid, identity):
    try:
        kind, value = identity.split(':',1);value = int(value)
    except ValueError:
        raise ControlError('NOT_FOUND') from None
    definitions = {'action':('research_actions','details_json'),'runtime':('runtime_events','details_json'),
                   'planning':('planning_events','payload_json'),'commit':('state_events','after_json')}
    if kind in definitions:
        table,column = definitions[kind]
        row = state._db.execute(f'SELECT {column} FROM {table} WHERE research_id=? AND rowid=?',(rid,value)).fetchone()
    elif kind == 'tool':
        row = state._db.execute("SELECT json_object('status',tc.status,'latency_ms',tc.latency_ms,'tool_name',tc.tool_name) FROM tool_calls tc JOIN agent_runs ar ON ar.agent_run_id=tc.agent_run_id JOIN contracts c ON c.contract_id=ar.contract_id WHERE c.research_id=? AND tc.rowid=?",(rid,value)).fetchone()
    else:
        row = None
    if not row:
        raise ControlError('NOT_FOUND')
    from .research_flow import public_fields
    return public_fields(json.loads(row[0]))
