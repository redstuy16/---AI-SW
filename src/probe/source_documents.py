"""공개 원문 수집과 페이지 근거. 네트워크 요청은 연구 전송 경계를 거친다."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urljoin, urlsplit

import httpx

from .control_plane import ControlError, PinnedTransport
from .database import to_json
from .schemas import utc_now
from .scholarly import ScholarlyError, metadata_digest, source_from_row
from .storage import sha256_file
from .bounded_http import bounded_bytes, ResponseLimitError

_PDF_CONTROL = ContextVar("pdf_extraction_control", default=None)

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 40
MAX_TEXT_BYTES = 2_000_000
EXTRACTOR_VERSION = "pypdf-pages-v1"


def validate_public_url(url, *, allow_loopback=False, root=False):
    try:
        target = urlsplit(url)
        host, port = target.hostname, target.port
        if not host or target.username or target.password or target.fragment:
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
            local = address.is_loopback
            if not local and not address.is_global:
                raise ControlError("SEARCH_DESTINATION_DENIED")
        except ValueError:
            local = False
        if local:
            if not allow_loopback or target.scheme not in {"https", "http"}:
                raise ValueError()
        elif target.scheme != "https" or port not in {None, 443}:
            raise ValueError()
        if root and (target.path not in {"", "/"} or target.query):
            raise ValueError()
        if re.search(r"(?i)(?:api[_-]?key|token|password|secret)=|sk-[\w-]{12,}", target.query):
            raise ValueError()
        return url.rstrip("/") if root else url
    except (ValueError, TypeError):
        raise ControlError("SEARCH_DESTINATION_DENIED") from None


class PublicDocumentClient:
    def __init__(self, *, transport=None, timeout=20):
        self.transport, self.timeout = transport, timeout
        self.dispatch_guard = None
        self.retries = 0
        self.last_usage = {}
        self.last_content_type = None

    async def get_bytes(self, url, *, limit=MAX_BYTES, allow_loopback=False, headers=None):
        original = urlsplit(url)
        safe_headers = {"User-Agent": "Probe/0.1 public-document", **(headers or {})}
        for hop in range(4):
            validate_public_url(url, allow_loopback=allow_loopback)
            target = urlsplit(url)
            if (target.scheme, target.netloc) != (original.scheme, original.netloc):
                safe_headers.pop("Authorization", None)
            transport = self.transport or PinnedTransport.public_resource(url, allow_loopback=allow_loopback)
            async with httpx.AsyncClient(transport=transport, timeout=self.timeout, trust_env=False,
                                         follow_redirects=False) as client:
                if self.dispatch_guard:
                    self.dispatch_guard()
                try:
                    async with client.stream("GET", url, headers=safe_headers) as response:
                        self.last_usage.update({k: response.headers[k] for k in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Credits-Used", "X-RateLimit-Reset") if k in response.headers})
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if hop == 3 or not response.headers.get("Location"):
                                raise ScholarlyError("공개 자료 이동 횟수 초과", code="DOCUMENT_REDIRECT_LIMIT")
                            if allow_loopback:
                                raise ScholarlyError("검색 서버 주소가 변경됨", code="SEARCH_DESTINATION_DENIED")
                            url = urljoin(url, response.headers["Location"])
                            continue
                        if response.status_code != 200:
                            raise ScholarlyError("공개 자료 요청 실패", code="SEARCH_RATE_LIMITED" if response.status_code == 429 else "DOCUMENT_HTTP_ERROR",
                                                  status="RATE_LIMITED" if response.status_code == 429 else "FAILED", status_code=response.status_code)
                        self.last_content_type = response.headers.get("Content-Type", "")
                        self.last_usage.update({k: response.headers[k] for k in
                            ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Credits-Used", "X-RateLimit-Reset") if k in response.headers})
                        try:
                            body = await bounded_bytes(response, limit)
                        except ResponseLimitError as exc:
                            raise ScholarlyError("공개 자료 크기 또는 압축 형식 제한", code="DOCUMENT_TOO_LARGE" if exc.code == "RESPONSE_TOO_LARGE" else exc.code) from None
                        return body, url
                except httpx.TimeoutException:
                    raise ScholarlyError("공개 자료 시간 초과", code="DOCUMENT_TIMEOUT") from None
                except httpx.TransportError:
                    raise ScholarlyError("공개 자료 연결 실패", code="DOCUMENT_TRANSPORT_ERROR") from None
        raise ScholarlyError("공개 자료 이동 횟수 초과", code="DOCUMENT_REDIRECT_LIMIT")

    async def get_json(self, url, *, allow_loopback=False, headers=None):
        data, _ = await self.get_bytes(url, limit=2_000_000, allow_loopback=allow_loopback, headers=headers)
        try:
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise ScholarlyError("공개 검색 응답 형식 오류", code="PROVIDER_INVALID_RESPONSE") from None


def document_record(state, rid, source_id):
    if not state._db.execute("SELECT 1 FROM sqlite_master WHERE name='control_configs'").fetchone():
        return None
    row = state._db.execute("SELECT payload FROM control_configs WHERE kind='source_document' AND id=?", (source_id,)).fetchone()
    value = json.loads(row[0]) if row else None
    if value and value.get("research_id") != rid:
        raise ControlError("SOURCE_DOCUMENT_OWNER_MISMATCH")
    return value


def checked_document(state, rid, source_id, *, require_text=True):
    source = state._one("SELECT * FROM sources WHERE research_id=? AND source_id=?", (rid, source_id))
    record = document_record(state, rid, source_id)
    if (not record or source["status"] == "INVALIDATED" or record["source_metadata_hash"] != source['metadata_hash']
            or record["source_metadata_hash"] != metadata_digest(source_from_row(source))):
        raise ControlError("SOURCE_DOCUMENT_STALE")
    for key, kind in (("pdf", "SOURCE_PDF"), ("text", "SOURCE_TEXT")):
        item = record.get(key)
        if not item:
            if key == "text" and not require_text:
                continue
            raise ControlError("SOURCE_DOCUMENT_UNREADABLE")
        artifact = state.file_artifact(item["artifact_id"], rid)
        if (artifact["status"] != "VERIFIED" or artifact["artifact_type"] != kind or artifact["sha256"] != item["sha256"]
                or artifact['relative_path'] != item['relative_path']):
            raise ControlError("SOURCE_DOCUMENT_CHANGED")
    if require_text:
        if record["status"] != "READY" or record.get("extractor_version") != EXTRACTOR_VERSION:
            raise ControlError("SOURCE_DOCUMENT_UNREADABLE")
        path = state.workspace.path(rid, record["text"]["relative_path"])
        if path.stat().st_size > MAX_TEXT_BYTES + 20000:
            raise ControlError("SOURCE_DOCUMENT_CHANGED")
        pages = json.loads(path.read_text(encoding="utf-8", errors="strict"))["pages"]
        if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES or any(
                p.get("page") != i + 1 or not isinstance(p.get("text"), str) for i, p in enumerate(pages)):
            raise ControlError("SOURCE_DOCUMENT_CHANGED")
        return record, pages
    return record, None


def document_span(state, rid, source_id, location, *, proof=None):
    record, pages = checked_document(state, rid, source_id)
    match = re.fullmatch(r"fulltext page ([1-9][0-9]?)", location)
    if not match or int(match[1]) > len(pages):
        raise ControlError("SOURCE_DOCUMENT_LOCATION_INVALID")
    expected = {k: record[k] for k in ("pdf", "text", "extractor_version", "source_metadata_hash")}
    if proof is not None and proof != expected:
        raise ControlError("SOURCE_DOCUMENT_CHANGED")
    return pages[int(match[1]) - 1]["text"], expected


def _limit_memory():
    if os.name != "nt":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        return
    import ctypes
    from ctypes import wintypes as w
    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong), ("flags", w.DWORD),
                    ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t), ("active", w.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    info = Extended(); info.basic.flags = 0x100; info.process_memory = 512 * 1024 * 1024
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        raise ControlError("PDF_RESOURCE_LIMIT_UNAVAILABLE")


def _extract_file(path):
    _limit_memory()
    from pypdf import PdfReader
    reader = PdfReader(path, strict=False)
    if reader.is_encrypted:
        raise ControlError("PDF_ENCRYPTED")
    pages, size = [], 0
    for index, page in enumerate(reader.pages[:MAX_PAGES]):
        text = page.extract_text() or ""
        text = re.sub(r"[ \t]+", " ", text).strip()
        size += len(text.encode("utf-8", errors="strict"))
        if size > MAX_TEXT_BYTES:
            raise ControlError("PDF_TEXT_TOO_LARGE")
        pages.append({"page": index + 1, "text": text})
    if not any(p["text"] for p in pages):
        raise ControlError("PDF_OCR_REQUIRED")
    return {"pages": pages, "total_pages": len(reader.pages), "truncated": len(reader.pages) > MAX_PAGES,
            "metadata_title": str((reader.metadata or {}).get("/Title", ""))[:500], "extractor_version": EXTRACTOR_VERSION}


def extract_pdf(path):
    from .runtime_environment import child_environment
    from .analysis_process import terminate
    env = child_environment()
    control = _PDF_CONTROL.get()
    cancel = control[0] if control else threading.Event()
    expires = control[1] if control else time.monotonic() + 20
    process = None
    try:
        process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-m", "probe.source_documents", str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        while True:
            if cancel.is_set() or time.monotonic() >= expires:
                raise ControlError("PDF_EXTRACTION_TIMEOUT")
            try:
                stdout, _ = process.communicate(timeout=min(.1, max(.001, expires - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process:
            try:
                terminate(process)
            except ControlError as exc:
                exc.child_pid = process.pid
                raise
    if len(stdout) > MAX_TEXT_BYTES * 7 + 50000:
        raise ControlError("PDF_TEXT_TOO_LARGE")
    try:
        value = json.loads(stdout)
    except (ValueError, UnicodeError):
        raise ControlError("PDF_EXTRACTION_FAILED") from None
    if process.returncode or value.get("error"):
        raise ControlError(value.get("error", "PDF_EXTRACTION_FAILED"))
    return value


async def _extract_document(path, policy):
    cancel = threading.Event()
    expires = time.monotonic() + 20
    run = policy.store.run(policy.rid)
    if run.get("started_at") and run["status"] in {"STARTING", "RUNNING", "RESUMING", "PAUSE_REQUESTED", "STOP_REQUESTED"}:
        from .control_runtime import snapshot_remaining
        expires = min(expires, time.monotonic() + max(0, snapshot_remaining(policy.snapshot, run["started_at"])))
    token = _PDF_CONTROL.set((cancel, expires))
    task = asyncio.create_task(asyncio.to_thread(extract_pdf, path))
    try:
        while not task.done():
            if policy.before_dispatch:
                policy.before_dispatch()
            if time.monotonic() >= expires:
                raise ControlError("PDF_EXTRACTION_TIMEOUT")
            await asyncio.wait({task}, timeout=.05)
        return task.result()
    except BaseException:
        cancel.set()
        # 자식 종료 확인 전에는 파서 자원을 반환하지 않는다.
        try:
            await asyncio.shield(task)
        except ControlError as exc:
            if exc.code == "ANALYSIS_TERMINATION_FAILED":
                from .database import transaction
                from uuid import uuid4
                with transaction(policy.store.db):
                    policy.store.db.execute("INSERT INTO control_configs VALUES('analysis_process',?,1,?)",
                        (uuid4().hex,to_json({'pid':getattr(exc,'child_pid',os.getpid()),'owner_pid':os.getpid(),
                            'research_id':policy.rid,'status':'TERMINATION_FAILED'})))
                raise
        raise
    finally:
        _PDF_CONTROL.reset(token)


def identity_matches(source, extracted):
    from .literature import _terms
    title = _terms(source.title)
    header = extracted.get("metadata_title", "") + " " + " ".join(p["text"][:2000] for p in extracted["pages"][:2])
    hits = title & _terms(header)
    return len(hits) >= min(2, len(title)) and len(hits) / max(1, len(title)) >= .6


async def acquire_document(state, policy, source_id, *, client=None):
    from .release import _secret_free
    rid = policy.rid
    source = source_from_row(state._one("SELECT * FROM sources WHERE research_id=? AND source_id=?", (rid, source_id)))
    previous = document_record(state, rid, source_id)
    if previous and (previous["status"] == "READY" or previous["status"] == "FAILED" and (
            not previous.get("retryable") or utc_now().isoformat() < previous.get("retry_at", "")
            or previous.get("extraction_attempts", 0) >= 3)):
        if previous.get("pdf"):
            checked_document(state, rid, source_id, require_text=previous["status"] == "READY")
        return previous
    if not policy.snapshot.get("fulltext_enabled"):
        return None
    count = state._db.execute("SELECT COUNT(*) FROM control_configs WHERE kind='source_document' AND json_extract(payload,'$.research_id')=?", (rid,)).fetchone()[0]
    if not previous and count >= 3:
        return None
    record = previous or {"research_id": rid, "source_id": source_id, "source_metadata_hash": source.metadata_hash,
                          "status": "RUNNING", "retrieved_at": utc_now().isoformat()}
    client = client or PublicDocumentClient()
    try:
        if not record.get("pdf"):
            url = source.provider_ids.get("oa_pdf_url")
            headers = None
            if not url and policy.snapshot.get("openalex_archive_enabled") and source.openalex_id:
                url, headers = await policy.archive_access(source.openalex_id, client)
            if not url:
                return None
            policy.approve_document_url(source, url)
            data, final_url = await policy.fetch_document(url, client, headers=headers)
            if not data.startswith(b"%PDF-") or not _secret_free("source.pdf", data):
                raise ControlError("PDF_INVALID_OR_SECRET")
            record.update(status="DOWNLOADED", url=url, final_url=final_url)
            record = state.save_source_document(rid, source_id, record, pdf=data)
        path = state.workspace.path(rid, record["pdf"]["relative_path"])
        if sha256_file(path) != record["pdf"]["sha256"]:
            raise ControlError("SOURCE_DOCUMENT_CHANGED")
        from .resource_queue import ResourcePool
        async with ResourcePool(policy.store).lease(rid, 'search', 'source-pdf-parser', capacity=1, timeout=30):
            if policy.before_dispatch:
                policy.before_dispatch()
            record["extraction_attempts"] = record.get("extraction_attempts", 0) + 1
            record = state.save_source_document(rid, source_id, record)
            extracted = await _extract_document(path, policy)
        if not identity_matches(source, extracted):
            raise ControlError("SOURCE_DOCUMENT_IDENTITY_UNVERIFIED")
        text = to_json(extracted).encode("utf-8", errors="strict")
        if not _secret_free("source.json", text) or any(secret in text.decode("utf-8") for secret in policy.protected()):
            raise ControlError("SECRET_IN_SOURCE_DOCUMENT")
        record.update(status="READY", extractor_version=EXTRACTOR_VERSION, truncated=extracted["truncated"],
                      total_pages=extracted["total_pages"], extracted_pages=len(extracted["pages"]), retryable=False, retry_at=None)
        record = state.save_source_document(rid, source_id, record, text=text)
        state.runtime_event(rid, "SOURCE_DOCUMENT_READY", {"source_id": source_id, "pages": record["extracted_pages"], "truncated": record["truncated"]})
    except (ControlError, ScholarlyError, ValueError) as exc:
        from datetime import timedelta
        error = getattr(exc, "code", "PDF_EXTRACTION_FAILED")
        retryable = error in {"DOCUMENT_TIMEOUT", "SEARCH_RATE_LIMITED", "PDF_EXTRACTION_TIMEOUT"} or getattr(exc, "status_code", 0) in {500, 502, 503, 504}
        record.update(status="FAILED", error=error, retryable=retryable,
                      retry_at=(utc_now() + timedelta(seconds=30)).isoformat() if retryable else None)
        record = state.save_source_document(rid, source_id, record)
        state.runtime_event(rid, "SOURCE_DOCUMENT_LIMITED", {"source_id": source_id, "code": record["error"]})
    return record


if __name__ == "__main__":
    try:
        output = _extract_file(sys.argv[1])
    except ModuleNotFoundError:
        output = {"error": "PDF_PARSER_UNAVAILABLE"}
    except Exception as exc:
        output = {"error": exc.code if isinstance(exc, ControlError) else "PDF_EXTRACTION_FAILED"}
    text = to_json(output)
    text.encode("utf-8", errors="strict")
    print(text)
