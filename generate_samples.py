"""
Generates the sample invoice PDFs used for the demo.

Run:  python generate_samples.py
Output: samples/*.pdf

Two render paths:
  - text PDFs (reportlab)  -> machine-readable text layer, like a vendor's accounting system export
  - scanned PDF (PIL)      -> image only, no text layer, like a photocopied / scanned invoice
"""
from __future__ import annotations

import io
import os
import random
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "samples")
os.makedirs(OUT, exist_ok=True)

BUYER = ["Meridian Manufacturing Pvt Ltd", "Plot 14, MIDC Industrial Area", "Pune 411019, Maharashtra, India", "GSTIN: 27AADCM9876L1ZQ"]


@dataclass
class Item:
    description: str
    qty: float
    unit_price: float

    @property
    def amount(self) -> float:
        return round(self.qty * self.unit_price, 2)


@dataclass
class Invoice:
    file_name: str
    vendor_name: str
    vendor_address: list[str]
    vendor_tax_label: str
    vendor_tax_id: str
    invoice_number: str | None
    invoice_date: str | None
    due_date: str | None
    po_reference: str | None
    currency: str
    items: list[Item]
    tax_label: str
    tax_rate: float  # percent
    banner: str | None = None
    notes: list[str] = field(default_factory=list)
    scanned: bool = False
    total_override: float | None = None

    @property
    def subtotal(self) -> float:
        return round(sum(i.amount for i in self.items), 2)

    @property
    def tax(self) -> float:
        return round(self.subtotal * self.tax_rate / 100, 2)

    @property
    def total(self) -> float:
        if self.total_override is not None:
            return self.total_override
        return round(self.subtotal + self.tax, 2)


def money(v: float, cur: str) -> str:
    sym = {"INR": "Rs. ", "USD": "$ ", "EUR": "EUR "}.get(cur, cur + " ")
    return f"{sym}{v:,.2f}"


