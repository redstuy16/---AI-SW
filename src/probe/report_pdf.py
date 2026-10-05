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
    candidates = [Path(os.environ["PROBE_PDF_FONT"])] if os.environ.get("PROBE_PDF_FONT") else []
    candidates += [Path("C:/Windows/Fonts/malgun.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf")]
    for path in candidates:
        if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".ttf":
            return path
    raise ControlError("PDF_KOREAN_FONT_REQUIRED")


def _pdf_text(value):
    """한글 기본 글꼴에 없는 수학 기호를 의미가 같은 표기로 보존한다."""
    text=html.escape(str(value)).replace('−','-').replace('⁻','<super>-</super>')
    for subscript,number in zip('₀₁₃₄₅₆₇₈₉','013456789'):
        text=text.replace(subscript,'<sub>'+number+'</sub>')
    return text.replace('\n','<br/>')


def _encode_binary_streams(data):
    """그림·글꼴의 ASCII85만 16진수로 바꾸며 원본 스트림과 내용은 보존한다."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, ArrayObject, StreamObject
    from pypdf.filters import ASCII85Decode
    reader=PdfReader(io.BytesIO(data));writer=PdfWriter()
    writer.clone_document_from_reader(reader)
    changed=False
    for stream in writer._objects:
        if not isinstance(stream, StreamObject):continue
        filters=stream.get('/Filter')
        filters=list(filters) if isinstance(filters, ArrayObject) else [filters]
        if not filters or filters[0] != '/ASCII85Decode':continue
        changed=True
        decoded=ASCII85Decode.decode(stream._data)
        stream._data=decoded.hex().upper().encode('ascii')+b'>'
        stream[NameObject('/Filter')]=ArrayObject([NameObject('/ASCIIHexDecode'),*filters[1:]])
    if not changed:return data
    output=io.BytesIO();writer.write(output)
    return output.getvalue()


def render_pdf(state, rid, *, protected_values=(), include_preview=False):
    from .report_publication import selected_revision
    with selected_revision(state, rid):
        return _render_pdf(state, rid, protected_values=protected_values, include_preview=include_preview)


def _render_pdf(state, rid, *, protected_values=(), include_preview=False):
    manifest = validate_report_snapshot(state, rid)
    view = friendly_report(state, rid)
    from .report_publication import report_root
    source = report_root(state, rid) / "evidence/sources.json"
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
    font_name = "PROBE-" + sha256(str(font_path).encode("utf-8")).hexdigest()[:12]
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
    base = ParagraphStyle("본문", fontName=font_name, fontSize=9, leading=15,
                          wordWrap="CJK", alignment=TA_LEFT, spaceAfter=7)
    title = ParagraphStyle("제목", parent=base, fontSize=18, leading=25, spaceAfter=12)
    heading = ParagraphStyle("절", parent=base, fontSize=12, leading=18, spaceBefore=12, spaceAfter=8, keepWithNext=True)
    small = ParagraphStyle("표", parent=base, fontSize=7.5, leading=12, spaceAfter=0)
    width = A4[0] - 88
    def paragraph(value, style=base):
        from .report_content import public_text
        return Paragraph(_pdf_text(public_text(value)), style)
    def table(headers, rows, widths=None):
        data = [[paragraph(v, small) for v in headers]] + [[paragraph(v, small) for v in row] for row in rows]
        item = Table(data, colWidths=widths or [width / len(headers)] * len(headers), repeatRows=1, hAlign="LEFT", splitByRow=1, splitInRow=1)
        item.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#edf0ec")),
            ("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#d6d8d2")),("VALIGN",(0,0),(-1,-1),"TOP"),
            ("LEFTPADDING",(0,0),(-1,-1),7),("RIGHTPADDING",(0,0),(-1,-1),7),
            ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
        return item
    from .report_composition import compose_public
    story = compose_public(state, rid, view, paragraph, table, base, title, heading, width, sources)
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
                                  title="Probe 연구 결과",author="Probe",pageCompression=0)
    def footer(pdf, doc):
        pdf.setFont(font_name, 8)
        pdf.drawString(44,24,"Probe · " + ("부분 보고서" if not view["complete"] else "연구 결과"))
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
    data = _encode_binary_streams(output.getvalue())
    if not data.startswith(b"%PDF-") or len(data) > 8_000_000:
        raise ControlError("PDF_RENDER_FAILED")
    latest = validate_report_snapshot(state, rid)
    if latest["state_version"] != manifest["state_version"] or latest["files"] != manifest["files"]:
        raise ControlError("PDF_STATE_CHANGED")
    output = {"data":data, "sha256":sha256(data).hexdigest(), "state_version":manifest["state_version"],
              "renderer":"reportlab", "report_format":"pdf", "report_type":view.get("report_type", "analysis")}
    if include_preview:
        output["preview"] = view
    return output
