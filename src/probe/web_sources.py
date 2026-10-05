"""공개 HTML 원문을 실행하지 않고 문단과 원본 해시로 검증한다."""
from __future__ import annotations

from hashlib import sha256
from html.parser import HTMLParser
import json
import re
import time

from .control_plane import ControlError
from .database import to_json
from .schemas import new_id, utc_now
from .scholarly import metadata_digest, source_from_row
from .source_documents import MAX_TEXT_BYTES, validate_public_url

MAX_HTML_BYTES = 2_000_000
MAX_PARAGRAPHS = 2000
EXTRACTOR_VERSION = "html-paragraphs-v1"


class _Paragraphs(HTMLParser):
    """실행 요소·숨겨진 요소·사이트 탐색을 제외한 순서 있는 문단."""
    excluded = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "button", "iframe", "template"}
    blocks = {"p", "div", "article", "section", "main", "li", "h1", "h2", "h3", "h4", "blockquote", "tr", "br"}
    void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.parts, self.paragraphs, self.title_parts = [], [], [], []
        self.size, self.tags = 0, 0
        self.deadline = time.monotonic() + 2
        self.in_title = self.in_head = 0

    def check_time(self):
        if time.monotonic() > self.deadline:
            raise ControlError("WEB_SOURCE_PARSE_TIMEOUT")

    def flush(self):
        self.check_time()
        text = " ".join("".join(self.parts).split())
        self.parts = []
        if text:
            self.size += len(text.encode("utf-8", errors="strict"))
            if self.size > MAX_TEXT_BYTES or len(self.paragraphs) >= MAX_PARAGRAPHS:
                raise ControlError("WEB_SOURCE_TEXT_LIMIT")
            self.paragraphs.append({"paragraph": len(self.paragraphs) + 1, "text": text})

    def handle_starttag(self, tag, attrs):
        self.check_time()
        self.tags += 1
        if self.tags > 200000:
            raise ControlError("WEB_SOURCE_TEXT_LIMIT")
        if tag in self.blocks: self.flush()
        attrs = dict(attrs)
        hidden = (bool(self.stack and self.stack[-1][1]) or tag in self.excluded or "hidden" in attrs
                  or attrs.get("aria-hidden") == "true" or bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", attrs.get("style") or "", re.I)))
        if tag not in self.void:
            if len(self.stack) >= 128:
                raise ControlError("WEB_SOURCE_DEPTH_LIMIT")
            self.stack.append((tag, hidden))
            self.in_title += tag == "title"
            self.in_head += tag == "head"

    def handle_endtag(self, tag):
        self.check_time()
        if tag in self.blocks: self.flush()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                for removed, _ in self.stack[index:]:
                    self.in_title -= removed == "title"
                    self.in_head -= removed == "head"
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.check_time()
        if self.stack and self.stack[-1][1]: return
        if self.in_title:
            self.title_parts.append(data)
        elif not self.in_head:
            self.parts.append(data)


def extract_html(data: bytes, content_type=""):
    if not data or len(data) > MAX_HTML_BYTES:
        raise ControlError("WEB_SOURCE_SIZE_LIMIT")
    if content_type and not any(kind in content_type.casefold() for kind in ("text/html", "application/xhtml+xml", "text/plain")):
        raise ControlError("WEB_SOURCE_FORMAT_UNSUPPORTED")
    charset = re.search(rb"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", data[:4096])
    declared = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", content_type, re.I)
    encoding = declared.group(1) if declared else charset.group(1).decode("ascii") if charset else "utf-8"
    if encoding.casefold() not in {"utf-8", "utf8", "ascii", "iso-8859-1", "windows-1252", "euc-kr", "cp949"}:
        raise ControlError("WEB_SOURCE_ENCODING_UNSUPPORTED")
    try:
        text = data.decode(encoding, errors="strict")
        text.encode("utf-8", errors="strict")
        parser = _Paragraphs()
        if "text/plain" in content_type:
            for paragraph in re.split(r"\n\s*\n", text):
                parser.parts = [paragraph]
                parser.flush()
        else:
            if not re.search(r"<(?:html|body|article|p|div|!doctype)\b", text, re.I):
                raise ControlError("WEB_SOURCE_FORMAT_UNSUPPORTED")
            for offset in range(0, len(text), 64 * 1024):
                parser.check_time()
                parser.feed(text[offset:offset + 64 * 1024])
            parser.close()
            parser.flush()
        parser.check_time()
        if not parser.paragraphs: raise ControlError("WEB_SOURCE_EMPTY")
        return {"extractor_version": EXTRACTOR_VERSION, "title": " ".join("".join(parser.title_parts).split()), "paragraphs": parser.paragraphs}
    except (UnicodeError, LookupError):
        raise ControlError("WEB_SOURCE_ENCODING_INVALID") from None


def ensure_source_contract(state, rid):
    contract = state._db.execute("SELECT contract_id FROM contracts WHERE research_id=? ORDER BY rowid DESC LIMIT 1", (rid,)).fetchone()
    if contract is None:
        from .schemas import ResearchContract
        identity = new_id("C")
        state.issue_contract(ResearchContract(contract_id=identity, research_id=rid, task_type="public_source_fetch",
            issued_by="system", assigned_role="manager", objective="공개 과학 원문의 해시와 문단 위치 보존",
            allowed_tools=["public_html.fetch", "public_pdf.fetch"], output_schema_id="PublicSourceDocument"), runtime_key="ai-web-source-acquisition")
        return identity
    return contract[0]


def save_web_document(state, rid, source_id, *, data, parsed, url, retrieved_at=None):
    """원문과 추출 텍스트를 같은 상태 변경에 등록한다."""
    validate_public_url(url)
    source = state._one("SELECT * FROM sources WHERE research_id=? AND source_id=?", (rid, source_id))
    if source["status"] == "INVALIDATED" or source["metadata_hash"] != metadata_digest(source_from_row(source)):
        raise ControlError("WEB_SOURCE_STALE")
    # 파싱 결과를 호출자가 조작해 다른 내용을 원문으로 등록하지 못하게 재검사한다.
    again = extract_html(data, parsed.get("content_type", ""))
    if any(again[key] != parsed[key] for key in ("extractor_version", "title", "paragraphs")):
        raise ControlError("WEB_SOURCE_EXTRACTION_MISMATCH")
    payload = to_json(parsed).encode("utf-8", errors="strict")
    contract_id = ensure_source_contract(state, rid)
    record = {"research_id": rid, "source_id": source_id, "source_metadata_hash": source["metadata_hash"],
              "status": "READY", "url": url, "retrieved_at": retrieved_at or utc_now().isoformat(),
              "extractor_version": EXTRACTOR_VERSION, "paragraph_count": len(parsed["paragraphs"])}
    artifacts = []
    for key, content, kind, suffix in (("html", data, "SOURCE_HTML", "html"), ("text", payload, "SOURCE_WEB_TEXT", "json")):
        digest, identity = sha256(content).hexdigest(), new_id("ART")
        relative = "artifacts/web_sources/" + source_id + "-" + digest[:16] + "." + suffix
        path = state.workspace.path(rid, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        record[key] = {"artifact_id": identity, "relative_path": relative, "sha256": digest}
        artifacts.append((identity, kind, relative, digest, len(content)))
    def write():
        old = state._db.execute("SELECT revision FROM control_configs WHERE kind='web_source_document' AND id=?", (source_id,)).fetchone()
        for identity, kind, relative, digest, size in artifacts:
            state._db.execute("INSERT INTO artifacts(artifact_id,research_id,contract_id,kind,payload_json,producer_type,producer_id,artifact_type,relative_path,sha256,size_bytes,created_at,status) VALUES(?,?,?,'file',?,'system',?,?,?,?,?,?,'VERIFIED')",
                (identity, rid, contract_id, to_json({"source_id": source_id}), source_id, kind, relative, digest, size, utc_now().isoformat()))
        state._db.execute("INSERT OR REPLACE INTO control_configs VALUES('web_source_document',?,?,?)", (source_id, (old[0] if old else 0) + 1, to_json(record)))
    state._planning_commit(rid, "WEB_SOURCE_DOCUMENT_SAVED", "source", source_id, {"url": url, "html": record["html"], "text": record["text"]}, write)
    return record


def checked_web_document(state, rid, source_id):
    source = state._one("SELECT * FROM sources WHERE research_id=? AND source_id=?", (rid, source_id))
    row = state._db.execute("SELECT payload FROM control_configs WHERE kind='web_source_document' AND id=?", (source_id,)).fetchone()
    record = json.loads(row[0]) if row else None
    if (not record or record.get("research_id") != rid or record.get("source_id") != source_id or record.get("status") != "READY"
            or source["status"] == "INVALIDATED" or record["source_metadata_hash"] != source["metadata_hash"]
            or source["metadata_hash"] != metadata_digest(source_from_row(source)) or record["extractor_version"] != EXTRACTOR_VERSION):
        raise ControlError("WEB_SOURCE_STALE")
    contents = {}
    for key, kind in (("html", "SOURCE_HTML"), ("text", "SOURCE_WEB_TEXT")):
        item = record[key]
        artifact = state.file_artifact(item["artifact_id"], rid)
        if (artifact["status"] != "VERIFIED" or artifact["artifact_type"] != kind
                or artifact["relative_path"] != item["relative_path"] or artifact["sha256"] != item["sha256"]):
            raise ControlError("WEB_SOURCE_CHANGED")
        contents[key] = state.workspace.path(rid, item["relative_path"]).read_bytes()
        if sha256(contents[key]).hexdigest() != item["sha256"]: raise ControlError("WEB_SOURCE_CHANGED")
    parsed = json.loads(contents["text"].decode("utf-8", errors="strict"))
    original = extract_html(contents["html"], parsed.get("content_type", ""))
    if any(parsed[key] != original[key] for key in ("title", "paragraphs", "extractor_version")):
        raise ControlError("WEB_SOURCE_EXTRACTION_MISMATCH")
    return record, parsed["paragraphs"]


def webpage_span(state, rid, source_id, location, *, proof=None):
    record, paragraphs = checked_web_document(state, rid, source_id)
    match = re.fullmatch(r"webpage paragraph ([1-9][0-9]*)", location)
    if not match or int(match.group(1)) > len(paragraphs): raise ControlError("WEB_SOURCE_LOCATION_INVALID")
    number = int(match.group(1))
    span = paragraphs[number - 1]
    if span["paragraph"] != number: raise ControlError("WEB_SOURCE_LOCATION_INVALID")
    value = {"html_sha256": record["html"]["sha256"], "text_sha256": record["text"]["sha256"],
             "extractor_version": record["extractor_version"], "paragraph": number, "url": record["url"],
             "source_metadata_hash": record["source_metadata_hash"]}
    if proof is not None and proof != value: raise ControlError("WEB_SOURCE_PROOF_CHANGED")
    return span["text"], value
