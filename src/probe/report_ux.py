"""검증된 원본과 수치 출처만 읽는 일반 사용자용 보고서."""
from __future__ import annotations

import json
import math
import html
import re

from .final_report import build_final_conclusion, validate_final_conclusion, _trusted_stat, _verified_experiments, ReportValidationError
from .storage import sha256_file


TITLES = ["한눈에 보기", "핵심 결론", "결론의 근거", "데이터와 분석 방법", "시각화", "추가 분석과 비교", "검증", "한계와 미해결 문제", "재현 방법"]
SUPPORT = {"SUPPORTED": "정해진 범위에서 근거가 지지합니다. 인과관계는 확인되지 않았습니다.",
           "PARTIALLY_SUPPORTED": "지지하는 근거와 상충하거나 제외된 근거가 함께 있습니다.",
           "NOT_SUPPORTED": "검증된 근거는 제안한 관계를 지지하지 않습니다.",
           "INCONCLUSIVE": "현재 검증된 근거만으로 종합 결론을 확정할 수 없습니다."}
METRICS = {"n": "분석 표본 수", "sample_size": "분석 표본 수", "estimate": "관계 추정값", "r": "상관계수",
           "periods.0.mean": "앞 기간 편차 평균", "periods.1.mean": "뒤 기간 편차 평균", "difference": "뒤 기간 − 앞 기간",
           "metrics.estimate": "관계 추정값", "metrics.p_value": "p-value (계산된 값)",
           "counts.total": "입력 표본 수", "counts.used": "사용한 표본 수", "counts.missing_excluded": "결측 제외 수",
            "uncertainty.bounds.0": "추정 구간 하한", "uncertainty.bounds.1": "추정 구간 상한"}

METHOD_LABELS = {"pearson_correlation": "Pearson 상관 분석", "spearman_correlation": "Spearman 순위 상관 분석",
    "period_mean_difference": "두 기간 평균 비교", "two_period_comparison": "두 기간 평균 비교",
    "ridge_holdout": "Ridge 분리 평가", "ridge_rolling_origin": "Ridge 시계열 순차 평가"}



INQUIRY_SECTIONS = (("탐구 목적", "purpose"), ("이론적 배경", "explanation"),
                    ("탐구 방법", "method"), ("결과 및 해석", "results"), ("결론", "conclusion"))


def inquiry_sections(draft):
    """새 탐구 본문은 동일한 순서로 출력하고 기존 저장본은 원래 형식을 유지한다."""
    draft = draft or {}
    if not any(draft.get(key) for key in ("purpose", "method", "results", "conclusion")):
        return []
    return [{"title": title, "key": key, "text": draft.get(key, "")} for title, key in INQUIRY_SECTIONS if draft.get(key)]