# --------------------------------------------------------------------------- text PDF
def render_text_pdf(inv: Invoice, path: str) -> None:
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm)
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=20, alignment=0, spaceAfter=2)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=8.5, leading=11, textColor=colors.HexColor("#444444"))
    normal = ParagraphStyle("n", parent=ss["Normal"], fontSize=9.5, leading=12)
    bold = ParagraphStyle("b", parent=normal, fontName="Helvetica-Bold")
    banner_style = ParagraphStyle("ban", parent=normal, fontName="Helvetica-Bold", textColor=colors.HexColor("#b00020"), fontSize=11)

    story = []
    if inv.banner:
        story.append(Paragraph(inv.banner, banner_style))
        story.append(Spacer(1, 4))
    story.append(Paragraph("TAX INVOICE" if inv.tax_rate else "INVOICE", h1))
    story.append(Paragraph(f"<b>{inv.vendor_name}</b>", normal))
    for line in inv.vendor_address:
        story.append(Paragraph(line, small))
    story.append(Paragraph(f"{inv.vendor_tax_label}: {inv.vendor_tax_id}", small))
    story.append(Spacer(1, 10))

    meta_left = [
        ["Invoice No:", inv.invoice_number or ""],
        ["Invoice Date:", inv.invoice_date or ""],
        ["Due Date:", inv.due_date or ""],
        ["PO Reference:", inv.po_reference or ""],
        ["Currency:", inv.currency],
    ]
    # drop empty rows (simulates vendors that just omit fields)
    meta_left = [r for r in meta_left if r[1]]
    bill_to = [Paragraph("<b>Bill To</b>", normal)] + [Paragraph(l, small) for l in BUYER]
    meta_tbl = Table([[Table(meta_left, colWidths=[30 * mm, 55 * mm], style=TableStyle([("FONTSIZE", (0, 0), (-1, -1), 9), ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)])), bill_to]], colWidths=[95 * mm, 80 * mm])
    meta_tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(meta_tbl)
    story.append(Spacer(1, 12))

    rows = [["#", "Description", "Qty", "Unit Price", "Amount"]]
    for i, it in enumerate(inv.items, 1):
        rows.append([str(i), it.description, f"{it.qty:g}", money(it.unit_price, inv.currency), money(it.amount, inv.currency)])
    rows.append(["", "", "", "Subtotal", money(inv.subtotal, inv.currency)])
    if inv.tax_rate:
        rows.append(["", "", "", f"{inv.tax_label} ({inv.tax_rate:g}%)", money(inv.tax, inv.currency)])
    rows.append(["", "", "", "Total Due", money(inv.total, inv.currency)])
    t = Table(rows, colWidths=[10 * mm, 85 * mm, 18 * mm, 30 * mm, 32 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, len(inv.items)), 0.4, colors.HexColor("#cccccc")),
        ("FONTNAME", (3, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (3, -1), (-1, -1), 1, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t)
    story.append(Spacer(1, 14))
    for n in inv.notes:
        story.append(Paragraph(n, small))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Bank: HDFC Bank, A/c 50200012345678, IFSC HDFC0001234. Please quote the invoice number on remittance.", small))
    doc.build(story)


# --------------------------------------------------------------------------- scanned PDF
def _font(size: int, bold: bool = False):
    for name in (["arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold else ["arial.ttf", "DejaVuSans.ttf"]):
        for folder in ("C:/Windows/Fonts", "/usr/share/fonts/truetype/dejavu", "/Library/Fonts"):
            p = os.path.join(folder, name)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def render_scanned_pdf(inv: Invoice, path: str) -> None:
    W, H = 1240, 1754  # A4 @ 150 dpi
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f_h1, f_b, f_n, f_s = _font(40, True), _font(22, True), _font(21), _font(18)
    y = 90
    if inv.banner:
        d.text((90, y), inv.banner, fill=(170, 0, 30), font=f_b); y += 40
    d.text((90, y), "TAX INVOICE", fill="black", font=f_h1); y += 60
    d.text((90, y), inv.vendor_name, fill="black", font=f_b); y += 32
    for line in inv.vendor_address + [f"{inv.vendor_tax_label}: {inv.vendor_tax_id}"]:
        d.text((90, y), line, fill=(60, 60, 60), font=f_s); y += 26
    y += 20
    meta = [("Invoice No:", inv.invoice_number), ("Invoice Date:", inv.invoice_date), ("Due Date:", inv.due_date), ("PO Reference:", inv.po_reference), ("Currency:", inv.currency)]
    yy = y
    for k, v in meta:
        if v:
            d.text((90, yy), k, fill="black", font=f_b); d.text((300, yy), v, fill="black", font=f_n); yy += 30
    d.text((760, y), "Bill To", fill="black", font=f_b)
    for i, line in enumerate(BUYER):
        d.text((760, y + 30 + i * 26), line, fill=(60, 60, 60), font=f_s)
    y = max(yy, y + 30 + len(BUYER) * 26) + 30
    # table
    cols = [90, 140, 720, 820, 980, 1150]
    d.rectangle([90, y, 1150, y + 36], fill=(31, 41, 55))
    for x, h in zip(cols[:-1], ["#", "Description", "Qty", "Unit Price", "Amount"]):
        d.text((x + 8, y + 6), h, fill="white", font=f_b)
    y += 36
    for i, it in enumerate(inv.items, 1):
        d.rectangle([90, y, 1150, y + 36], outline=(190, 190, 190))
        vals = [str(i), it.description, f"{it.qty:g}", money(it.unit_price, inv.currency), money(it.amount, inv.currency)]
        for x, v in zip(cols[:-1], vals):
            d.text((x + 8, y + 7), v, fill="black", font=f_n)
        y += 36
    y += 10
    totals = [("Subtotal", inv.subtotal)]
    if inv.tax_rate:
        totals.append((f"{inv.tax_label} ({inv.tax_rate:g}%)", inv.tax))
    totals.append(("Total Due", inv.total))
    for label, v in totals:
        fnt = f_b if label == "Total Due" else f_n
        d.text((820, y), label, fill="black", font=fnt); d.text((990, y), money(v, inv.currency), fill="black", font=fnt); y += 32
    y += 24
    for n in inv.notes:
        d.text((90, y), n, fill=(60, 60, 60), font=f_s); y += 26
    d.text((90, y + 10), "Bank: ICICI Bank, A/c 002105501234, IFSC ICIC0000021. Quote invoice number on remittance.", fill=(60, 60, 60), font=f_s)
    # stamp
    d.ellipse([900, 1450, 1120, 1600], outline=(40, 70, 160), width=4)
    d.text((930, 1505), "RECEIVED", fill=(40, 70, 160), font=f_b)

    # scanner artefacts: slight rotation, noise, blur, grey cast, jpeg compression
    img = img.rotate(0.7, resample=Image.BICUBIC, expand=False, fillcolor="white")
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    px = img.load()
    rnd = random.Random(7)
    for _ in range(18000):
        x, y2 = rnd.randrange(W), rnd.randrange(H)
        g = rnd.randrange(120, 230)
        px[x, y2] = (g, g, g)
    img = img.convert("L").point(lambda p: int(p * 0.92 + 10))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=55)
    buf.seek(0)

    c = rl_canvas.Canvas(path, pagesize=A4)
    from reportlab.lib.utils import ImageReader
    c.drawImage(ImageReader(buf), 0, 0, width=A4[0], height=A4[1])
    c.showPage()
    c.save()


# --------------------------------------------------------------------------- sample set
ACME = ("Acme Industrial Supplies Pvt Ltd", ["Gat No. 221, Chakan Industrial Estate", "Pune 410501, Maharashtra, India"], "GSTIN", "27AAACA1234F1Z5")
NORTHWIND = ("Northwind Logistics Ltd", ["Warehouse 7, Bhiwandi Logistics Park", "Thane 421302, Maharashtra, India"], "GSTIN", "29AABCN5678G1Z2")
GLOBEX = ("Globex Technologies Inc", ["500 Market Street, Suite 1200", "San Francisco, CA 94105, USA"], "EIN", "45-1234567")
INITECH = ("Initech Software Services LLP", ["Tower B, 4th Floor, Cyber Hub", "Gurugram 122002, Haryana, India"], "GSTIN", "07AAFFI9012H1Z8")
SHADY = ("Shady Ventures Trading Co", ["Shop 3, Unknown Lane", "Mumbai 400001, India"], "GSTIN", "27ZZZZZ0000Z1Z9")

SAMPLES: list[Invoice] = [
    Invoice("01_happy_path_acme.pdf", *ACME, "INV-ACM-2041", "2026-10-28", "2026-11-27", "PO-2026-0101", "INR",
            [Item("Hex bolts M12 x 60, zinc plated", 2500, 18.50), Item("Stainless steel pipe fittings 1/2 in", 800, 92.40), Item("Gasket sheets 3mm, 1m x 1m", 120, 300.00)],
            "GST", 18, notes=["Goods delivered against PO-2026-0101 on 2026-10-26. Delivery challan DC-7781."]),

    Invoice("02_scanned_northwind.pdf", *NORTHWIND, "INV-NW-7731", "2026-10-05", "2026-11-19", "PO-2026-0102", "INR",
            [Item("Freight Mumbai to Pune, 20ft container, Sept 2026 (14 trips)", 14, 4500.00), Item("Loading and unloading charges", 14, 1200.00)],
            "GST", 5, notes=["Goods transport agency services. Reverse charge not applicable."], scanned=True),

    Invoice("03_missing_po_globex.pdf", *GLOBEX, "INV-GLX-00912", "2026-10-30", "2026-11-29", None, "USD",
            [Item("Globex Analytics Platform - annual subscription, 50 seats (Nov 2026 - Oct 2027)", 1, 24000.00)],
            "Tax", 0, notes=["Subscription renewal as per master agreement MSA-2024-118. Auto-renewal notice sent 2026-09-30."]),

    Invoice("04a_split_initech_1of2.pdf", *INITECH, "INV-INT-5101", "2026-10-31", "2026-11-30", "PO-2026-0104", "INR",
            [Item("Development services - Milestone 1 of 2 (design and API build)", 1, 700000.00)],
            "GST", 18, notes=["Milestone 1 accepted by client on 2026-10-29 (acceptance note AN-114)."]),

    Invoice("04b_split_initech_2of2.pdf", *INITECH, "INV-INT-5102", "2026-11-02", "2026-12-02", "PO-2026-0104", "INR",
            [Item("Development services - Milestone 2 of 2 (integration and UAT)", 1, 500000.00)],
            "GST", 18, notes=["Final milestone. PO-2026-0104 fully billed with this invoice."]),

    Invoice("04c_split_initech_overrun.pdf", *INITECH, "INV-INT-5103", "2026-11-03", "2026-12-03", "PO-2026-0104", "INR",
            [Item("Additional change requests - CR-07 to CR-11", 1, 150000.00)],
            "GST", 18, notes=["Change requests discussed verbally with project manager."]),

    Invoice("05_duplicate_acme.pdf", *ACME, "INV-ACM-2041", "2026-10-28", "2026-11-27", "PO-2026-0101", "INR",
            [Item("Hex bolts M12 x 60, zinc plated", 2500, 18.50), Item("Stainless steel pipe fittings 1/2 in", 800, 92.40), Item("Gasket sheets 3mm, 1m x 1m", 120, 300.00)],
            "GST", 18, banner="PAYMENT REMINDER - COPY OF ORIGINAL INVOICE", notes=["Goods delivered against PO-2026-0101 on 2026-10-26. Delivery challan DC-7781.", "This is a reminder copy. Original sent 2026-10-28."]),

    Invoice("06_unknown_vendor.pdf", *SHADY, "INV-SV-0001", "2026-11-01", "2026-11-08", "PO-2026-0101", "INR",
            [Item("Industrial supplies as per PO", 1, 156170.00)],
            "GST", 18, notes=["Urgent: please process within 7 days. Updated bank details: Account 9988776655, IFSC UNKN0000123."]),

    Invoice("07_over_tolerance_acme.pdf", *ACME, "INV-ACM-2058", "2026-11-02", "2026-12-02", "PO-2026-0105", "INR",
            [Item("Safety helmets, ISI marked", 200, 185.00), Item("Cut-resistant gloves, pair", 400, 32.00)],
            "GST", 18, notes=["Quantity increased from 180 to 200 helmets at site request."]),

    Invoice("08_missing_fields_globex.pdf", *GLOBEX, None, None, None, "PO-2026-0107", "USD",
            [Item("Implementation services - Phase 1 (October 2026)", 60, 250.00)],
            "Tax", 0, notes=["Hours as per approved timesheets."]),
]


def main() -> None:
    for inv in SAMPLES:
        path = os.path.join(OUT, inv.file_name)
        (render_scanned_pdf if inv.scanned else render_text_pdf)(inv, path)
        print(f"wrote {inv.file_name:36s} total={money(inv.total, inv.currency)}")


if __name__ == "__main__":
    main()
