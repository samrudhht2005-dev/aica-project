"""Printable weigh-ticket label: QR PNG + FPDF label (server-side)."""
from __future__ import annotations

import io
import tempfile
from datetime import datetime
from pathlib import Path

import qrcode
from fpdf import FPDF

from models.db_models import WeighTicket


def qr_png_bytes(payload: str, *, box_size: int = 8, border: int = 2) -> bytes:
    """Encode opaque ticket token as a PNG QR image."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _pdf_text(value) -> str:
    text_val = "" if value is None else str(value)
    return text_val.encode("latin-1", "replace").decode("latin-1")


def _fmt_money(value: float) -> str:
    return f"Rs {float(value or 0):.2f}"


def _fmt_weight(weight: float, unit: str) -> str:
    w = float(weight or 0)
    if w == int(w):
        w_str = str(int(w))
    else:
        w_str = f"{w:.3f}".rstrip("0").rstrip(".")
    return f"{w_str} {unit or 'kg'}"


def create_weigh_label_pdf(ticket: WeighTicket) -> bytes:
    """
    Single-page printable label (A5-ish content on A4) with QR + ticket facts.
    Label text uses server snapshots; QR encodes public_token only.
    """
    payload = ticket.public_token
    png = qr_png_bytes(payload, box_size=10, border=2)

    created = ticket.created_at or datetime.utcnow()
    created_str = created.strftime("%Y-%m-%d %H:%M")
    token_suffix = payload[-10:] if len(payload) > 10 else payload

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()

    # Label frame (~100mm x 140mm) suitable for normal printer / cut-out.
    left, top, width, height = 55, 30, 100, 140
    pdf.set_draw_color(30, 30, 30)
    pdf.set_line_width(0.4)
    pdf.rect(left, top, width, height)

    pdf.set_xy(left, top + 6)
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(width, 8, "AICA", ln=1, align="C")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_x(left)
    pdf.cell(width, 6, "Weigh Ticket Label", ln=1, align="C")

    pdf.set_font("Helvetica", "B", 11)
    pdf.set_xy(left + 6, top + 24)
    pdf.cell(width - 12, 7, _pdf_text(f"Product: {ticket.product_name_snapshot}"), ln=1)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_x(left + 6)
    pdf.cell(width - 12, 6, _pdf_text(f"Weight: {_fmt_weight(ticket.weight, ticket.unit)}"), ln=1)
    pdf.set_x(left + 6)
    pdf.cell(width - 12, 6, _pdf_text(f"Unit price: {_fmt_money(ticket.unit_price_snapshot)}"), ln=1)
    pdf.set_x(left + 6)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(width - 12, 7, _pdf_text(f"Total: {_fmt_money(ticket.total_amount_snapshot)}"), ln=1)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_x(left + 6)
    pdf.cell(width - 12, 6, _pdf_text(f"Status: {(ticket.status or '').upper()}"), ln=1)
    pdf.set_x(left + 6)
    pdf.cell(width - 12, 6, _pdf_text(f"Generated: {created_str}"), ln=1)
    pdf.set_x(left + 6)
    pdf.cell(width - 12, 6, _pdf_text(f"Ref: ...{token_suffix}"), ln=1)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(png)
        tmp_path = tmp.name
    try:
        qr_w = 55
        qr_x = left + (width - qr_w) / 2
        qr_y = top + 78
        pdf.image(tmp_path, x=qr_x, y=qr_y, w=qr_w, h=qr_w)
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass

    pdf.set_xy(left, top + height - 12)
    pdf.set_font("Helvetica", "I", 7)
    pdf.cell(width, 5, "Scan at PoS. Price/weight verified by AICA server.", align="C")

    raw = pdf.output(dest="S")
    if isinstance(raw, str):
        return raw.encode("latin-1")
    return bytes(raw)