def report_visual_specs(draft, detailed_design=None):
    """화면과 PDF가 함께 사용하는 설명·설계 도식의 의미 자료다."""
    draft, design = draft or {}, detailed_design or {}
    if inquiry_sections(draft) and not draft.get("variables") and not draft.get("procedure") and not design.get("variables") and not design.get("procedure"):
        return []
    from .research_design import ROLE_LABELS, CARD_FIELDS
    variables = []
    for variable in design.get("variables", []):
        if not variable.get("active", True):
            continue
        details = variable.get("details", {})
        control_fields = {"fixed_value", "maintain", "check", "tolerance", "difficulty"}
        describe = lambda keys: "\n".join(CARD_FIELDS.get(k, k) + ": " + v["value"] for k, v in details.items() if k in keys and v.get("value"))
        variables.append({"name": variable["name"], "role": ROLE_LABELS.get(variable["role"], variable["role"]),
            "unit": details.get("unit", {}).get("value", ""),
            "definition": describe(set(details) - control_fields - {"unit"}), "control": describe(control_fields)})
    for variable in draft.get("variables", []):
        existing = next((v for v in variables if v["name"] == variable["name"]), None)
        if existing is None:
            variables.append(dict(variable))
        else:
            for key, value in variable.items():
                if value and key in {"definition", "control"}:
                    existing[key] = "\n".join(dict.fromkeys(filter(None, [existing.get(key), value])))
                elif value:
                    existing[key] = value
    if not variables:
        sentences = [v.strip() for v in re.split(r"[\n。]|(?<=[.!?])\s+", draft.get("explanation") or draft.get("summary") or "") if v.strip()]
        variables = [{"name": value, "role": "원리·조건"} for value in sentences[:4]] or [{"name": "연구 질문과 적용 조건을 확인합니다.", "role": "설명 범위"}]
    steps = list(dict.fromkeys([v["text"] for v in design.get("procedure", []) if v.get("text")] + draft.get("procedure", [])))
    principle = draft.get("report_type") == "principle"
    if not steps:
        steps = (["질문에서 설명할 현상과 조건을 확인합니다.", "핵심 원리와 가정을 현상에 연결합니다.", "적용 범위와 추가 측정이 필요한 조건을 구분합니다."] if principle else
                 ["비교 조건과 측정 항목·단위를 정합니다.", "통제 조건을 유지하며 원자료와 오차를 기록합니다.", "현재 자료로 분석하고 미확인 범위를 함께 보고합니다."])
    outcome = [i for i, v in enumerate(variables) if any(word in v["role"].replace(" ", "").lower() for word in ("outcome", "output", "종속", "결과항목", "확인할출력"))]
    drivers = [i for i, v in enumerate(variables) if any(word in v["role"].replace(" ", "").lower() for word in ("manipulated", "독립", "조작", "바꿀입력", "fixed", "통제", "같게유지"))]
    edges = []
    for source in drivers:
        for target in (outcome[:1] if source != drivers[0] else outcome):
            fixed = any(word in variables[source]["role"].replace(" ", "").lower() for word in ("fixed", "통제", "같게유지"))
            edges.append({"from": source, "to": target, "label": "동일한 비교 조건 유지" if fixed else "검증할 관계 · 효과 미확인"})
    if principle:
        edges = [{"from": i, "to": i + 1, "label": "설명에서 이어지는 원리·조건"} for i in range(len(variables) - 1)]
    return [{"kind": "relationship", "title": "원리·조건 구조" if principle else "변인과 통제 조건",
        "caption": "원리 설명의 구성 · 실제 측정 결과가 아닙니다." if principle else "실험 설계안 · 조작·측정·통제 역할을 구분하며 인과 효과를 입증하지 않습니다.", "nodes": variables, "edges": edges},
        {"kind": "procedure", "title": "설명 확인 흐름" if principle else "탐구 절차 흐름",
        "caption": "설명을 확인하는 순서 · 수행 결과가 아닙니다." if principle else "계획한 절차 · 단계의 수행과 측정 결과는 별도 기록으로 확인합니다.", "steps": steps}]


def _figure_description(state, rid, artifact):
    row = state._db.execute("SELECT request_json,result_json FROM tool_calls WHERE request_id=?", (artifact["producer_id"],)).fetchone()
    if not row:
        return {"caption": "검증된 분석 그림 · 상세 축과 범위는 원본 산출물에서 확인합니다.", "plot": {}}
    request, result = json.loads(row[0]), json.loads(row[1])
    args, proof = request.get("args", {}), result.get("provenance", {})
    plan = args.get("plan", {})
    dataset_id = args.get("dataset_id") or plan.get("dataset_id") or proof.get("dataset_id")
    dataset = state._db.execute("SELECT row_count,sha256,original_name FROM datasets WHERE research_id=? AND dataset_id=?", (rid, dataset_id)).fetchone() if dataset_id else None
    if dataset is not None and proof.get("dataset_sha256") and proof["dataset_sha256"] != dataset["sha256"]:
        raise ReportValidationError("FIGURE_DATASET_CHANGED")
    variables, units = plan.get("variables", {}), dict(plan.get("units", {}))
    x, y = args.get("x", variables.get("x", "")), args.get("y", variables.get("y", ""))
    unit_sources = {column: "FROZEN_SKILL_PLAN" for column in units}
    for axis, column in (("x", x), ("y", y)):
        declared = _axis_label_unit(args.get(axis + "_label", ""))
        if column and declared:
            units[column], unit_sources[column] = declared, "VERIFIED_PLOT_LABEL"
    committed = state._db.execute("SELECT payload_json FROM staged_mutations WHERE research_id=? AND status='COMMITTED' AND json_extract(payload_json,'$.scientific.figure_artifact_id')=? AND json_extract(payload_json,'$.scientific.dataset_id')=? ORDER BY rowid DESC LIMIT 1", (rid, artifact["artifact_id"], dataset_id)).fetchone()
    figure_method = plan.get("method", "")
    if committed:
        committed_payload = json.loads(committed[0])
        figure_method = committed_payload.get("scientific", {}).get("method", figure_method)
        provenance = committed_payload.get("agent_result", {}).get("provenance", {})
        scope = provenance.get("qualified_scope", {}) if provenance.get("qualified_profile") else {}
        frozen_unit = _axis_label_unit(scope.get("transform", {}).get("unit", ""))
        if frozen_unit and y == "value":
            units[y], unit_sources[y] = frozen_unit, "QUALIFIED_TRANSFORM"
            if x == "year":
                units[x], unit_sources[x] = "년", "QUALIFIED_TRANSFORM"
    plot = {"title": args.get("title") or plan.get("skill_id", "분석 그림"), "x": x, "y": y,
        "x_label": args.get("x_label") or x, "y_label": args.get("y_label") or y,
        "units": units, "unit_sources": unit_sources, "dataset_id": dataset_id, "dataset_sha256": dataset["sha256"] if dataset else None,
        "input_rows": dataset["row_count"] if dataset else None}
    plot["method"] = figure_method
    plot["method_label"] = METHOD_LABELS.get(figure_method, figure_method)
    parts = [plot["title"]]
    if plot["method_label"]:
        parts.append("분석 방법: " + plot["method_label"])
    if plot["x_label"]:
        parts.append("가로축: " + plot["x_label"] + " (단위: " + (units.get(x) or "원자료에 제공되지 않음") + ")")
    if plot["y_label"]:
        parts.append("세로축: " + plot["y_label"] + " (단위: " + (units.get(y) or "원자료에 제공되지 않음") + ")")
    if dataset:
        parts.append("자료: " + _dataset_label(dataset["original_name"]) + " · 입력 " + str(dataset["row_count"]) + "행")
    return {"caption": " · ".join(parts) + " · 현재 검증된 산출물", "plot": plot}


