"""프로세스 인증 값의 해시만 보관하는 비밀 검사."""
from __future__ import annotations

from hashlib import sha256
import re
import threading
import weakref


_LOCK = threading.RLock()
_REGISTRY = weakref.WeakSet()


class MemorySecrets:
    def __init__(self):
        self.digests = set()
        with _LOCK:
            _REGISTRY.add(self)

    def remember(self, value):
        digest = sha256(value.encode("ascii")).hexdigest()
        with _LOCK:
            self.digests.add((len(value), digest))
        return digest

    def clear(self):
        with _LOCK:
            self.digests.clear()


def _spans(value):
    text = value.decode("latin1") if isinstance(value, bytes) else value
    with _LOCK:
        digests = set().union(*(entry.digests for entry in _REGISTRY))
    for length in {length for length, _ in digests}:
        for match in re.finditer(r"(?=([A-Za-z0-9_-]{" + str(length) + r"}))", text):
            if (length, sha256(match[1].encode("ascii")).hexdigest()) in digests:
                yield match.start(), match.start() + length


def contains_auth_material(value):
    return next(_spans(value), None) is not None


def redact_auth_material(value):
    spans = sorted(set(_spans(value)))
    if not spans:
        return value
    output, cursor = [], 0
    for start, end in spans:
        if start < cursor:
            continue
        output.extend((value[cursor:start], "[인증 비밀 제거됨]"))
        cursor = end
    return "".join(output) + value[cursor:]
