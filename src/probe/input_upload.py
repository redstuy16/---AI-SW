"""소유자가 선택한 CSV를 입력 경계 안에 복사한다."""
from __future__ import annotations

import base64
import binascii
import csv
from hashlib import sha256
import io
from pathlib import Path
import re
import json
import os
import unicodedata
from datetime import datetime, timedelta

from .control_plane import ControlError, safe_source
from .database import to_json
from .schemas import new_id, utc_now

MAX_BYTES = 5_000_000
MAX_TOTAL_BYTES = 20_000_000
MAX_FILES = 10


class Attachments:
    """기존 운영 설정에 첨부 원본의 소유권·해시·수명만 기록한다."""

    def __init__(self, store, workspace, *, protected_values=()):
        self.store, self.workspace = store, Path(workspace)
        self.protected_values = tuple(v.encode("utf-8", errors="strict") for v in protected_values if v)

    def path(self, item, *, temporary=False):
        root = self.workspace / ("inputs" if item["extension"] == ".csv" else "attachments")
        if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in (root, *root.parents)):
            raise ControlError("SOURCE_PATH_UNSAFE")
        root.mkdir(parents=True, exist_ok=True)
        path = root / (item["attachment_id"] + (".csv" if item["extension"] == ".csv" else ".bin") + (".part" if temporary else ""))
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ControlError("SOURCE_PATH_UNSAFE")
        return path

    def public(self, item):
        return {k:item.get(k) for k in ("attachment_id","name","size_bytes","sha256","status","processing_error","rows","supported_parser","retained_for_provenance")}

    def begin(self, body):
        if set(body) - {"filename","size_bytes","draft_id","mime"}:
            raise ControlError("UPLOAD_INVALID")
        name, size, draft = body.get("filename"), body.get("size_bytes"), body.get("draft_id")
        if not isinstance(name, str) or not 1 <= len(name) <= 160 or any(ord(c) < 32 or c in '/\\:' for c in name) or name in {".",".."}:
            raise ControlError("UPLOAD_FILENAME_INVALID")
        name = unicodedata.normalize("NFC", name)
        name.encode("utf-8", errors="strict")
        if type(size) is not int or not 0 <= size <= MAX_BYTES:
            raise ControlError("UPLOAD_SIZE_OR_CONTENT")
        if not isinstance(draft, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", draft):
            raise ControlError("ATTACHMENT_DRAFT_REQUIRED")
        identity = new_id("ATT")
        item = {"attachment_id":identity, "draft_id":draft, "name":name,
            "size_bytes":size, "extension":Path(name).suffix.lower(), "status":"RESERVED",
            "sha256":None, "supported_parser":None, "processing_error":None,
            "referenced_by":[], "created_at":utc_now().isoformat()}
        with self.store.transaction():
            rows = [json.loads(r[0]) for r in self.store.db.execute("SELECT payload FROM control_configs WHERE kind='attachment' AND json_extract(payload,'$.status')!='DELETED'")]
            active = [r for r in rows if r["draft_id"] == draft and r["status"] not in {"DELETED","RETAINED"}]
            if len(active) >= MAX_FILES or sum(r["size_bytes"] for r in active) + size > MAX_TOTAL_BYTES:
                raise ControlError("UPLOAD_BATCH_LIMIT")
            if sum(r["size_bytes"] for r in rows if r["status"] != "DELETED") + size > 100_000_000:
                raise ControlError("UPLOAD_STORAGE_LIMIT")
            if sum(r["status"] != "DELETED" for r in rows) >= 500:
                raise ControlError("UPLOAD_STORAGE_LIMIT")
            self.store.db.execute("INSERT INTO control_configs VALUES('attachment',?,1,?)", (identity,to_json(item)))
        return self.public(item)

    def get(self, identity, draft=None):
        if not isinstance(identity, str) or not re.fullmatch(r"ATT-[a-f0-9]{32}", identity):
            raise ControlError("ATTACHMENT_NOT_FOUND")
        try:
            item = self.store.config("attachment", identity)
        except ControlError:
            raise ControlError("ATTACHMENT_NOT_FOUND") from None
        if draft is not None and item["draft_id"] != draft:
            raise ControlError("ATTACHMENT_OWNER_MISMATCH")
        return item

    def update(self, item):
        self.store.db.execute("UPDATE control_configs SET revision=revision+1,payload=? WHERE kind='attachment' AND id=?",
                              (to_json(item), item["attachment_id"]))

    def receive(self, identity, stream, length):
        item = self.get(identity)
        if item["status"] != "RESERVED" or length != item["size_bytes"] or not 0 <= length <= MAX_BYTES:
            raise ControlError("UPLOAD_STATE_INVALID")
        item["status"] = "UPLOADING"
        self.update(item)
        temporary, target = self.path(item, temporary=True), self.path(item)
        digest, remaining, carry, head = sha256(), length, b"", b""
        try:
            with temporary.open("xb") as output:
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        raise ControlError("UPLOAD_TRUNCATED")
                    if len(chunk) > remaining:
                        raise ControlError("UPLOAD_SIZE_OR_CONTENT")
                    if self.get(identity)["status"] == "DELETED":
                        raise ControlError("UPLOAD_CANCELLED")
                    remaining -= len(chunk)
                    scan = carry + chunk
                    if any(value in scan for value in self.protected_values) or re.search(
                            rb"(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{35}|ghp_[A-Za-z0-9]{36}|OPENAI_API_KEY)", scan):
                        raise ControlError("SECRET_IN_SOURCE")
                    carry = scan[-8192:]
                    if len(head) < 2048:
                        head = (head + chunk)[:2048]
                    output.write(chunk);digest.update(chunk)
            if item["extension"] in {".html",".htm",".svg",".exe",".dll",".bat",".cmd",".ps1",".lnk"} or head.startswith((b"MZ",b"\x7fELF")) or re.search(rb"(?is)^\s*(?:<\?xml[^>]*>\s*)?(?:<!doctype\s+html|<html\b|<svg\b)", head):
                raise ControlError("UPLOAD_ACTIVE_CONTENT_BLOCKED")
            with self.store.transaction():
                current = self.get(identity)
                if current["status"] != "UPLOADING":
                    raise ControlError("UPLOAD_CANCELLED")
                os.replace(temporary, target)
                item.update(status="ATTACHED", sha256=digest.hexdigest())
                self.update(item)
            self.parse(item)
            return self.public(item)
        except (ControlError, OSError) as exc:
            temporary.unlink(missing_ok=True)
            current = self.get(identity)
            if current["status"] != "DELETED":
                target.unlink(missing_ok=True)
                current.update(status="BLOCKED" if isinstance(exc, ControlError) and exc.code in {"SECRET_IN_SOURCE","UPLOAD_ACTIVE_CONTENT_BLOCKED"} else "FAILED",
                               processing_error=exc.code if isinstance(exc, ControlError) else "UPLOAD_STORAGE_FAILED")
                self.update(current)
            raise

    def parse(self, item):
        parser = item["extension"]
        if parser not in {".csv",".txt",".md",".json"} or item["size_bytes"] > 1_000_000 and parser != ".csv":
            item.update(status="OPAQUE", supported_parser=None)
        else:
            try:
                text = self.path(item).read_text(encoding="utf-8-sig", errors="strict")
                if "\x00" in text:
                    raise ValueError()
                if parser == ".csv":
                    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
                    header = next(reader)
                    if not 2 <= len(header) <= 200 or len(set(header)) != len(header) or any(not c.strip() for c in header):
                        raise ValueError()
                    count = 0
                    for row in reader:
                        if len(row) != len(header):
                            raise ValueError()
                        count += 1
                    if not count:
                        raise ValueError()
                    item["rows"] = count
                elif parser == ".json":
                    value = json.loads(text)
                    if not isinstance(value, (dict,list)):
                        raise ValueError()
                item.update(status="READ", supported_parser=parser[1:])
            except (ValueError, UnicodeError, StopIteration, csv.Error, RecursionError):
                item.update(status="FAILED", processing_error="UPLOAD_PARSE_FAILED")
        with self.store.transaction():
            if self.get(item["attachment_id"])["status"] != "ATTACHED":
                raise ControlError("UPLOAD_CANCELLED")
            self.update(item)

    def delete(self, identity, draft):
        if not draft:
            raise ControlError("ATTACHMENT_DRAFT_REQUIRED")
        failed = False
        with self.store.transaction():
            item = self.get(identity, draft)
            if item["referenced_by"]:
                item.update(status="RETAINED", retained_for_provenance=True)
                self.update(item)
                return {**self.public(item), "bytes_deleted":False, "removed_from_draft":True}
            try:
                self.path(item).unlink(missing_ok=True)
                temporary = self.path(item, temporary=True)
                try:
                    temporary.unlink(missing_ok=True)
                except PermissionError:
                    if item["status"] != "UPLOADING":
                        raise
                    item.update(status="DELETED")
                    self.update(item)
                    return {**self.public(item), "bytes_deleted":False, "cancellation_requested":True, "removed_from_draft":True}
            except OSError:
                item.update(status="DELETE_FAILED", processing_error="UPLOAD_DELETE_FAILED")
                self.update(item)
                failed = True
            if not failed:
                item.update(status="DELETED")
                self.update(item)
        if failed:
            raise ControlError("UPLOAD_DELETE_FAILED")
        return {**self.public(item), "bytes_deleted":True, "removed_from_draft":True}

    def validate(self, identities, draft, analysis=None):
        if identities and not draft:
            raise ControlError("ATTACHMENT_DRAFT_REQUIRED")
        items = [self.get(identity, draft) for identity in identities]
        for item in items:
            if item["status"] not in {"READ","ATTACHED","OPAQUE","RETAINED"}:
                raise ControlError("ATTACHMENT_NOT_READY")
            from .storage import sha256_file
            if sha256_file(self.path(item)) != item["sha256"]:
                raise ControlError("SOURCE_HASH_MISMATCH")
        if analysis is not None:
            selected = next((i for i in items if i["attachment_id"] == analysis and i["supported_parser"] == "csv"), None)
            if selected is None:
                raise ControlError("ANALYSIS_ATTACHMENT_INVALID")
        else:
            tables = [i for i in items if i["supported_parser"] == "csv"]
            selected = tables[0] if len(tables) == 1 else None
        return items, selected["attachment_id"] + ".csv" if selected else None

    def reference(self, identities, rid):
        with self.store.transaction():
            for identity in identities:
                item = self.get(identity)
                if rid not in item["referenced_by"]:
                    item["referenced_by"].append(rid)
                    self.update(item)

    def read_content(self, identity, draft):
        """안전하게 읽은 자료만 한 파일씩 제한된 크기로 추출한다."""
        item = self.get(identity, draft)
        self.validate([identity], draft)
        if item["status"] not in {"READ", "RETAINED"} or not item["supported_parser"]:
            raise ControlError("ATTACHMENT_PARSER_UNAVAILABLE")
        text = self.path(item).read_text(encoding="utf-8-sig", errors="strict")
        parser = item["supported_parser"]
        if parser == "csv":
            reader = csv.reader(io.StringIO(text, newline=""), strict=True)
            headers = next(reader)
            from itertools import islice
            return {"parser":parser, "headers":headers, "preview_rows":list(islice(reader,100)),
                    "row_count":item["rows"], "preview_truncated":item["rows"] > 100}
        if parser == "json":
            return {"parser":parser, "value":json.loads(text)}
        return {"parser":parser, "text":text}

    def cleanup(self):
        expired = utc_now() - timedelta(days=1)
        cleaned = 0
        rows = self.store.db.execute("SELECT payload FROM control_configs WHERE kind='attachment' AND json_extract(payload,'$.status') IN ('RESERVED','UPLOADING','FAILED') ORDER BY json_extract(payload,'$.created_at') LIMIT 100")
        for record in rows:
            row = json.loads(record[0])
            if not row["referenced_by"] and row["status"] in {"RESERVED","UPLOADING","FAILED"} and datetime.fromisoformat(row["created_at"]) < expired:
                self.delete(row["attachment_id"], row["draft_id"]);cleaned += 1
        return cleaned


def upload_csv(root: Path, body: dict, *, protected_values=()):
    if set(body) != {"filename", "content_base64"}:
        raise ControlError("UPLOAD_INVALID")
    name, encoded = body["filename"], body["content_base64"]
    if (not isinstance(name, str) or not 1 <= len(name) <= 160
            or any(c in name for c in '/\\:') or not name.lower().endswith('.csv')
            or any(ord(c) < 32 for c in name) or not isinstance(encoded, str)
            or len(encoded) > 4 * ((MAX_BYTES + 2) // 3)):
        raise ControlError("UPLOAD_INVALID")
    try:
        data = base64.b64decode(encoded, validate=True)
        text = data.decode('utf-8-sig', errors='strict')
    except (ValueError, UnicodeError, binascii.Error):
        raise ControlError("UPLOAD_UTF8_REQUIRED") from None
    if not data or len(data) > MAX_BYTES or '\x00' in text:
        raise ControlError("UPLOAD_SIZE_OR_CONTENT")
    if any(v and v in text for v in protected_values) or re.search(
            r'(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{35}|ghp_[A-Za-z0-9]{36}|OPENAI_API_KEY)', text):
        raise ControlError("SECRET_IN_SOURCE")
    try:
        rows = csv.reader(io.StringIO(text, newline=''), strict=True)
        header = next(rows)
        if not 2 <= len(header) <= 200 or len(set(header)) != len(header) or any(not c.strip() for c in header):
            raise ValueError()
        count = 0
        for row in rows:
            if len(row) != len(header):
                raise ValueError()
            count += 1
        if not count:
            raise ValueError()
    except (ValueError, StopIteration, csv.Error):
        raise ControlError("UPLOAD_CSV_INVALID") from None
    if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in (root, *root.parents)):
        raise ControlError("SOURCE_PATH_UNSAFE")
    root.mkdir(parents=True, exist_ok=True)
    relative = 'upload-' + sha256(data).hexdigest() + '.csv'
    target = root / relative
    if target.exists():
        if safe_source(root, relative).read_bytes() != data:
            raise ControlError("SOURCE_HASH_MISMATCH")
    else:
        with target.open('xb') as stream:
            stream.write(data)
    safe_source(root, relative)
    return {"name": name, "source_relative": relative, "size_bytes": len(data),
            "sha256": sha256(data).hexdigest(), "rows": count, "paid_calls": 0}
