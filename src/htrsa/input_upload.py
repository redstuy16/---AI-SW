"""소유자가 선택한 CSV를 입력 경계 안에 복사한다."""
from __future__ import annotations

import base64
import binascii
import csv
from hashlib import sha256
import io
from pathlib import Path
import re

from .control_plane import ControlError, safe_source

MAX_BYTES = 5_000_000


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
