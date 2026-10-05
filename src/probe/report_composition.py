"""조사 보고서의 목차·인용·설계 그림을 로컬에서 조판한다."""
from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

from .control_plane import ControlError
from .research_report import report_inputs
from .storage import sha256_file


def compose(state, rid, view, paragraph, table, base, title, heading, width, font_name):
    from reportlab.platypus import Paragraph, Image, Spacer
    from reportlab.platypus.tableofcontents import TableOfContents
    from reportlab.platypus import Table, TableStyle
    from reportlab.lib.colors import HexColor
    record = view["ai_report"]
    draft = record.get("draft") or {}
    from .report_ux import inquiry_sections
    if inquiry_sections(draft) or (record["status"] != "READY" and not draft.get("procedure") and not draft.get("variables")):
        return compose_inquiry(state, rid, view, paragraph, table, base, title, heading, width)
    inputs = report_inputs(state, rid)
    source_map = {v["evidence_id"]: v for v in inputs["evidence"]}
    references = list(dict.fromkeys(v["source_id"] for v in inputs["evidence"]))
    numbering = {ref: i + 1 for i, ref in enumerate(references)}
    report_type = draft.get("report_type", "design")
    type_label = {"principle": "원리 설명", "design": "실험 설계", "literature": "문헌 조사", "analysis": "자료 분석"}[report_type]
    story = [paragraph("과학 탐구 보고서 · " + type_label, title)]
    if record["status"] != "READY" or not view["complete"]:
        story.append(paragraph("부분 보고서 · 확인된 범위까지 작성됨"))
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
    if draft.get("explanation"):
        story += [paragraph("원리와 적용 범위", heading), paragraph(draft["explanation"])]
    if report_type == "principle":
        story.append(paragraph("모델 지식 기반 원리 설명입니다. 실제 실험·관측 결과와 독립적 문헌 검증은 별도입니다."))
    if record.get("error"):
        story.append(paragraph("작성 제한: " + record["error"]))
    section("근거", "evidence")
    for claim in draft.get("claims", []):
        item = source_map[claim["evidence_id"]]
        story += [paragraph(claim["text"] + " [" + str(numbering[item["source_id"]]) + "]"),
                  paragraph("인용 원문: " + claim["quote"]), paragraph("확인 범위: " + (item.get("evidence_location", "") if item.get("text_field") in {"fulltext", "webpage"} else "제공사에서 확보한 초록"))]
        if item.get("source_scope"):
            story.append(paragraph("대상·조건 일치 문헌" if item["source_scope"] == "DIRECT" else "원리를 설명하는 간접 문헌"))
    if not draft.get("claims"):
        for item in inputs["evidence"][:8]:
            story += [paragraph(item["title"] + " [" + str(numbering[item["source_id"]]) + "]"),
                      paragraph(("공개 원문에서 확인: " if item.get("text_field") in {"fulltext", "webpage"} else "초록에서 확인: ") + item["evidence_text"])]
        if not inputs["evidence"]:
            story.append(paragraph("확인된 문헌 근거 없음. 제목만 수집한 문헌에서는 결론을 만들지 않았습니다."))
    opposing = state._db.execute("SELECT evidence_text FROM evidence WHERE research_id=? AND source_type='LITERATURE' AND status='VERIFIED' AND polarity='CONTRADICT'", (rid,)).fetchall()
    if opposing:
        story.append(paragraph("상충하는 근거", heading))
        for item in opposing:
            story.append(paragraph(item[0]))
    section("분석 또는 실험 설계", "design")
    design = view.get("research_design", {})
    if design.get("available"):
        story += [paragraph("이번 연구의 조건", heading), paragraph("연구 설계 수정본: " + str(design["revision"]))]
        for label in ("처음 정한 조건", "실제로 사용한 자료·방법", "확인하지 못한 조건 또는 달라진 점", "이 결론으로 말할 수 있는 범위"):
            story.append(paragraph(label, heading))
            values = design[label] if isinstance(design[label], list) else [design[label]]
            for value in values:
                story.append(paragraph(value))
    if view["display_numbers"]:
        story.append(table(["검증된 수치", "값"], [[n["label"], n["value"]] for n in view["display_numbers"]]))
    measurements = record.get("requested_measurements", {})
    if measurements.get("rows"):
        story += [paragraph(measurements.get("scope_label", "요청한 음료별 방출 속도") + " · 비교 가능한 측정값 미확보"),
                  table(["비교 항목", measurements.get("value_label", "방출 속도"), "단위"], [[v["item"], v["rate"], v["unit"]] for v in measurements["rows"]])]
    variables = draft.get("variables") or [{"name": "비교할 조건", "role": "독립변인"}, {"name": "측정할 결과", "role": "종속변인"}, {"name": "동일하게 유지할 조건", "role": "통제변인"}]
    story += [paragraph("실험 설계안 · 아직 수행하지 않은 측정 절차", heading),
              table(["변인", "역할·단위", "정의·통제 방법"], [[v["name"], v["role"] + (" · " + v.get("unit", "") if v.get("unit") else ""), "\n".join(filter(None, [v.get("definition"), v.get("control")]))] for v in variables])]
    if draft.get("materials"):
        story.append(paragraph("준비물: " + " · ".join(draft["materials"])))
    steps = draft.get("procedure") or ["비교할 조건과 측정 방법을 정합니다.", "나머지 조건을 동일하게 유지하며 시간별 결과를 기록합니다.", "원본 측정 자료와 단위·오차를 보존한 뒤 분석합니다."]
    for i, step in enumerate(steps, 1):
        story.append(paragraph(str(i) + ". " + step))
    story.append(paragraph(draft.get("measurement") or "측정 자료, 단위, 관측 시간과 반복 측정의 오차가 필요합니다."))
    section("시각화", "figures")
    for spec in view.get("visual_specs", []):
        diagram_heading = paragraph(spec["title"], heading)
        diagram_heading.keepWithNext = True
        story.append(diagram_heading)
        rows = []
        if spec["kind"] == "relationship":
            for node in spec["nodes"]:
                text = node["name"] + (" · 단위 " + node["unit"] if node.get("unit") else "")
                text += "\n" + "\n".join(filter(None, [node.get("definition"), node.get("control")]))
                rows.append([paragraph(node["role"]), paragraph(text)])
            diagram = Table(rows, colWidths=[width * .25, width * .75], splitByRow=1, splitInRow=1)
        else:
            for index, step in enumerate(spec["steps"], 1):
                rows.append([paragraph(str(index)), paragraph(step)])
                if index < len(spec["steps"]):
                    rows.append([paragraph("↓"), paragraph("다음 단계")])
            diagram = Table(rows, colWidths=[width * .10, width * .90], splitByRow=1, splitInRow=1)
        diagram.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), HexColor("#dcebe4")),
            ("BACKGROUND", (1, 0), (-1, -1), HexColor("#f1f6f3")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), .5, HexColor("#c6d8ce")), ("LINEBELOW", (0, 0), (-1, -1), .35, HexColor("#d5e2dc")),
            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        story += [diagram, paragraph(spec["caption"])]
        for edge in spec.get("edges", []):
            story.append(paragraph(spec["nodes"][edge["from"]]["name"] + " → " + spec["nodes"][edge["to"]]["name"] + " · " + edge["label"]))
    for image in view["images"]:
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
    for item in draft.get("limitations", []) or ["초록을 확보하지 못한 문헌은 읽은 자료로 취급하지 않습니다.", "직접 측정한 결과 없이 실험 결과를 확정할 수 없습니다."]:
        story.append(paragraph(item))
    story.append(paragraph("문헌 주장의 의미와 실험 설계의 타당성은 추가 검토가 필요합니다. 인용·수치·그림 참조 검사는 과학적 정확성을 보증하지 않습니다."))
    section("참고문헌", "references")
    for source in inputs["sources"]:
        ref = "[" + str(numbering[source["source_id"]]) + "] " if source["source_id"] in numbering else "미검토 · "
        locations = list(dict.fromkeys(v.get("evidence_location", "") for v in inputs["evidence"] if v["source_id"] == source["source_id"] and v.get("text_field") in {"fulltext", "webpage"}))
        scope = "공개 원문 확인 · " + ", ".join(locations) if locations else "초록 확인" if source["source_id"] in references else "서지정보만 확보 · 주장에 사용하지 않음"
        story += [paragraph(ref + source["title"]), paragraph(scope)]
        url = source.get("url") or ""
        target = urlsplit(url)
        if target.scheme == "https" and target.netloc and not target.username and not target.password:
            escaped = html.escape(url, quote=True)
            story.append(Paragraph('<link href="' + escaped + '">' + escaped + '</link>', base))
    story.append(paragraph("작성 기록: AI 작성 · 수정본 " + str(record["revision"]) + " · " + record["generated_at"][:10]))
    story.append(paragraph("근거 수정본 SHA-256: " + record["input_fingerprint"]))
    return story


