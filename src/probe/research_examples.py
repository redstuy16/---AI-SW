"""로컬 탐구 예시를 실제 근거와 분리해 제한된 참고 문맥으로 읽는다."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .control_plane import ControlError
from .database import to_json
from .storage import sha256_file

VERSION = "reference-examples-v2"
SECTIONS = {"purpose": ("탐구 목적", "purpose"), "background": ("이론적 배경", "background"),
    "method": ("탐구 방법", "method"), "interpretation": ("결과 및 해석", "interpretation"),
    "conclusion": ("결론", "conclusion"), "hypothesis": ("가설", "hypothesis"),
    "variables": ("변인", "변수", "통제", "variable", "control"),
    "procedure": ("절차", "procedure"), "data_structure": ("자료", "데이터", "단위", "data", "record"),
    "figure_structure": ("그림", "그래프", "figure", "chart", "visual")}



def _safe(path, root=None):
    path = Path(path)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise ControlError("REFERENCE_PATH_BLOCKED")
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise ControlError("REFERENCE_PATH_BLOCKED")
        if candidate.exists() and getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400:
            raise ControlError("REFERENCE_PATH_BLOCKED")
    resolved = path.resolve()
    if root is not None and not resolved.is_relative_to(Path(root).resolve()):
        raise ControlError("REFERENCE_PATH_BLOCKED")
    return resolved


def _terms(text):
    words = {v for v in re.findall(r"[A-Za-z]+|[가-힣]{2,}", text.lower()) if v not in {"탐구", "실험", "연구", "가설", "변인", "절차", "자료", "결과", "비교", "방법"}}
    return words | {word[i:i + 2] for word in words if re.fullmatch(r"[가-힣]+", word) for i in range(len(word) - 1)}


def _write_cache(path, value):
    data = (to_json(value) + "\n").encode("utf-8", errors="strict")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_example(path, cache):
    size = path.stat().st_size
    if size <= 0 or size > (10 * 1024 * 1024 if path.suffix.lower() == ".pdf" else 1_000_000):
        raise ControlError("REFERENCE_SIZE_INVALID")
    digest = sha256_file(path)
    key = hashlib.sha256((digest + VERSION).encode("utf-8", errors="strict")).hexdigest()
    cached = cache / (key + ".json")
    value = None
    if cached.is_file():
        try:
            _safe(cached, cache)
            value = json.loads(cached.read_text(encoding="utf-8", errors="strict"))
            content_hash = hashlib.sha256(to_json(value["pages"]).encode("utf-8", errors="strict")).hexdigest()
            if value.get("sha256") != digest or value.get("version") != VERSION or value.get("text_sha256") != content_hash:
                value = None
        except (OSError, ValueError, KeyError, UnicodeError):
            value = None
    if value is None:
        if path.suffix.lower() == ".pdf":
            from .source_documents import extract_pdf
            if not path.read_bytes().startswith(b"%PDF-"):
                raise ControlError("REFERENCE_PDF_INVALID")
            extracted = extract_pdf(path)
            pages, truncated = extracted["pages"], extracted["truncated"]
            extractor = extracted["extractor_version"]
        else:
            text = path.read_text(encoding="utf-8-sig", errors="strict")
            if "\x00" in text:
                raise ControlError("REFERENCE_TEXT_INVALID")
            pages, truncated, extractor = [{"page": 1, "text": text}], False, "utf8-text-v1"
        if not any(v["text"].strip() for v in pages):
            raise ControlError("REFERENCE_TEXT_EMPTY")
        if sha256_file(path) != digest:
            raise ControlError("REFERENCE_FILE_CHANGED")
        value = {"sha256": digest, "version": VERSION, "extractor_version": extractor, "pages": pages,
            "truncated": truncated, "empty_pages": [v["page"] for v in pages if not v["text"].strip()],
            "text_sha256": hashlib.sha256(to_json(pages).encode("utf-8", errors="strict")).hexdigest()}
        from .release import _secret_free
        if not _secret_free("reference.json", to_json(value).encode("utf-8", errors="strict")):
            raise ControlError("REFERENCE_SECRET_BLOCKED")
        _write_cache(cached, value)
    return value


def _excerpt(document, query):
    terms, chunks = _terms(query), []
    for page in document["pages"]:
        lines = page["text"].splitlines()
        for index, line in enumerate(lines):
            if not line.strip() or re.match(r"^\s*#*\s*(?:실측 결과|측정 결과|observed results)\b", line, re.I):
                continue
            section = next((key for key, labels in SECTIONS.items() if any(label in line.lower() for label in labels)), None)
            following = "\n".join(lines[index:index + 4]).strip()[:420]
            overlap = len(terms & _terms(following))
            if section or overlap:
                chunks.append((section, overlap, {"page": page["page"], "line": index + 1, "text": following}))
    selected = []
    for key in SECTIONS:
        candidates = [v for v in chunks if v[0] == key]
        if candidates:
            selected.append({"section": key, **max(candidates, key=lambda v: v[1])[2]})
    if not selected:
        selected = [{"section": "structure", **v[2]} for v in sorted(chunks, key=lambda v: -v[1])[:3]]
    return selected


def freeze_reference_examples(state, store, rid, snapshot, *, examples_dir=None):
    """연구별 참고 발췌를 최초 한 번만 고정하며 원본은 변경하지 않는다."""
    row = store.db.execute("SELECT payload FROM control_configs WHERE kind='reference_examples' AND id=?", (rid,)).fetchone()
    if row:
        return json.loads(row[0])
    root = examples_dir or snapshot.get("reference_examples_dir") or state.workspace.root.parent / "reference_examples"
    if not examples_dir and not snapshot.get("reference_examples_dir") and not Path(root).exists():
        root = Path(__file__).with_name("report_examples")
    value = {"purpose": "STRUCTURE_REFERENCE_ONLY", "version": VERSION, "examples": [], "issues": []}
    try:
        root = _safe(root)
        if root.is_dir():
            cache = _safe(state.workspace.root / ".reference_examples_cache")
            cache.mkdir(parents=True, exist_ok=True)
            query = snapshot.get("question", "") + " " + to_json((snapshot.get("detailed_design") or {}).get("variables", []))
            query_terms, candidates = _terms(query), []
            for path in sorted(root.iterdir(), key=lambda v: v.name.lower())[:100]:
                if path.suffix.lower() not in {".txt", ".md", ".pdf"}:
                    continue
                try:
                    path = _safe(path, root)
                    if not path.is_file():
                        continue
                    document = _read_example(path, cache)
                    content = "\n".join(v["text"] for v in document["pages"])
                    overlap = query_terms & _terms(path.stem + " " + content)
                    if not overlap:
                        continue
                    score = len(overlap) + 2 * len(query_terms & _terms(path.stem))
                    candidates.append((score, path.name, document))
                except (ControlError, OSError, UnicodeError, ValueError, ImportError) as exc:
                    value["issues"].append({"name": path.name, "code": getattr(exc, "code", None) or "REFERENCE_READ_FAILED"})
            seen = set()
            for _, name, document in sorted(candidates, key=lambda v: (-v[0], v[1])):
                if document["sha256"] in seen:
                    continue
                seen.add(document["sha256"])
                excerpts = _excerpt(document, query)
                if not excerpts:
                    continue
                value["examples"].append({"example_id": "EXAMPLE-" + document["sha256"][:16], "title": Path(name).stem,
                    "sha256": document["sha256"], "text_only": True, "truncated": document["truncated"],
                    "empty_pages": document["empty_pages"], "excerpts": excerpts})
                if len(value["examples"]) == 3:
                    break
    except (ControlError, OSError) as exc:
        value["issues"].append({"code": getattr(exc, "code", None) or "REFERENCE_DIRECTORY_UNAVAILABLE"})
    value["fingerprint"] = hashlib.sha256(to_json(value).encode("utf-8", errors="strict")).hexdigest()
    from .research_report import _save
    _save(store, "reference_examples", rid, value)
    state.runtime_event(rid, "REFERENCE_EXAMPLES_FROZEN", {"count": len(value["examples"]), "issues": value["issues"], "fingerprint": value["fingerprint"]})
    return value


def reference_examples_context(state, rid):
    if not state._db.execute("SELECT 1 FROM sqlite_master WHERE name='control_configs'").fetchone():
        return None
    row = state._db.execute("SELECT payload FROM control_configs WHERE kind='reference_examples' AND id=?", (rid,)).fetchone()
    if not row:
        return None
    value = json.loads(row[0])
    return {key: value[key] for key in ("purpose", "version", "fingerprint", "examples")} if value.get("examples") else None
