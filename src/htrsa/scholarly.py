"""제한된 문헌 검색. 제공사 텍스트는 신뢰하지 않는 자료다."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import os
import re
from typing import Literal, Protocol
from urllib.parse import quote

import httpx
from pydantic import Field, ValidationError, field_validator, model_validator

from .database import from_json, to_json
from .schemas import StrictModel, utc_now


class ScholarlyError(Exception):
    """정책 안에서 신뢰할 수 있는 제공사 응답을 가져오지 못했다."""

    def __init__(self, message: str, *, code: str = "SCHOLARLY_ERROR",
                 status: str = "FAILED", status_code: int | None = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.status_code = status_code
        self.retry_after = retry_after


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", value.strip(), flags=re.I)
    value = value.strip().lower()
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else None


def normalize_openalex_id(value: str | None) -> str | None:
    if not value:
        return None
    match = re.fullmatch(r"(?:https?://openalex\.org/)?(W\d+)", value.strip(), re.I)
    return match.group(1).upper() if match else None


def metadata_digest(source: "NormalizedSource") -> str:
    fields = source.model_dump(exclude={"source_id", "research_id", "retrieved_at", "metadata_hash",
                                        "status", "provider", "provider_ids"}, mode="json")
    return hashlib.sha256(to_json(fields).encode("utf-8", errors="strict")).hexdigest()


class SearchRequest(StrictModel):
    research_id: str = Field(min_length=1)
    query: str = Field(min_length=2, max_length=500)
    year_from: int | None = Field(default=None, ge=1800, le=2100)
    year_to: int | None = Field(default=None, ge=1800, le=2100)
    limit: int = Field(default=10, ge=1, le=25)
    require_doi: bool = False
    open_access_only: bool = False
    page: int = Field(default=1, ge=1, le=10)
    search_policy: Literal['AUTO', 'DISABLED', 'ALLOWED'] | None = None
    evidence_required: bool | None = None

    @field_validator("query")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 2:
            raise ValueError("query is blank")
        return value

    @model_validator(mode="after")
    def ordered_years(self):
        if self.year_from is not None and self.year_to is not None and self.year_from > self.year_to:
            raise ValueError("year range is reversed")
        return self


class SearchIntent(StrictModel):
    kind: Literal["CORE", "MECHANISM", "CONTRADICTION", "CONFOUNDER"]
    query: str = Field(min_length=2, max_length=500)


class NormalizedSource(StrictModel):
    source_id: str | None = None
    research_id: str | None = None
    source_type: Literal["LITERATURE"] = "LITERATURE"
    title: str = Field(min_length=1)
    authors: list[str] = Field(default_factory=list)
    publication_year: int | None = None
    doi: str | None = None
    openalex_id: str | None = None
    source_name: str | None = None
    abstract: str | None = None
    url: str | None = None
    is_open_access: bool | None = None
    cited_by_count: int | None = None
    referenced_works: list[str] = Field(default_factory=list)
    related_works: list[str] = Field(default_factory=list)
    provider: str
    provider_ids: dict[str, str] = Field(default_factory=dict)
    retrieved_at: str = Field(default_factory=lambda: utc_now().isoformat())
    metadata_hash: str | None = None
    status: Literal["DISCOVERED", "RELEVANT", "IRRELEVANT", "EVIDENCE_EXTRACTED", "VERIFIED", "INVALIDATED"] = "DISCOVERED"

    @field_validator("doi")
    @classmethod
    def canonical_doi(cls, value: str | None) -> str | None:
        return normalize_doi(value)

    @field_validator("openalex_id")
    @classmethod
    def canonical_openalex(cls, value: str | None) -> str | None:
        return normalize_openalex_id(value)


class SearchResult(StrictModel):
    provider: str
    request: SearchRequest
    sources: list[NormalizedSource]
    total_results: int | None = None


def source_from_row(row) -> NormalizedSource:
    return NormalizedSource(source_id=row["source_id"], research_id=row["research_id"],
                            title=row["title"], authors=from_json(row["authors_json"]),
                            publication_year=row["publication_year"], doi=row["doi"],
                            openalex_id=row["openalex_id"], source_name=row["source_name"],
                            abstract=row["abstract"], url=row["url"],
                            is_open_access=None if row["is_open_access"] is None else bool(row["is_open_access"]),
                            cited_by_count=row["cited_by_count"], provider=row["provider"],
                            referenced_works=from_json(row["referenced_works_json"]),
                            related_works=from_json(row["related_works_json"]),
                            provider_ids=from_json(row["provider_ids_json"]),
                            retrieved_at=row["retrieved_at"], metadata_hash=row["metadata_hash"],
                            status=row["status"])


class ScholarlySearchProvider(Protocol):
    name: str

    async def search(self, request: SearchRequest) -> SearchResult: ...
    async def get_work(self, external_id: str) -> NormalizedSource: ...


def _apply_filters(request: SearchRequest, sources: list[NormalizedSource]) -> list[NormalizedSource]:
    return [source for source in sources
            if (not request.require_doi or source.doi is not None)
            and (not request.open_access_only or source.is_open_access is True)
            and (request.year_from is None or
                 (source.publication_year is not None and source.publication_year >= request.year_from))
            and (request.year_to is None or
                 (source.publication_year is not None and source.publication_year <= request.year_to))]


class ScholarlyHTTPClient:
    def __init__(self, *, timeout: float = 12.0, retries: int = 2, max_bytes: int = 2_000_000,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.timeout = timeout
        self.retries = retries
        self.max_bytes = max_bytes
        self.transport = transport
        self.last_status = "UNCONFIGURED"
        self.last_status_code: int | None = None
        self.last_retry_after: float | None = None
        self.last_error_code: str | None = None
        self.dispatch_guard = None

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        if not value:
            return None
        try:
            seconds = float(value)
            return max(0.0, seconds)
        except ValueError:
            pass
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None

    async def get_json(self, url: str, params: dict[str, str | int] | None = None) -> dict:
        from urllib.parse import urlsplit
        target = urlsplit(url)
        if target.scheme != 'https' or target.hostname not in {'api.crossref.org', 'api.openalex.org'} or target.username or target.password or target.port not in {None, 443}:
            raise ScholarlyError('승인되지 않은 검색 주소', code='SEARCH_DESTINATION_DENIED')
        transport = self.transport
        if transport is None:
            from .control_plane import Connection, PinnedTransport
            transport = PinnedTransport(Connection(connection_id='scholarly', display_name='문헌 검색', adapter_id='openai_compatible', base_url='https://' + target.hostname, auth_strategy='none', destination_approved=True))
        headers = {"User-Agent": "H-TRSA/0.1 scholarly-research (metadata only)", "Accept": "application/json"}
        self.last_status = "FAILED"
        self.last_status_code = None
        self.last_retry_after = None
        self.last_error_code = None
        async with httpx.AsyncClient(timeout=self.timeout, transport=transport, follow_redirects=False, trust_env=False,
                                     headers=headers) as client:
            for attempt in range(self.retries + 1):
                try:
                    if self.dispatch_guard is not None:
                        self.dispatch_guard()
                    async with client.stream("GET", url, params=params) as response:
                        self.last_status_code = response.status_code
                        if response.status_code in {429, 500, 502, 503, 504}:
                            retry_after = self._retry_after_seconds(response.headers.get("Retry-After"))
                            self.last_retry_after = retry_after
                            if attempt < self.retries:
                                delay = min(2.0, 0.2 * (2 ** attempt))
                                if retry_after is not None:
                                    delay = min(2.0, max(delay, retry_after))
                                await asyncio.sleep(delay)
                                continue
                            code = "SEARCH_RATE_LIMITED" if response.status_code == 429 else "PROVIDER_HTTP_ERROR"
                            self.last_status = "RATE_LIMITED" if response.status_code == 429 else "FAILED"
                            self.last_error_code = code
                            raise ScholarlyError(f"provider HTTP {response.status_code}", code=code,
                                                  status=self.last_status, status_code=response.status_code,
                                                  retry_after=retry_after)
                        if response.status_code != 200:
                            self.last_status = "FAILED"
                            self.last_error_code = "PROVIDER_HTTP_ERROR"
                            raise ScholarlyError(f"provider HTTP {response.status_code}",
                                                  code="PROVIDER_HTTP_ERROR", status="FAILED",
                                                  status_code=response.status_code)
                        header_length = response.headers.get("Content-Length", "0")
                        if not header_length.isdecimal():
                            self.last_error_code = "PROVIDER_INVALID_RESPONSE"
                            raise ScholarlyError("invalid provider content length", code="PROVIDER_INVALID_RESPONSE")
                        if int(header_length) > self.max_bytes:
                            self.last_error_code = "PROVIDER_RESPONSE_TOO_LARGE"
                            raise ScholarlyError("provider response exceeds size limit",
                                                  code="PROVIDER_RESPONSE_TOO_LARGE")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > self.max_bytes:
                                self.last_error_code = "PROVIDER_RESPONSE_TOO_LARGE"
                                raise ScholarlyError("provider response exceeds size limit",
                                                      code="PROVIDER_RESPONSE_TOO_LARGE")
                        try:
                            value = __import__("json").loads(body)
                        except (ValueError, UnicodeError) as exc:
                            self.last_error_code = "PROVIDER_INVALID_RESPONSE"
                            raise ScholarlyError("invalid provider JSON", code="PROVIDER_INVALID_RESPONSE") from exc
                        if not isinstance(value, dict):
                            self.last_error_code = "PROVIDER_INVALID_RESPONSE"
                            raise ScholarlyError("provider JSON must be an object",
                                                  code="PROVIDER_INVALID_RESPONSE")
                        self.last_status = "AVAILABLE"
                        self.last_error_code = None
                        return value
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt >= self.retries:
                        self.last_status = "FAILED"
                        self.last_error_code = "PROVIDER_TRANSPORT_ERROR"
                        raise ScholarlyError("provider request failed", code="PROVIDER_TRANSPORT_ERROR",
                                              status="FAILED") from exc
                    await asyncio.sleep(min(2.0, 0.2 * (2 ** attempt)))
        self.last_status = "FAILED"
        self.last_error_code = "PROVIDER_TRANSPORT_ERROR"
        raise ScholarlyError("provider request failed", code="PROVIDER_TRANSPORT_ERROR", status="FAILED")


def _abstract_from_index(index: object) -> str | None:
    if not isinstance(index, dict) or not index:
        return None
    positions: dict[int, str] = {}
    for word, offsets in index.items():
        if not isinstance(word, str) or not isinstance(offsets, list):
            raise ScholarlyError("invalid abstract index")
        for offset in offsets:
            if not isinstance(offset, int) or offset < 0 or offset > 20000 or offset in positions:
                raise ScholarlyError("invalid abstract position")
            positions[offset] = word
    if not positions or max(positions) + 1 != len(positions):
        raise ScholarlyError("incomplete abstract index")
    return " ".join(positions[i] for i in range(len(positions)))


def normalize_openalex_work(work: dict) -> NormalizedSource:
    if not isinstance(work, dict) or not isinstance(work.get("title") or work.get("display_name"), str):
        raise ScholarlyError("OpenAlex work has no title")
    authors = [part["author"]["display_name"] for part in work.get("authorships", [])
               if isinstance(part, dict) and isinstance(part.get("author"), dict)
               and isinstance(part["author"].get("display_name"), str)]
    source = (work.get("primary_location") or {}).get("source") or {}
    oa = work.get("open_access") or {}
    identity = normalize_openalex_id(work.get("id"))
    result = NormalizedSource(title=work.get("title") or work["display_name"], authors=authors,
                              publication_year=work.get("publication_year"), doi=work.get("doi"),
                              openalex_id=identity, source_name=source.get("display_name"),
                              abstract=_abstract_from_index(work.get("abstract_inverted_index")),
                              url=work.get("id"), is_open_access=oa.get("is_oa"),
                              cited_by_count=work.get("cited_by_count"),
                              referenced_works=[identity for item in work.get("referenced_works") or []
                                                if (identity := normalize_openalex_id(item))],
                              related_works=[identity for item in work.get("related_works") or []
                                             if (identity := normalize_openalex_id(item))],
                              provider="scholarly.openalex",
                              provider_ids={"openalex": identity} if identity else {})
    result.metadata_hash = metadata_digest(result)
    return result


def normalize_crossref_work(work: dict) -> NormalizedSource:
    if not isinstance(work, dict) or not isinstance(work.get("title"), list) or not work["title"]:
        raise ScholarlyError("Crossref work has no title")
    issued = (work.get("published") or work.get("issued") or {}).get("date-parts") or []
    year = issued[0][0] if issued and issued[0] else None
    authors = [" ".join(filter(None, [item.get("given"), item.get("family")])) for item in work.get("author", [])
               if isinstance(item, dict)]
    doi = normalize_doi(work.get("DOI"))
    result = NormalizedSource(title=work["title"][0], authors=authors, publication_year=year,
                              doi=doi, source_name=(work.get("container-title") or [None])[0],
                              abstract=None, url=work.get("URL"), provider="scholarly.crossref",
                              provider_ids={"crossref_doi": doi} if doi else {})
    result.metadata_hash = metadata_digest(result)
    return result


class OpenAlexProvider:
    name = "scholarly.openalex"

    def __init__(self, client: ScholarlyHTTPClient | None = None, api_key: str | None = None):
        self.client = client or ScholarlyHTTPClient()
        self.api_key = api_key if api_key is not None else os.getenv("OPENALEX_API_KEY")

    async def search(self, request: SearchRequest) -> SearchResult:
        filters = []
        if request.year_from is not None:
            filters.append(f"publication_year:>{request.year_from - 1}")
        if request.year_to is not None:
            filters.append(f"publication_year:<{request.year_to + 1}")
        if request.require_doi:
            filters.append("has_doi:true")
        if request.open_access_only:
            filters.append("open_access.is_oa:true")
        params: dict[str, str | int] = {"search": request.query, "per_page": request.limit, "page": request.page}
        if filters:
            params["filter"] = ",".join(filters)
        if self.api_key:
            params["api_key"] = self.api_key
        payload = await self.client.get_json("https://api.openalex.org/works", params)
        works = payload.get("results")
        if not isinstance(works, list):
            raise ScholarlyError("OpenAlex results missing")
        try:
            sources = [normalize_openalex_work(item) for item in works[:request.limit]]
        except (ValidationError, TypeError, KeyError, ValueError, AttributeError) as exc:
            raise ScholarlyError("invalid OpenAlex work metadata") from exc
        return SearchResult(provider=self.name, request=request, sources=_apply_filters(request, sources),
                            total_results=(payload.get("meta") or {}).get("count"))

    async def get_work(self, external_id: str) -> NormalizedSource:
        identity = normalize_openalex_id(external_id)
        doi = normalize_doi(external_id)
        if not identity and not doi:
            raise ValueError("expected OpenAlex ID or DOI")
        key = identity or f"https://doi.org/{doi}"
        params = {"api_key": self.api_key} if self.api_key else None
        try:
            return normalize_openalex_work(await self.client.get_json(
                "https://api.openalex.org/works/" + quote(key, safe=""), params))
        except (ValidationError, TypeError, KeyError, ValueError, AttributeError) as exc:
            raise ScholarlyError("invalid OpenAlex work metadata") from exc


class CrossrefProvider:
    name = "scholarly.crossref"

    def __init__(self, client: ScholarlyHTTPClient | None = None, mailto: str | None = None):
        self.client = client or ScholarlyHTTPClient()
        self.mailto = mailto if mailto is not None else os.getenv("CROSSREF_MAILTO")

    async def search(self, request: SearchRequest) -> SearchResult:
        params: dict[str, str | int] = {"query.bibliographic": request.query, "rows": request.limit,
                                        "offset": (request.page - 1) * request.limit}
        if self.mailto:
            params["mailto"] = self.mailto
        if request.year_from or request.year_to:
            params["filter"] = ",".join(part for part in [
                f"from-pub-date:{request.year_from}" if request.year_from else "",
                f"until-pub-date:{request.year_to}" if request.year_to else ""] if part)
        payload = await self.client.get_json("https://api.crossref.org/works", params)
        message = payload.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("items"), list):
            raise ScholarlyError("Crossref results missing")
        try:
            sources = [normalize_crossref_work(item) for item in message["items"][:request.limit]]
        except (ValidationError, TypeError, KeyError, ValueError, AttributeError) as exc:
            raise ScholarlyError("invalid Crossref work metadata") from exc
        return SearchResult(provider=self.name, request=request, sources=_apply_filters(request, sources),
                            total_results=message.get("total-results"))

    async def get_work(self, external_id: str) -> NormalizedSource:
        doi = normalize_doi(external_id)
        if not doi:
            raise ValueError("expected DOI")
        params = {"mailto": self.mailto} if self.mailto else None
        payload = await self.client.get_json("https://api.crossref.org/works/" + quote(doi, safe=""), params)
        if not isinstance(payload.get("message"), dict):
            raise ScholarlyError("Crossref work missing")
        try:
            source = normalize_crossref_work(payload["message"])
        except (ValidationError, TypeError, KeyError, ValueError, AttributeError) as exc:
            raise ScholarlyError("invalid Crossref work metadata") from exc
        if source.doi != doi:
            raise ScholarlyError("Crossref DOI mismatch")
        return source


class FakeScholarlyProvider:
    name = "scholarly.fake"

    def __init__(self, results: dict[str, list[NormalizedSource]]):
        self.results = results
        self.calls: list[str] = []

    async def search(self, request: SearchRequest) -> SearchResult:
        self.calls.append(request.query)
        sources = _apply_filters(request, self.results.get(request.query, []))[:request.limit]
        return SearchResult(provider=self.name, request=request, sources=sources, total_results=len(sources))

    async def get_work(self, external_id: str) -> NormalizedSource:
        for group in self.results.values():
            for source in group:
                if external_id in {source.doi, source.openalex_id}:
                    return source
        raise ScholarlyError("fake work not found")
