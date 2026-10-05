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

AI_SEARCH_PRICE_CHECKED_AT = '2026-10-05T00:00:00+00:00'


def default_search_price():
    from datetime import datetime
    fresh = 0 <= (utc_now() - datetime.fromisoformat(AI_SEARCH_PRICE_CHECKED_AT)).total_seconds() <= 30 * 86400
    return Decimal('.01') if fresh else None, 'https://developers.openai.com/api/docs/pricing'


def free_search_price(provider):
    from datetime import datetime
    fresh = 0 <= (utc_now() - datetime.fromisoformat('2026-10-04T15:00:00+00:00')).total_seconds() <= 30 * 86400
    source = 'https://help.openalex.org/access/example-costs/' if provider == 'scholarly.openalex' else (
        'https://docs.searxng.org/dev/search_api.html' if provider == 'scholarly.searxng' else 'https://www.crossref.org/services/metadata-retrieval/')
    return Decimal(0) if fresh else None, source


class SearchPolicy(StrEnum):
    AUTO = 'AUTO'
    DISABLED = 'DISABLED'
    ALLOWED = 'ALLOWED'


def useful(question):
    return bool(re.search(r'최근|최신|문헌|논문|출처|인용|공개 데이터|current|recent|latest|literature|citation|source verification', question, re.I))


def self_contained_calculation(snapshot, question=None):
    """입력값이 있는 계산 문제의 자동 검색을 선택 사항으로 구분한다."""
    text = question or snapshot.get('question', '')
    if (snapshot.get('execution_mode') != 'SCIENCE_AUTO'
            or snapshot.get('search_policy') != 'AUTO'
            or snapshot.get('search_required') or snapshot.get('public_search_query')
            or useful(text) or re.search(r'직접\s*(?:찾|확보|수집)|자료를?\s*(?:찾|확보|수집)', text)):
        return False
    return (bool(re.search(r'계산|적합|회귀|calculate|regression', text, re.I))
            and bool(re.search(r'가정|주어진|제공|다음.*자료|given|provided|=', text, re.I))
            and len(re.findall(r'\d+(?:\.\d+)?', text)) >= 2)


def private_query(query, protected=()):
    return (any(v and v in query for v in protected)
            or bool(re.search(r'(?i)(sk-[\w-]{12,}|AIza[\w-]{35}|ghp_\w{36}|[A-Z]:[\\/]|/home/|/Users/|https?://|@|API[_ ]?KEY|password|confidential|private|internal|비밀번호|비공개|주민등록|\d{3}[- ]\d{3,4}[- ]\d{4}|(?:\d+[,.]\d+[,; ]+){3})', query)))


def public_query(snapshot):
    """직접 지정한 검색어가 없으면 새 연구의 질문에서 공개 검색 주제를 구성한다."""
    text = str(snapshot.get('public_search_query') or '').strip()
    if not text and snapshot.get('settings_version', 1) >= 2:
        text = str(snapshot.get('question') or snapshot.get('title') or '').strip()
        if len(text)>400 and not private_query(text):
            text=re.split(r'\.(?:\s|$)|\n',text,maxsplit=1)[0].strip()[:400]
    return re.sub(r'\s+', ' ', text.translate(str.maketrans('₀₁₂₃₄₅₆₇₈₉', '0123456789'))).strip()


def decision(snapshot, *, question=None, budget_ok=True, protected=()):
    policy = SearchPolicy(snapshot.get('search_policy', 'DISABLED'))
    required = snapshot.get('search_required', False)
    if policy == SearchPolicy.DISABLED:
        return 'SEARCH_REQUIRED_BUT_DISABLED' if required else 'SEARCH_DISABLED'
    if self_contained_calculation(snapshot, question):
        return 'SEARCH_NOT_NEEDED'
    if snapshot.get("search_attempt_limit", 5) == 0:
        return 'SEARCH_ATTEMPT_LIMIT'
    if not required and not snapshot.get('ai_report_enabled') and not useful(question or snapshot['question']):
        return 'SEARCH_NOT_NEEDED'
    if snapshot.get('ai_report_enabled') and snapshot.get('public_search_consent') is not True:
        return 'SEARCH_EGRESS_DENIED'
    if snapshot.get('egress') != 'research' and not (snapshot.get('egress') == 'selected' and snapshot.get('public_search_consent') is True):
        return 'SEARCH_EGRESS_DENIED'
    query = public_query(snapshot)
    if not query:
        return 'SEARCH_QUERY_REQUIRED'
    if private_query(query, protected):
        return 'SEARCH_PRIVATE_QUERY_BLOCKED'
    if not required and snapshot.get('adaptive_budget', True) and not budget_ok:
        return 'SEARCH_OPTIONAL_REDUCED'
    return 'SEARCH_ALLOWED'


