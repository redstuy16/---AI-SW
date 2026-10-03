"""검증된 원본과 수치 출처만 읽는 일반 사용자용 보고서."""
from __future__ import annotations

import json
import math
import html

from .final_report import build_final_conclusion, validate_final_conclusion, _trusted_stat, _verified_experiments, ReportValidationError
from .storage import sha256_file


TITLES = ["한눈에 보기", "핵심 결론", "결론의 근거", "데이터와 분석 방법", "시각화", "추가 분석과 비교", "검증", "한계와 미해결 문제", "재현 방법"]
SUPPORT = {"SUPPORTED": "정해진 범위에서 근거가 지지합니다. 인과관계는 확인되지 않았습니다.",
           "PARTIALLY_SUPPORTED": "지지하는 근거와 상충하거나 제외된 근거가 함께 있습니다.",
           "NOT_SUPPORTED": "검증된 근거는 제안한 관계를 지지하지 않습니다.",
           "INCONCLUSIVE": "현재 검증된 근거만으로 종합 결론을 확정할 수 없습니다."}
METRICS = {"n": "분석 표본 수", "sample_size": "분석 표본 수", "estimate": "관계 추정값", "r": "상관계수",
           "metrics.estimate": "관계 추정값", "metrics.p_value": "p-value (계산된 값)",
           "counts.total": "입력 표본 수", "counts.used": "사용한 표본 수", "counts.missing_excluded": "결측 제외 수",
           "uncertainty.bounds.0": "추정 구간 하한", "uncertainty.bounds.1": "추정 구간 상한"}


def friendly_report(state, rid):
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
                           "release_path": f"friendly_figures/{row['artifact_id']}{path.suffix.lower()}", "sha256": artifact["sha256"], "caption": "검증된 분석 산출물 · 축과 범위는 원본 분석 계획을 따릅니다."})
    limitations = {"현재 말할 수 있는 것": SUPPORT[conclusion.support_level],
                   "현재 말할 수 없는 것": "인과관계, 외부 모집단 일반화, 보고되지 않은 p-value나 유의성은 확인하지 않았습니다.",
                   "미해결 검증과 복구": conclusion.unresolved_questions or ["추가로 기록된 미해결 문제 없음 · 전체 타당성 보증은 아님"],
                   "실행하지 않은 환경 검증": ["Docker / Live LLM / Live Search는 별도 환경 기록을 확인해야 합니다.", "라이브 에이전트 효능 NOT_VALIDATED"]}
    display = []
    for number in numbers:
        field = number["field"]
        if field in METRICS:
            display.append({"label": METRICS[field], **number})
        elif field.startswith("metrics.") and field.rsplit(".", 1)[-1] in {"mae", "rmse", "r2"}:
            name = {"mae": "평균 절대 오차", "rmse": "제곱근 평균 제곱 오차", "r2": "결정계수"}[field.rsplit(".", 1)[-1]]
            display.append({"label": ("기준 모델 " if "baseline" in field else "분석 모델 ") + name, **number})
    return {"available": True, "mode": "DETERMINISTIC_LOCAL", "ai_narrative": "NOT_RUN", "titles": TITLES, "report_style": style,
            "source_semantics": state.cycle5.snapshot(rid) if state.cycle5.enabled(rid) else {"enabled": False},
            "question": conclusion.research_question, "status": run["run_status"], "stop_reason": run["stop_reason"],
            "complete": run["stop_reason"] == "GOAL_ANSWERED", "conclusion": SUPPORT[conclusion.support_level],
            "support_level": conclusion.support_level, "numbers": numbers, "display_numbers": display, "analyses": methods, "images": images,
            "evidence_refs": conclusion.evidence_refs, "contradictions": conclusion.contradiction_refs,
            "limitations": limitations, "comparison": "추가 분석은 별도 승인된 방법별 결과로 비교합니다. 분석 간 차이를 새 유의성이나 인과 효과로 해석하지 않습니다.",
            "reproduction": "검증 후 내보내기의 manifest, 입력 hash, 고정 분석 계획과 재현 안내를 사용하세요."}


def markdown_report(view):
    lines = ["# 연구 결과", "", "로컬 결정론적 보고서 · AI 서술 API 실행 안 함", ""]
    for index, title in enumerate(view["titles"], 1):
        lines += [f"## {index}. {title}", ""]
        if index == 1:
            lines += [html.escape(view["question"]).replace("[", "&#91;").replace("]", "&#93;"), "", "상태: " + str(view["stop_reason"] or "진행 중")]
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
    return "\n".join(lines)
