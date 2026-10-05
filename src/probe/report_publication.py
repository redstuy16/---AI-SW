"""불변 보고서 수정본과 작은 현재 포인터의 원자적 발행 경계."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import re
from uuid import uuid4

from .control_plane import ControlError
from .database import to_json
from .storage import sha256_file

_SELECTED = ContextVar("report_revision", default=None)


@contextmanager
def selected_revision(state, rid, root=None):
    root = root or report_root(state, rid)
    token = _SELECTED.set((rid, root))
    try:
        yield root
    finally:
        _SELECTED.reset(token)


def report_root(state, rid):
    selected = _SELECTED.get()
    if selected and selected[0] == rid:
        return selected[1]
    legacy = state.workspace.path(rid, "research_output")
    pointer = legacy / "current.json"
    if not pointer.exists():
        return legacy
    try:
        value = json.loads(pointer.read_text(encoding="utf-8", errors="strict"))
        if value.get("validation_version") != 2 or not re.fullmatch(r"[a-f0-9]{32}", value["revision"]):
            raise ValueError()
        root = state.workspace.path(rid, "report_revisions/" + value["revision"])
        if sha256_file(root / "manifests/artifact_manifest.json") != value["manifest_sha256"]:
            raise ValueError()
        return root
    except (OSError, ValueError, KeyError, TypeError):
        raise ControlError("REPORT_PUBLICATION_INVALID") from None


def new_revision(state, rid):
    root = state.workspace.path(rid, "report_revisions/" + uuid4().hex)
    root.mkdir(parents=True, exist_ok=False)
    if state._db.execute("SELECT 1 FROM sqlite_master WHERE name='control_configs'").fetchone():
        row = state._db.execute("SELECT payload FROM control_configs WHERE kind='report_rewrite' AND id=?", (rid,)).fetchone()
        if row:
            value = json.loads(row[0])
            if value.get("status") == "RUNNING":
                from .database import transaction
                with transaction(state._db):
                    state._db.execute("UPDATE control_configs SET revision=revision+1,payload=? WHERE kind='report_rewrite' AND id=?",
                        (to_json({**value, "candidate_revision": root.name}), rid))
    return root


def publish_revision(state, rid, root):
    from .release import validate_report_snapshot
    with selected_revision(state, rid, root):
        manifest = validate_report_snapshot(state, rid)
    pointer = state.workspace.path(rid, "research_output/current.json")
    pointer.parent.mkdir(parents=True, exist_ok=True)
    value = {"revision": root.name, "validation_version": 2,
             "manifest_sha256": sha256_file(root / "manifests/artifact_manifest.json")}
    encoded = to_json(value).encode("utf-8", errors="strict")
    temporary = pointer.with_name(".current-" + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        from .database import transaction
        with transaction(state._db):
            if state.state_version(rid) != manifest["state_version"]:
                raise ControlError("STALE_INPUT")
            os.replace(temporary, pointer)
    finally:
        temporary.unlink(missing_ok=True)
