"""연구 범위 안의 경로 해석과 해시 계산."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


class UnsafeWorkspacePathError(Exception):
    """경로가 해당 연구의 작업 공간을 벗어난다."""


class DatasetIntegrityError(Exception):
    """데이터가 등록된 해시와 일치하지 않는다."""


class ArtifactIntegrityError(Exception):
    """산출물이 등록된 해시와 일치하지 않는다."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> str:
    from .database import to_json
    content = (to_json(value) + "\n").encode("utf-8", errors="strict")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return sha256_bytes(content)


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def research_root(self, research_id: str) -> Path:
        if not re.fullmatch(r"R-[0-9a-f]{32}", research_id):
            raise UnsafeWorkspacePathError("invalid research ID")
        candidate = (self.root / research_id).resolve()
        if candidate.parent != self.root:
            raise UnsafeWorkspacePathError("research directory escaped workspace root")
        return candidate

    def path(self, research_id: str, relative: str) -> Path:
        base = self.research_root(research_id).resolve()
        candidate = (base / relative).resolve()
        if candidate == base or not candidate.is_relative_to(base):
            raise UnsafeWorkspacePathError(relative)
        return candidate

    def prepare(self, research_id: str) -> Path:
        base = self.research_root(research_id)
        for folder in ("inputs/datasets", "scripts", "results", "figures", "artifacts"):
            self.path(research_id, folder).mkdir(parents=True, exist_ok=True)
        return base
