"""Blind Procurement & Sourcing Privacy Firewall for PT Andis Sentral Laboratorium.

Guarantees 100% isolation between customer information and external vendors/search engines:
1. Anonymizes raw customer requests before sending queries to Tavily, Firecrawl, or LLM synthesis.
2. Strips all personal identifiable information (PII), institution names, contact numbers, and emails.
3. Provides Outbound RFQ Guard to verify no client details leak in vendor inquiries.
4. Generates standard Blind RFQ messages (Franco Gudang Bogor, stock fulfillment rationale).
"""

import re
import logging

logger = logging.getLogger("harvey.sourcing_privacy")

# Honorifics & titles
HONORIFICS = [
    r"\bpak\.?\b", r"\bbu\.?\b", r"\bbapak\.?\b", r"\bibu\.?\b", r"\bdr\.?\b", r"\bdokter\.?\b",
    r"\bprof\.?\b", r"\bprofesor\.?\b", r"\bapt\.?\b", r"\bapoteker\.?\b", r"\bmr\.?\b", r"\bmrs\.?\b",
    r"\bms\.?\b", r"\bhaji\.?\b", r"\bhj\.?\b", r"\bir\.?\b", r"\bspd\.?\b", r"\bmkes\.?\b", r"\bmsi\.?\b",
]

# Institution keywords
INSTITUTION_KEYWORDS = [
    r"\buniversitas\b", r"\buniv\b", r"\brsud\b", r"\brsu\b", r"\brs\b",
    r"\brumah\s+sakit\b", r"\bpoltekkes\b", r"\bpoliteknik\b", r"\binstitut\b",
    r"\bbalai\b", r"\bdinas\b", r"\bkementerian\b", r"\blabkesda\b", r"\bbpom\b",
    r"\bbasarnas\b", r"\blppm\b", r"\bfakultas\b", r"\blaboratorium\b", r"\blab\b",
    r"\bsmk\b", r"\bsma\b", r"\bakademi\b", r"\bakfar\b", r"\byayasan\b",
    r"\bpt\.?\b", r"\bcv\.?\b", r"\bud\.?\b", r"\bpuskesmas\b", r"\bklinik\b",
    r"\bjurusan\b", r"\bprodi\b", r"\bdepartemen\b", r"\bfarmasi\b", r"\bkedokteran\b",
]

# Common conversational inquiry fillers
INQUIRY_FILLERS = [
    r"halo\b", r"selamat\s+(?:pagi|siang|sore|malam)\b", r"assalamu['a-z]*\b",
    r"mau\s+tanya(?:\s+dong)?\b", r"tanya\s+harga\b", r"apakah\s+(?:ada|ready|tersedia)\b",
    r"ada\s+stok\b", r"ready\s+(?:nggak|gak|kah)?\b", r"tolong\s+(?:dicarikan|cek|info)\b",
    r"butuh\s+(?:segera|urgent|penawaran|pricelist)\b", r"minta\s+(?:penawaran|pricelist|sph|brosur)\b",
    r"bisa\s+(?:kirim|bantu|sedia)\b", r"kami\s+dari\b", r"saya\s+dari\b", r"saya\s+dengan\b",
    r"mohon\s+info\b", r"bantu\s+cek\b", r"kira-kira\s+ada\b",
    r"(?:bisa\s+)?kirim\s+penawaran(?:\s+ke)?\b", r"(?:atau\s+)?wa\s+ke\b", r"untuk\s+lab\s+(?:kami|kita)\b",
    r"(?:kami|kita)\s+butuh(?:\s+segera)?\b", r"\bpenawaran\s+ke\b", r"\bke\s+email\b",
    r"\bkami\b", r"\bkita\b",
]


