"""선택적 문헌 검색을 기존 제공사·근거 검증·비용 원장에 연결한다."""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import re
from time import perf_counter

from .control_plane import ControlError
from .schemas import utc_now, new_id
from .scholarly import SearchResult, SearchRequest, metadata_digest

CROSSREF_PRICE_CHECKED_AT = '2026-10-02T16:27:49+00:00'


class SearchPolicy(StrEnum):
    AUTO = 'AUTO'
    DISABLED = 'DISABLED'
    ALLOWED = 'ALLOWED'


def useful(question):
    return bool(re.search(r'최근|최신|문헌|논문|출처|인용|공개 데이터|current|recent|latest|literature|citation|source verification', question, re.I))


def private_query(query, protected=()):
    return (any(v and v in query for v in protected)
            or bool(re.search(r'(?i)(sk-[\w-]{12,}|AIza[\w-]{35}|ghp_\w{36}|[A-Z]:[\\/]|/home/|/Users/|https?://|@|API[_ ]?KEY|password|confidential|private|internal|비밀번호|비공개|주민등록|\d{3}[- ]\d{3,4}[- ]\d{4}|(?:\d+[,.]\d+[,; ]+){3})', query)))


def decision(snapshot, *, question=None, budget_ok=True, protected=()):
    policy = SearchPolicy(snapshot.get('search_policy', 'DISABLED'))
    required = snapshot.get('search_required', False)
    if policy == SearchPolicy.DISABLED:
        return 'SEARCH_REQUIRED_BUT_DISABLED' if required else 'SEARCH_DISABLED'
    if not required and not useful(question or snapshot['question']):
        return 'SEARCH_NOT_NEEDED'
    if snapshot.get('egress') != 'research':
        return 'SEARCH_EGRESS_DENIED'
    query = snapshot.get('public_search_query', '').strip()
    if not query:
        return 'SEARCH_QUERY_REQUIRED'
    if private_query(query, protected):
        return 'SEARCH_PRIVATE_QUERY_BLOCKED'
    if not required and snapshot.get('adaptive_budget', True) and not budget_ok:
        return 'SEARCH_OPTIONAL_REDUCED'
    return 'SEARCH_ALLOWED'


