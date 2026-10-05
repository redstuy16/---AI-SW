"""과학 보고서의 객관적 렌더 검사를 기록하고 내용 평가는 사람 검토로 남긴다."""
from __future__ import annotations

import argparse
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from probe.database import to_json
from probe.release import _secret_free
from probe.report_pdf import render_pdf
from probe.report_ux import friendly_report
from probe.service import StateService
from probe.storage import Workspace


def _normalized(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def _safe_output(directory):
    target = Path(directory)
    target = target if target.is_absolute() else ROOT / target
    for candidate in (target, *target.parents):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise ValueError("VISUAL_QA_PATH_BLOCKED")
    target = target.resolve()
    if not target.is_relative_to(ROOT.resolve()):
        raise ValueError("VISUAL_QA_PATH_BLOCKED")
    return target


def _clean(name, data, protected_values=()):
    if not _secret_free(name, data):
        return False
    for value in protected_values:
        if value and (value.encode("utf-8", errors="strict") in data or json.dumps(value, ensure_ascii=True)[1:-1].encode("utf-8", errors="strict") in data):
            return False
    return True


def _write(path, data, protected_values=()):
    # 텍스트는 호출자가 먼저 UTF-8로 변환하고 모든 산출물은 비밀값을 검사한다.
    if not _clean(path.name, data, protected_values):
        raise ValueError("VISUAL_QA_SECRET_BLOCKED")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_json(path, value, protected_values=()):
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8", errors="strict")
    _write(path, data, protected_values)


def _activity(state, rid):
    db = state._db
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    value = {"state_version": state.state_version(rid), "connection_changes": db.total_changes}
    for table in ("agent_runs", "spend_ledger", "control_audit"):
        if table in tables:
            value[table] = db.execute("SELECT COUNT(*) FROM " + table + " WHERE research_id=?", (rid,)).fetchone()[0]
    return value


def _poppler():
    found = shutil.which("pdftoppm")
    if found:
        return Path(found)
    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/Library/bin/pdftoppm.exe"
    return bundled if bundled.is_file() else None


def _render_pages(pdf, directory, page_count):
    renderer = _poppler()
    if renderer is None:
        raise ValueError("VISUAL_QA_POPPLER_UNAVAILABLE")
    if page_count < 1 or page_count > 80:
        raise ValueError("VISUAL_QA_PAGE_LIMIT")
    # 페이지별 시간을 제한하고 이전 검사 파일과 섞이지 않는 이름을 사용한다.
    for index in range(1, page_count + 1):
        prefix = directory / ("page-" + str(index))
        result = subprocess.run([str(renderer), "-f", str(index), "-l", str(index), "-singlefile", "-r", "110", "-png", str(pdf), str(prefix)],
                                capture_output=True, timeout=60, check=False)
        if result.returncode != 0:
            raise ValueError("VISUAL_QA_RENDER_FAILED")
    return [directory / ("page-" + str(index) + ".png") for index in range(1, page_count + 1)]


def _pixel_details(path, protected_values=()):
    from PIL import Image
    data = path.read_bytes()
    if not _clean(path.name, data, protected_values):
        path.unlink(missing_ok=True)
        raise ValueError("VISUAL_QA_SECRET_BLOCKED")
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        # 쪽 번호만 찍힌 페이지도 공백 본문으로 구분하도록 바깥 여백을 제외한다.
        body = image.convert("L").crop((int(width * .05), int(height * .05), int(width * .95), int(height * .92)))
        histogram = body.histogram()
        dark = sum(histogram[:235])
        fraction = dark / (body.width * body.height) if body.width and body.height else 0
    return {"path": str(path), "sha256": sha256(data).hexdigest(), "width": width, "height": height,
            "body_dark_pixels": dark, "body_dark_fraction": fraction,
            "body_nonblank": width >= 100 and height >= 100 and dark >= 20 and fraction >= .00001}


def _image_draws(reader):
    def count(resources, stream, ancestors=()):
        if not resources or stream is None:
            return 0
        from pypdf.generic import ContentStream
        value = stream if isinstance(stream, ContentStream) else ContentStream(stream, reader)
        total = 0
        objects = resources.get("/XObject", {})
        for operands, operator in value.operations:
            if operator != b"Do" or not operands or operands[0] not in objects:
                continue
            ref = objects[operands[0]]
            object_value = ref.get_object()
            identity = (getattr(ref, "idnum", None), getattr(ref, "generation", None))
            if object_value.get("/Subtype") == "/Image":
                total += 1
            elif object_value.get("/Subtype") == "/Form" and identity not in ancestors:
                total += count(object_value.get("/Resources", resources), object_value, ancestors + (identity,))
        return total
    return sum(count(page.get("/Resources"), page.get_contents()) for page in reader.pages)


def inspect_report(state, rid, output_dir, *, protected_values=()):
    """기록과 모델을 바꾸지 않고 PDF·페이지 PNG·객관적 검사 결과를 만든다."""
    result = {"research_id": rid, "status": "INSPECTION_UNAVAILABLE", "objective_status": "INCOMPLETE",
        "visual_and_scientific_quality": "HUMAN_REVIEW_REQUIRED", "content_superiority": "NOT_ASSESSED",
        "scope": "텍스트·순서·단위 공개·픽셀 보존 검사이며 논리의 타당성, 그림의 설명력, 보고서 내용의 우위를 평가하지 않습니다.",
        "paid_calls": 0, "inference_calls": 0, "search_calls": 0, "checks": [], "errors": [], "artifacts": {}}
    directory, before = None, None
    protected_values = tuple(value for value in protected_values if value)
    def check(identity, passed, **detail):
        result["checks"].append({"check": identity, "passed": bool(passed), **detail})
    try:
        directory = _safe_output(output_dir)
        before = _activity(state, rid)
        view = friendly_report(state, rid)
        encoded_view = to_json(view).encode("utf-8", errors="strict")
        if not _clean("visual-view.json", encoded_view, protected_values):
            raise ValueError("VISUAL_QA_SECRET_BLOCKED")
        rendered = render_pdf(state, rid, protected_values=protected_values)
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(rendered["data"]))
        page_text = [page.extract_text() or "" for page in reader.pages]
        text = "\n\n".join(page_text)
        text_bytes = text.encode("utf-8", errors="strict")
        if not _clean("report-text.txt", text_bytes, protected_values):
            raise ValueError("VISUAL_QA_SECRET_BLOCKED")
        pdf = directory / "report.pdf"
        _write(pdf, rendered["data"], protected_values)
        _write(directory / "report-text.txt", text_bytes, protected_values)
        result["artifacts"].update(pdf={"path": str(pdf), "sha256": rendered["sha256"]}, text=str(directory / "report-text.txt"))
        normalized = _normalized(text)
        type_label = {"principle": "원리 설명", "design": "실험 설계", "literature": "문헌 조사", "analysis": "자료 분석"}.get(view.get("report_type"), "")
        expected_title = "과학 탐구 보고서 · " + type_label if view.get("ai_report") else "연구 결과"
        check("REPORT_TITLE_PRESERVED", _normalized(expected_title) in normalized)
        check("RESEARCH_QUESTION_PRESERVED", _normalized(view["question"]) in normalized)
        check("PDF_TEXT_AVAILABLE", bool(normalized), page_count=len(reader.pages))
        specs = view.get("visual_specs", [])
        check("TWO_SEMANTIC_VISUAL_SPECS", len(specs) >= 2 and {item.get("kind") for item in specs} >= {"relationship", "procedure"}, count=len(specs))
        for index, spec in enumerate(specs):
            title_position = normalized.find(_normalized(spec.get("title", "")))
            caption_position = normalized.find(_normalized(spec.get("caption", "")), max(0, title_position))
            check("DIAGRAM_TITLE_CAPTION_" + str(index), title_position >= 0 and caption_position >= title_position)
            section = normalized[title_position:caption_position] if title_position >= 0 and caption_position >= title_position else ""
            for node_index, node in enumerate(spec.get("nodes", [])):
                expected = [node.get(key, "") for key in ("name", "role", "unit", "definition", "control")]
                check("DIAGRAM_NODE_" + str(index) + "_" + str(node_index), all(_normalized(value) in section for value in expected if value))
            cursor = 0
            for step_index, step in enumerate(spec.get("steps", [])):
                position = section.find(_normalized(step), cursor)
                check("DIAGRAM_STEP_" + str(index) + "_" + str(step_index), position >= cursor)
                if position >= 0:
                    cursor = position + len(_normalized(step))
            for edge_index, edge in enumerate(spec.get("edges", [])):
                edge_text = spec["nodes"][edge["from"]]["name"] + " → " + spec["nodes"][edge["to"]]["name"] + " · " + edge["label"]
                check("DIAGRAM_RELATION_" + str(index) + "_" + str(edge_index), _normalized(edge_text) in normalized)
        cursor, figure_positions = 0, []
        for index, image in enumerate(view.get("images", [])):
            caption = _normalized(image["caption"])
            position = normalized.find(caption, cursor)
            check("VERIFIED_FIGURE_CAPTION_" + str(index), bool(caption) and position >= cursor, artifact_id=image["artifact_id"])
            figure_positions.append(position)
            if position >= 0:
                cursor = position + len(caption)
            plot = image.get("plot", {})
            for axis, label in (("x", "가로축"), ("y", "세로축")):
                column, axis_label = plot.get(axis), plot.get(axis + "_label")
                unit = (plot.get("units") or {}).get(column)
                expected = label + ": " + str(axis_label) + " (단위: " + (unit or "원자료에 제공되지 않음") + ")"
                check("FIGURE_UNIT_DISCLOSURE_" + str(index) + "_" + axis,
                      bool(axis_label) and _normalized(expected) in caption,
                      unit_status="DECLARED_IN_VERIFIED_PLOT" if unit else "UNKNOWN_EXPLICITLY_DISCLOSED",
                      unit_source=(plot.get("unit_sources") or {}).get(column))
        figures = view.get("images", [])
        check("VERIFIED_FIGURE_LIST_ORDER", all(position >= 0 for position in figure_positions) and figure_positions == sorted(figure_positions), expected_ids=[item["artifact_id"] for item in figures])
        draws = _image_draws(reader)
        check("VERIFIED_FIGURES_EMBEDDED", draws >= len(figures), expected_figures=len(figures), embedded_image_draws=draws)
        pages = _render_pages(pdf, directory, len(reader.pages))
        pixels = [_pixel_details(path, protected_values) for path in pages]
        result["artifacts"]["pages"] = pixels
        check("PDF_ALL_PAGES_RENDERED", len(pixels) == len(reader.pages))
        for index, details in enumerate(pixels, 1):
            check("PAGE_BODY_NONBLANK_" + str(index), details["body_nonblank"], body_dark_fraction=details["body_dark_fraction"])
        result["report_type"] = view.get("report_type")
        result["counts"] = {"pages": len(reader.pages), "visual_specs": len(specs), "verified_figures": len(figures)}
        result["human_review_items"] = ["핵심 답변과 결론이 질문에 답하는가", "도식이 원리·변인 관계와 절차를 설명하는가",
            "축·단위·자료 범위·비교 조건의 의미가 정확한가", "여백·줄바꿈·크기·색·잘림 없이 읽을 수 있는가",
            "가설·원리·예상 결과·실제 관측이 혼동되지 않는가", "단일 모델과 비교한 내용·시각 자료의 우위가 실제 검토로 확인되는가"]
    except Exception as exc:
        code = getattr(exc, "code", None)
        if not code and re.fullmatch(r"[A-Z0-9_:.-]+", str(exc)):
            code = str(exc)
        result["errors"].append({"code": code or type(exc).__name__, "scope": "LOCAL_VISUAL_INSPECTION_ONLY"})
    finally:
        if before is not None:
            try:
                after = _activity(state, rid)
                check("STATE_USAGE_SEARCH_ACTIVITY_UNCHANGED", before == after)
            except Exception as exc:
                result["errors"].append({"code": type(exc).__name__, "scope": "ACTIVITY_CHECK_ONLY"})
        if not result["errors"]:
            passed = bool(result["checks"]) and all(item["passed"] for item in result["checks"])
            result.update(status="OBJECTIVE_CHECKS_PASSED" if passed else "OBJECTIVE_CHECKS_FAILED", objective_status="PASS" if passed else "FAIL")
        if directory is not None:
            try:
                _write_json(directory / "visual-quality.json", result, protected_values)
            except Exception as exc:
                result.update(status="INSPECTION_UNAVAILABLE", objective_status="INCOMPLETE")
                result["errors"].append({"code": type(exc).__name__, "scope": "LOCAL_RESULT_WRITE_ONLY"})
    return result


def main():
    parser = argparse.ArgumentParser(description="기존 완료 연구를 읽기 전용으로 렌더 검사합니다.")
    parser.add_argument("--run-folder", type=Path, required=True)
    parser.add_argument("--research-id")
    arguments = parser.parse_args()
    folder = arguments.run_folder.resolve()
    db = sqlite3.connect((folder / "state.sqlite").as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        state = StateService(db, Workspace(folder / "workspace"))
        rows = db.execute("SELECT research_id FROM control_runs WHERE status='COMPLETED' ORDER BY rowid").fetchall()
        results = [inspect_report(state, row[0], folder / "visual-quality" / row[0]) for row in rows if not arguments.research_id or row[0] == arguments.research_id]
        _write_json(folder / "visual-quality" / "inspection-index.json", {"paid_calls": 0, "results": results})
        print(to_json({"paid_calls": 0, "results": [{"research_id": item["research_id"], "status": item["status"], "checks": len(item["checks"]), "failed_checks": [check["check"] for check in item["checks"] if not check["passed"]], "errors": item["errors"], "visual_and_scientific_quality": item["visual_and_scientific_quality"]} for item in results]}))
    finally:
        db.close()


if __name__ == "__main__":
    main()
