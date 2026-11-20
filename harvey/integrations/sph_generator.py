"""Official Quotation (SPH) PDF & Excel/Sheets Generator for PT. Andis Sentral Laboratorium.

Replicates the exact visual layout and structure of `Template Penawaran ANDISLAB.xlsx`:
- Header banner (#D9E2F3) with official ANDIS circular logo & Cibinong address.
- Numbering format: QUO/{seq:03d}.ASL/{MM}-{YYYY}
- Two-column BILL TO / SHIP TO & ATTCH TO / Jabatan / EMAIL / PHONE / DATE / CURRENCY / VALID UNTIL.
- Item table with Brand, Model/Cat.No, and structured Specification key-value rows.
- Summary block: SUB TOTAL, optional Discount, PPN 11%, and shaded TOTAL PRICE.
- Official Term & Conditions + Computerized Signature banner ('This signature is already computerized').
- Page footer: 'PT. Andis Sentral Laboratorium | www.andislabs.com' & 'BANK INFORMATION BSI 7316536489'.
- Hybrid Sync: Appends to `Template Penawaran ANDISLAB.xlsx` & `data/Rekap_Master_SPH_AndisLab.xlsx` (+ Google Sheets CLI when signed in).
"""

from __future__ import annotations

import copy
import datetime
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import aiosqlite
import openpyxl
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image as RLImage,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.sph")

SPH_ARCHIVE_DIR = PROJECT_ROOT / "data" / "sph_archives"
LOGO_PATH = PROJECT_ROOT / "data" / "assets" / "sph" / "andislab_logo.png"
PRODUCTS_IMG_DIR = PROJECT_ROOT / "data" / "assets" / "sph" / "products"
TEMPLATE_XLSX_PATH = PROJECT_ROOT / "Template Penawaran ANDISLAB.xlsx"
MASTER_REKAP_XLSX_PATH = PROJECT_ROOT / "data" / "Rekap_Master_SPH_AndisLab.xlsx"
DB_PATH = PROJECT_ROOT / "data" / "harvey.db"

_IMG_SYNONYMS = {
    "timbangan": ["balance", "analytical"],
    "neraca": ["balance", "analytical"],
    "penangas": ["water", "bath"],
    "waterbath": ["water", "bath"],
    "labu ukur": ["volumetric", "flask"],
    "gelas ukur": ["measuring", "cylinder"],
    "gelas beker": ["beaker"],
    "gelas piala": ["beaker"],
    "cawan petri": ["petri", "dish"],
    "mikropipet": ["micropette", "micropipette", "pipette"],
    "pipet ukur": ["measuring", "pipette", "pipet"],
    "spektrofotometer": ["spectrophotometer", "uv", "vis"],
    "autoklaf": ["autoclave"],
    "inkubator": ["incubator"],
    "lemari asam": ["flammable", "laminar"],
    "mikroskop": ["microscope"],
}


_BRAND_TOKENS = {
    "aczet", "radwag", "thermo", "scientific", "memmert", "iwaki", "normax",
    "dlab", "hanna", "instrument", "instruments", "daihan", "labtech", "yamato",
    "hirayama", "longyue", "aelab", "isolab", "komagome", "milwaukee", "infitek",
    "labex", "value", "teco", "andislab", "bel", "art", "lovibond", "ohaus",
    "mettler", "toledo", "sartorius", "shimadzu", "eppendorf", "corning", "pyrex",
}