def _axis_label_unit(label):
    """검증된 그림에 명시한 단위만 읽고 설계 초안에서는 추정하지 않는다."""
    if not isinstance(label, str):
        return None
    units = {"degC": "°C", "°C": "°C", "degF": "°F", "°F": "°F", "degF_difference": "°F 편차",
        "K": "K", "m": "m", "cm": "cm", "mm": "mm", "s": "s", "ms": "ms", "g": "g", "kg": "kg", "mg": "mg",
        "L": "L", "mL": "mL", "Pa": "Pa", "kPa": "kPa", "N": "N", "J": "J", "W": "W", "Hz": "Hz", "mol": "mol",
        "m/s": "m/s", "m/s²": "m/s²", "m/s^2": "m/s²"}
    label = label.strip()
    if label in units:
        return units[label]
    wrapped = re.search(r"[\[(]([^\])]+)[\])]\s*$", label)
    return units.get(wrapped[1].strip()) if wrapped else None


def _dataset_label(name):
    return "검증된 분석 입력 자료" if re.search(r"[a-f0-9]{32,}", name, re.I) else name


def report_execution_details(state, rid, summary):
    """전체 실행 이력을 보존하면서 사용자에게 자료·방법·조건을 읽기 쉽게 보여준다."""
    labels = {"dataset_id": "입력 자료", "method": "분석 방법", "variables": "분석 열", "parameters": "분석 조건",
        "periods": "비교 기간", "group": "집단", "label": "구분", "start": "시작", "end": "끝", "year": "연도", "value": "측정값",
        "x": "가로축", "y": "세로축", "name": "항목", "unit": "단위", "units": "단위", "question": "연구 질문",
        "transform": "자료 변환", "operation": "처리", "baseline": "기준", "aggregation": "집계", "minimum_years": "최소 연도 수"}
    methods = {"period_mean_difference": "두 기간 평균 비교", "two_period_comparison": "두 기간 평균 비교", "pearson_correlation": "피어슨 상관계수", "spearman_correlation": "스피어만 상관계수"}
    hidden = {"plan_hash", "plan_ref", "dataset_sha256", "source_sha256", "source_metadata_hash", "plan_fingerprint"}
    def describe(value, prefix=""):
        lines = []
        if isinstance(value, dict):
            for key, item in value.items():
                if key in hidden:
                    continue
                label = labels.get(key, key)
                if key == "dataset_id":
                    row = state._db.execute("SELECT original_name FROM datasets WHERE research_id=? AND dataset_id=?", (rid, item)).fetchone()
                    item = _dataset_label(row[0]) if row else "연결된 입력 자료"
                elif key == "method":
                    item = methods.get(item, item)
                lines.extend(describe(item, (prefix + " · " if prefix else "") + label))
        elif isinstance(value, list):
            for index, item in enumerate(value, 1):
                lines.extend(describe(item, prefix + " " + str(index)))
        elif value is not None:
            lines.append(prefix + ": " + str(value))
        return lines
    display = []
    for raw in summary.get("실제로 사용한 자료·방법", []):
        try:
            display.append(" · ".join(describe(json.loads(raw))))
        except (ValueError, TypeError):
            display.append(raw)
    return {**summary, "실제로 사용한 자료·방법": display}


