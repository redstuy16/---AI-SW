"""기존 운영 설정·감사 기록에서 프로세스 사이의 자원 사용을 제한한다."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from hashlib import sha256
import json
import os
import time
from urllib.parse import urlsplit
from uuid import uuid4

from .control_plane import ControlBoundary, ControlError, process_alive, process_identity, process_owned
from .database import to_json
from .schemas import utc_now


def resource_key(connection):
    endpoint = urlsplit(connection.base_url)
    # 같은 서버의 별칭·모델 프로필은 하나의 추론 자원을 공유한다.
    host = 'loopback' if connection.endpoint_class=='loopback' else endpoint.hostname
    authority = f'{endpoint.scheme}://{host}:{endpoint.port or (443 if endpoint.scheme == "https" else 80)}'
    return ('local-' if connection.endpoint_class=='loopback' else 'remote-') + sha256(authority.encode('utf-8', errors='strict')).hexdigest()[:24]


class ResourcePool:
    def __init__(self, store, *, max_pending=32, poll_sec=.1):
        self.store = store
        self.max_pending, self.poll_sec = max_pending, poll_sec

    def rows(self, rid=None):
        query = "SELECT payload FROM control_configs WHERE kind='resource_job'"
        args = ()
        if rid:
            query += " AND json_extract(payload,'$.research_id')=?"
            args = (rid,)
        return [json.loads(r[0]) for r in self.store.db.execute(query + ' ORDER BY rowid', args)]

    def _write(self, value):
        self.store.db.execute("INSERT INTO control_configs(kind,id,revision,payload) VALUES('resource_job',?,1,?) ON CONFLICT(kind,id) DO UPDATE SET revision=revision+1,payload=excluded.payload", (value['id'], to_json(value)))
        self.store.audit(value['research_id'], 'RESOURCE_' + value['status'],
                         {k: value[k] for k in ('id', 'resource', 'status', 'role', 'purpose', 'reason', 'resolved_model_id') if k in value})

    def recover(self):
        from .analysis_process import reconcile_processes
        reconcile_processes(self.store)
        with self.store.transaction():
            self._recover_jobs()

    def _recover_jobs(self):
        # 시간 초과만으로 살아 있는 소유자를 회수하면 중복 실행할 수 있다.
        for row in self.rows():
            if row['status'] in {'QUEUED', 'RUNNING'} and not process_owned(row['pid'], row.get('pid_birth')):
                active = [value for value in self.store.configs('analysis_process')
                          if value.get('research_id') == row['research_id'] and value.get('status') in {'RUNNING', 'TERMINATION_FAILED'}
                          and (value.get('container') or process_alive(value.get('pid')))]
                if active:
                    self._write(dict(row, reason='ANALYSIS_TERMINATION_FAILED'))
                    self.store.db.execute("UPDATE control_configs SET revision=revision+1,payload=json_set(payload,'$.status','TERMINATION_FAILED') WHERE kind='analysis_process' AND json_extract(payload,'$.research_id')=? AND json_extract(payload,'$.status')='RUNNING'", (row['research_id'],))
                    continue
                self._write(dict(row, status='CANCELLED', reason='OWNER_PROCESS_EXITED', finished_at=utc_now().isoformat()))

    def enqueue(self, rid, role, resource, *, capacity, purpose, shared_limits=None):
        from .analysis_process import reconcile_processes
        reconcile_processes(self.store)
        with self.store.transaction():
            for row in self.store.configs('analysis_process'):
                if row.get('status') == 'TERMINATION_FAILED' or row.get('status') == 'RUNNING' and not process_alive(row.get('owner_pid', row['pid'])):
                    if row.get('container') or process_alive(row['pid']):
                        raise ControlError('ANALYSIS_TERMINATION_FAILED')
            self.recover()
            active = [r for r in self.rows() if r['status'] in {'QUEUED', 'RUNNING'}]
            if len(active) >= self.max_pending:
                raise ControlError('RESOURCE_QUEUE_FULL')
            value = {'id': uuid4().hex, 'research_id': rid, 'role': role, 'resource': resource,
                     'purpose': purpose, 'capacity': capacity, 'status': 'QUEUED', 'reason': None,
                     'limits':{resource:capacity,**(shared_limits or {})},
                     'pid': os.getpid(), 'pid_birth': process_identity(os.getpid()), 'created_at': utc_now().isoformat(), 'started_at': None,
                     'finished_at': None}
            self._write(value)
            # 종료된 표시 메타데이터만 제한한다. 원래 감사·비용 기록은 보존한다.
            terminal = [r for r in self.rows() if r['status'] not in {'QUEUED', 'RUNNING'}]
            for old in terminal[:-128]:
                self.store.db.execute("DELETE FROM control_configs WHERE kind='resource_job' AND id=?", (old['id'],))
        return value

    def try_start(self, value):
        with self.store.transaction():
            self.recover()
            current = self.store.config('resource_job', value['id'])
            if current['status'] != 'QUEUED':
                raise ControlError('RESOURCE_JOB_INACTIVE')
            if current['purpose'] == 'research':
                run = self.store.db.execute('SELECT status FROM control_runs WHERE research_id=?', (current['research_id'],)).fetchone()
                if run and run[0] in {'PAUSE_REQUESTED', 'STOP_REQUESTED'}:
                    self._write(dict(current, status='CANCELLED', reason=run[0], finished_at=utc_now().isoformat()))
                    return run[0]
            all_active = [r for r in self.rows() if r['status'] in {'QUEUED','RUNNING'}]
            for resource in current.get('limits',{current['resource']:current['capacity']}):
                active = [r for r in all_active if resource in r.get('limits',{r['resource']:r['capacity']})]
                capacity = min(r.get('limits',{r['resource']:r['capacity']})[resource] for r in active)
                running = sum(r['status']=='RUNNING' for r in active)
                ahead = sum(r['status']=='QUEUED' and r['created_at']<current['created_at'] for r in active)
                if running + ahead >= capacity:
                    return False
            self._write(dict(current, status='RUNNING', started_at=utc_now().isoformat()))
            return True

    def finish(self, value, status='COMPLETED', reason=None):
        with self.store.transaction():
            current = self.store.config('resource_job', value['id'])
            if current['status'] in {'QUEUED', 'RUNNING'}:
                outcome = {k:value[k] for k in ('resolved_model_id',) if k in value}
                self._write(dict(current, **outcome, status=status, reason=reason, finished_at=utc_now().isoformat()))

    def summary(self, rid=None):
        all_rows = self.rows()
        result = []
        for row in all_rows:
            if rid and row['research_id'] != rid:
                continue
            resources = set(row.get('limits',{row['resource']:row['capacity']}))
            ahead = sum(bool(resources & set(r.get('limits',{r['resource']:r['capacity']}))) and (r['status'] == 'RUNNING' or r['status'] == 'QUEUED' and r['created_at'] < row['created_at']) for r in all_rows if r['id'] != row['id'])
            display = {k:v for k,v in row.items() if k!='pid'}
            if row['status'] in {'QUEUED','RUNNING'} and not process_owned(row['pid'], row.get('pid_birth')):
                display.update(status='CANCELLED',stored_status=row['status'],reason='OWNER_PROCESS_EXITED')
            result.append({**display, 'ahead_count': ahead if display['status'] == 'QUEUED' else 0})
        return result

    @asynccontextmanager
    async def lease(self, rid, role, resource, *, capacity=1, purpose='research', timeout=60, shared_limits=None):
        value = self.enqueue(rid, role, resource, capacity=capacity, purpose=purpose,shared_limits=shared_limits)
        started = time.monotonic()
        try:
            while True:
                if time.monotonic() - started >= timeout:
                    self.finish(value, 'TIMED_OUT', 'QUEUE_TIMEOUT')
                    raise ControlError('RESOURCE_QUEUE_TIMEOUT')
                result = self.try_start(value)
                if result is True:
                    break
                if result:
                    raise ControlBoundary(result)
                await asyncio.sleep(self.poll_sec)
            yield value
        except BaseException as exc:
            if getattr(exc, 'code', None) == 'ANALYSIS_TERMINATION_FAILED':
                raise
            self.finish(value, 'CANCELLED', getattr(exc, 'code', type(exc).__name__))
            raise
        else:
            self.finish(value)

    @contextmanager
    def sync_lease(self, rid, role, *, timeout=120, deadline=None, boundary=None):
        value = self.enqueue(rid, role, 'heavy-analysis', capacity=1, purpose='research')
        started = time.monotonic()
        try:
            while True:
                if boundary:
                    boundary()
                if deadline is not None and time.monotonic() >= deadline:
                    raise ControlError('TIME_LIMIT')
                result = self.try_start(value)
                if result is True:
                    break
                if result:
                    raise ControlBoundary(result)
                if time.monotonic() - started >= timeout:
                    self.finish(value, 'TIMED_OUT', 'QUEUE_TIMEOUT')
                    raise ControlError('RESOURCE_QUEUE_TIMEOUT')
                time.sleep(self.poll_sec)
            yield value
        except BaseException as exc:
            if getattr(exc, 'code', None) == 'ANALYSIS_TERMINATION_FAILED':
                raise
            self.finish(value, 'CANCELLED', getattr(exc, 'code', type(exc).__name__))
            raise
        else:
            self.finish(value)
