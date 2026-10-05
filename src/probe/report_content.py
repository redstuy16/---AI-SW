"""보고서에 공개할 탐구 내용만 구성하고 내부 운영 기록의 노출을 차단한다."""
from __future__ import annotations

import re
import unicodedata

_HASH = re.compile(r"\b[0-9a-f]{64}\b", re.I)
_ID = re.compile(r"\b[A-Z][A-Z_]{0,20}-[0-9a-f]{8,64}\b", re.I)
_CODE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_INTERNAL = re.compile(
    r"SHA[\s–‑-]?256|\b(?:manifest|provenance|numeric_mentions|artifact_id|experiment_id|state_version|input_fingerprint)\b|"
    r"\bDocker\b|Live\s*(?:LLM|Search)|"
    r"실행하지 않은 환경 검증|라이브 에이전트|미해결 검증과 복구|"
    r"현재 출처·해시·필수 재검증|검증 후 내보내기|작성 기록|연구 ID|실험 ID|상태 버전|"
    r"AI\s*(?:본문 작성|작성|서술 API)|API[·ㆍ\s]*(?:검증|호출|키)|"
    r"보고서 출처 및 원본 해시|산출물 해시|원문 해시|근거 수정본|형식 대조|선택값 대조|"
    r"검증 시스템|비용 원장|토큰 예산|API 비용|복구 기록|워커 PID|스키마|"
    r"검증된 그림 없음|추가로 기록된 미해결 문제 없음|전체 타당성 보증|추가 분석은 별도 승인|"
    r"입력한 연구 의도이며 실제 관측이나 검증 완료를 뜻하지|자료·열 연결과 실행 증거|"
    r"계획과 실제 사용 여부를 구분|실제 자료와 검증된 분석의 지원 범위|"
    r"(?:cost_report|research_trace|state_snapshot|report_view|final_report)\.(?:jsonl?|md)|"
    r"(?:manifests|evidence|experiments|hypotheses)/\S+\.json|"
    r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|/(?:tmp|home|workspace)/)\S+",
    re.I,
)


def internal_content(value):
    """구조화 참조가 아닌 표시 문장에 내부 식별자·운영 정보가 있는지 검사한다."""
    text = unicodedata.normalize("NFKC", str(value))
    return bool(_HASH.search(text) or _ID.search(text) or _CODE.search(text) or _INTERNAL.search(text))



def empty_table_lines(text):
    """열 이름과 구분선만 있는 Markdown 표의 위치를 찾는다."""
    lines = text.splitlines()
    for index in range(len(lines) - 1):
        if not lines[index].strip().startswith("|"):
            continue
        cells = [v.strip() for v in lines[index + 1].strip().strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", v) for v in cells):
            end = index + 2
            has_data = False
            while end < len(lines) and lines[end].strip().startswith("|"):
                has_data = has_data or any(v.strip() for v in lines[end].strip().strip("|").split("|"))
                end += 1
            if not has_data:
                yield from range(index, end)


def public_text(value):
    """이전 저장본은 보존하고 출력에서 내부 문장을 제외한다."""
    result = []
    text = str(value or "")
    hidden = set(empty_table_lines(text))
    for index, line in enumerate(text.splitlines()):
        if index in hidden:
            continue
        line = _HASH.sub("", line)
        line = _ID.sub("", line).strip(" ·;:")
        parts = re.split(r"(?<=[.!?。])\s+", line)
        clean = " ".join(part for part in parts if not internal_content(part)).strip()
        if clean:
            result.append(clean)
    return "\n".join(result)


def public_sections(view, sources=()):
    """내용이 있는 본문·실제 표·방법·관련 한계·서지만 허용한다."""
    from .report_ux import inquiry_sections, METHOD_LABELS
    draft = (view.get("ai_report") or {}).get("draft") or {}
    sections = []

    def add(title, text="", table=None, images=False):
        text = public_text(text)
        if text or table or images:
            sections.append({"title": title, "text": text, "table": table, "images": images})

    inquiry = inquiry_sections(draft)
    if inquiry:
        for item in inquiry:
            add(item["title"], item["text"])
    else:
        add("결론", draft.get("summary") or view.get("conclusion"))
        add("이론적 배경", draft.get("explanation"))
    design = view.get("research_design") or {}
    if design.get("available"):
        conditions = []
        for label in ("처음 정한 조건", "실제로 사용한 자료·방법", "확인하지 못한 조건 또는 달라진 점", "이 결론으로 말할 수 있는 범위"):
            values = design.get(label, [])
            value = public_text("\n".join(values) if isinstance(values, list) else values)
            if value:
                conditions.append(label + ":\n" + value)
        add("이번 연구의 조건", "\n".join(conditions))
    add("문헌 근거", "\n".join(c["text"] for c in draft.get("claims", [])))
    methods = list(dict.fromkeys(METHOD_LABELS.get(a["method"], a["method"]) for a in view.get("analyses", [])))
    add("분석 방법", "\n".join(methods))
    if draft.get("variables"):
        rows = [[public_text(v.get(k, "")) for k in ("name", "role", "unit", "definition", "control")] for v in draft["variables"]]
        rows = [row for row in rows if row[0] and row[1]]
        if rows:
            add("변인과 조건", table=(["변인", "역할", "단위", "정의", "통제"], rows))
    add("사용 자료·준비물", "\n".join(draft.get("materials", [])))
    add("미수행 실험 설계" if draft.get("report_type") == "design" else "탐구 절차", "\n".join(draft.get("procedure", [])))
    add("측정 방법", draft.get("measurement"))
    numbers = view.get("display_numbers", [])
    if numbers:
        add("분석 결과", table=(["분석 지표", "값"], [[public_text(n.get("label", "분석값")), str(n["value"])] for n in numbers]))
    if view.get("images"):
        add("분석 그림", images=True)
    limits = list(draft.get("limitations", []))
    card = view.get("conclusion_card") or {}
    if card.get("available"):
        limits += [card.get("scope", ""), *card.get("unconfirmed", [])]
    limits = list(dict.fromkeys(filter(None, (public_text(v) for v in limits))))
    add("한계", "\n".join(limits))
    refs = sources or view.get("references", [])
    lines = []
    for source in refs:
        if source.get("status") not in {None, "VERIFIED"}:
            continue
        title = public_text(source.get("title", ""))
        if not title:
            continue
        lines.append(title)
        url = source.get("url") or ""
        from urllib.parse import urlsplit
        target = urlsplit(url)
        if target.scheme == "https" and target.netloc and not target.username and not target.password:
            lines.append(url)
    add("참고문헌", "\n".join(dict.fromkeys(lines)))
    return sections