def friendly_report(state, rid):
    from .report_publication import report_root, selected_revision
    from .release import validate_report_snapshot
    root = report_root(state, rid)
    view = root / "report_view.json"
    if view.is_file():
        with selected_revision(state, rid, root):
            validate_report_snapshot(state, rid)
            return json.loads(view.read_text(encoding="utf-8", errors="strict"))
    return _friendly_report(state, rid)


def _friendly_report(state, rid):
    run = state._one("SELECT run_status,stop_reason FROM research_runs WHERE research_id=?", (rid,))
    style = "friendly"
    if state._db.execute("SELECT 1 FROM sqlite_master WHERE name='control_runs'").fetchone():
        controlled = state._db.execute("SELECT snapshot FROM control_runs WHERE research_id=?", (rid,)).fetchone()
        if controlled:
            snapshot = json.loads(controlled[0])
            applied = state._db.execute("SELECT payload FROM control_configs WHERE kind='research_effective' AND id=?", (rid,)).fetchone()
            if applied:
                snapshot.update(json.loads(applied[0])["settings"])
            style = snapshot.get("report_style", "friendly")
    conclusion = build_final_conclusion(state, rid)
    from .qualified_profiles import conclusion_card
    card = conclusion_card(state, rid)
    if card["available"] and not card["current"]:
        raise ReportValidationError("QUALIFIED_PROFILE_REVALIDATION_REQUIRED")
    validate_final_conclusion(state, rid, conclusion)
    experiments, numbers, methods = _verified_experiments(state, rid), [], []
    for exp in experiments:
        payload = json.loads(exp["payload_json"])
        methods.append({"experiment_id": exp["experiment_id"], "method": payload.get("method", exp.get("method")), "plan": payload})
        evidence = state._db.execute("SELECT provenance_json FROM evidence WHERE research_id=? AND experiment_id=? AND status='VERIFIED'", (rid, exp["experiment_id"])).fetchone()
        for field, provenance in json.loads(evidence[0] or "{}").items() if evidence else []:
            try:
                value = _trusted_stat(state, rid, exp["experiment_id"], field)
                if not math.isfinite(value):
                    continue
                numbers.append({"field": field, "value": value, "experiment_id": exp["experiment_id"], "provenance": provenance})
            except (ReportValidationError, KeyError, ValueError):
                continue
    images = []
    for row in state._db.execute("SELECT * FROM artifacts WHERE research_id=? AND status='VERIFIED' AND artifact_type='FIGURE'", (rid,)):
        artifact = state.file_artifact(row["artifact_id"], rid)
        path = state.workspace.path(rid, artifact["relative_path"])
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} and sha256_file(path) == artifact["sha256"]:
            images.append({"artifact_id": row["artifact_id"], "url": f"/api/research/{rid}/artifacts/{row['artifact_id']}/preview",
                           "release_path": f"friendly_figures/{row['artifact_id']}{path.suffix.lower()}", "sha256": artifact["sha256"], **_figure_description(state, rid, artifact)})
    limitations = {"현재 말할 수 있는 것": SUPPORT[conclusion.support_level],
                   "현재 말할 수 없는 것": "인과관계, 외부 모집단 일반화, 보고되지 않은 p-value나 유의성은 확인하지 않았습니다.",
                   "미해결 검증과 복구": conclusion.unresolved_questions or ["추가로 기록된 미해결 문제 없음 · 전체 타당성 보증은 아님"],
                   "실행하지 않은 환경 검증": ["Docker / Live LLM / Live Search는 별도 환경 기록을 확인해야 합니다.", "라이브 에이전트 효능 NOT_VALIDATED"]}
    method_labels = {item["experiment_id"]: METHOD_LABELS.get(item["method"], item["method"]) for item in methods}
    display = []
    for number in numbers:
        field = number["field"]
        if field in METRICS:
            display.append({"label": METRICS[field], **number})
        elif field.startswith("metrics.") and field.rsplit(".", 1)[-1] in {"mae", "rmse", "r2"}:
            name = {"mae": "평균 절대 오차", "rmse": "제곱근 평균 제곱 오차", "r2": "결정계수"}[field.rsplit(".", 1)[-1]]
            display.append({"label": ("기준 모델 " if "baseline" in field else "분석 모델 ") + name, **number})
    for item in display:
        item["method_label"] = method_labels.get(item["experiment_id"], "")
        if len(experiments) > 1 and item["method_label"]:
            item["label"] = item["method_label"] + " · " + item["label"]
    from .research_report import report_record
    ai_report = report_record(state, rid)
    from .research_design import current_design
    design_record = current_design(state, rid)
    visual_specs = report_visual_specs((ai_report or {}).get("draft"), (design_record or {}).get("design")) if ai_report else []
    if ai_report and ai_report["status"] != "READY" and not (ai_report.get("draft") or {}).get("variables") and not (ai_report.get("draft") or {}).get("procedure"):
        visual_specs = []
    return {"available": True, "mode": "AI_GROUNDED" if ai_report and ai_report["status"] == "READY" else "DETERMINISTIC_LOCAL",
            "ai_narrative": ai_report["status"] if ai_report else "NOT_RUN", "ai_report": ai_report, "titles": TITLES, "report_style": style,
            "source_semantics": state.cycle5.snapshot(rid) if state.cycle5.enabled(rid) else {"enabled": False},
            "research_design": report_execution_details(state, rid, __import__("probe.research_design", fromlist=["summary"]).summary(state, rid)),
            "question": conclusion.research_question, "status": run["run_status"], "stop_reason": run["stop_reason"],
            "complete": run["stop_reason"] in {"GOAL_ANSWERED", "QUALIFIED_PROCEDURE_COMPLETED", "LITERATURE_REVIEW_COMPLETED", "SCIENCE_INQUIRY_COMPLETED"},
            "report_type": ((ai_report or {}).get("draft") or {}).get("report_type", "analysis"), "visual_specs": visual_specs,
            "conclusion": ai_report["draft"]["summary"] if ai_report and ai_report["status"] == "READY" else card["calculation"] if card["available"] else SUPPORT[conclusion.support_level],
            "support_level": conclusion.support_level, "numbers": numbers, "display_numbers": display, "analyses": methods, "images": images, "conclusion_card":card,
            "evidence_refs": conclusion.evidence_refs, "contradictions": conclusion.contradiction_refs,
            "limitations": limitations, "comparison": "추가 분석은 별도 승인된 방법별 결과로 비교합니다. 분석 간 차이를 새 유의성이나 인과 효과로 해석하지 않습니다.",
            "reproduction": "검증 후 내보내기의 manifest, 입력 hash, 고정 분석 계획과 재현 안내를 사용하세요."}


