"""고정 예시를 별도 작업 공간에 준비하며 실제 연구·비용 자료를 사용하지 않는다."""
from pathlib import Path
import json

from .database import initialize, to_json
from .release import validate_report_snapshot
from .service import StateService
from .storage import Workspace


def prepare_example(database, workspace):
    database, workspace = Path(database).resolve(), Path(workspace).resolve()
    root = Path(__file__).resolve().parents[2]
    live = (root / "build/workbench/state.sqlite").resolve()
    if database == live or workspace == (root / "build/workbench/workspace").resolve():
        raise ValueError("제출 예시는 기존 연구 작업대와 다른 경로를 사용하세요.")
    database.parent.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    marker = database.parent / "submission-example.json"
    if marker.is_file() and database.is_file():
        cached = json.loads(marker.read_text(encoding="utf-8", errors="strict"))
        db = initialize(database)
        try:
            state = StateService(db, Workspace(workspace))
            manifest = validate_report_snapshot(state, cached["research_id"])
            if "report.pdf" not in manifest["files"]:
                raise ValueError("제출 예시의 PDF가 없습니다.")
            return cached
        finally:
            db.close()
    from .demo import run_demo_a
    result = run_demo_a(database, workspace, submission=True)
    db = initialize(database)
    try:
        manifest = validate_report_snapshot(StateService(db, Workspace(workspace)), result["research_id"])
        if "report.pdf" not in manifest["files"]:
            raise ValueError("제출 예시의 PDF를 검증하지 못했습니다.")
    finally:
        db.close()
    record = {"research_id": result["research_id"], "mode": "DEMO", "paid_calls": 0,
              "description": "고정 CSV와 모의 응답을 사용하는 제출 예시", "validation_version": 2}
    data = to_json(record).encode("utf-8", errors="strict")
    with marker.open("xb") as stream:
        stream.write(data)
    return record