def search_allocation(store, rid, snapshot, *, price, price_source, view=None):
    """모델 완료 예산을 보존하고 별도 검색 단가로 남은 전송 횟수를 제한한다."""
    from .control_plane import micro
    from .product_policy import completion_budget
    view = view or completion_budget(store, rid, snapshot)
    limit = min(snapshot.get('search_attempt_limit', 5), store.defaults().search_attempt_limit)
    for kind in ('research_effective', 'research_settings'):
        try:
            value = store.config(kind, rid)['settings'].get('search_attempt_limit')
            if value is not None:
                limit = min(limit, value)
        except ControlError:
            pass
    try:
        used = store.config('search_attempts', rid)['used']
    except ControlError:
        used = 0
    if type(used) is not int or used < 0:
        raise ControlError('SEARCH_COUNTER_INVALID')
    remaining = max(0, limit - used)
    record = {'configured_limit': limit, 'used': used, 'remaining_attempts': remaining,
              'automatic': snapshot.get('adaptive_budget', True),
              'model_completion_reserve_usd': view['completion_reserve_usd'],
              'available_usd': view['available_usd'], 'search_unit_price_usd': str(price) if price is not None else None,
              'price_source': price_source, 'allowed_attempts': 0, 'status': 'PRICE_UNKNOWN'}
    if price is None or not price_source:
        return record
    unit = micro(price)
    if not remaining:
        return record | {'status': 'SEARCH_ATTEMPT_LIMIT'}
    if unit == 0:
        return record | {'allowed_attempts': remaining, 'status': 'FREE_REQUEST_LIMIT', 'cash_cost_usd': '0'}
    if not snapshot.get('adaptive_budget', True):
        return record | {'allowed_attempts': remaining, 'status': 'MANUAL_LIMIT'}
    if not view['can_complete']:
        return record | {'status': 'COMPLETION_RESERVE_BLOCKED'}
    available = max(Decimal(0), Decimal(view['available_usd']) - Decimal(view['completion_reserve_usd'] or '0'))
    # 원장은 요청마다 올림한다. 같은 단위로 나누어 작은 유료 요청도 공짜로 계산하지 않는다.
    funds = int(available * 1000000)
    request_cap = min(store.defaults().request_limit_usd, Decimal(snapshot['request_limit_usd']))
    count = remaining if unit == 0 else min(remaining, funds // unit)
    if unit > micro(request_cap):
        count = 0
    return record | {'allowed_attempts': count, 'search_budget_usd': str(available),
                     'status': 'AUTOMATIC_LIMIT' if count else 'COMPLETION_RESERVE_BLOCKED'}


class PolicyProvider:
    def __init__(self, provider, store, credentials, snapshot, *, price=None, price_source=None, before_dispatch=None):
        self.provider, self.store, self.credentials, self.snapshot = provider, store, credentials, snapshot
        self.name, self.price, self.price_source = provider.name, price, price_source
        self.before_dispatch = before_dispatch
        self.rid = snapshot.get('research_id')

    def protected(self):
        return self.credentials.active_secrets([*(c.get('credential_env_name') for c in self.store.configs('connection')), 'OPENALEX_API_KEY'])

    def allowance_usage(self, client):
        raw = getattr(client, 'last_usage', {})
        if not isinstance(raw, dict):
            return {}
        names = {'X-RateLimit-Limit', 'X-RateLimit-Remaining', 'X-RateLimit-Credits-Used', 'X-RateLimit-Reset'}
        protected = self.protected()
        return {name: str(value) for name, value in raw.items() if name in names and len(str(value)) <= 128
                and re.fullmatch(r'[0-9TZ:+. -]+', str(value)) and not any(secret in str(value) for secret in protected)}

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
        protected = self.protected()
        if decision(snapshot, protected=protected) != 'SEARCH_ALLOWED' or private_query(request.query, protected):
            return False
        cached = SearchResult.model_validate(previous)
        if cached.request != request:
            return False
        if self.snapshot.get('ai_report_enabled'):
            proof = self.store.db.execute("SELECT payload FROM control_audit WHERE research_id=? AND kind='SEARCH_COMPLETED' ORDER BY seq DESC", (request.research_id,)).fetchall()
            import json
            completed = any((item := json.loads(row[0])).get('status') == 'COMPLETED' and item.get('query') == request.query
                            and item.get('provider') == self.name and item.get('selected_result_ids') == [s.doi or s.openalex_id or s.metadata_hash or sha256(s.title.encode()).hexdigest() for s in cached.sources]
                            and [v.get('metadata_hash') for v in item.get('sources', [])] == [metadata_digest(s) for s in cached.sources]
                            for row in proof)
            if completed:
                for source in cached.sources:
                    row = self.store.db.execute("SELECT status,metadata_hash FROM sources WHERE research_id=? AND (doi=? OR openalex_id=?)", (request.research_id, source.doi, source.openalex_id)).fetchone()
                    if row and row['status'] == 'INVALIDATED':
                        return False
                self.store.audit(request.research_id, 'SEARCH_COMPLETED_REPLAY_REUSED', {'provider':self.name,'query_hash':sha256(request.query.encode()).hexdigest(),'search_cost':'0'})
                return True
        if not cached.sources:
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
        protected = self.protected()
        task = self.task_snapshot(request)
        self.rid = request.research_id
        status = decision(task, budget_ok=self.price == 0 or view['can_complete'], protected=protected)
        if private_query(request.query, protected):
            status = 'SEARCH_PRIVATE_QUERY_BLOCKED'
        if status == 'SEARCH_ALLOWED' and (self.price is None or not self.price_source):
            status = 'PRICE_UNKNOWN'
        allocation = search_allocation(self.store, request.research_id, task,
            price=self.price, price_source=self.price_source, view=view)
        if status == 'SEARCH_OPTIONAL_REDUCED' and allocation['status'] == 'COMPLETION_RESERVE_BLOCKED':
            status = 'COMPLETION_RESERVE_BLOCKED'
        if status == 'SEARCH_ALLOWED' and not allocation['allowed_attempts']:
            status = allocation['status']
        record = {'search_request_id': new_id('SRCH'), 'provider': self.name,
                  'query_hash': sha256(request.query.encode('utf-8', errors='strict')).hexdigest(),
                  'query_reference': 'owner-approved public query' if status == 'SEARCH_ALLOWED' else 'redacted',
                  'started_at': utc_now().isoformat(), 'result_count': 0, 'selected_result_ids': [],
                  'search_cost': None, 'latency_ms': 0, 'status': status}
        self.store.audit(request.research_id, 'SEARCH_BUDGET_ALLOCATION', allocation)
        if status != 'SEARCH_ALLOWED':
            self.store.audit(request.research_id, 'SEARCH_POLICY_DECISION', record)
            if task.get('search_required') or status == 'COMPLETION_RESERVE_BLOCKED':
                raise ControlError(status)
            return SearchResult(provider=self.name, request=request, sources=[])
        from .product_policy import effective_cap
        reserve = Decimal(view['completion_reserve_usd'] or '0') if self.snapshot.get('adaptive_budget') and self.price != 0 else 0
        reservation = self.store.reserve(rid=request.research_id, connection=self.name, model='metadata-search',
            role='search', purpose='web_search', bound=self.price,
            run_limit=effective_cap(self.store, request.research_id, self.snapshot),
            monthly_limit=min(self.store.defaults().monthly_limit_usd, Decimal(self.snapshot['monthly_limit_usd'])),
            request_limit=min(self.store.defaults().request_limit_usd, Decimal(self.snapshot['request_limit_usd'])),
            attempts=self.snapshot['depth_limits']['attempts'], revision=self.price_source, completion_reserve=reserve)
        started = perf_counter()
        dispatched = 0
        client = getattr(self.provider, "client", None)
        previous_guard = getattr(client, "dispatch_guard", None)
        def guard():
            nonlocal dispatched
            if self.before_dispatch:
                self.before_dispatch()
            self._time_guard(request.research_id)
            self.store.dispatch_search(reservation, request.research_id,
                min(task.get("search_attempt_limit", 5), allocation['used'] + allocation['allowed_attempts']), self.name,
                snapshot=None if self.price == 0 else self.snapshot, completion_reserve=reserve)
            dispatched += 1
            self.store.audit(request.research_id, 'SEARCH_DISPATCHED', record | {'reservation_id': reservation, 'transport_attempt':dispatched})
        try:
            if client is not None and hasattr(client, "dispatch_guard"):
                if previous_guard is not None:
                    raise ControlError("SEARCH_CLIENT_BUSY")
                if self.price != 0 and client.retries:
                    raise ControlError("SEARCH_RETRY_BOUND_UNAVAILABLE")
                client.dispatch_guard = guard
            else:
                guard()
            result = await self.provider.search(request)
            if result.request != request or result.provider != self.name or len(result.sources) > request.limit:
                raise ControlError('SEARCH_RESULT_CONTRACT')
            content = result.model_dump_json()
            if any(v and v in content for v in protected) or re.search(r'(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{35}|ghp_[A-Za-z0-9]{36})', content):
                raise ControlError('SECRET_IN_SEARCH_RESPONSE')
            record.update(status='COMPLETED', result_count=len(result.sources), query=request.query,
                          selected_result_ids=[s.doi or s.openalex_id or s.metadata_hash or sha256(s.title.encode()).hexdigest() for s in result.sources],
                          sources=[{'url': s.url, 'doi': s.doi, 'openalex_id': s.openalex_id, 'retrieved_at': s.retrieved_at, 'metadata_hash': metadata_digest(s),
                                    'oa_pdf_url': s.provider_ids.get('oa_pdf_url'), 'retrieval_reference': record['search_request_id']} for s in result.sources])
            return result
        except Exception:
            record['status'] = 'FAILED'
            raise
        finally:
            if client is not None and hasattr(client, "dispatch_guard"):
                client.dispatch_guard = previous_guard
            record.update(completed_at=utc_now().isoformat(), latency_ms=round((perf_counter()-started)*1000, 3), actual_dispatch_attempts=dispatched)
            record['free_allowance_usage'] = self.allowance_usage(client)
            record['cash_cost_usd'] = '0' if self.price == 0 else record.get('search_cost')
            # 무료 메타데이터만 실패 비용을 0으로 확정한다. 유료 전송의 모호한 비용은 유지한다.
            if not dispatched:
                self.store.transition(reservation, 'RELEASED')
                record.update(status="BLOCKED_BEFORE_DISPATCH", search_cost="0")
            elif self.price == 0 or record['status'] == 'COMPLETED':
                self.store.transition(reservation, 'SETTLED', settled=self.price, response_id=record['search_request_id'])
                record['search_cost'] = str(self.price)
            else:
                self.store.transition(reservation, 'UNRESOLVED')
            self.store.audit(request.research_id, 'SEARCH_COMPLETED', record)

    async def get_work(self, external_id):
        from .scholarly import normalize_doi, normalize_openalex_id, NormalizedSource
        doi, identity = normalize_doi(external_id), normalize_openalex_id(external_id)
        if self.price != 0 or not self.rid or not (doi or identity) or not self.store.db.execute(
                "SELECT 1 FROM sources WHERE research_id=? AND (doi=? OR openalex_id=?) AND status NOT IN ('IRRELEVANT','INVALIDATED')",
                (self.rid, doi, identity)).fetchone():
            raise ControlError('SEARCH_FETCH_NOT_APPROVED')
        key = sha256((self.rid + self.name + external_id).encode('utf-8', errors='strict')).hexdigest()
        try:
            cached = self.store.config('source_lookup', key)
        except ControlError:
            cached = None
        if cached:
            if self.before_dispatch:
                self.before_dispatch()
            if decision(self.snapshot, budget_ok=True, protected=self.protected()) != 'SEARCH_ALLOWED':
                raise ControlError('SEARCH_FETCH_NOT_APPROVED')
            if cached['status'] == 'FAILED':
                from .scholarly import ScholarlyError
                raise ScholarlyError('이미 기록된 보완 조회 실패', code=cached['error'])
            source = NormalizedSource.model_validate(cached['source'])
            if sha256(source.model_dump_json().encode()).hexdigest() != cached['sha256']:
                raise ControlError('SEARCH_CACHE_CHANGED')
            if (doi and source.doi != doi or identity and source.openalex_id != identity
                    or any(v in source.model_dump_json() for v in self.protected())):
                raise ControlError('SEARCH_CACHE_CHANGED')
            return source
        async def invoke():
            source = await self.provider.get_work(external_id)
            if doi and source.doi != doi or identity and source.openalex_id != identity:
                raise ControlError('SEARCH_FETCH_IDENTITY_MISMATCH')
            if any(v in source.model_dump_json() for v in self.protected()):
                raise ControlError('SECRET_IN_SEARCH_RESPONSE')
            return source
        try:
            source = await self._free_request('lookup', external_id, getattr(self.provider, 'client', None), invoke)
            value = {'status': 'COMPLETED', 'source': source.model_dump(mode='json'),
                     'sha256': sha256(source.model_dump_json().encode()).hexdigest()}
        except Exception as exc:
            from .control_plane import ControlBoundary
            if isinstance(exc, ControlBoundary) or isinstance(exc, ControlError):
                raise
            value = {'status': 'FAILED', 'error': getattr(exc, 'code', 'SEARCH_FETCH_FAILED')}
            self.store.put('source_lookup', key, value)
            raise
        self.store.put('source_lookup', key, value)
        return source

    def _time_guard(self, rid):
        from .control_runtime import snapshot_remaining
        run = self.store.db.execute('SELECT started_at FROM control_runs WHERE research_id=?', (rid,)).fetchone()
        if run and run[0] and snapshot_remaining(self.snapshot, run[0]) <= 0:
            raise ControlError('TIME_LIMIT')

    async def _free_request(self, kind, identity, client, invoke):
        from .product_policy import effective_cap
        status = decision(self.snapshot, budget_ok=True, protected=self.protected())
        if status != 'SEARCH_ALLOWED' or self.price != 0 or not self.price_source:
            raise ControlError(status if status != 'SEARCH_ALLOWED' else 'SEARCH_FETCH_NOT_APPROVED')
        allocation = search_allocation(self.store, self.rid, self.snapshot, price=Decimal(0), price_source=self.price_source)
        if not allocation['allowed_attempts']:
            raise ControlError(allocation['status'])
        reservation = self.store.reserve(rid=self.rid, connection=self.name, model='public-' + kind, role='search', purpose='web_search',
            bound=0, run_limit=effective_cap(self.store, self.rid, self.snapshot), monthly_limit=self.snapshot['monthly_limit_usd'],
            request_limit=self.snapshot['request_limit_usd'], attempts=self.snapshot['depth_limits']['attempts'], revision=self.price_source)
        previous = getattr(client, 'dispatch_guard', None)
        dispatched = 0
        record = {'kind': kind, 'identity_hash': sha256(identity.encode('utf-8', errors='strict')).hexdigest(),
                  'reservation_id': reservation, 'provider': self.name, 'status': 'FAILED', 'cash_cost_usd': '0'}
        def guard():
            nonlocal dispatched
            if self.before_dispatch:
                self.before_dispatch()
            self._time_guard(self.rid)
            self.store.dispatch_search(reservation, self.rid, allocation['configured_limit'], self.name, snapshot=None)
            dispatched += 1
        try:
            if previous is not None:
                raise ControlError('SEARCH_CLIENT_BUSY')
            if client is None:
                guard()
            else:
                client.dispatch_guard = guard
            result = await invoke()
            record['status'] = 'COMPLETED'
            if hasattr(result, 'provider_ids'):
                record.update(doi=result.doi, openalex_id=result.openalex_id,
                              metadata_hash=metadata_digest(result), oa_pdf_url=result.provider_ids.get('oa_pdf_url'))
            return result
        except Exception as exc:
            record['error'] = getattr(exc, 'code', 'SEARCH_FETCH_FAILED')
            raise
        finally:
            if client is not None:
                client.dispatch_guard = previous
            self.store.transition(reservation, 'SETTLED' if dispatched else 'RELEASED', settled=0)
            record.update(actual_dispatch_attempts=dispatched, free_allowance_usage=self.allowance_usage(client))
            self.store.audit(self.rid, 'SEARCH_FETCH_COMPLETED', record)

    def approve_document_url(self, source, url):
        from .source_documents import validate_public_url
        validate_public_url(url)
        if any(v in url for v in self.protected()):
            raise ControlError('SECRET_IN_DOCUMENT_URL')
        if url == 'https://content.openalex.org/works/' + str(source.openalex_id) + '.pdf' and self.snapshot.get('openalex_archive_enabled'):
            return
        import json
        for row in self.store.db.execute("SELECT kind,payload FROM control_audit WHERE research_id=? AND kind IN ('SEARCH_COMPLETED','SEARCH_FETCH_COMPLETED')", (self.rid,)):
            record = json.loads(row['payload'])
            entries = record.get('sources', []) if row['kind'] == 'SEARCH_COMPLETED' else [record]
            if record.get('status') == 'COMPLETED' and any(
                    v.get('oa_pdf_url') == url and (v.get('metadata_hash') == source.metadata_hash
                        or source.doi and v.get('doi') == source.doi or source.openalex_id and v.get('openalex_id') == source.openalex_id) for v in entries):
                return
        raise ControlError('DOCUMENT_URL_UNAPPROVED')

    async def fetch_document(self, url, client, *, headers=None):
        if not self.snapshot.get('fulltext_enabled'):
            raise ControlError('DOCUMENT_FETCH_DISABLED')
        async def invoke():
            return await client.get_bytes(url, headers=headers)
        return await self._free_request('document', url, client, invoke)

    async def archive_access(self, identity, client):
        from .source_documents import PublicDocumentClient
        key = self.credentials.get('OPENALEX_API_KEY')
        if not key:
            raise ControlError('OPENALEX_KEY_REQUIRED')
        quota_client = PublicDocumentClient(transport=client.transport)
        async def check():
            return await quota_client.get_json('https://api.openalex.org/rate-limit', headers={'Authorization': 'Bearer ' + key})
        quota = await self._free_request('quota', identity, quota_client, check)
        try:
            info = quota.get('rate_limit', quota)
            free = Decimal(str(info['daily_remaining_usd']))
            prepaid = Decimal(str(info['prepaid_balance_usd']))
            cost = Decimal(str(info['endpoint_costs_usd']['content']))
            if (not free.is_finite() or not prepaid.is_finite() or not cost.is_finite()
                    or not Decimal(0) < cost <= Decimal('.01') or free < cost or prepaid != 0):
                raise ValueError()
        except (KeyError, ValueError, ArithmeticError):
            raise ControlError('OPENALEX_FREE_ALLOWANCE_UNVERIFIED') from None
        return 'https://content.openalex.org/works/' + identity + '.pdf', {'Authorization': 'Bearer ' + key}


def qualified_literature(state, rid):
    from .final_report import _validate_literature_provenance
    for row in state._db.execute("SELECT * FROM evidence WHERE research_id=? AND source_type='LITERATURE' AND status='VERIFIED'", (rid,)):
        source = state._db.execute("SELECT * FROM sources WHERE research_id=? AND source_id=?", (rid, row["source_id"])).fetchone()
        if source is not None:
            _validate_literature_provenance(dict(source), dict(row), state=state)
            return True
    return False


async def run_search(state, store, credentials, rid, snapshot, *, provider=None, price=None, price_source=None, before_dispatch=None, queries=None, document_client=None, gateway=None):
    if qualified_literature(state, rid) and not queries:
        return
    from .product_policy import completion_budget
    status = decision(snapshot, budget_ok=provider is None or price == 0 or completion_budget(store, rid, snapshot)['can_complete'],
                      protected=credentials.active_secrets([c.get('credential_env_name') for c in store.configs('connection')] + ['OPENALEX_API_KEY']))
    if status != 'SEARCH_ALLOWED':
        store.audit(rid, 'SEARCH_POLICY_DECISION', {'status': status, 'search_policy': snapshot.get('search_policy', 'DISABLED')})
        if snapshot.get('search_required'):
            raise ControlError(status)
        return
    free_backend=provider is None and snapshot.get('search_backend')=='FREE_SCHOLARLY'
    if free_backend:
        from .scholarly import OpenAlexProvider
        provider=OpenAlexProvider(api_key='');price=Decimal(0);price_source='무료 공개 학술 검색'
    if provider is None:
        from .ai_web_search import run_ai_web_search
        return await run_ai_web_search(state, store, credentials, rid, snapshot, gateway=gateway,
            before_dispatch=before_dispatch, queries=queries, document_client=document_client)
    from .literature import LiteratureCoordinator, LiteratureConfig, default_intents, topic_concepts
    from .scholarly import SearchIntent, ScholarlyError, source_from_row
    snapshot = dict(snapshot, research_id=rid, public_search_query=public_query(snapshot)[:400])
    policy = PolicyProvider(provider, store, credentials, snapshot, price=price, price_source=price_source, before_dispatch=before_dispatch)
    supplement = None
    if free_backend:
        from .scholarly import CrossrefProvider
        supplement=PolicyProvider(CrossrefProvider(mailto=''),store,credentials,snapshot,price=Decimal(0),
                                  price_source='무료 공개 학술 검색',before_dispatch=before_dispatch)
    question = snapshot['question']
    if queries:
        protected = policy.protected()
        if len(queries) > 3 or any(not isinstance(q, str) or not 2 <= len(q.strip()) <= 400 or private_query(q, protected) for q in queries):
            raise ControlError('SEARCH_PRIVATE_QUERY_BLOCKED')
    base = snapshot['public_search_query'].strip().translate(str.maketrans('₀₁₂₃₄₅₆₇₈₉', '0123456789'))
    phrases = list(dict.fromkeys([base, *((q.strip().translate(str.maketrans('₀₁₂₃₄₅₆₇₈₉', '0123456789'))) for q in (queries or []))]))
    english = next((q for q in phrases if re.search('[a-zA-Z]{4,}', q) and not re.search('[가-힣]', q)), None)
    if not english and {'co2','release'} <= topic_concepts(question + ' ' + base):
        english = 'CO2 release rate temperature carbonated beverages'
        phrases.append(english)
    core = phrases if len(phrases) > 1 else [base, default_intents(base)[1].query]
    if english:
        core = list(dict.fromkeys([base, english, *core]))
    mechanism = 'CO2 degassing temperature carbonated water' if {'co2','release'} <= topic_concepts(question + ' ' + base) else (
        phrases[2] if len(phrases) > 2 else (english or base) + ' mechanism')
    plan = [SearchIntent(kind='CORE', query=q) for q in core]
    plan += [SearchIntent(kind='MECHANISM', query=mechanism),
             SearchIntent(kind='CONTRADICTION', query='null effect conflicting evidence ' + (english or base))]
    question += ' ' + ' '.join(core[1:])

    async def enrich(source_id):
        source = source_from_row(state._one('SELECT * FROM sources WHERE research_id=? AND source_id=?', (rid, source_id)))
        for backend in [p for p in (policy, supplement) if p is not None and p.name != source.provider]:
            if source.abstract:
                break
            identity = source.doi or (source.openalex_id if backend.name == 'scholarly.openalex' else None)
            if not identity:
                continue
            try:
                replacement = await backend.get_work(identity)
                state.upsert_source(rid, replacement)
                source = source_from_row(state._one('SELECT * FROM sources WHERE research_id=? AND source_id=?', (rid, source_id)))
            except ScholarlyError:
                continue
            except ControlError as exc:
                if exc.code == 'SEARCH_ATTEMPT_LIMIT':
                    return
                if exc.code not in {'SEARCH_FETCH_NOT_APPROVED','SEARCH_FETCH_IDENTITY_MISMATCH'}:
                    raise
        if not source.abstract and snapshot.get('fulltext_enabled'):
            from .source_documents import acquire_document
            await acquire_document(state, policy, source_id, client=document_client)
    state.runtime_event(rid, 'LITERATURE_SEARCH_STARTED', {'query_count': len(plan)})
    try:
        coordinator = LiteratureCoordinator(state, policy, fallback=supplement,
            config=LiteratureConfig(max_queries=4, max_results_per_query=5,
                action_limit=min(200,max(50,int(snapshot.get('science_max_actions',50)))) if free_backend else 50,
                max_adaptive_queries=min(20,max(2,int(snapshot.get('search_attempt_limit',6)))) if free_backend else 6), enricher=enrich)
        await coordinator.run_adaptive(rid, question, plan)
    except ControlError as exc:
        if exc.code not in {"SEARCH_ATTEMPT_LIMIT", "COMPLETION_RESERVE_BLOCKED"}:
            raise
        store.audit(rid, "SEARCH_ATTEMPT_LIMIT_REACHED", {"limit":snapshot.get("search_attempt_limit", 5), "reason": exc.code})
    if snapshot.get('search_required') and not qualified_literature(state, rid):
        raise ControlError('SEARCH_REQUIRED_EVIDENCE_MISSING')
