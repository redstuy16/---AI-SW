"""검증된 보고서의 현재 수정본을 로컬 PDF로 렌더링한다."""
from __future__ import annotations

from hashlib import sha256
import html
import io
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from .control_plane import ControlError
from .report_ux import friendly_report
from .release import _secret_free, validate_report_snapshot
from .storage import sha256_file
from .database import to_json


def _font():
    candidates = [Path(os.environ["HTRSA_PDF_FONT"])] if os.environ.get("HTRSA_PDF_FONT") else []
    candidates += [Path("C:/Windows/Fonts/malgun.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf")]
    for path in candidates:
        if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".ttf":
            return path
    raise ControlError("PDF_KOREAN_FONT_REQUIRED")


def render_pdf(state, rid, *, protected_values=(), include_preview=False):
    manifest = validate_report_snapshot(state, rid)
    view = friendly_report(state, rid)
    source = state.workspace.path(rid, "research_output/evidence/sources.json")
    sources = json.loads(source.read_text(encoding="utf-8", errors="strict"))
    text = to_json({"view":view, "sources":sources})
    text.encode("utf-8", errors="strict")
    if len(text.encode("utf-8")) > 2_000_000 or not _secret_free("report.json", text.encode("utf-8")) or any(v and v in text for v in protected_values):
        raise ControlError("PDF_CONTENT_BLOCKED")
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Image
        from reportlab.pdfgen import canvas
    except ImportError:
        raise ControlError("PDF_RENDERER_UNAVAILABLE") from None
    font_path = _font()
    font_name = "HTRSA-" + sha256(str(font_path).encode("utf-8")).hexdigest()[:12]
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
    base = ParagraphStyle("본문", fontName=font_name, fontSize=9, leading=15,
                          wordWrap="CJK", alignment=TA_LEFT, spaceAfter=7)
    title = ParagraphStyle("제목", parent=base, fontSize=18, leading=25, spaceAfter=12)
    heading = ParagraphStyle("절", parent=base, fontSize=12, leading=18, spaceBefore=12, spaceAfter=8)
    small = ParagraphStyle("표", parent=base, fontSize=7.5, leading=12, spaceAfter=0)
    width = A4[0] - 88
    def paragraph(value, style=base):
        return Paragraph(html.escape(str(value)).replace("\n", "<br/>"), style)
    def table(headers, rows, widths=None):
        data = [[paragraph(v, small) for v in headers]] + [[paragraph(v, small) for v in row] for row in rows]
        item = Table(data, colWidths=widths or [width / len(headers)] * len(headers), repeatRows=1, hAlign="LEFT", splitByRow=1, splitInRow=1)
        item.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#edf0ec")),
            ("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#d6d8d2")),("VALIGN",(0,0),(-1,-1),"TOP"),
            ("LEFTPADDING",(0,0),(-1,-1),7),("RIGHTPADDING",(0,0),(-1,-1),7),
            ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
        return item
    story = [paragraph("연구 결과", title), paragraph(view["question"]),
        paragraph("연구 ID: " + rid + " · 상태 버전: " + str(manifest["state_version"])),
        paragraph("검증된 수정본의 로컬 보고서 · " + ("실행 종료" if view["complete"] else "부분·중단·미확정 보고서"))]
    design = view.get("research_design", {})
    if design.get("available"):
        story.append(paragraph("이번 연구의 조건", heading))
        story.append(paragraph("연구 설계 수정본: " + str(design["revision"]), small))
        for name in ("처음 정한 조건", "실제로 사용한 자료·방법", "확인하지 못한 조건 또는 달라진 점", "이 결론으로 말할 수 있는 범위"):
            story.append(paragraph(name, heading))
            values = design[name] if isinstance(design[name], list) else [design[name]]
            for value in values:
                story.append(paragraph(value))
    for index, label in enumerate(view["titles"], 1):
        story.append(paragraph(str(index) + ". " + label, heading))
        if index == 1:
            story.append(paragraph("상태: " + view["status"] + " · 중단 사유: " + str(view["stop_reason"] or "진행 중")))
        elif index == 2:
            story.append(paragraph(view["conclusion"]))
            from .qualified_profiles import conclusion_card
            card = conclusion_card(state, rid)
            if card["available"]:
                story.append(paragraph("결론 검토 카드", heading))
                for label, value in [("무엇을 알아본 결과인가?", card["question"]),
                    ("어떤 자료를 사용했나?", card["source"]), ("무엇을 계산했나?", card["calculation"]),
                    ("어디까지 말할 수 있나?", card["scope"]),
                    ("무엇은 아직 확인하지 못했나?", " · ".join(card["unconfirmed"])),
                    ("이 결론은 현재도 유효한가?", card["currentness"])]:
                    story += [paragraph(label), paragraph(value)]
                representation = card.get("representation")
                if representation:
                    label = "선택 키별 형식 대조 수행" if representation.get("performed") else "형식 대조 미수행"
                    story.append(paragraph(label + " · " + ("필수" if representation.get("required") else "선택")))
                    story.append(paragraph(representation["limitation"]))
                from .qualified_profiles import source_inspection
                inspection = source_inspection(state, rid)
                source_record = inspection.get("primary", {})
                story.append(paragraph("자료 SHA-256: " + str(source_record.get("source_sha256")), small))
                story.append(paragraph("해시 대상: 전송 처리 후 저장한 UTF-8 텍스트. 출처 설명 HTML은 수집하지 않았습니다.\n" +
                    ("앱에서 HTTP 응답을 관측했습니다." if source_record.get("capture", {}).get("http_observed") else "로컬로 제공된 자료이며 원래 HTTP 수집은 관측하지 않았습니다."), small))
                if card.get("changes"):
                    change = card["changes"]
                    story.append(paragraph("이전 결과와 현재 결과", heading))
                    story.append(paragraph("이전 질문: " + change["old_question"] + "\n현재 질문: " + change["new_question"]))
                    for group, selection in change["groups"].items():
                        story.append(paragraph(group + " · 추가: " + str(selection["added"]) + " · 제외: " + str(selection["removed"])))
                    story.append(table(["수치", "이전", "현재", "변경"],
                        [[v["name"], v["before"], v["after"], "변경" if v["changed"] else "동일"] for v in change["values"]]))
                    story.append(paragraph("현재 결과는 현재 자료·질문·의미·검증 버전을 재검사했습니다. 이전 결과는 이력입니다."))
        elif index == 3:
            story.append(table(["검증 수치","값","실험"], [[n.get("label",n["field"]),n["value"],n["experiment_id"]] for n in view["display_numbers"]], [width*.4,width*.2,width*.4]))
            for ref in view["evidence_refs"]:
                story.append(paragraph("근거: " + str(ref)))
        elif index == 4:
            rows = []
            for analysis in view["analyses"]:
                plan = {key:value for key,value in analysis["plan"].items() if key not in {"numeric_provenance"}}
                rows.append([analysis["method"],analysis["experiment_id"],json.dumps(plan,ensure_ascii=False)])
            story.append(table(["분석 방법","실험 ID","분석 계획"],rows,[width*.22,width*.3,width*.48]))
        elif index == 5:
            for image in view["images"]:
                record = state.file_artifact(image["artifact_id"], rid)
                path = state.workspace.path(rid, record["relative_path"])
                if path.stat().st_size > 5_000_000 or sha256_file(path) != image["sha256"]:
                    raise ControlError("PDF_FIGURE_INVALID")
                from PIL import Image as PillowImage
                with PillowImage.open(path) as probe:
                    iw, ih = probe.size
                    if iw * ih > 16_000_000 or iw <= 0 or ih <= 0:
                        raise ControlError("PDF_FIGURE_INVALID")
                scale = min(width / iw, 280 / ih)
                story += [Image(str(path), width=iw*scale, height=ih*scale), paragraph(image["caption"])]
            if not view["images"]:
                story.append(paragraph("검증된 그림 없음"))
        elif index == 6:
            story.append(paragraph(view["comparison"]))
        elif index == 7:
            story.append(paragraph("현재 출처·해시·필수 재검증을 통과한 기록만 포함합니다."))
            for number in view["numbers"]:
                proof = number["provenance"]
                story.append(paragraph(number["field"] + ": " + str(number["value"])))
                story.append(paragraph("원본: " + str(proof.get("artifact_id")) + " · 필드: " + str(proof.get("field")) + " · 데이터 SHA-256: " + str(proof.get("dataset_sha256")), small))
        elif index == 8:
            for name, value in view["limitations"].items():
                story += [paragraph(name),paragraph(" · ".join(value) if isinstance(value,list) else value)]
        else:
            story.append(paragraph(view["reproduction"]))
            story.append(paragraph("보고서 출처 및 원본 해시", heading))
            for item in sources:
                story.append(paragraph(item["title"] + " · " + str(item.get("metadata_hash"))))
                url = item.get("url")
                target = urlsplit(url or "")
                if target.scheme == "https" and target.netloc and not target.username and not target.password:
                    escaped = html.escape(url, quote=True)
                    story.append(Paragraph('<link href="' + escaped + '">' + escaped + '</link>', base))
            story.append(table(["산출물","SHA-256"], sorted(manifest["files"].items()), [width*.35,width*.65]))
    class ReportDocument(SimpleDocTemplate):
        def afterFlowable(self, flowable):
            key = getattr(flowable, "report_bookmark", None)
            if key:
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(flowable.getPlainText(), key, 0)
                self.notify('TOCEntry', (0, flowable.getPlainText(), self.page, key))
    if view.get("ai_report"):
        from .report_composition import compose
        story = compose(state, rid, view, paragraph, table, base, title, heading, width, font_name)
    output = io.BytesIO()
    document = ReportDocument(output,pagesize=A4,leftMargin=44,rightMargin=44,topMargin=42,bottomMargin=46,
                                  title="H-TRSA 연구 결과",author="H-TRSA",pageCompression=0)
    def footer(pdf, doc):
        pdf.setFont(font_name, 8)
        pdf.drawString(44,24,"H-TRSA · " + ("부분 보고서" if not view["complete"] else "연구 결과"))
        pdf.drawRightString(A4[0]-44,24,str(doc.page))
    class StableCanvas(canvas.Canvas):
        def __init__(self,*args,**kwargs):
            kwargs["invariant"] = True
            super().__init__(*args,**kwargs)
    try:
        if view.get("ai_report"):
            document.multiBuild(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=StableCanvas)
        else:
            document.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=StableCanvas)
    except ControlError:
        raise
    except Exception:
        raise ControlError("PDF_RENDER_FAILED") from None
    data = output.getvalue()
    if not data.startswith(b"%PDF-") or len(data) > 8_000_000:
        raise ControlError("PDF_RENDER_FAILED")
    latest = validate_report_snapshot(state, rid)
    if latest["state_version"] != manifest["state_version"] or latest["files"] != manifest["files"]:
        raise ControlError("PDF_STATE_CHANGED")
    output = {"data":data, "sha256":sha256(data).hexdigest(), "state_version":manifest["state_version"],
              "renderer":"reportlab", "report_format":"pdf"}
    if include_preview:
        output["preview"] = view
    return output
