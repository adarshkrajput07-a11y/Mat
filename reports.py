"""PDF reports for the auction: one squad sheet per team + the unsold list.
Needs reportlab (pip install reportlab). Functions return (pdf_bytes, file_name)."""
import io
import os
import re
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HERE = os.path.dirname(os.path.abspath(__file__))
GREEN, DARK, GOLD = colors.Color(10 / 255, 92 / 255, 54 / 255), colors.Color(4 / 255, 32 / 255, 28 / 255), \
    colors.Color(255 / 255, 196 / 255, 46 / 255)
ZEBRA = colors.Color(0.93, 0.97, 0.94)

FONT = "Helvetica"
_GLYPHS = None
try:  # media/font.ttf (DejaVu) covers far more characters than the built-in fonts
    pdfmetrics.registerFont(TTFont("AucFont", os.path.join(HERE, "media", "font.ttf")))
    FONT = "AucFont"
    _GLYPHS = pdfmetrics.getFont("AucFont").face.charToGlyph
except Exception:  # pragma: no cover
    pass


def clean(text) -> str:
    """Escape for reportlab and drop characters the font cannot draw (e.g. emoji)."""
    text = original = str(text or "")
    if _GLYPHS is not None:
        text = "".join(c for c in text if ord(c) in _GLYPHS or c == " ")
    else:
        text = text.encode("latin-1", "ignore").decode("latin-1")
    text = text.strip()
    if not text and original.strip():
        text = "(unsupported characters)"
    return escape(text)


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:40] or "team"


def _styles():
    base = dict(fontName=FONT, leading=13)
    return {
        "title": ParagraphStyle("t", fontSize=20, leading=24, textColor=GOLD, **{k: v for k, v in base.items() if k != "leading"}),
        "sub": ParagraphStyle("s", fontSize=10, leading=14, textColor=colors.white, fontName=FONT),
        "cell": ParagraphStyle("c", fontSize=9.5, textColor=colors.black, **base),
        "head": ParagraphStyle("h", fontSize=9.5, textColor=colors.white, **base),
        "info": ParagraphStyle("i", fontSize=10, textColor=colors.black, fontName=FONT, leading=15),
    }


def _footer(brand):
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont(FONT, 8)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 9 * mm, clean(brand))
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {doc.page}")
        canvas.restoreState()
    return draw


def _build(brand, title, subtitle, info_lines, header, rows, widths, foot=None):
    st, buf = _styles(), io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=14 * mm, bottomMargin=16 * mm, title=title, author=brand)
    banner = Table([[Paragraph(clean(title), st["title"])], [Paragraph(clean(subtitle), st["sub"])]],
                   colWidths=[180 * mm])
    banner.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), DARK),
                                ("LINEBELOW", (0, -1), (-1, -1), 3, GOLD),
                                ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (0, 0), 10),
                                ("BOTTOMPADDING", (0, -1), (-1, -1), 10)]))
    story = [banner, Spacer(1, 6 * mm)]
    for line in info_lines:
        story.append(Paragraph(line, st["info"]))
    story.append(Spacer(1, 5 * mm))

    data = [[Paragraph(clean(h), st["head"]) for h in header]]
    for r in rows:
        data.append([Paragraph(clean(c), st["cell"]) for c in r])
    if foot:
        data.append([Paragraph(f"<b>{clean(c)}</b>", st["cell"]) for c in foot])
    table = Table(data, colWidths=[w * mm for w in widths], repeatRows=1)
    style = [("BACKGROUND", (0, 0), (-1, 0), GREEN), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("GRID", (0, 0), (-1, -1), 0.4, colors.Color(0.75, 0.8, 0.77)),
             ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
    last = len(data) - (2 if foot else 1)
    for i in range(1, last + 1):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), ZEBRA))
    if foot:
        style += [("BACKGROUND", (0, -1), (-1, -1), colors.Color(1, 0.94, 0.75))]
    table.setStyle(TableStyle(style))
    story.append(table)
    foot_fn = _footer(brand)
    doc.build(story, onFirstPage=foot_fn, onLaterPages=foot_fn)
    return buf.getvalue()


def _when():
    return datetime.now().strftime("%d %b %Y, %H:%M")


def squad_pdf(brand, team_name, manager, vc, rows, budget, left, squad_size):
    """rows: [(player, username, role, base, price)] -> (pdf_bytes, filename)."""
    spent = budget - left
    info = [f"<b>Manager:</b> {clean(manager) or '-'}",
            f"<b>Vice-captain:</b> {clean(vc) or '-'}",
            f"<b>Players:</b> {len(rows)}/{squad_size} &nbsp;&nbsp; <b>Budget:</b> {budget} &nbsp;&nbsp; "
            f"<b>Spent:</b> {spent} &nbsp;&nbsp; <b>Left:</b> {left}"]
    body = [(i, n, f"@{u}" if u else "-", role or "-", base, price)
            for i, (n, u, role, base, price) in enumerate(rows, 1)]
    if not body:
        body = [("-", "No players bought", "", "", "", "")]
    foot = ("", "TOTAL", "", "", "", spent)
    data = _build(brand, f"{team_name} - Squad", f"{brand} · {_when()}", info,
                  ["#", "Player", "Username", "Role", "Base", "Sold for"], body,
                  [10, 62, 38, 28, 20, 22], foot)
    return data, f"{safe_name(team_name)}_squad.pdf"


def unsold_pdf(brand, rows):
    """rows: [(player, username, set_name, base, status)] -> (pdf_bytes, filename)."""
    info = [f"<b>Players not sold:</b> {len(rows)}"]
    body = [(i, n, f"@{u}" if u else "-", s or "-", base, status)
            for i, (n, u, s, base, status) in enumerate(rows, 1)]
    if not body:
        body = [("-", "Everyone was sold!", "", "", "", "")]
    data = _build(brand, "Unsold Players", f"{brand} · {_when()}", info,
                  ["#", "Player", "Username", "Set", "Base", "Status"], body,
                  [10, 58, 36, 34, 18, 24])
    return data, "unsold_players.pdf"
