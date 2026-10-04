"""조사 보고서의 목차·인용·설계 그림을 로컬에서 조판한다."""
from __future__ import annotations

import html
from urllib.parse import urlsplit

from .control_plane import ControlError
from .research_report import report_inputs
from .storage import sha256_file


def compose(state, rid, view, paragraph, table, base, title, heading, width, font_name):
    from reportlab.platypus import Paragraph, Image, Spacer
    from reportlab.platypus.tableofcontents import TableOfContents
    from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon
    record = view["ai_report"]
    draft = record.get("draft") or {}
    inputs = report_inputs(state, rid)
    source_map = {v["evidence_id"]: v for v in inputs["evidence"]}
    references = list(dict.fromkeys(v["source_id"] for v in inputs["evidence"]))
    numbering = {ref: i + 1 for i, ref in enumerate(references)}
    story = [paragraph("연구 보고서", title), paragraph(view["question"]),
             paragraph(("AI 작성" if record["status"] == "READY" and view["complete"] else "부분 보고서 · AI 작성" if record["status"] == "READY" else "부분 보고서 · AI 작성 미완료") +
                       " · 수정본 " + str(record["revision"]) + " · " + record["generated_at"][:10]),
             paragraph("문헌 조사와 실험 설계는 직접 측정한 결과와 구분합니다.")]
    toc = TableOfContents()
    toc.levelStyles = [base]
    story += [paragraph("목차", heading), toc, Spacer(1, 12)]
    def section(label, key):
        item = paragraph(label, heading)
        item.keepWithNext = True
        item.report_bookmark = key
        story.append(item)
    section("핵심 답변", "answer")
    story.append(paragraph(draft.get("summary") or "확보한 기록으로 확인된 범위만 정리했습니다. 직접 측정하지 않은 수치 결과는 없습니다."))
    if record.get("error"):
        story.append(paragraph("작성 제한: " + record["error"]))
    section("근거", "evidence")
    for claim in draft.get("claims", []):
        item = source_map[claim["evidence_id"]]
        story += [paragraph(claim["text"] + " [" + str(numbering[item["source_id"]]) + "]"),
                  paragraph("인용 원문: " + claim["quote"]), paragraph("확인 범위: " + (item.get("evidence_location", "") if item.get("text_field") == "fulltext" else "제공사에서 확보한 초록"))]
        if item.get("source_scope"):
            story.append(paragraph("대상·조건 일치 문헌" if item["source_scope"] == "DIRECT" else "원리를 설명하는 간접 문헌"))
    if not draft.get("claims"):
        for item in inputs["evidence"][:8]:
            story += [paragraph(item["title"] + " [" + str(numbering[item["source_id"]]) + "]"),
                      paragraph(("공개 원문에서 확인: " if item.get("text_field") == "fulltext" else "초록에서 확인: ") + item["evidence_text"])]
        if not inputs["evidence"]:
            story.append(paragraph("확인된 문헌 근거 없음. 제목만 수집한 문헌에서는 결론을 만들지 않았습니다."))
    opposing = state._db.execute("SELECT evidence_text FROM evidence WHERE research_id=? AND source_type='LITERATURE' AND status='VERIFIED' AND polarity='CONTRADICT'", (rid,)).fetchall()
    if opposing:
        story.append(paragraph("상충하는 근거", heading))
        for item in opposing:
            story.append(paragraph(item[0]))
    section("분석 또는 실험 설계", "design")
    if view["display_numbers"]:
        story.append(table(["검증된 수치", "값"], [[n["label"], n["value"]] for n in view["display_numbers"]]))
    measurements = record.get("requested_measurements", {})
    if measurements.get("rows"):
        story += [paragraph("요청한 음료별 방출 속도 · 비교 가능한 측정값 미확보"),
                  table(["비교 항목", "방출 속도", "단위"], [[v["item"], v["rate"], v["unit"]] for v in measurements["rows"]])]
    variables = draft.get("variables") or [{"name": "비교할 조건", "role": "독립변인"}, {"name": "측정할 결과", "role": "종속변인"}, {"name": "동일하게 유지할 조건", "role": "통제변인"}]
    story += [paragraph("실험 설계안 · 아직 수행하지 않은 측정 절차", heading),
              table(["변인", "역할"], [[v["name"], v["role"]] for v in variables])]
    if draft.get("materials"):
        story.append(paragraph("준비물: " + " · ".join(draft["materials"])))
    steps = draft.get("procedure") or ["비교할 조건과 측정 방법을 정합니다.", "나머지 조건을 동일하게 유지하며 시간별 결과를 기록합니다.", "원본 측정 자료와 단위·오차를 보존한 뒤 분석합니다."]
    for i, step in enumerate(steps, 1):
        story.append(paragraph(str(i) + ". " + step))
    story.append(paragraph(draft.get("measurement") or "측정 자료, 단위, 관측 시간과 반복 측정의 오차가 필요합니다."))
    section("시각화", "figures")
    drawing = Drawing(width, 112)
    nodes = variables[:3]
    gap, card = 16, (width - 32) / 3
    for i, node in enumerate(nodes):
        x = i * (card + gap)
        drawing.add(Rect(x, 28, card, 70, fillColor=__import__("reportlab.lib.colors", fromlist=["HexColor"]).HexColor("#eef4f0"), strokeColor=None))
        drawing.add(String(x + 10, 80, node["role"][:14], fontName=font_name, fontSize=8))
        name = node["name"]
        for line, start in enumerate(range(0, min(len(name), 42), 14)):
            drawing.add(String(x + 10, 60 - line * 12, name[start:start + 14], fontName=font_name, fontSize=9))
        if i < len(nodes) - 1:
            drawing.add(Line(x + card + 2, 63, x + card + gap - 2, 63))
            drawing.add(Polygon([x + card + gap - 2, 63, x + card + gap - 7, 66, x + card + gap - 7, 60]))
    story += [drawing, paragraph("변인 관계도 · 설계안. 화살표는 검증된 인과관계나 측정 결과가 아닙니다.")]
    selected = set(draft.get("figure_refs", []))
    for image in view["images"]:
        if image["artifact_id"] not in selected:
            continue
        path = state.workspace.path(rid, state.file_artifact(image["artifact_id"], rid)["relative_path"])
        if sha256_file(path) != image["sha256"] or path.stat().st_size > 5_000_000:
            raise ControlError("PDF_FIGURE_INVALID")
        from PIL import Image as PillowImage
        with PillowImage.open(path) as probe:
            iw, ih = probe.size
        if iw <= 0 or ih <= 0 or iw * ih > 16_000_000:
            raise ControlError("PDF_FIGURE_INVALID")
        scale = min(width / iw, 260 / ih)
        story += [Image(str(path), width=iw * scale, height=ih * scale), paragraph(image["caption"])]
    section("한계", "limits")
    for item in draft.get("limitations", []) or ["초록을 확보하지 못한 문헌은 읽은 자료로 취급하지 않습니다.", "직접 측정한 결과 없이 방출 속도나 실험 결과를 확정할 수 없습니다."]:
        story.append(paragraph(item))
    story.append(paragraph("문헌 주장의 의미와 실험 설계의 타당성은 추가 검토가 필요합니다. 인용·수치·그림 참조 검사는 과학적 정확성을 보증하지 않습니다."))
    section("참고문헌", "references")
    for source in inputs["sources"]:
        ref = "[" + str(numbering[source["source_id"]]) + "] " if source["source_id"] in numbering else "미검토 · "
        locations = list(dict.fromkeys(v.get("evidence_location", "") for v in inputs["evidence"] if v["source_id"] == source["source_id"] and v.get("text_field") == "fulltext"))
        scope = "공개 원문 확인 · " + ", ".join(locations) if locations else "초록 확인" if source["source_id"] in references else "서지정보만 확보 · 주장에 사용하지 않음"
        story += [paragraph(ref + source["title"]), paragraph(scope)]
        url = source.get("url") or ""
        target = urlsplit(url)
        if target.scheme == "https" and target.netloc and not target.username and not target.password:
            escaped = html.escape(url, quote=True)
            story.append(Paragraph('<link href="' + escaped + '">' + escaped + '</link>', base))
    story.append(paragraph("근거 수정본 SHA-256: " + record["input_fingerprint"]))
    return story
