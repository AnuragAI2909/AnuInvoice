"""Invoice rendering: on-screen HTML preview, PDF export and WhatsApp share link."""
from __future__ import annotations

import re
from html import escape as h
from io import BytesIO
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape as xesc

from .invoice import ApprovalError, Invoice, fmt_money, fmt_qty

ACCENT = "#1F4E79"


# =========================================================================== HTML


def to_html(inv: Invoice) -> str:
    s, c, t = inv.settings, inv.customer, inv.totals
    cur = s.currency
    approved = inv.is_approved
    badge = (
        f'<span class="badge ok">APPROVED by {h(inv.approval.reviewer)}</span>' if approved
        else '<span class="badge draft">DRAFT - pending approval</span>'
    )
    rows = []
    for i, l in enumerate(inv.lines, 1):
        if l.resolved:
            desc = f"<b>{h(l.name)}</b>" + (f"<div class='sub'>{h(l.description)}</div>" if l.description else "")
            if l.requested and l.requested.lower() != l.name.lower():
                desc += f"<div class='sub'>Requested as: “{h(l.requested)}”</div>"
            flag = ' <span class="tag warn">reviewer-confirmed</span>' if l.status == "reviewer_confirmed" else ""
            rows.append(
                f"<tr><td>{i}</td><td>{desc}{flag}</td><td class='r'>{h(fmt_qty(l.quantity))}</td>"
                f"<td>{h(l.unit)}</td><td class='r'>{fmt_money(l.unit_price, cur)}</td>"
                f"<td class='r'>{fmt_money(l.amount, cur)}</td></tr>"
            )
        else:
            sug = f" - suggestion: {h(l.suggestion)}" if l.suggestion else ""
            reason = "invalid quantity" if l.status == "invalid_quantity" else "not in price list"
            rows.append(
                f"<tr class='flag'><td>{i}</td><td colspan='4'><b>{h(l.requested)}</b> "
                f"<span class='tag bad'>⚠ {reason}{sug}</span></td><td class='r'>-</td></tr>"
            )
    disc = (f"<tr><td>Discount ({h(f'{s.discount_pct:g}')}%)</td><td class='r'>- {fmt_money(t.discount, cur)}</td></tr>"
            if t.discount else "")
    notes = f"<div class='box'><b>Notes</b><br>{h(inv.notes)}</div>" if inv.notes.strip() else ""
    who = "<br>".join(h(x) for x in (c.name, c.company, c.email, c.phone) if x) or "<i>Customer details missing</i>"
    tax_id = f"<br>{h(s.tax_label)} No: {h(s.business_tax_id)}" if s.business_tax_id else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body{{font-family:'Segoe UI',Arial,sans-serif;color:#1c2733;margin:0;padding:24px;background:#fff}}
.top{{display:flex;justify-content:space-between;gap:24px;border-bottom:3px solid {ACCENT};padding-bottom:14px}}
h1{{margin:0;color:{ACCENT};letter-spacing:2px;font-size:30px}} .biz{{font-size:13px;line-height:1.5}}
.meta{{text-align:right;font-size:13px;line-height:1.6}}
.badge{{display:inline-block;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700}}
.ok{{background:#dff5e3;color:#14692b}} .draft{{background:#fff1d6;color:#8a5a00}}
.bill{{margin:18px 0;font-size:13px;line-height:1.5}} .bill b.l{{color:#66788a;font-size:11px;letter-spacing:1px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:{ACCENT};color:#fff;text-align:left;padding:8px}} td{{padding:8px;border-bottom:1px solid #e3e8ee;vertical-align:top}}
.r{{text-align:right;white-space:nowrap}} .sub{{color:#66788a;font-size:11.5px}}
tr.flag td{{background:#fdecea}} .tag{{font-size:11px;border-radius:4px;padding:1px 6px}}
.tag.warn{{background:#fff1d6;color:#8a5a00}} .tag.bad{{background:#f8c9c4;color:#8c1d13}}
.tot{{width:44%;margin:14px 0 0 auto}} .tot td{{border:0;padding:4px 8px}}
.tot tr.g td{{border-top:2px solid {ACCENT};font-weight:700;font-size:15px;color:{ACCENT}}}
.box{{margin-top:16px;background:#f5f8fb;border-radius:6px;padding:10px 12px;font-size:12.5px;line-height:1.5}}
</style></head><body>
<div class="top"><div class="biz"><b style="font-size:16px">{h(s.business_name)}</b><br>{h(s.business_address)}<br>{h(s.business_email)}{tax_id}</div>
<div class="meta"><h1>INVOICE</h1>{badge}<br><b>{h(inv.number)}</b><br>Issued: {inv.issue_date:%d %b %Y}<br>Due: {inv.due_date:%d %b %Y}</div></div>
<div class="bill"><b class="l">BILL TO</b><br>{who}</div>
<table><tr><th>#</th><th>Description</th><th class="r">Qty</th><th>Unit</th><th class="r">Rate</th><th class="r">Amount</th></tr>{''.join(rows)}</table>
<table class="tot"><tr><td>Subtotal</td><td class="r">{fmt_money(t.subtotal, cur)}</td></tr>{disc}
<tr><td>{h(s.tax_label)} ({h(f'{s.tax_rate:g}')}%)</td><td class="r">{fmt_money(t.tax, cur)}</td></tr>
<tr class="g"><td>Total</td><td class="r">{fmt_money(t.total, cur)}</td></tr></table>
{notes}<div class="box"><b>Payment terms</b><br>Due within {s.payment_terms_days} days. {h(s.payment_info)}</div>
</body></html>"""


# ============================================================================ PDF

_FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("/Library/Fonts/DejaVuSans.ttf", "/Library/Fonts/DejaVuSans-Bold.ttf"),
    ("C:/Windows/Fonts/DejaVuSans.ttf", "C:/Windows/Fonts/DejaVuSans-Bold.ttf"),
]


def _register_fonts() -> tuple[str, str, bool]:
    """Return (regular, bold, supports_rupee_glyph)."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for reg, bold in _FONT_CANDIDATES:
        if Path(reg).exists() and Path(bold).exists():
            try:
                font = TTFont("InvSans", reg)
                pdfmetrics.registerFont(font)
                pdfmetrics.registerFont(TTFont("InvSans-Bold", bold))
                pdfmetrics.registerFontFamily("InvSans", normal="InvSans", bold="InvSans-Bold")
                return "InvSans", "InvSans-Bold", 0x20B9 in font.face.charToGlyph
            except Exception:
                continue
    return "Helvetica", "Helvetica-Bold", False


def to_pdf(inv: Invoice) -> bytes:
    """Render the approved invoice as a PDF. Refuses to export unapproved/stale invoices."""
    if not inv.is_approved:
        raise ApprovalError("Invoice must be approved (and unchanged since approval) before export.")

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    reg, bold, rupee = _register_fonts()
    cur = inv.settings.currency
    ascii_only = cur == "INR" and not rupee
    money = lambda v: fmt_money(v, cur, ascii_only)  # noqa: E731
    accent = colors.HexColor(ACCENT)

    base = ParagraphStyle("b", fontName=reg, fontSize=9.5, leading=13)
    small = ParagraphStyle("s", parent=base, fontSize=8, leading=10.5, textColor=colors.HexColor("#5a6b7b"))
    right = ParagraphStyle("r", parent=base, alignment=TA_RIGHT)
    title = ParagraphStyle("t", parent=base, fontName=bold, fontSize=24, leading=28, textColor=accent, alignment=TA_RIGHT)
    hdr = ParagraphStyle("h", parent=base, fontName=bold, textColor=colors.white)
    hdr_r = ParagraphStyle("hr", parent=hdr, alignment=TA_RIGHT)

    s, c, t = inv.settings, inv.customer, inv.totals
    P = lambda txt, st=base: Paragraph(xesc(str(txt)).replace("\n", "<br/>"), st)  # noqa: E731

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"Invoice {inv.number}", author=s.business_name)
    story = []

    biz = f"<font name='{bold}' size='12'>{xesc(s.business_name)}</font><br/>{xesc(s.business_address)}<br/>{xesc(s.business_email)}"
    if s.business_tax_id:
        biz += f"<br/>{xesc(s.tax_label)} No: {xesc(s.business_tax_id)}"
    meta = (f"<font name='{bold}'>{xesc(inv.number)}</font><br/>Issued: {inv.issue_date:%d %b %Y}"
            f"<br/>Due: {inv.due_date:%d %b %Y}")
    head = Table([[Paragraph(biz, base), [Paragraph("INVOICE", title), Paragraph(meta, right)]]],
                 colWidths=[95 * mm, 79 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, 0), 1.6, accent),
                              ("BOTTOMPADDING", (0, 0), (-1, 0), 8), ("LEFTPADDING", (0, 0), (0, 0), 0),
                              ("RIGHTPADDING", (-1, 0), (-1, 0), 0)]))
    story += [head, Spacer(1, 8)]

    who = "\n".join(x for x in (c.name, c.company, c.email, c.phone) if x) or "Customer details missing"
    story += [P("BILL TO", small), P(who), Spacer(1, 10)]

    data = [[Paragraph("#", hdr), Paragraph("Description", hdr), Paragraph("Qty", hdr_r),
             Paragraph("Unit", hdr), Paragraph("Rate", hdr_r), Paragraph("Amount", hdr_r)]]
    for i, l in enumerate((x for x in inv.lines if x.resolved), 1):
        desc = f"<font name='{bold}'>{xesc(l.name)}</font>"
        if l.description:
            desc += f"<br/><font size='7.5' color='#5a6b7b'>{xesc(l.description)}</font>"
        data.append([P(i), Paragraph(desc, base), P(fmt_qty(l.quantity), right), P(l.unit),
                     P(money(l.unit_price), right), P(money(l.amount), right)])
    items = Table(data, colWidths=[9 * mm, 71 * mm, 15 * mm, 18 * mm, 28 * mm, 33 * mm], repeatRows=1)
    items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), accent), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor("#d5dde5")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f9fc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [items, Spacer(1, 8)]

    tot = [[P("Subtotal", right), P(money(t.subtotal), right)]]
    if t.discount:
        tot.append([P(f"Discount ({s.discount_pct:g}%)", right), P("- " + money(t.discount), right)])
    tot.append([P(f"{s.tax_label} ({s.tax_rate:g}%)", right), P(money(t.tax), right)])
    grand = ParagraphStyle("g", parent=right, fontName=bold, fontSize=11, textColor=accent)
    tot.append([Paragraph("Total", grand), Paragraph(money(t.total), grand)])
    totals = Table(tot, colWidths=[40 * mm, 34 * mm], hAlign="RIGHT")
    totals.setStyle(TableStyle([("LINEABOVE", (0, -1), (-1, -1), 1.2, accent),
                                ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story += [totals, Spacer(1, 12)]

    if inv.notes.strip():
        story += [P("NOTES", small), P(inv.notes), Spacer(1, 8)]
    story += [P("PAYMENT TERMS", small), P(f"Due within {s.payment_terms_days} days. {s.payment_info}"), Spacer(1, 14)]
    story += [P(f"Approved by {inv.approval.reviewer} on {inv.approval.approved_at.replace('T', ' ')}  |  "
                f"Ref {inv.approval.fingerprint[:10]}", small)]

    doc.build(story)
    return buf.getvalue()


# ================================================================== WhatsApp share


def whatsapp_link(inv: Invoice) -> str:
    """Click-to-chat link pre-filled with an invoice summary (opens WhatsApp; user presses send)."""
    t = inv.totals
    lines = [f"Hello {inv.customer.name or 'there'},",
             f"Your invoice {inv.number} from {inv.settings.business_name} is ready.", ""]
    for l in (x for x in inv.lines if x.resolved):
        lines.append(f"- {l.name} x {fmt_qty(l.quantity)}: {fmt_money(l.amount, inv.settings.currency, True)}")
    lines += ["", f"Total (incl. {inv.settings.tax_label} {inv.settings.tax_rate:g}%): "
                  f"{fmt_money(t.total, inv.settings.currency, True)}",
              f"Due by {inv.due_date:%d %b %Y}."]
    phone = re.sub(r"\D", "", inv.customer.phone or "")
    if inv.settings.currency == "INR" and len(phone) == 10:
        phone = "91" + phone
    return f"https://wa.me/{phone}?text={quote(chr(10).join(lines))}"