def resolve_product_image(item: dict[str, Any], fetch_web_if_missing: bool = True) -> Path | None:
    """Resolve the exact product photo for an SPH item from local library or web fallback."""
    PRODUCTS_IMG_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Explicit path
    explicit = item.get("image_path")
    if explicit and Path(explicit).exists():
        return Path(explicit)

    name = str(item.get("name") or item.get("canonical_name") or "").lower()
    brand = str(item.get("brand") or "").lower()
    model = str(item.get("model") or item.get("catalog_no") or "").lower()
    combined = f"{name} {brand} {model}"

    # Expand synonyms
    extra_tokens: set[str] = set()
    for indo_k, eng_list in _IMG_SYNONYMS.items():
        if indo_k in combined:
            extra_tokens.update(eng_list)

    raw_tokens = set(re.findall(r"[a-z0-9]{2,}", combined)) | extra_tokens
    stop_words = {"standard", "unit", "with", "for", "and", "the", "alat", "lab", "laboratory", "plus", "pro", "series"}
    tokens = {t for t in raw_tokens if t not in stop_words}
    item_brands = tokens & _BRAND_TOKENS
    model_tokens = {t for t in re.findall(r"[a-z0-9]{2,}", model) if t not in stop_words and t not in _BRAND_TOKENS}
    item_type_tokens = tokens - _BRAND_TOKENS - model_tokens
    model_clean = re.sub(r"[^a-z0-9]", "", model)

    best_path: Path | None = None
    best_score = 0.0

    for img_p in PRODUCTS_IMG_DIR.iterdir():
        if img_p.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        fname = img_p.stem.lower()
        if fname in ("logo andis", "andislabcustom", "reagen dan glassware"):
            continue
        f_tokens = {t for t in re.findall(r"[a-z0-9]{2,}", fname) if t not in stop_words}
        f_brands = f_tokens & _BRAND_TOKENS
        f_type_tokens = f_tokens - _BRAND_TOKENS
        fname_clean = re.sub(r"[^a-z0-9]", "", fname)

        # Rule 1: If both item and file specify known brands, they must not conflict
        if item_brands and f_brands and not (item_brands & f_brands):
            continue

        # Rule 2: Must share at least one core product-type token (prevents matching different instruments of same brand)
        type_overlap = item_type_tokens & f_type_tokens
        if item_type_tokens and not type_overlap:
            continue

        score = len(type_overlap) * 3.0
        if item_brands & f_brands:
            score += 3.0

        # Rule 3: Model check
        if model_clean and len(model_clean) >= 3:
            if model_clean in fname_clean:
                score += 15.0
            else:
                # If the item has a specific model code not present in this local file,
                # skip local file so we can fetch or match the exact model photo
                continue

        if score > best_score:
            best_score = score
            best_path = img_p

    if best_path and best_score >= 5.0:
        return best_path

    # 2. Web Image Fallback (Bing Image Search for exact Brand + Model + Product Name)
    if fetch_web_if_missing and (name or model):
        try:
            import httpx
            q_parts = [p for p in [brand, model, name] if p and p not in ("andislab standard", "-")]
            q_str = " ".join(q_parts).strip()
            slug = re.sub(r"[^a-z0-9]+", "_", f"{name}_{brand}_{model}".lower()).strip("_")[:55] or "lab_item"
            target_file = PRODUCTS_IMG_DIR / f"{slug}.jpg"
            if target_file.exists() and target_file.stat().st_size > 1500:
                return target_file

            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            with httpx.Client(timeout=6.0, follow_redirects=True, headers=headers) as client:
                r = client.get("https://www.bing.com/images/search", params={"q": q_str, "first": "1"})
                if r.status_code == 200:
                    m_urls = re.findall(r"https://tse\d\.mm\.bing\.net/th\?id=OIP\.[A-Za-z0-9_\-]+", r.text)
                    for img_url in m_urls[:3]:
                        ir = client.get(f"{img_url}&w=400&h=400&c=7&p=0")
                        if ir.status_code == 200 and len(ir.content) > 1500:
                            target_file.write_bytes(ir.content)
                            return target_file
        except Exception as e:
            logger.debug(f"SPHGenerator: Web image fetch skipped for '{name}': {e}")

    return best_path

# Corporate Palette from `Template Penawaran ANDISLAB.xlsx`
COLOR_HEADER_BG = colors.HexColor("#D9E2F3")
COLOR_PURPLE_LINE = colors.HexColor("#5B4B8A")
COLOR_LINK_BLUE = colors.HexColor("#2F5597")
COLOR_TEXT_DARK = colors.HexColor("#000000")


def format_idr(amount: float | int) -> str:
    """Format number as Indonesian Rupiah number string with dots (e.g. '19.650.000')."""
    val = int(round(float(amount or 0)))
    return f"{val:,}".replace(",", ".")