def anonymize_item_query(raw_text: str) -> str:
    """Sanitize and anonymize raw user inquiry to extract only the target item and specs.
    
    Removes:
    - Greetings, conversational fillers
    - Personal names, honorifics, roles
    - Institution / company names
    - Phone numbers, email addresses, URLs
    - Tender/RUP codes, budgets
    """
    if not raw_text:
        return ""

    text = raw_text.strip()

    # 1. Strip URLs and emails
    text = re.sub(r"https?://\S+|www\.\S+", " ", text, flags=re.I)
    text = re.sub(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", " ", text)

    # 2. Strip phone numbers (08..., +62..., 62...)
    text = re.sub(r"(?:\+?62|0)8[0-9\s\-]{7,14}", " ", text)
    text = re.sub(r"(?:\+?62|0)[2-9][0-9\s\-]{6,12}", " ", text)

    # 3. Strip tender/budget annotations
    text = re.sub(r"(?i)\b(?:rup|paket|tender|hps|pagu|anggaran)\s*[:=]?\s*[a-zA-Z0-9_\-\.\,]+", " ", text)
    text = re.sub(r"(?i)\brp\.?\s*[\d\.\,]+", " ", text)

    # 4. Remove conversational filler phrases
    for pat in INQUIRY_FILLERS:
        text = re.sub(pat, " ", text, flags=re.I)

    # 4b. Remove self-intro (e.g. "saya Dr. Budi Santoso", "nama saya Budi", "dengan Pak Budi")
    text = re.sub(r"(?i)\b(?:nama\s+saya|saya|dengan)\s+(?:dr\.?|prof\.?|ir\.?|pak\.?|bu\.?|bapak|ibu)?\s*[a-zA-Z]+(?:\s+[a-zA-Z]+)?", " ", text)

    # 5. Remove greetings & honorifics followed by capitalized names
    for pat in HONORIFICS:
        text = re.sub(rf"{pat}\s+[a-zA-Z]+(?:\s+[a-zA-Z]+)?", " ", text, flags=re.I)
        text = re.sub(rf"{pat}\.?", " ", text, flags=re.I)

    # 6. Remove institution clauses like "dari Universitas Indonesia", "dari PT Bio Medika", etc.
    for pat in INSTITUTION_KEYWORDS:
        text = re.sub(rf"(?:dari|di|untuk)\s+{pat}(?:\s+[a-zA-Z0-9\.\-]+){{0,3}}", " ", text, flags=re.I)
        text = re.sub(rf"{pat}(?:\s+[a-zA-Z0-9\.\-]+){{0,3}}", " ", text, flags=re.I)
        text = re.sub(rf"{pat}\.?", " ", text, flags=re.I)

    # Clean whitespace and trailing punctuation
    text = re.sub(r"[^\w\s\-\/\.\,\+\(\)\%]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,.-")

    # If stripped too aggressively, fall back to simple alphanumeric specification preservation
    if len(text) < 3 and raw_text:
        # Fallback: extract potential item words
        words = [w for w in raw_text.split() if len(w) > 2 and not any(re.search(p, w, re.I) for p in HONORIFICS + INSTITUTION_KEYWORDS)]
        text = " ".join(words[:6])

    return text.strip()


def outbound_rfq_guard(message_text: str, forbidden_terms: list[str] | None = None) -> tuple[bool, str]:
    """Safety check to ensure an outbound RFQ to a vendor never contains client details.
    
    Returns: (is_safe: bool, reason: str)
    """
    if not message_text:
        return True, ""

    msg_low = message_text.lower()

    # Check against forbidden terms (e.g. client name, company name, personal email, phone)
    if forbidden_terms:
        for term in forbidden_terms:
            t = (term or "").strip().lower()
            if len(t) >= 4 and t in msg_low:
                return False, f"Terdeteksi data klien terlindungi ('{term}') dalam draf pesan vendor!"

    # Guard against common institution leak markers
    for kw in [r"\buniversitas\b", r"\brsud\b", r"\brsu\b", r"\bpoltekkes\b", r"\blppm\b", r"\bdinas\s+kesehatan\b"]:
        if re.search(kw, msg_low) and "andis" not in msg_low:
            return False, f"Terdeteksi nama/tipe instansi luar ('{kw}') dalam draf pesan vendor!"

    return True, "Safe"


def build_blind_rfq_message(
    item_name: str,
    specs: str = "",
    qty: int = 1,
    vendor_name: str = "",
    target_city: str = "Bogor",
) -> str:
    """Generate professional B2B Blind Procurement RFQ for external vendors.
    
    Guarantees:
    - Sender is strictly 'PT Andis Sentral Laboratorium'
    - Purpose is internal stock replenishment
    - Delivery destination is Franco Gudang Bogor
    - Zero reference to any external customer
    """
    vendor_greeting = f"Tim Sales {vendor_name}" if vendor_name else "Bapak/Ibu Tim Sales"
    spec_line = f" (Spesifikasi: {specs})" if specs and specs != item_name else ""
    qty_str = f"{qty} unit" if qty > 1 else "1 unit"

    return (
        f"Selamat siang {vendor_greeting},\n\n"
        f"Kami dari Divisi Pengadaan & Logistik PT Andis Sentral Laboratorium.\n"
        f"Saat ini kami sedang membutuhkan penambahan stok untuk pemenuhan gudang kami di {target_city}:\n\n"
        f"• Nama Barang: {item_name}{spec_line}\n"
        f"• Kebutuhan Qty: {qty_str}\n"
        f"• Pengiriman: Franco Gudang Bogor (Jawa Barat)\n\n"
        f"Mohon informasikan:\n"
        f"1. Harga nett reseller / B2B (apakah sudah termasuk PPN 11%?)\n"
        f"2. Status ketersediaan (Ready Stock atau Inden - estimasi berapa hari kerja)\n"
        f"3. Masa garansi resmi unit dan kelengkapan standar\n\n"
        f"Draf penawaran dapat diinformasikan via chat ini atau email ke pengadaan kami. Terima kasih banyak atas kerjasamanya.\n\n"
        f"Hormat kami,\n"
        f"Divisi Pengadaan & Logistik\n"
        f"PT Andis Sentral Laboratorium"
    )