def markdown_report(view):
    record = view.get("ai_report")
    writing = "AI 작성 · 수정본 " + str(record["revision"]) if record and record["status"] == "READY" else "부분 보고서 · AI 작성 미완료" if record else "로컬 결정론적 보고서 · AI 서술 API 실행 안 함"
    sections = inquiry_sections((record or {}).get("draft"))
    if sections:
        lines = ["# 과학 탐구 보고서", ""]
        if record["status"] != "READY" or not view["complete"]:
            lines += ["부분 보고서 · 확인된 범위까지 작성됨", ""]
        for item in sections:
            lines += ["## " + item["title"], "", item["text"], ""]
        claims = record["draft"].get("claims", [])
        if claims:
            lines += ["## 참고 근거", ""]
            lines += [claim["text"] + " [" + claim["evidence_id"] + "]" for claim in claims]
        return "\n".join(lines)
    lines = ["# 연구 결과", ""]
    for index, title in enumerate(view["titles"], 1):
        lines += [f"## {index}. {title}", ""]
        if index == 1:
            lines += ["상태: " + str(view["stop_reason"] or "진행 중")]
        elif index == 2:
            lines += [view["conclusion"]]
        elif index in {3, 4, 7}:
            lines += ["검증된 분석 결과와 원본 provenance에 연결된 수치만 포함합니다."]
            if index == 4:
                lines += ["- " + str(a["method"]) for a in view["analyses"]]
                for source in view.get("source_semantics", {}).get("sources", []):
                    meaning = f"{source['column']}: {source['quantity_name']} ({source['quantity_kind']}), 단위 {source['unit']}, 기준 {source['baseline'] or '미지정'}, {source['spatial_scope']}, {source['temporal_scope']}, {source['review_status']}"
                    lines += ["- " + html.escape(meaning).replace("[", "&#91;").replace("]", "&#93;")]
            if index == 3:
                lines += ["- " + n["field"] + ": " + str(n["value"]) for n in view["numbers"]]
        elif index == 5:
            lines += ["![검증된 분석 그림](" + i["release_path"] + ")" for i in view["images"]] or ["검증된 그림 없음"]
        elif index == 6:
            lines += [view["comparison"]]
        elif index == 8:
            for key, value in view["limitations"].items():
                lines += ["### " + key, "", str(value), ""]
        else:
            lines += [view["reproduction"]]
        lines += [""]
    lines += ["작성 기록: " + writing, ""]
    return "\n".join(lines)