def _terbilang_core(n: int) -> str:
    satuan = [
        "", "Satu", "Dua", "Tiga", "Empat", "Lima",
        "Enam", "Tujuh", "Delapan", "Sembilan", "Sepuluh", "Sebelas"
    ]
    if n < 12:
        return satuan[n]
    if n < 20:
        return _terbilang_core(n - 10) + " Belas"
    if n < 100:
        hasil = _terbilang_core(n // 10) + " Puluh"
        if n % 10 > 0:
            hasil += " " + _terbilang_core(n % 10)
        return hasil
    if n < 200:
        hasil = "Seratus"
        if n - 100 > 0:
            hasil += " " + _terbilang_core(n - 100)
        return hasil
    if n < 1000:
        hasil = _terbilang_core(n // 100) + " Ratus"
        if n % 100 > 0:
            hasil += " " + _terbilang_core(n % 100)
        return hasil
    if n < 2000:
        hasil = "Seribu"
        if n - 1000 > 0:
            hasil += " " + _terbilang_core(n - 1000)
        return hasil
    if n < 1_000_000:
        hasil = _terbilang_core(n // 1000) + " Ribu"
        if n % 1000 > 0:
            hasil += " " + _terbilang_core(n % 1000)
        return hasil
    if n < 1_000_000_000:
        hasil = _terbilang_core(n // 1_000_000) + " Juta"
        if n % 1_000_000 > 0:
            hasil += " " + _terbilang_core(n % 1_000_000)
        return hasil
    if n < 1_000_000_000_000:
        hasil = _terbilang_core(n // 1_000_000_000) + " Miliar"
        if n % 1_000_000_000 > 0:
            hasil += " " + _terbilang_core(n % 1_000_000_000)
        return hasil
    return str(n)


def terbilang_rupiah(nominal: float | int) -> str:
    """Convert numeric Rupiah value to formal Indonesian words."""
    n = int(round(float(nominal or 0)))
    if n <= 0:
        return "Nol Rupiah"
    return f"{_terbilang_core(n).strip()} Rupiah"


def parse_specifications(spec_input: str | dict | list | None) -> list[tuple[str, str]]:
    """Parse raw specification string/dict/list into structured (Key, Value) pairs."""
    if not spec_input:
        return [("Specification", "Standard Laboratory Grade")]

    if isinstance(spec_input, dict):
        return [(str(k).strip(), str(v).strip()) for k, v in spec_input.items() if v]

    if isinstance(spec_input, list):
        pairs = []
        for idx, entry in enumerate(spec_input, 1):
            if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                pairs.append((str(entry[0]).strip(), str(entry[1]).strip()))
            else:
                s = str(entry).strip()
                if ":" in s:
                    k, v = s.split(":", 1)
                    pairs.append((k.strip(), v.strip()))
                elif s:
                    pairs.append((f"Feature {idx}", s))
        return pairs or [("Specification", "Standard Laboratory Grade")]

    raw = str(spec_input).strip()
    parts = [p.strip() for p in re.split(r"[\|\n;]+", raw) if p.strip()]
    if len(parts) == 1 and "," in raw and ":" in raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]

    pairs: list[tuple[str, str]] = []
    for idx, p in enumerate(parts, 1):
        if ":" in p:
            k, v = p.split(":", 1)
            pairs.append((k.strip(), v.strip()))
        else:
            # Infer smart key name if none provided
            p_low = p.lower()
            if any(u in p_low for u in ["rpm", "speed"]):
                pairs.append(("Speed Range", p))
            elif any(u in p_low for u in ["°c", "temp", "rt+"]):
                pairs.append(("Temperature Range", p))
            elif any(u in p_low for u in ["liter", "ml", "ul", "µl", "capacity", "kapasitas", "gram", "kg"]):
                pairs.append(("Capacity / Range", p))
            elif any(u in p_low for u in ["volt", "220v", "watt", "hz"]):
                pairs.append(("Power Supply", p))
            elif any(u in p_low for u in ["mm", "cm", "dimensi", "size"]):
                pairs.append(("Dimensions", p))
            elif idx == 1:
                pairs.append(("Main Feature", p))
            else:
                pairs.append((f"Detail {idx}", p))

    return pairs if pairs else [("Specification", raw)]


def _draw_page_footer(canvas, doc):
    """Draw the exact bottom footer from Template Penawaran ANDISLAB on every page."""
    canvas.saveState()
    canvas.setFont("Helvetica", 8.5)
    canvas.setFillColor(COLOR_TEXT_DARK)
    y_pos = 12 * mm
    canvas.drawString(15 * mm, y_pos, "PT. Andis Sentral Laboratorium | www.andislabs.com")
    canvas.drawRightString(A4[0] - 15 * mm, y_pos, "BANK INFORMATION BSI 7316536489")
    canvas.restoreState()


class SPHGenerator:
    """Generates official PT. Andis Sentral Laboratorium PDF & Excel Quotations."""

    def __init__(self, db_path: Path | str = DB_PATH, archive_dir: Path | str = SPH_ARCHIVE_DIR):
        self.db_path = Path(db_path)
        self.archive_dir = Path(archive_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    async def get_next_quotation_number(self, now: datetime.date | None = None) -> str:
        """Generate sequential quotation number: QUO/{seq:03d}.ASL/{MM}-{YYYY}."""
        d = now or datetime.date.today()
        seq = 23  # Start after existing QUO/022 in template
        if self.db_path.exists():
            try:
                async with aiosqlite.connect(self.db_path) as db:
                    await db.execute("""
                        CREATE TABLE IF NOT EXISTS quotes (
                            id TEXT PRIMARY KEY,
                            prospect_id TEXT,
                            company TEXT NOT NULL,
                            contact_name TEXT,
                            phone TEXT,
                            email TEXT,
                            items_summary TEXT,
                            total_amount REAL DEFAULT 0,
                            sent_date TEXT NOT NULL,
                            status TEXT DEFAULT 'pending',
                            stage INTEGER DEFAULT 0,
                            last_followup_at TEXT,
                            notes TEXT
                        )
                    """)
                    async with db.execute("SELECT COUNT(*) FROM quotes") as cur:
                        row = await cur.fetchone()
                        if row and row[0]:
                            seq = max(seq, int(row[0]) + 23)
            except Exception as e:
                logger.debug(f"SPHGenerator: DB sequence lookup fallback: {e}")

        return f"QUO/{seq:03d}.ASL/{d.month:02d}-{d.year}"

    def render_pdf(
        self,
        quotation_no: str,
        company_name: str,
        contact_name: str = "Tim Pengadaan / Laboratorium",
        contact_title: str = "Laboratory / Procurement",
        address: str = "Indonesia",
        email: str = "-",
        phone: str = "-",
        items: list[dict[str, Any]] | None = None,
        discount_pct: float = 0.0,
        include_ppn: bool = True,
        validity_days: int = 30,
        payment_terms: str = "50% Down Payment and 50% Full Payment After Delivery",
        stock_note: str = "Ready Stock Tidak Terikat",
        price_delivery_note: str = "Franco Jabodetabek Include Delivery",
        sales_name: str = "Ahnaf",
        sales_title: str = "Sales Executive",
        sales_phone: str = "0819-9157-5096",
        output_filename: str | None = None,
    ) -> dict[str, Any]:
        """Render a PDF matching `Template Penawaran ANDISLAB` and return metadata + path."""
        items = items or []
        today = datetime.date.today()
        date_str = today.strftime("%d-%b-%y")

        safe_comp = re.sub(r"[^A-Za-z0-9_-]+", "_", (company_name or "Client").strip()).strip("_")[:35]
        safe_quo = re.sub(r"[^A-Za-z0-9_-]+", "_", quotation_no)
        fname = output_filename or f"SPH_AndisLab_{safe_quo}_{safe_comp}.pdf"
        if not fname.lower().endswith(".pdf"):
            fname += ".pdf"
        pdf_path = self.archive_dir / fname

        doc = SimpleDocTemplate(
            str(pdf_path),
            pagesize=A4,
            leftMargin=15 * mm,
            rightMargin=15 * mm,
            topMargin=12 * mm,
            bottomMargin=20 * mm,
            title=f"Quotation {quotation_no} - {company_name}",
            author="PT. Andis Sentral Laboratorium",
        )

        usable_w = A4[0] - 30 * mm  # 180 mm
        styles = getSampleStyleSheet()

        style_header_title = ParagraphStyle(
            "HeaderTitle",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            alignment=1,  # Center
            textColor=COLOR_TEXT_DARK,
        )
        style_header_addr = ParagraphStyle(
            "HeaderAddr",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=12.5,
            alignment=1,
            textColor=COLOR_TEXT_DARK,
        )
        style_quo_title = ParagraphStyle(
            "QuoTitle",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=13,
            alignment=1,
            textColor=COLOR_TEXT_DARK,
        )
        style_normal = ParagraphStyle(
            "CellNormal",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=11,
            textColor=COLOR_TEXT_DARK,
        )
        style_bold = ParagraphStyle(
            "CellBold",
            parent=style_normal,
            fontName="Helvetica-Bold",
        )
        style_bold_center = ParagraphStyle(
            "CellBoldCenter",
            parent=style_bold,
            alignment=1,
        )
        style_spec_hdr = ParagraphStyle(
            "SpecHdr",
            parent=style_normal,
            fontName="Helvetica",
            fontSize=8.5,
            alignment=1,
        )
        style_italic_bold = ParagraphStyle(
            "ItalicBold",
            parent=style_normal,
            fontName="Helvetica-BoldOblique",
            fontSize=8.5,
            leading=11,
        )
        style_italic = ParagraphStyle(
            "ItalicText",
            parent=style_normal,
            fontName="Helvetica-Oblique",
            fontSize=8.5,
            leading=11,
        )
        style_sig_banner = ParagraphStyle(
            "SigBanner",
            parent=style_normal,
            fontName="Helvetica-BoldOblique",
            fontSize=9,
            leading=12,
            alignment=1,
        )

        story = []

        # ── 1. HEADER BANNER (Logo + Company Name & Address) ──
        if LOGO_PATH.exists():
            logo_img = RLImage(str(LOGO_PATH), width=20 * mm, height=20 * mm)
        else:
            logo_img = Paragraph("<b>ANDIS</b>", style_bold_center)

        header_text_block = [
            Paragraph("PT. ANDIS SENTRAL LABORATORIUM", style_header_title),
            Spacer(1, 1.5 * mm),
            Paragraph(
                "Jl. Raya mayor oking jaya atmaja. 122 Cibinong komplek ruko mayor<br/>"
                "oking 2, Blok A no. 5, Kabupaten Bogor, Jawa Barat 16017, www.andislab.com",
                style_header_addr,
            ),
        ]

        header_table = Table(
            [[logo_img, header_text_block, ""]],
            colWidths=[28 * mm, usable_w - 56 * mm, 28 * mm],
        )
        header_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_HEADER_BG),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (0, 0), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LINEBELOW", (0, 0), (-1, 0), 1.5, COLOR_PURPLE_LINE),
            ])
        )
        story.append(header_table)
        story.append(Spacer(1, 3 * mm))

        # ── 2. QUOTATION NUMBER TITLE ──
        story.append(Paragraph(f"<u>QUOTATION : {quotation_no}</u>", style_quo_title))
        story.append(Spacer(1, 4 * mm))

        # ── 3. BILL TO / SHIP TO & ATTCH TO METADATA ──
        email_display = (
            f'<font color="#2F5597">{email}</font>' if email and "@" in email else (email or "-")
        )
        meta_rows = [
            [
                Paragraph("<b>BILL TO :</b>", style_bold),
                "",
                Paragraph("<b>ATTCH TO.</b>", style_bold),
                Paragraph("<b>:</b>", style_bold),
                Paragraph(f"<b>{contact_name or '-'}</b>", style_bold),
            ],
            [
                Paragraph(f"<b>{company_name}</b>", style_bold),
                "",
                Paragraph("<b>Jabatan</b>", style_bold),
                Paragraph("<b>:</b>", style_bold),
                Paragraph(f"<b>{contact_title or 'Tim Pengadaan / Lab'}</b>", style_bold),
            ],
            [
                Paragraph(address or "-", style_normal),
                "",
                Paragraph("<b>EMAIL</b>", style_bold),
                Paragraph("<b>:</b>", style_bold),
                Paragraph(email_display, style_normal),
            ],
            [
                "",
                "",
                Paragraph("<b>PHONE</b>", style_bold),
                Paragraph("<b>:</b>", style_bold),
                Paragraph(str(phone or "-"), style_normal),
            ],
            [
                "",
                "",
                Paragraph("<b>DATE</b>", style_bold),
                Paragraph("<b>:</b>", style_bold),
                Paragraph(date_str, style_normal),
            ],
            [
                Paragraph("<b>SHIP TO :</b>", style_bold),
                "",
                Paragraph("<b>CURRENCY</b>", style_bold),
                Paragraph("<b>:</b>", style_bold),
                Paragraph("IDR RUPIAH", style_normal),
            ],
            [
                Paragraph(f"<b>{company_name}</b>", style_bold),
                "",
                Paragraph("<b>REV. NUMBER</b>", style_bold),
                Paragraph("<b>:</b>", style_bold),
                Paragraph("1 / 2 / 3*", style_normal),
            ],
            [
                Paragraph(address or "-", style_normal),
                "",
                Paragraph("<b>VALID UNTIL</b>", style_bold),
                Paragraph("<b>:</b>", style_bold),
                Paragraph(f"{validity_days} DAYS", style_normal),
            ],
        ]

        meta_table = Table(
            meta_rows,
            colWidths=[88 * mm, 8 * mm, 26 * mm, 4 * mm, 54 * mm],
        )
        meta_table.setStyle(
            TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ])
        )
        story.append(meta_table)
        story.append(Spacer(1, 4 * mm))

        # ── 4. ITEMS TABLE ──
        # Columns: No (10mm) | Spec Key / Item Name (45mm) | Spec Val / Model (45mm) | Unit Price (34mm) | Qty (12mm) | Total Price (34mm)
        col_widths = [10 * mm, 45 * mm, 45 * mm, 34 * mm, 12 * mm, 34 * mm]

        table_data: list[list[Any]] = [
            [
                Paragraph("<b>No</b>", style_bold_center),
                Paragraph("<b>Item</b>", style_bold_center),
                "",
                Paragraph("<b>Unit Price</b>", style_bold_center),
                Paragraph("<b>Qty</b>", style_bold_center),
                Paragraph("<b>Total Price</b>", style_bold_center),
            ]
        ]

        t_styles: list[tuple] = [
            # Header styling
            ("SPAN", (1, 0), (2, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), COLOR_HEADER_BG),
            ("BOX", (0, 0), (-1, 0), 0.8, colors.black),
            ("INNERGRID", (0, 0), (-1, 0), 0.8, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, 0), 4),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ]

        subtotal = 0.0
        current_row = 1

        for idx, item in enumerate(items, 1):
            item_start_row = current_row
            name = str(item.get("name") or item.get("canonical_name") or "Laboratory Instrument").strip()
            brand = str(item.get("brand") or "AndisLab Standard").strip()
            model = str(item.get("model") or item.get("catalog_no") or "").strip()
            qty = int(item.get("qty") or 1)
            unit_price = float(item.get("unit_price") or item.get("price") or 0.0)
            line_total = unit_price * qty
            subtotal += line_total

            specs = parse_specifications(item.get("specifications") or item.get("key_specs"))
            # Ensure at least 4 spec rows so product image has a clean vertical frame
            while len(specs) < 4:
                specs.append(("", ""))

            prod_img_path = resolve_product_image(item, fetch_web_if_missing=True)
            prod_img_flowable: Any = ""
            if prod_img_path and prod_img_path.exists():
                try:
                    prod_img_flowable = RLImage(str(prod_img_path), width=33 * mm, height=33 * mm, kind="proportional")
                except Exception as e:
                    logger.debug(f"SPHGenerator: RLImage load skipped for {prod_img_path}: {e}")
                    prod_img_flowable = ""

            # Price sub-tables (Rp on left, value on right, just like Excel accounting format)
            up_str = f"<b>Rp &nbsp;&nbsp;&nbsp; {format_idr(unit_price)}</b>"
            tp_str = f"<b>Rp &nbsp;&nbsp;&nbsp; {format_idr(line_total)}</b>"

            # Row 1 of item: No | Item Name | Model/Cat.No | Unit Price | Qty | Total Price
            table_data.append([
                Paragraph(f"<b>{idx}</b>", style_bold_center),
                Paragraph(f"<b>{name}</b>", style_bold),
                Paragraph(f"<b>{model}</b>", style_bold),
                Paragraph(up_str, ParagraphStyle(f"UP{idx}", parent=style_bold, alignment=2)),
                Paragraph(f"<b>{qty}</b>", style_bold_center),
                Paragraph(tp_str, ParagraphStyle(f"TP{idx}", parent=style_bold, alignment=2)),
            ])
            current_row += 1

            # Row 2 of item: Brand (and start of Product Image span in col 3)
            img_span_start = current_row
            table_data.append([
                "",
                Paragraph(f"<b>{brand}</b>", style_bold),
                "",
                prod_img_flowable,
                "",
                "",
            ])
            t_styles.append(("SPAN", (1, current_row), (2, current_row)))
            current_row += 1

            # Row 3 of item: 'Specification' centered subheader
            table_data.append([
                "",
                Paragraph("Specification", style_spec_hdr),
                "",
                "",
                "",
                "",
            ])
            t_styles.append(("SPAN", (1, current_row), (2, current_row)))
            current_row += 1

            # Subsequent rows: Key | Value specifications
            for sk, sv in specs:
                table_data.append([
                    "",
                    Paragraph(str(sk), style_normal) if sk else "",
                    Paragraph(str(sv), style_normal) if sv else "",
                    "",
                    "",
                    "",
                ])
                current_row += 1

            item_end_row = current_row - 1
            if prod_img_flowable != "":
                t_styles.extend([
                    ("SPAN", (3, img_span_start), (5, item_end_row)),
                    ("ALIGN", (3, img_span_start), (5, item_end_row), "CENTER"),
                    ("VALIGN", (3, img_span_start), (5, item_end_row), "MIDDLE"),
                ])
            # Vertical column borders for this item block
            t_styles.extend([
                ("BOX", (0, item_start_row), (0, item_end_row), 0.7, colors.black),
                ("BOX", (1, item_start_row), (2, item_end_row), 0.7, colors.black),
                ("BOX", (3, item_start_row), (3, item_end_row), 0.7, colors.black),
                ("BOX", (4, item_start_row), (4, item_end_row), 0.7, colors.black),
                ("BOX", (5, item_start_row), (5, item_end_row), 0.7, colors.black),
                ("TOPPADDING", (0, item_start_row), (-1, item_end_row), 1.5),
                ("BOTTOMPADDING", (0, item_start_row), (-1, item_end_row), 1.5),
            ])

        items_table = Table(table_data, colWidths=col_widths, repeatRows=1)
        items_table.setStyle(TableStyle(t_styles))
        story.append(items_table)

        # ── 5. TOTALS SUMMARY BLOCK ──
        discount_amount = round(subtotal * (discount_pct / 100.0), 2) if discount_pct > 0 else 0.0
        after_discount = max(0.0, subtotal - discount_amount)
        ppn_amount = round(after_discount * 0.11, 2) if include_ppn else 0.0
        grand_total = after_discount + ppn_amount

        style_right_bold = ParagraphStyle("RightBold", parent=style_bold, alignment=2)
        style_grand_label = ParagraphStyle("GrandLabel", parent=style_bold, fontSize=9.5, alignment=2)
        style_grand_val = ParagraphStyle("GrandVal", parent=style_bold, fontSize=10, alignment=2)

        totals_rows = [
            [
                "",
                Paragraph("<b>SUB TOTAL</b>", style_right_bold),
                Paragraph("<b>:</b>", style_bold_center),
                Paragraph("<b>Rp</b>", style_bold),
                Paragraph(f"<b>{format_idr(subtotal)}</b>", style_right_bold),
            ]
        ]
        if discount_amount > 0:
            totals_rows.append([
                "",
                Paragraph(f"<b>DISCOUNT ({discount_pct:g}%)</b>", style_right_bold),
                Paragraph("<b>:</b>", style_bold_center),
                Paragraph("<b>Rp</b>", style_bold),
                Paragraph(f"<b>- {format_idr(discount_amount)}</b>", style_right_bold),
            ])
        if include_ppn:
            totals_rows.append([
                "",
                Paragraph("<b>PPN 11%</b>", style_right_bold),
                Paragraph("<b>:</b>", style_bold_center),
                Paragraph("<b>Rp</b>", style_bold),
                Paragraph(f"<b>{format_idr(ppn_amount)}</b>", style_right_bold),
            ])
        totals_rows.append([
            "",
            Paragraph("<b>TOTAL PRICE</b>", style_grand_label),
            Paragraph("<b>:</b>", style_bold_center),
            Paragraph("<b>Rp</b>", style_grand_val),
            Paragraph(f"<b>{format_idr(grand_total)}</b>", style_grand_val),
        ])

        totals_table = Table(
            totals_rows,
            colWidths=[96 * mm, 36 * mm, 6 * mm, 10 * mm, 32 * mm],
        )
        last_t_idx = len(totals_rows) - 1
        totals_table.setStyle(
            TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("BACKGROUND", (1, last_t_idx), (4, last_t_idx), COLOR_HEADER_BG),
                ("LINEBELOW", (1, last_t_idx), (4, last_t_idx), 1.2, COLOR_PURPLE_LINE),
            ])
        )
        story.append(totals_table)
        story.append(Spacer(1, 4 * mm))

        # ── 6. TERM & CONDITIONS & SIGNATURE (KeepTogether so it never orphans) ──
        tc_rows = [
            [Paragraph("<u><b>Term &amp; Conditions</b></u>", style_bold), "", ""],
            [
                Paragraph("<i><b>Price</b></i>", style_italic_bold),
                Paragraph(":", style_normal),
                Paragraph(f"<i><b>{price_delivery_note}</b></i>", style_italic_bold),
            ],
            [
                Paragraph("<i><b>Item</b></i>", style_italic_bold),
                Paragraph(":", style_normal),
                Paragraph(f"<i><b>{stock_note}</b></i>", style_italic_bold),
            ],
            [
                Paragraph("<i><b>Validity</b></i>", style_italic_bold),
                Paragraph(":", style_normal),
                Paragraph(f"<i>{validity_days} days</i>", style_italic),
            ],
            [
                Paragraph("<i><b>Payment</b></i>", style_italic_bold),
                Paragraph(":", style_normal),
                Paragraph(f"<i>{payment_terms}</i>", style_italic),
            ],
            [
                Paragraph("<i><b>Waranty</b></i>", style_italic_bold),
                Paragraph(":", style_normal),
                Paragraph("<i><b>1 year guarantee, sparepart not included</b></i>", style_italic_bold),
            ],
            [
                "",
                "",
                Paragraph(
                    "<i><b>Warranty does not apply if the damage is caused by the operator error, chemical or electrical. "
                    "Grounding, electrical and chemical prepared and responsible by customer (if needed).</b></i>",
                    style_italic_bold,
                ),
            ],
            [
                "",
                "",
                Paragraph(
                    "<i><b>* Kindly note that once the Purchase Order is confirmed, the purchase shall be considered final and "
                    "cannot be cancelled or refunded under any circumstances.</b></i>",
                    style_italic_bold,
                ),
            ],
        ]

        tc_table = Table(tc_rows, colWidths=[38 * mm, 6 * mm, usable_w - 44 * mm])
        tc_table.setStyle(
            TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 1.2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ])
        )

        sig_banner = Table(
            [[Paragraph("<i><b>This signature is already computerized</b></i>", style_sig_banner)]],
            colWidths=[usable_w],
        )
        sig_banner.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_HEADER_BG),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ])
        )

        footer_block = KeepTogether([
            tc_table,
            Spacer(1, 5 * mm),
            Paragraph("<i><b>Sincerely yours,</b></i>", style_italic_bold),
            Spacer(1, 1.5 * mm),
            sig_banner,
            Spacer(1, 2 * mm),
            Paragraph(f"<u><b>{sales_name}</b></u>", style_bold),
            Paragraph(f"<i>{sales_title}</i>", style_italic),
            Paragraph(f"m: {sales_phone}", style_normal),
        ])
        story.append(footer_block)

        doc.build(story, onFirstPage=_draw_page_footer, onLaterPages=_draw_page_footer)

        return {
            "quotation_no": quotation_no,
            "pdf_path": str(pdf_path),
            "filename": fname,
            "company_name": company_name,
            "contact_name": contact_name,
            "subtotal": subtotal,
            "discount_pct": discount_pct,
            "discount_amount": discount_amount,
            "ppn_amount": ppn_amount,
            "grand_total": grand_total,
            "terbilang": terbilang_rupiah(grand_total),
            "formatted_subtotal": f"Rp {format_idr(subtotal)}",
            "formatted_grand_total": f"Rp {format_idr(grand_total)}",
        }

    def sync_to_excel_and_sheets(
        self,
        sph_meta: dict[str, Any],
        items: list[dict[str, Any]],
        contact_title: str = "Tim Pengadaan / Lab",
        address: str = "Indonesia",
        email: str = "-",
        phone: str = "-",
    ) -> dict[str, Any]:
        """Sync generated SPH to:
        1. `data/Rekap_Master_SPH_AndisLab.xlsx` (Master Log Table)
        2. `Template Penawaran ANDISLAB.xlsx` (New worksheet tab cloned from template)
        3. Google Sheets via `gsheets` CLI if GOOGLE_SHEETS_SPH_ID is configured in .env
        """
        sync_status = {"master_rekap_xlsx": False, "template_xlsx": False, "gsheets": False}
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        items_summary = ", ".join(
            f"{it.get('name') or it.get('canonical_name')} (x{it.get('qty', 1)})" for it in items
        )
        total_hpp = sum(float(it.get("hpp") or it.get("harga_beli") or 0) * int(it.get("qty", 1)) for it in items)
        est_profit = max(0.0, float(sph_meta.get("subtotal", 0)) - float(sph_meta.get("discount_amount", 0)) - total_hpp)

        row_values = [
            sph_meta.get("quotation_no", ""),
            now_str,
            sph_meta.get("company_name", ""),
            sph_meta.get("contact_name", ""),
            phone,
            email,
            items_summary,
            float(sph_meta.get("subtotal", 0)),
            float(sph_meta.get("discount_amount", 0)),
            float(sph_meta.get("ppn_amount", 0)),
            float(sph_meta.get("grand_total", 0)),
            total_hpp,
            est_profit,
            sph_meta.get("pdf_path", ""),
            "SENT / ACTIVE",
        ]

        # 1. Append to Master Rekap XLSX
        try:
            if MASTER_REKAP_XLSX_PATH.exists():
                wb_rekap = openpyxl.load_workbook(MASTER_REKAP_XLSX_PATH)
                ws_rekap = wb_rekap.active
            else:
                wb_rekap = openpyxl.Workbook()
                ws_rekap = wb_rekap.active
                ws_rekap.title = "Rekap_SPH_AndisLab"
                ws_rekap.append([
                    "No Quotation", "Tanggal", "Instansi / Perusahaan", "Nama PIC",
                    "No WhatsApp", "Email", "Rincian Barang", "Subtotal (Rp)",
                    "Diskon (Rp)", "PPN 11% (Rp)", "Grand Total (Rp)",
                    "Total HPP (Rp)", "Estimasi Profit (Rp)", "Lokasi File PDF", "Status"
                ])
            ws_rekap.append(row_values)
            MASTER_REKAP_XLSX_PATH.parent.mkdir(parents=True, exist_ok=True)
            wb_rekap.save(MASTER_REKAP_XLSX_PATH)
            sync_status["master_rekap_xlsx"] = True
        except Exception as e:
            logger.warning(f"SPHGenerator: Master Rekap Excel sync failed: {e}")

        # 2. Update `Template Penawaran ANDISLAB.xlsx` if available and not locked
        if TEMPLATE_XLSX_PATH.exists() and items:
            try:
                wb_tpl = openpyxl.load_workbook(TEMPLATE_XLSX_PATH)
                base_sheet_name = " PT Ecostar Laboratories" if " PT Ecostar Laboratories" in wb_tpl.sheetnames else wb_tpl.sheetnames[0]
                ws_new = wb_tpl.copy_worksheet(wb_tpl[base_sheet_name])
                clean_tab = re.sub(r"[\[\]\*\?\/\\:]", "", sph_meta.get("company_name", "SPH"))[:28].strip() or "SPH_Baru"
                if clean_tab in wb_tpl.sheetnames:
                    clean_tab = f"{clean_tab[:22]}_{datetime.date.today().strftime('%d%m')}"
                ws_new.title = clean_tab

                ws_new["A7"] = f"QUOTATION : {sph_meta.get('quotation_no')}"
                ws_new["A10"] = sph_meta.get("company_name", "")
                ws_new["A11"] = address or "Indonesia"
                ws_new["G9"] = sph_meta.get("contact_name", "")
                ws_new["G10"] = contact_title
                ws_new["G11"] = email
                ws_new["G12"] = f"` {phone}"
                ws_new["G13"] = datetime.datetime.now()

                first_it = items[0]
                ws_new["B19"] = first_it.get("name") or first_it.get("canonical_name") or "Alat Lab"
                ws_new["D19"] = first_it.get("model") or first_it.get("catalog_no") or ""
                ws_new["E19"] = float(first_it.get("unit_price") or first_it.get("price") or 0)
                ws_new["F19"] = int(first_it.get("qty") or 1)
                ws_new["B20"] = first_it.get("brand") or "AndisLab"

                wb_tpl.save(TEMPLATE_XLSX_PATH)
                sync_status["template_xlsx"] = True
            except Exception as e:
                logger.debug(f"SPHGenerator: Template Penawaran ANDISLAB.xlsx tab append skipped: {e}")

        return sync_status

    async def generate_and_register_sph(
        self,
        company_name: str,
        items: list[dict[str, Any]],
        contact_name: str = "Tim Pengadaan / Laboratorium",
        contact_title: str = "Laboratory / Procurement",
        address: str = "Indonesia",
        email: str = "-",
        phone: str = "-",
        prospect_id: str = "",
        discount_pct: float = 0.0,
        include_ppn: bool = True,
        validity_days: int = 30,
        payment_terms: str = "50% Down Payment and 50% Full Payment After Delivery",
        stock_note: str = "Ready Stock Tidak Terikat",
        price_delivery_note: str = "Franco Jabodetabek Include Delivery",
        quotation_no: str | None = None,
        output_filename: str | None = None,
        register_in_db: bool = True,
    ) -> dict[str, Any]:
        """Full pipeline: generate sequential QUO number -> render PDF -> sync Excel -> save to quotes DB."""
        quo_no = quotation_no or await self.get_next_quotation_number()
        sph_meta = self.render_pdf(
            quotation_no=quo_no,
            company_name=company_name,
            contact_name=contact_name,
            contact_title=contact_title,
            address=address,
            email=email,
            phone=phone,
            items=items,
            discount_pct=discount_pct,
            include_ppn=include_ppn,
            validity_days=validity_days,
            payment_terms=payment_terms,
            stock_note=stock_note,
            price_delivery_note=price_delivery_note,
            output_filename=output_filename,
        )
        sync_res = self.sync_to_excel_and_sheets(
            sph_meta=sph_meta,
            items=items,
            contact_title=contact_title,
            address=address,
            email=email,
            phone=phone,
        )
        sph_meta["sync_status"] = sync_res

        if register_in_db and self.db_path.exists():
            try:
                from harvey.integrations.quote_chaser import QuoteChaser
                chaser = QuoteChaser()
                items_str = ", ".join(
                    f"{it.get('name') or it.get('canonical_name')} (x{it.get('qty', 1)})" for it in items
                )
                await chaser.add_quote(
                    quote_id=quo_no,
                    company=company_name,
                    contact_name=contact_name,
                    phone=phone if phone != "-" else "",
                    email=email if email != "-" else "",
                    items_summary=items_str,
                    total_amount=float(sph_meta["grand_total"]),
                    prospect_id=prospect_id,
                    notes=f"PDF: {sph_meta['pdf_path']}",
                )
            except Exception as e:
                logger.debug(f"SPHGenerator: QuoteChaser DB registration warning: {e}")

        return sph_meta