class PolicyProvider:
    def __init__(self, provider, store, credentials, snapshot, *, price=None, price_source=None, before_dispatch=None):
        self.provider, self.store, self.credentials, self.snapshot = provider, store, credentials, snapshot
        self.name, self.price, self.price_source = provider.name, price, price_source
        self.before_dispatch = before_dispatch

    def task_snapshot(self, request):
        value = dict(self.snapshot)
        if request.search_policy and value.get('search_policy', 'DISABLED') != 'DISABLED':
            value['search_policy'] = request.search_policy
        if request.evidence_required:
            value['search_required'] = True
        return value

    def can_reuse(self, request, previous):
        from datetime import datetime, timedelta
        from .scholarly import metadata_digest
        if self.before_dispatch:
            self.before_dispatch()
        snapshot = self.task_snapshot(request)
        protected = self.credentials.active_secrets(c.get('credential_env_name') for c in self.store.configs('connection'))
        if decision(snapshot, protected=protected) != 'SEARCH_ALLOWED' or private_query(request.query, protected):
            return False
        cached = SearchResult.model_validate(previous)
        if cached.request != request or not cached.sources:
            return False
        for source in cached.sources:
            try:
                retrieved = datetime.fromisoformat(source.retrieved_at)
                if retrieved.tzinfo is None or not 0 <= (utc_now() - retrieved).total_seconds() <= timedelta(days=1).total_seconds():
                    return False
            except ValueError:
                return False
            row = self.store.db.execute("SELECT status,metadata_hash FROM sources WHERE research_id=? AND (doi=? OR openalex_id=?)", (request.research_id, source.doi, source.openalex_id)).fetchone()
            if not row or row['status'] != 'VERIFIED' or row['metadata_hash'] != metadata_digest(source):
                return False
        self.store.audit(request.research_id, 'SEARCH_VERIFIED_CACHE_REUSED', {'provider': self.name, 'query_hash': sha256(request.query.encode()).hexdigest(), 'search_cost': '0'})
        return True

    async def search(self, request: SearchRequest):
        from .product_policy import completion_budget
        if self.before_dispatch:
            self.before_dispatch()
        view = completion_budget(self.store, request.research_id, self.snapshot)
        protected = self.credentials.active_secrets(c.get('credential_env_name') for c in self.store.configs('connection'))
        task = self.task_snapshot(request)
        status = decision(task, budget_ok=view['can_complete'], protected=protected)
        if private_query(request.query, protected):
            status = 'SEARCH_PRIVATE_QUERY_BLOCKED'
        if status == 'SEARCH_ALLOWED' and (self.price is None or not self.price_source):
            status = 'PRICE_UNKNOWN'
        record = {'search_request_id': new_id('SRCH'), 'provider': self.name,
                  'query_hash': sha256(request.query.encode('utf-8', errors='strict')).hexdigest(),
                  'query_reference': 'owner-approved public query' if status == 'SEARCH_ALLOWED' else 'redacted',
                  'started_at': utc_now().isoformat(), 'result_count': 0, 'selected_result_ids': [],
                  'search_cost': None, 'latency_ms': 0, 'status': status}
        if status != 'SEARCH_ALLOWED':
            self.store.audit(request.research_id, 'SEARCH_POLICY_DECISION', record)
            if task.get('search_required'):
                raise ControlError(status)
            return SearchResult(provider=self.name, request=request, sources=[])
        from .product_policy import effective_cap
        reserve = Decimal(view['completion_reserve_usd'] or '0') if self.snapshot.get('adaptive_budget') else 0
        reservation = self.store.reserve(rid=request.research_id, connection=self.name, model='metadata-search',
            role='search', purpose='web_search', bound=self.price,
            run_limit=effective_cap(self.store, request.research_id, self.snapshot),
            monthly_limit=min(self.store.defaults().monthly_limit_usd, Decimal(self.snapshot['monthly_limit_usd'])),
            request_limit=min(self.store.defaults().request_limit_usd, Decimal(self.snapshot['request_limit_usd'])),
            attempts=self.snapshot['depth_limits']['attempts'], revision=self.price_source, completion_reserve=reserve)
        self.store.transition(reservation, 'DISPATCHED')
        started = perf_counter()
        self.store.audit(request.research_id, 'SEARCH_DISPATCHED', record | {'reservation_id': reservation})
        try:
            result = await self.provider.search(request)
            if result.request != request or result.provider != self.name or len(result.sources) > request.limit:
                raise ControlError('SEARCH_RESULT_CONTRACT')
            content = result.model_dump_json()
            if any(v and v in content for v in protected) or re.search(r'(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{35}|ghp_[A-Za-z0-9]{36})', content):
                raise ControlError('SECRET_IN_SEARCH_RESPONSE')
            record.update(status='COMPLETED', result_count=len(result.sources), query=request.query,
                          selected_result_ids=[s.doi or s.openalex_id or s.metadata_hash or sha256(s.title.encode()).hexdigest() for s in result.sources],
                          sources=[{'url': s.url, 'retrieved_at': s.retrieved_at, 'metadata_hash': metadata_digest(s), 'retrieval_reference': record['search_request_id']} for s in result.sources])
            return result
        except Exception:
            record['status'] = 'FAILED'
            raise
        finally:
            record.update(completed_at=utc_now().isoformat(), latency_ms=round((perf_counter()-started)*1000, 3))
            # 무료 메타데이터만 실패 비용을 0으로 확정한다. 유료 전송의 모호한 비용은 유지한다.
            if self.price == 0 or record['status'] == 'COMPLETED':
                self.store.transition(reservation, 'SETTLED', settled=self.price, response_id=record['search_request_id'])
                record['search_cost'] = str(self.price)
            else:
                self.store.transition(reservation, 'UNRESOLVED')
            self.store.audit(request.research_id, 'SEARCH_COMPLETED', record)

    async def get_work(self, external_id):
        raise ControlError('SEARCH_FETCH_NOT_APPROVED')


async def run_search(state, store, credentials, rid, snapshot, *, provider=None, price=None, price_source=None, before_dispatch=None):
    from .product_policy import completion_budget
    status = decision(snapshot, budget_ok=completion_budget(store, rid, snapshot)['can_complete'],
                      protected=credentials.active_secrets(c.get('credential_env_name') for c in store.configs('connection')))
    if status != 'SEARCH_ALLOWED':
        store.audit(rid, 'SEARCH_POLICY_DECISION', {'status': status, 'search_policy': snapshot.get('search_policy', 'DISABLED')})
        if snapshot.get('search_required'):
            raise ControlError(status)
        return
    from .literature import LiteratureCoordinator, LiteratureConfig, default_intents
    if provider is None:
        from datetime import datetime
        from .scholarly import CrossrefProvider
        provider = CrossrefProvider(mailto='')
        fresh = 0 <= (utc_now() - datetime.fromisoformat(CROSSREF_PRICE_CHECKED_AT)).total_seconds() <= 30 * 86400
        price, price_source = Decimal(0) if fresh else None, 'https://www.crossref.org/services/metadata-retrieval/'
    policy = PolicyProvider(provider, store, credentials, snapshot, price=price, price_source=price_source, before_dispatch=before_dispatch)
    limit = 3 if snapshot.get('performance_profile') in {'DEEP', 'MAX'} else 2
    intents = default_intents(snapshot['public_search_query'])
    plan = [intents[0], intents[2]] if limit == 2 else intents
    await LiteratureCoordinator(state, policy, config=LiteratureConfig(max_queries=limit, max_results_per_query=5)).run(rid, snapshot['public_search_query'], plan)
    if snapshot.get('search_required') and not state._db.execute("SELECT 1 FROM sources WHERE research_id=? AND status='VERIFIED'", (rid,)).fetchone():
        raise ControlError('SEARCH_REQUIRED_EVIDENCE_MISSING')
