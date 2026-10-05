"""조사 보고서의 목차·인용·설계 그림을 로컬에서 조판한다."""
from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

from .control_plane import ControlError
from .research_report import report_inputs
from .storage import sha256_file


def compose(state, rid, view, paragraph, table, base, title, heading, width, font_name):
    from .report_ux import inquiry_sections
    draft = (view.get("ai_report") or {}).get("draft") or {}
    if inquiry_sections(draft):
        return compose_inquiry(state, rid, view, paragraph, table, base, title, heading, width)
    return compose_public(state, rid, view, paragraph, table, base, title, heading, width)


def compose_public(state, rid, view, paragraph, table, base, title, heading, width, sources=()):
    """실제 내용이 있는 항목만 출력하고 내부 운영 정보를 조판하지 않는다."""
    from reportlab.platypus import Paragraph, Table, TableStyle
    from reportlab.lib.colors import HexColor
    from .report_content import public_sections
    if not sources:
        sources = report_inputs(state, rid)["sources"]
    story = [paragraph("과학 탐구 보고서" if view.get("ai_report") else "연구 결과", title)]
    if not view["complete"] or view.get("ai_report") and view["ai_report"]["status"] != "READY":
        story.append(paragraph("부분 보고서 · 확인된 범위까지 작성됨"))
    for index, item in enumerate(public_sections(view, sources)):
        label = paragraph(item["title"], heading)
        label.report_bookmark = "section-" + str(index)
        story.append(label)
        if item["title"] == "참고문헌":
            for line in item["text"].splitlines():
                target = urlsplit(line)
                if target.scheme == "https" and target.netloc and not target.username and not target.password:
                    escaped = html.escape(line, quote=True)
                    story.append(Paragraph('<link href="' + escaped + '">' + escaped + '</link>', base))
                else:
                    story.append(paragraph(line))
        elif item["text"]:
            story.append(paragraph(item["text"]))
        if item["table"]:
            story.append(table(*item["table"]))
        if item["images"]:
            append_inquiry_images(state, rid, view, story, paragraph, width)
    draft = (view.get("ai_report") or {}).get("draft") or {}
    if not draft.get("variables") and not draft.get("procedure") and not view.get("research_design", {}).get("available"):
        return story
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
