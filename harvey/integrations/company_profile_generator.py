"""Company Profile PDF Generator for PT Andis Sentral Laboratorium (AndisLab).
Generates an elegant, professional 2-page B2B Company Profile in data/assets/give_first/Company_Profile_AndisLab.pdf.
"""

from __future__ import annotations

import logging
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Image as RLImage,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.company_profile")

PRIMARY_COLOR = colors.HexColor("#0D3B66")    # Elegant Navy
SECONDARY_COLOR = colors.HexColor("#F4D35E")  # Gold accent
ACCENT_BLUE = colors.HexColor("#0077B6")      # Electric Cyan Blue
DARK_GRAY = colors.HexColor("#2B2D42")        # Body text
LIGHT_BG = colors.HexColor("#F8F9FA")         # Card background
BORDER_GRAY = colors.HexColor("#E5E7EB")

OUTPUT_PDF_PATH = PROJECT_ROOT / "data" / "assets" / "give_first" / "Company_Profile_AndisLab.pdf"
LOGO_PATH = PROJECT_ROOT / "data" / "assets" / "sph" / "andislab_logo.png"


def generate_company_profile_pdf(output_path: Path | None = None) -> Path:
    """Generate high-impact 2-page Company Profile PDF."""
    target_path = output_path or OUTPUT_PDF_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(target_path),
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    )

    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        "HeroTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=PRIMARY_COLOR,
    )
    subtitle_style = ParagraphStyle(
        "HeroSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=15,
        textColor=ACCENT_BLUE,
    )
    body_style = ParagraphStyle(
        "BodyDark",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=13,
        textColor=DARK_GRAY,
    )
    bold_style = ParagraphStyle(
        "BoldDark",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=13,
        textColor=DARK_GRAY,
    )
    h2_style = ParagraphStyle(
        "SectionHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=16,
        textColor=PRIMARY_COLOR,
        spaceAfter=4,
    )
    card_title_style = ParagraphStyle(
        "CardTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=14,
        textColor=PRIMARY_COLOR,
    )

    story = []

    # ── HEADER (Logo + Company Identity) ──
    header_data = []
    logo_cell = ""
    if LOGO_PATH.exists():
        try:
            logo_img = RLImage(str(LOGO_PATH), width=48 * mm, height=16 * mm)
            logo_cell = logo_img
        except Exception:
            logo_cell = Paragraph("<b>AndisLab</b>", title_style)
    else:
        logo_cell = Paragraph("<b>PT Andis Sentral Laboratorium</b>", title_style)

    header_text = Paragraph(
        "<b>PT ANDIS SENTRAL LABORATORIUM</b><br/>"
        "<font size='8' color='#555555'>Distributor & Supplier Resmi Peralatan, Instrumen & Bahan Kimia Laboratorium<br/>"
        "Gudang Utama: Jabodetabek | Hotline WA: 0819-9157-5096 | Website: www.andislab.com</font>",
        body_style,
    )

    header_table = Table([[logo_cell, header_text]], colWidths=[52 * mm, 130 * mm])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(header_table)
    story.append(HRFlowable(width="100%", thickness=1.5, color=PRIMARY_COLOR, spaceBefore=4, spaceAfter=8))

    # ── HERO SUMMARY ──
    hero_text = Paragraph(
        "<b>Solusi Cepat, Resmi & Ready Stock untuk Kebutuhan Laboratorium Anda</b><br/>"
        "<font color='#444444'>"
        "PT Andis Sentral Laboratorium (AndisLab) adalah mitra terpercaya pengadaan peralatan laboratorium untuk "
        "industri manufaktur, pertambangan, farmasi, rumah sakit/klinik, universitas, hingga instansi pemerintah di seluruh Indonesia. "
        "Dengan komitmen anti-indent, kami menyediakan <b>1.140+ item ready stock</b> di gudang Jabodetabek bergaransi resmi."
        "</font>",
        body_style,
    )
    story.append(hero_text)
    story.append(Spacer(1, 4 * mm))

    # ── VALUE PROPOSITION (4 PILLARS) ──
    p1 = Paragraph("<b>📦 1.140+ Ready Stock</b><br/><font size='8' color='#555'>Unit siap kirim dari gudang Jabodetabek tanpa antre indent impor.</font>", body_style)
    p2 = Paragraph("<b>🛡️ Garansi Resmi 12 Bulan</b><br/><font size='8' color='#555'>Jaminan unit original, sertifikat kalibrasi, dan instalasi teknisi onsite.</font>", body_style)
    p3 = Paragraph("<b>📄 SPH Cepat 1x24 Jam</b><br/><font size='8' color='#555'>Penerbitan surat penawaran harga resmi ber-KOP untuk telaah HPS & anggaran.</font>", body_style)
    p4 = Paragraph("<b>🏛️ Etalase E-Katalog INAPROC</b><br/><font size='8' color='#555'>Memenuhi legalitas pengadaan pemerintah (LPSE, E-Katalog, Faktur Pajak).</font>", body_style)

    pillars_table = Table([[p1, p2], [p3, p4]], colWidths=[91 * mm, 91 * mm])
    pillars_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(pillars_table)
    story.append(Spacer(1, 5 * mm))

    # ── KATEGORI PRODUK UTAMA ──
    story.append(Paragraph("Kategori Portofolio Produk & Prinsipal", h2_style))

    cat1 = Paragraph(
        "<b>1. Instrumen Analitik & Presisi</b><br/>"
        "• Timbangan Analitik 4 & 5 Desimal (Aczet, Precisa, Sartorius)<br/>"
        "• Moisture Analyzer / Penentu Kadar Air (Aczet MB50)<br/>"
        "• Spectrophotometer UV-Vis & Visible (Thermo Scientific, Infitek)<br/>"
        "• pH Meter, Conductivity Meter, DO Meter (Milwaukee, Hanna Instruments)",
        body_style,
    )

    cat2 = Paragraph(
        "<b>2. Alat Preparasi & Thermal Lab</b><br/>"
        "• Autoclave Sterilisasi 50L & 100L (Hirayama, Daihan Labtech)<br/>"
        "• Drying Oven & Incubator Lab (Memmert, Yamato, Daihan)<br/>"
        "• Muffle Furnace Suhu Tinggi 1200°C (Nabertherm, Daihan)<br/>"
        "• Centrifuge Kecepatan Tinggi & Hematokrit (DLAB, Hermle)",
        body_style,
    )

    cat3 = Paragraph(
        "<b>3. Glassware & Peralatan Volumetrik</b><br/>"
        "• Beaker, Erlenmeyer, Measuring Cylinder Class A (Iwaki, Normax)<br/>"
        "• Micropipette Single & Multi-Channel Autoclavable (DLAB, Aelab)<br/>"
        "• Lab Bottle, Buret Otomatis, Funnel Pemisah (Normax, Duran)<br/>"
        "• Hotplate Magnetic Stirrer & Vortex Mixer (DLAB MX-S)",
        body_style,
    )

    cat4 = Paragraph(
        "<b>4. Reagen, Media & Safety Lab</b><br/>"
        "• Bahan Kimia & Reagen Analisis Grade PA / Pro Analisis<br/>"
        "• Rapid Test & Diagnostic Strips Medis / Dinkes<br/>"
        "• Safety Cabinet, Flammable Storage, Eye Washer<br/>"
        "• Biosafety Cabinet Class II & Laminar Air Flow",
        body_style,
    )

    cat_table = Table([[cat1, cat2], [cat3, cat4]], colWidths=[91 * mm, 91 * mm])
    cat_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
    ]))
    story.append(cat_table)
    story.append(Spacer(1, 5 * mm))

    # ── CLIENT SEGMENTS SERVED ──
    story.append(Paragraph("Segmen Klien yang Kami Layani", h2_style))
    seg_text = Paragraph(
        "<b>🏭 Industri & Manufaktur:</b> QC pabrik F&B, farmasi, kimia, pakan ternak, kosmetik.<br/>"
        "<b>⛏️ Pertambangan & Mineral:</b> Lab pengujian kadar air, pengabuan mineral, dan batubara.<br/>"
        "<b>🏛️ Rumah Sakit & Balai Kesehatan:</b> RSUD, Dinkes, Puskesmas, Labkesda (E-Katalog LKPP).<br/>"
        "<b>🎓 Pendidikan & Balai Riset:</b> Laboratorium universitas, politeknik, dan balai pengujian lingkungan.",
        body_style,
    )
    story.append(seg_text)
    story.append(Spacer(1, 4 * mm))

    # ── FOOTER CALL TO ACTION ──
    cta_box = Table([[
        Paragraph(
            "<b>Hubungi Tim Konsultan Pengadaan AndisLab:</b><br/>"
            "💬 WhatsApp Sales: <b>0819-9157-5096</b> &nbsp;|&nbsp; ✉️ Email: <b>sales@andislab.com</b> &nbsp;|&nbsp; 🌐 <b>www.andislab.com</b><br/>"
            "<font size='8' color='#555'>Siap melayani penerbitan Surat Penawaran Harga (SPH), pendampingan HPS, dan kunjungan teknisi.</font>",
            body_style,
        )
    ]], colWidths=[182 * mm])
    cta_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EBF2FA")),
        ("BOX", (0, 0), (-1, -1), 1, ACCENT_BLUE),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(cta_box)

    doc.build(story)
    logger.info(f"Company Profile PDF berhasil dibuat di: {target_path}")
    return target_path


if __name__ == "__main__":
    generate_company_profile_pdf()