def inquiry_blocks(text):
    """본문의 소제목과 단순 표만 조판하며 HTML은 실행하지 않는다."""
    lines=text.splitlines();blocks=[];plain=[];index=0
    def flush():
        if plain:blocks.append(('paragraph','\n'.join(plain)));plain.clear()
    cells=lambda line:[cell.strip() for cell in line.strip().strip('|').split('|')]
    while index<len(lines):
        line=lines[index];match=re.match(r'^#{1,6}\s+(.+)$',line)
        if match:flush();blocks.append(('heading',match[1]));index+=1;continue
        if line.strip().startswith('|') and index+1<len(lines):
            header=cells(line);separator=cells(lines[index+1])
            if len(header)==len(separator) and 1<=len(header)<=8 and all(re.fullmatch(r':?-{3,}:?',v) for v in separator):
                rows=[];end=index+2
                while end<len(lines) and lines[end].strip().startswith('|') and len(rows)<300:
                    row=cells(lines[end])
                    if len(row)!=len(header):break
                    rows.append(row);end+=1
                if rows:flush();blocks.append(('table',(header,rows)));index=end;continue
        plain.append(line);index+=1
    flush();return blocks

def compose_inquiry(state, rid, view, paragraph, table, base, title, heading, width):
    """보고서 본문과 실제 근거만 조판하며 빈 설계·그림·결과표를 생성하지 않는다."""
    from reportlab.platypus import Paragraph
    from .report_ux import inquiry_sections
    record = view["ai_report"]
    draft = record["draft"]
    inputs = report_inputs(state, rid)
    sources = {item["evidence_id"]: item for item in inputs["evidence"]}
    claims = draft.get("claims", [])
    used = list(dict.fromkeys(sources[item["evidence_id"]]["source_id"] for item in claims))
    numbering = {sid: index + 1 for index, sid in enumerate(used)}
    story = [paragraph("과학 탐구 보고서", title)]
    if record["status"] != "READY" or not view["complete"]:
        story.append(paragraph("부분 보고서 · 확인된 범위까지 작성됨"))
    if not inquiry_sections(draft):
        story.append(paragraph(draft.get("summary", "본문 작성이 완료되지 않았습니다.")))
        for claim in claims:
            sid = sources[claim["evidence_id"]]["source_id"]
            story.append(paragraph(claim["text"] + " [" + str(numbering[sid]) + "]"))
    for item in inquiry_sections(draft):
        label = paragraph(item["title"], heading)
        label.keepWithNext = True
        label.report_bookmark = item["key"]
        story.append(label)
        for kind,value in inquiry_blocks(item['text']):
            story.append(table(*value) if kind=='table' else paragraph(value,heading) if kind=='heading' else paragraph(value))
        if item["key"] == "method":
            if draft.get("variables"):
                story.append(table(["조건", "역할", "정의"], [[v["name"], v["role"], " · ".join(filter(None, [v.get("unit"), v.get("definition"), v.get("control")]))] for v in draft["variables"]]))
            if draft.get("materials"):
                story.append(paragraph("사용 자료·준비물: " + " · ".join(draft["materials"])))
            if draft.get("procedure"):
                if draft.get("report_type") == "design":
                    story.append(paragraph("미수행 실험 설계"))
                for step in draft["procedure"]:
                    story.append(paragraph(step))
            if draft.get("measurement"):
                story.append(paragraph(draft["measurement"]))
        if item["key"] == "results":
            for claim in claims:
                sid = sources[claim["evidence_id"]]["source_id"]
                story.append(paragraph(claim["text"] + " [" + str(numbering[sid]) + "]"))
            if view["display_numbers"]:
                story.append(table(["분석 지표", "값"], [[n["label"], n["value"]] for n in view["display_numbers"]]))
            append_inquiry_images(state, rid, view, story, paragraph, width)
            if inputs.get('calculations'):
                values=[value for calculation in inputs['calculations'] for value in calculation['results']]
                story.append(table(['검산 항목','값','단위'],[[v['field'],format(v['value'],'.12g'),v['unit']] for v in values]))
        if item["key"] == "conclusion":
            for limit in draft.get("limitations", []):
                if limit not in item["text"]:
                    story.append(paragraph(limit))
    if used:
        story.append(paragraph("참고문헌", heading))
        for source in inputs["sources"]:
            if source["source_id"] not in numbering:
                continue
            story.append(paragraph("[" + str(numbering[source["source_id"]]) + "] " + source["title"]))
            url = source.get("url") or ""
            target = urlsplit(url)
            if target.scheme == "https" and target.netloc and not target.username and not target.password:
                escaped = html.escape(url, quote=True)
                story.append(Paragraph('<link href="' + escaped + '">' + escaped + '</link>', base))
    for dataset in inputs.get('public_datasets',[]):
        from reportlab.lib.styles import ParagraphStyle
        metadata_style=ParagraphStyle('자료 출처',parent=base,fontSize=8,leading=12,spaceAfter=5)
        story.append(paragraph('공개 관측 자료와 전처리',heading))
        meta=dataset['preprocessing']
        story.append(paragraph(str(meta['first_year'])+'–'+str(meta['last_year'])+' · '+str(meta['row_count'])+'개 연도 · 온도 공통 기준 '+meta['temperature_baseline'],metadata_style))
        for source in dataset['sources'].values():
            story.append(paragraph(source['agency']+' · '+source['definition']+' · '+source['unit']+' · 원래 기준 '+source['baseline'],metadata_style))
            escaped=html.escape(source['final_url'],quote=True)
            story.append(Paragraph('<link href="'+escaped+'">'+escaped+'</link>',metadata_style))
            story.append(paragraph('원문 SHA-256: '+source['sha256'],metadata_style))
        story.append(paragraph(meta['join']+' · '+meta['enso_aggregation'],metadata_style))
    return story


def append_inquiry_images(state, rid, view, story, paragraph, width):
    """기존 원본 무결성·크기 검사를 통과한 그림만 결과에 첨부한다."""
    from reportlab.platypus import Image
    for image in view["images"]:
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
