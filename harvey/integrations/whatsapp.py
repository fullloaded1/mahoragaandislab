"""WhatsApp Outreach & Anti-Ban Engine for Harvey.

Supports two providers:
1. ``web`` (default, $0): Uses local Google Chrome with a persistent profile
   (`data/whatsapp_profile`) so the user scans the WhatsApp Web QR code once.
2. ``fonnte``: Uses Fonnte API (`https://api.fonnte.com/send`) if `FONNTE_API_KEY`
   is set in `.env`.

Anti-Ban Safeguards:
- Strict daily cap (`max_daily_messages`, default 12/day) persisted in `data/whatsapp_activity.json`.
- Strict per-cycle cap (`max_per_cycle`, default 2 per 15-minute heartbeat).
- Sends ONLY during Indonesian business hours (`work_hours_start` - `work_hours_end`, Asia/Jakarta).
- Uses deterministic spintax + personal context (zero LLM token cost) or optional AI drafting,
  with character-by-character human typing simulation and random 45-120s delays.
"""

import asyncio
import json
import logging
import os
import random
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
import pytz
import uuid
import yaml

from harvey.config import EnvConfig, HarveyConfig
from harvey.integrations.openwa import OpenWAClient
from harvey.paths import PROJECT_ROOT
from harvey.state import StateManager

logger = logging.getLogger("harvey.whatsapp")

DATA_DIR = PROJECT_ROOT / "data"
PROFILE_DIR = DATA_DIR / "whatsapp_profile"
# Anti-BSOD and memory-safe flags for Windows Chromium/Chrome execution
CHROME_SAFE_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-software-rasterizer",
    "--disable-accelerated-2d-canvas",
    "--disable-gpu-compositing",
    "--mute-audio",
    "--no-first-run",
    "--no-default-browser-check",
    "--renderer-process-limit=2",
]

ACTIVITY_PATH = DATA_DIR / "whatsapp_activity.json"
GIVE_FIRST_DIR = DATA_DIR / "assets" / "give_first"
GIVE_FIRST_INDEX_PATH = GIVE_FIRST_DIR / "index.yaml"
_ASSET_CATALOG_CACHE: list[dict] | None = None


def load_asset_catalog(index_path: Path | None = None, reload: bool = False) -> list[dict]:
    """Load the give-first asset catalog from index.yaml (cached).
    Returns an empty list if index.yaml is missing or invalid."""
    global _ASSET_CATALOG_CACHE
    if not reload and index_path is None and _ASSET_CATALOG_CACHE is not None:
        return _ASSET_CATALOG_CACHE

    target = Path(index_path) if index_path is not None else GIVE_FIRST_INDEX_PATH
    if not target.exists():
        logger.debug(f"WhatsApp: Asset catalog not found at {target}")
        if index_path is None:
            _ASSET_CATALOG_CACHE = []
        return []

    try:
        content = target.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        if isinstance(data, dict) and isinstance(data.get("assets"), list):
            assets = data["assets"]
            if index_path is None:
                _ASSET_CATALOG_CACHE = assets
            return assets
        return []
    except Exception as e:
        logger.warning(f"WhatsApp: Error loading asset catalog from {target}: {e}")
        return []


def get_asset_by_id(asset_id: str, catalog: list[dict] | None = None) -> dict | None:
    """Find asset dictionary by id in catalog."""
    if not asset_id:
        return None
    if catalog is None:
        catalog = load_asset_catalog()
    for a in catalog:
        if a.get("id") == asset_id:
            return a
    return None


def detect_lead_segment(industry: str = "", company: str = "") -> str:
    """Map company and industry keywords to customer segment:
    pemerintah | kampus | rumah_sakit | pabrik_qc
    """
    text = f"{industry or ''} {company or ''}".lower()

    # 1. pemerintah
    pem_keys = ["dinas", "pemda", "pemkab", "pemkot", "kementerian", "balai", "badan", "puskesmas", "labkesda"]
    if any(k in text for k in pem_keys):
        return "pemerintah"

    # 2. kampus
    kampus_keys = ["universitas", "univ", "politeknik", "poltek", "institut", "sekolah tinggi", "stikes", "fakultas"]
    if any(k in text for k in kampus_keys):
        return "kampus"

    # 3. rumah_sakit
    rs_keys = ["rsud", "rumah sakit", "klinik", "hospital"]
    if any(k in text for k in rs_keys) or re.search(r"\brs\b", text):
        return "rumah_sakit"

    # 4. pabrik_qc (default for PT/industri/pabrik/manufaktur/food/farmasi)
    return "pabrik_qc"


def pick_asset_for(
    industry: str = "",
    company: str = "",
    catalog: list[dict] | None = None,
) -> dict | None:
    """Pick the most relevant give-first asset dictionary for a prospect.
    Chooses the first asset whose best_for list contains the detected segment.
    Falls back to the first available asset in catalog, or None if catalog is empty."""
    if catalog is None:
        catalog = load_asset_catalog()
    if not catalog:
        return None

    segment = detect_lead_segment(industry, company)
    for asset in catalog:
        best_for = asset.get("best_for") or []
        if segment in best_for:
            return asset

    return catalog[0] if catalog else None


def is_affirmative_consent(text: str) -> bool:
    """Check if an incoming reply expresses affirmative consent to receive an asset.
    Triggers: boleh, iya, ya, ok, oke, silakan, mau, kirim, monggo, siap, minta, tolong kirim.
    Whole-word, case-insensitive.
    Returns False if the text contains negations: tidak, gak, nggak, belum, ga usah, jangan.
    """
    s = (text or "").strip().lower()
    if not s:
        return False

    # Check negations first
    neg_pattern = r"\b(?:tidak|gak|nggak|ngga|belum|blm|tdk|jangan|ga\s+usah|g\s+usah|ndak)\b"
    if re.search(neg_pattern, s):
        return False

    # Check affirmative triggers (whole-word)
    aff_pattern = r"\b(?:boleh|iya|ya|ok|oke|okay|silakan|silahkan|mau|kirim|monggo|siap|minta|tolong\s+kirim)\b"
    return bool(re.search(aff_pattern, s))


def find_pending_asset_offer(notes: str) -> tuple[str, str] | None:
    """Find pending [WA_ASSET_OFFER:asset_id:date] without matching [WA_ASSET_SENT:asset_id...].
    Returns (asset_id, offer_date) or None. If multiple, returns the most recent one."""
    if not notes or "[WA_ASSET_OFFER:" not in notes:
        return None

    offers = list(re.finditer(r"\[WA_ASSET_OFFER:([a-zA-Z0-9_-]+):(\d{4}-\d{2}-\d{2})\]", notes))
    if not offers:
        return None

    # Check from newest to oldest
    for m in reversed(offers):
        asset_id = m.group(1)
        offer_date = m.group(2)
        sent_marker = f"[WA_ASSET_SENT:{asset_id}"
        if sent_marker not in notes:
            return asset_id, offer_date

    return None


def build_asset_caption(title: str, who: str) -> str:
    """Short warm Indonesian caption in Ahnaf's voice when sending a promised PDF asset."""
    clean_title = (title or "dokumen pengadaan alat lab").strip()
    clean_who = (who or "Pak/Bu").strip()
    return f"Berikut {clean_title} yang saya janjikan, {clean_who}. Semoga membantu tim. Kalau ada yang ingin didiskusikan, saya siap bantu. 🙏"


def build_asset_fallback_text(title: str, who: str) -> str:
    """Fallback text if PDF attachment fails, providing the website link and promise of manual send."""
    clean_title = (title or "dokumen pengadaan alat lab").strip()
    clean_who = (who or "Pak/Bu").strip()
    return (
        f"Halo {clean_who}, ini link informasi dan katalog kami di https://www.andislab.com. "
        f"Untuk dokumen *{clean_title}* segera kami kirimkan terpisah ya Pak/Bu. "
        "Kalau ada yang ingin didiskusikan, saya siap bantu. 🙏"
    )


CLOSING_ACK_PATTERNS = [
    r"^(?:ok|oke|baik|siap|noted)(?:\s+(?:pak|bu|mas|mbak|mba|kak|bang|om|bapak|ibu))*\.?$",
    r"^(?:terima\s*kasih|terimakasih|makasih|tq|ty|thanks|thank\s*you|matur\s*nuwun|hatur\s*nuhun)(?:\s+(?:banyak|ya|pak|bu|mas|mba|mbak|om|bang|kak|yah|lah|atas\s+kerjasamanya|atas\s+informasinya|atas\s+responnya|atas\s+infonya))*\.?$",
    r"^(?:sama[- ]sama|kembali|terima\s*kasih\s*kembali|siap\s*terima\s*kasih)(?:\s+(?:pak|bu|mas|mbak|mba|kak|bang|om|bapak|ibu))*\.?$",
    r"^(?:ok|oke|baik|siap|noted)(?:\s+(?:pak|bu|mas|mbak|mba|kak|bang|om|bapak|ibu))*\s+(?:siap|terima\s*kasih|terimakasih|makasih|tq|thanks)(?:\s+(?:banyak|ya|pak|bu|mas|mbak|mba|kak|bang|om|bapak|ibu))*\.?$",
    r"^(?:ok\s*siap|baik\s*terima\s*kasih|siap\s*mas|siap\s*mbak|oke\s*baik|noted\s*dengan\s*baik)(?:\s+(?:pak|bu|mas|mbak|mba|kak|bang|om|bapak|ibu))*\.?$",
    r"^(?:siap\s*pak\s*terima\s*kasih|baik\s*pak\s*terima\s*kasih)(?:\s+(?:pak|bu|mas|mbak|mba|kak|bang|om|bapak|ibu))*\.?$",
]


def strip_quoted_echo(text: str) -> str:
    """Remove quotes and echoes of previous messages from WhatsApp / Web.
    Handles WhatsApp timestamp headers e.g. '[14.04, 10/11/2026] andislab: ...',
    markdown quotes '> ...', and lines starting with sender prefixes.
    """
    if not text:
        return ""
    # 1. Regex for WhatsApp export/quote patterns: [HH.MM, DD/MM/YYYY] or [HH:MM, DD/MM/YY] Name: ...
    cleaned = re.sub(
        r"\[\d{1,2}[:.]\d{2}(?:[:.]\d{2})?,\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\]\s*[^:\n]+:\s*.*?(?=\n\n|\n[A-Z0-9a-z]|$)",
        "",
        text,
        flags=re.DOTALL,
    )
    # 2. Markdown quote prefixes and sender header lines
    lines = cleaned.splitlines()
    non_quote_lines = []
    for line in lines:
        stripped_line = line.strip()
        if stripped_line.startswith(">"):
            continue
        # If line starts with sender header like "andislab:" or "Ahnaf:" or "PT Andis Sentral Laboratorium:"
        if re.match(r"^(?:andislab|ahnaf|pt andis|harvey)\s*:\s*", stripped_line, re.IGNORECASE):
            continue
        non_quote_lines.append(line)

    result = "\n".join(non_quote_lines).strip()
    return result if result else text.strip()


def is_closing_acknowledgment(text: str) -> bool:
    """Check if incoming message is a pure closing / polite courtesy
    (e.g., 'terima kasih', 'makasih ya pak', 'baik pak', 'noted pak')
    that does not need another reply, avoiding infinite thank-you loops and saving LLM tokens.
    """
    if not text:
        return False
    t = text.strip().lower()
    t = re.sub(r"[\U00010000-\U0010ffff]", "", t).strip()  # remove emojis
    return any(re.match(p, t) for p in CLOSING_ACK_PATTERNS)


def is_internal_operator_number(phone: str, report_phone: str = "087764021131") -> bool:
    """Check if a phone number belongs to the internal sales operator (Ahnaf)."""
    if not phone:
        return False
    digits = re.sub(r"[^\d]", "", str(phone))
    if digits.startswith("08"):
        digits = "628" + digits[2:]

    rep_digits = re.sub(r"[^\d]", "", str(report_phone or "087764021131"))
    if rep_digits.startswith("08"):
        rep_digits = "628" + rep_digits[2:]

    internal_set = {"6287764021131", "087764021131", rep_digits}
    return digits in internal_set or digits.endswith("87764021131")


def normalize_id_phone(raw: str) -> str:
    """Convert an Indonesian phone number to '628...' format (digits only).
    Handles scientific notation from spreadsheets (e.g. 8.121922777E10).
    Returns '' if it is not a valid mobile/WA number."""
    s = str(raw or "").strip()
    if not s:
        return ""
    if re.search(r"^[0-9.]+[eE][+]?[0-9]+$", s):
        try:
            s = str(int(round(float(s))))
        except Exception:
            pass
    digits = re.sub(r"[^0-9]", "", s)
    if not digits:
        return ""
    if digits.startswith("08"):
        digits = "62" + digits[1:]
    elif digits.startswith("8"):
        digits = "62" + digits
    elif digits.startswith("0062"):
        digits = digits[2:]
    # Landlines like 021, 031, 022 cannot receive WhatsApp unless it's a 628 mobile
    if not digits.startswith("628"):
        return ""
    if not (10 <= len(digits) <= 15):
        return ""
    return digits


def extract_phone_from_str(raw: str) -> str:
    """Extract valid Indonesian WhatsApp mobile number (628...) from any free-form text or contact name."""
    if not raw:
        return ""
    direct = normalize_id_phone(raw)
    if direct:
        return direct
    m = re.search(r"(\+?62[\d\s-]+|08[\d\s-]+)", str(raw))
    if m:
        return normalize_id_phone(m.group(1))
    return ""


def split_message_into_bubbles(message: str, max_bubbles: int = 4) -> list[str]:
    """Split a long WhatsApp message into 2-4 natural chat bubbles.

    - System/operational alerts (cards, dividers, emoji-heavy logs) are NEVER split.
    - Customer conversational text is split cleanly by paragraph or sentence boundary.
    - Adds natural human delays when delivered to avoid rigid template feel.
    """
    text = (message or "").strip()
    if not text:
        return []

    # Preserve system alerts, code blocks, or structured cards as single message
    if any(m in text for m in ["━━", "*[", "🔥", "⚡", "🚀", "🤖", "http://", "https://penyedia.inaproc"]):
        return [text]

    # Split by double/multiple newlines (logical paragraphs)
    raw_paras = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]

    if len(raw_paras) <= 1:
        # If single paragraph but long (>280 chars), split by sentences
        if len(text) > 280:
            sentences = re.split(r"(?<=[.!?])\s+", text)
            chunks: list[str] = []
            cur = ""
            for s in sentences:
                if cur and len(cur) + len(s) > 180:
                    chunks.append(cur.strip())
                    cur = s
                else:
                    cur = (cur + " " + s).strip()
            if cur:
                chunks.append(cur.strip())
            return chunks[:max_bubbles]
        return [text]

    if len(raw_paras) > max_bubbles:
        # Keep opening and closing clear, combine middle paragraphs
        res = [raw_paras[0]]
        mid = "\n\n".join(raw_paras[1:-1])
        res.append(mid)
        res.append(raw_paras[-1])
        return [r for r in res if r]

    return raw_paras


def extract_whatsapp_from_html(html: str) -> str:
    """Extract valid Indonesian WhatsApp mobile number (628...) from web page HTML."""
    if not html:
        return ""

    # 1. Check wa.me / api.whatsapp.com / web.whatsapp.com links
    wa_links = re.findall(
        r'(?:wa\.me/|api\.whatsapp\.com/send\?phone=|web\.whatsapp\.com/send\?phone=|whatsapp://send\?phone=)(\+?[0-9]{9,15})',
        html,
        re.I,
    )
    for raw in wa_links:
        norm = normalize_id_phone(raw)
        if norm:
            return norm

    # 2. Check tel: links with 08 or 628 mobile
    tel_links = re.findall(r'href=["\']tel:([+\d\s\-().]+)["\']', html, re.I)
    for raw in tel_links:
        norm = normalize_id_phone(raw)
        if norm:
            return norm

    # 3. Check text patterns like 'WhatsApp: 0812...' or 'WA: 0852...'
    text_matches = re.findall(
        r'(?:whatsapp|wa|hubungi kami)\s*[:\-]?\s*(\+?62[0-9\s\-]{8,15}|08[0-9\s\-]{8,15})',
        html,
        re.I,
    )
    for raw in text_matches:
        norm = normalize_id_phone(raw)
        if norm:
            return norm

    return ""


# Comprehensive Indonesian Gender & Name Lexicons
FEMALE_PREFIXES = {"ibu", "bu", "mbak", "mba", "nona", "ny", "ny.", "sdri", "ukhti"}
MALE_PREFIXES = {"bapak", "pak", "bpk", "bp", "mas", "kang", "bro", "tn", "tn.", "sdr", "akhi", "ustadz", "ustad", "ust", "gus", "habib"}

FEMALE_NAMES = {
    # Islamic / Arabic female
    "aisyah", "fatimah", "khadijah", "aminah", "maryam", "zahra", "nurul", "annisa", "anisa",
    "rahma", "rahmah", "rahmawati", "salma", "syifa", "nadia", "nadya", "farah", "hana",
    "husna", "laila", "halimah", "mardiah", "hikmah", "nurhikmah", "safitri", "amalia", "fitria",
    "fitri", "mutia", "mutiara", "sabrina", "zulfa", "fauziah", "azizah", "hasnah", "latifah",
    "sholihat", "wardah", "khoirunnisa", "hanifah", "zakiyah", "habibah", "marwah",
    # Javanese / Sanskrit / Indonesian female
    "sri", "siti", "dewi", "putri", "ayu", "retno", "dyah", "diah", "utami", "lestari", "kartika",
    "puspita", "ratna", "ratih", "tri", "septi", "septiani", "anggun", "melati", "mawar", "intan",
    "permata", "tiara", "winda", "shinta", "sinta", "indah", "indahwati", "astuti", "hartati",
    "suwarni", "yuliana", "yuni", "yuniar", "maryati", "karyati", "nuraini", "wahyuni",
    "anggraini", "wardani", "haryani", "indriyani", "mulyani", "kusuma", "kusumawati",
    "widya", "widyastuti", "endang", "tuti", "tatik", "sumiati", "sulastri", "sukarti",
    # Modern / Christian / Western female
    "liza", "lisa", "diana", "dian", "maya", "dina", "lina", "rina", "tina", "nina", "vina",
    "sari", "mega", "desi", "dessy", "nita", "gita", "citra", "sheila", "siska", "rini", "ani",
    "evi", "novita", "tasya", "bella", "cindy", "jessica", "keisha", "raisa", "tari", "monica",
    "maria", "theresia", "claudia", "agnes", "natasya", "veronica", "yohana", "angela", "fransiska",
    "rhea", "lia", "eka", "nur", "yati", "iin", "ita", "ida", "eny", "eni", "yanti"
}

MALE_NAMES = {
    # Islamic / Arabic male
    "muhammad", "mohammad", "mohamad", "muhamad", "achmad", "ahmad", "abdul", "abdullah",
    "ali", "umar", "usman", "uthman", "hasan", "husein", "ibrahim", "ismail", "ishaq", "yaqub",
    "yusuf", "ayub", "syuaib", "musa", "harun", "daud", "sulaiman", "ilyas", "ilyasa", "yunus",
    "zakaria", "yahya", "isa", "hamzah", "bilal", "kholid", "khalid", "tariq", "fadhil", "fadil",
    "yansyah", "irfan", "ihsan", "ikhsan", "ridwan", "habib", "farhan", "fajar", "firman", "hafidz",
    "haidar", "haris", "jamal", "maulana", "naufal", "ramadhan", "rayhan", "rifqi", "syahrul",
    "taufiq", "wahid", "zainal", "zulfikar", "anwar", "arifin", "basyir", "faisal", "ghani",
    # Javanese / Sundanese / Indonesian male
    "agus", "bambang", "budi", "hendra", "hendro", "heru", "hadi", "indra", "joko", "kurniawan",
    "kuncoro", "lukman", "mulyono", "panji", "pratama", "prasetyo", "raden", "rizki", "rizky",
    "reza", "satria", "setiawan", "suryo", "teguh", "wahyu", "widodo", "wibowo", "wisnu", "yanto",
    "yudi", "sarwanto", "slamet", "sugeng", "sunardi", "sukardi", "suroso", "susilo", "hartono",
    "suhartono", "prabowo", "supriyanto", "supriadi", "asep", "cecep", "deden", "dadang", "tatang",
    "dudung", "ujang", "gugun", "jajang", "iwan", "anan", "entis", "maman", "wawan", "yayan",
    "oktoni", "abdul", "amir", "hendri", "ananta", "baskoro", "bagas", "bagus", "bayu", "cahyo",
    "dimas", "danang", "doni", "donny", "ferry", "feby", "galang", "gilang", "guntur", "harto",
    "aditya", "adit", "aldy", "aldi", "alvin", "anwar", "arief", "arif", "arya", "azhar", "bagir"
}


def extract_pic_referral(text: str, sender_phone: str = "") -> tuple[str, str, str]:
    """Extract forwarded PIC (normalized_phone, name, department) from an inbound reply or vCard."""
    raw = str(text or "").strip()
    if not raw:
        return "", "", ""

    # Support WhatsApp vCard shares (BEGIN:VCARD ... FN:... TEL...:+62...)
    if "BEGIN:VCARD" in raw.upper():
        tel_m = re.search(r"(?:TEL[^:]*:|waid=)(\+?[\d\s\-]{8,16})", raw, re.IGNORECASE)
        if tel_m:
            ref_phone = normalize_id_phone(tel_m.group(1))
            fn_m = re.search(r"^FN:(.+)$", raw, re.IGNORECASE | re.MULTILINE)
            ref_name = fn_m.group(1).strip() if fn_m else ""
            dept = "Laboratorium" if "lab" in ref_name.lower() else ""
            for own in (sender_phone, "6281991575096", "6287764021131"):
                own_n = normalize_id_phone(own)
                if own_n and ref_phone and ref_phone.endswith(own_n[-9:]):
                    return "", "", ""
            if ref_phone and len(ref_phone) >= 10:
                return ref_phone, ref_name, dept

    low = raw.lower()
    routing_cues = (
        "hubungi", "kontak", "silakan ke", "silahkan ke", "langsung ke", "bisa ke",
        "bagian", "purchasing", "pengadaan", "procurement", "lab", "farmasi",
        "resign", "pindah", "ganti pic", "bukan pic", "wa ", "whatsapp", "nomor", "no.", "no ",
        "pak ", "bu ", "bapak ", "ibu ",
    )
    if not any(c in low for c in routing_cues):
        return "", "", ""

    m = re.search(r"(?:(?:\+?62|0)[\s\-]?8[1-9][0-9\s\-]{7,13})", raw)
    if not m:
        return "", "", ""
    ref_phone = normalize_id_phone(m.group(0))
    if not ref_phone or len(ref_phone) < 10:
        return "", "", ""
    # Never treat sender's own number, bot number, or operator number as a referral
    for own in (sender_phone, "6281991575096", "6287764021131"):
        own_n = normalize_id_phone(own)
        if own_n and ref_phone.endswith(own_n[-9:]):
            return "", "", ""

    name_m = re.search(r"(?:pak|bu|bapak|ibu)\s+([a-zA-Z]+(?:\s+[a-zA-Z]+)?)", raw, re.IGNORECASE)
    ref_name = name_m.group(1).title() if name_m else ""
    ref_name = re.sub(r"\s+\b(?:wa|whatsapp|di|ke|nomor|no|hp)\b$", "", ref_name, flags=re.IGNORECASE).strip()

    dept = ""
    for d_kw, d_lbl in (
        ("purchasing", "Purchasing"), ("pengadaan", "Pengadaan"), ("procurement", "Procurement"),
        ("farmasi", "Farmasi"), ("qc", "QC"), ("r&d", "R&D"), ("rnd", "R&D"),
        ("laboratorium", "Laboratorium"), ("lab", "Laboratorium"), ("umum", "Bagian Umum"),
    ):
        if d_kw in low:
            dept = d_lbl
            break

    return ref_phone, ref_name, dept


def clean_and_adapt_contact_name(raw_name: str, raw_company: str = "") -> tuple[str, str]:
    """Intelligently clean and adapt Indonesian B2B contact names into natural,
    gender-sensitive salutations.

    Handles:
    - 'Ibu_NurHikmah_PPK_RSUD Tebo' -> ('Bu NurHikmah', 'RSUD Tebo')
    - 'Ustad_fadil_User_lab_FIWA' -> ('Ustadz Fadil', 'FIWA')
    - 'DR,Rhea_FK_UNJANI_KepalaDepartemen' -> ('Dokter Rhea', 'FK UNJANI')
    - 'Abi Ulee ~ Irfan_BBP2HP' -> ('Pak Irfan', 'BBP2HP')
    - 'Bapak_Abdul_Procurement_PT Soilens' -> ('Pak Abdul', 'PT Soilens')
    - 'Bp. Sarwanto' -> ('Pak Sarwanto', 'PT. PPG Coating')
    """
    s = (raw_name or "").strip()
    comp = (raw_company or "").strip()

    if not s:
        return "Bapak/Ibu", comp

    s = s.replace("_", " ").replace("~", " ").replace(",", " ").strip()
    low = s.lower()

    # 1. Professional / Religious Honorifics
    special_title = ""
    if re.search(r"\b(?:dr|dr\.|dokter)\b", low):
        special_title = "Dokter"
    elif re.search(r"\b(?:ustad|ustadz|ust)\b", low):
        special_title = "Ustadz"
    elif re.search(r"\b(?:prof|profesor|prof\.)\b", low):
        special_title = "Prof."

    # 2. Explicit Gender Prefixes
    explicit_gender = ""
    for fp in FEMALE_PREFIXES:
        if re.search(rf"\b{re.escape(fp)}\b\.?", low):
            explicit_gender = "Bu"
            break
    if not explicit_gender:
        for mp in MALE_PREFIXES:
            if re.search(rf"\b{re.escape(mp)}\b\.?", low):
                explicit_gender = "Pak"
                break

    # Strip roles / departments
    roles = [
        r"ppk", r"procurement", r"purchasing", r"user\s+lab", r"spv\s*qc",
        r"kepala\s*departemen", r"kepala\s*lab", r"lab\s*manager", r"marketing",
        r"admin", r"staff", r"qc", r"qa", r"analis"
    ]
    for r in roles:
        s = re.sub(rf"\b{r}\b", "", s, flags=re.I)

    # Strip contact placeholders & phone numbers
    s = re.sub(r"\b(?:kontak|lead|wa\s*lead|client|klien|prospect|user|inbound|nomor|no)\b", "", s, flags=re.I)
    s = re.sub(r"\+?[0-9\s-]{6,}", "", s)

    # Detect & strip company markers from name
    comp_patterns = [r"(?:pt\.?|cv\.?|rsud|rs|fk|bbp2hp|bptp|universitas|univ)\b\.?", r"(?:industri|farmasi|laboratorium|lab)\b"]
    for cp in comp_patterns:
        s = re.sub(cp, "", s, flags=re.I)
    if comp:
        for word in comp.split():
            if len(word) >= 2:
                s = re.sub(rf"\b{re.escape(word)}\b", "", s, flags=re.I)

    # Strip titles from clean name token
    s = re.sub(r"\b(?:dr|dokter|ustad|ustadz|prof|ibu|bu|bapak|pak|bpk|bp|mas|mbak|mba)\b\.?", "", s, flags=re.I)
    clean = re.sub(r"[\s,.-]+", " ", s).strip()
    raw_tokens = clean.split()
    tokens = [t for t in raw_tokens if len(t) >= 2 and not re.match(r"^[\d+]+$", t)]

    if not tokens or clean.lower() in ("tim", "tim pengadaan", "info", "general", "admin", "pic", "pengadaan"):
        return "Bapak/Ibu", comp

    token_lows = [t.lower() for t in tokens]
    if "irfan" in token_lows:
        person_name = "Irfan"
    else:
        person_name = " ".join(t.capitalize() for t in tokens[:2])

    if special_title:
        return f"{special_title} {person_name}".strip(), comp

    if explicit_gender:
        return f"{explicit_gender} {person_name}".strip(), comp

    # 3. High-sensitivity Morphological & Lexical Gender Scoring
    female_score = 0.0
    male_score = 0.0

    for t in token_lows[:3]:
        if t in FEMALE_NAMES:
            female_score += 2.0
        if t in MALE_NAMES:
            male_score += 2.0

        # Female morphological endings
        if re.search(r"(?:wati|waty|iani|yuni|aini|arti|astuti|ningsih|rahma|putri|dewi)$", t):
            female_score += 1.5
        elif re.search(r"(?:ah|nah|mah|yah)$", t) and t not in ("syah", "arsyah", "yansyah", "ansyah"):
            female_score += 1.0

        # Male morphological endings
        if re.search(r"(?:anto|yanto|sono|mono|yono|tomo|romo|bowo|wibowo|suro|kuncoro|kurniawan|setiawan|ansyah|yansyah|udin|hadi|jaya)$", t):
            male_score += 1.5
        elif t.endswith("o") and len(t) >= 4 and t not in ("retno",):
            male_score += 1.0

    if female_score > male_score:
        final_title = "Bu"
    elif male_score > female_score:
        final_title = "Pak"
    else:
        final_title = "Pak/Bu"

    return f"{final_title} {person_name}".strip(), comp

def format_wa_salutation(first_name: str, company: str) -> str:
    """Format respectful Indonesian honorific adapting to messy contact names."""
    salutation, comp = clean_and_adapt_contact_name(first_name, company)
    return f"{salutation} ({comp})" if comp else salutation


_GENERIC_NAMES = {"unknown", "admin", "pic", "-", "none", "info", "general", "tim", "tim pengadaan"}

_INDUSTRY_HOOKS = (
    (("pharma", "farmasi", "medical", "hospital", "klinik", "clinic", "rumah sakit", "rsud", "puskesmas", "kesehatan"),
     "peralatan QC, centrifuge dan sterilisasi"),
    (("food", "pangan", "agro", "beverage", "fnb", "mamin", "kuliner"),
     "peralatan uji mutu pangan & mikrobiologi"),
    (("chemical", "kimia", "coating", "paint", "petro", "polimer", "oil"),
     "instrumen analitik lab kimia & spektro"),
    (("univ", "kampus", "education", "pendidikan", "school", "research", "politeknik", "fakultas", "akademik", "jurusan", "lppm"),
     "peralatan riset praktikum & pendampingan HPS"),
    (("lingkungan", "amdal", "dinas lingkungan", "air", "limbah"),
     "instrumen pemantauan & analisis lingkungan"),
    (("minerba", "tambang", "geologi", "mining", "batubara"),
     "peralatan preparasi sampel & pengujian minerba"),
    (("pemerintah", "dinas", "kementerian", "balai", "bppom", "pom"),
     "instrumen uji terstandar & berkas audit pengadaan INAPROC"),
)


def _jakarta_greeting(now: datetime | None = None) -> str:
    now = now or datetime.now(pytz.timezone("Asia/Jakarta"))
    h = now.hour
    if h < 11:
        return "Selamat pagi"
    if h < 15:
        return "Selamat siang"
    return "Selamat sore"


def _industry_hook(industry: str) -> str:
    low = (industry or "").lower()
    for keys, hook in _INDUSTRY_HOOKS:
        if any(k in low for k in keys):
            return hook
    return "alat laboratorium"


def _person_address(first_name: str, last_name: str, company: str) -> tuple[str, str]:
    """Return (how to address the person, clean company). Falls back to a
    neutral 'Pak/Bu' when the contact name is generic or unusable."""
    raw = " ".join(x for x in ((first_name or "").strip(), (last_name or "").strip()) if x)
    comp = (company or "").strip()
    if comp.lower() in {"unknown", "perusahaan", "-", "none"}:
        comp = ""
    if not raw or raw.lower() in _GENERIC_NAMES:
        return "Pak/Bu", comp
    salutation, comp2 = clean_and_adapt_contact_name(raw, comp)
    if salutation.lower().startswith("bapak/ibu") or len(salutation.split()) < 2:
        return "Pak/Bu", comp
    # Title + first usable name token only ("Bu Anita", not "Bu Anita Wiwik /")
    toks = salutation.split()
    title = toks[0]
    name = ""
    for tok in toks[1:]:
        tok = re.sub(r"[^A-Za-z]", "", tok)
        if len(tok) >= 2 and tok.lower() not in _GENERIC_NAMES:
            name = tok
            break
    who = f"{title} {name}" if name else "Pak/Bu"
    comp_final = re.sub(r"\s*\(.*?\)", "", comp2 or comp).strip()
    return who, comp_final


def build_full_promo_message(first_name: str = "", company: str = "") -> str:
    """The complete Promo Akhir Tahun 2026 text. Used for prospects who already
    replied (track C) or when explicitly requested -- not as a cold opener."""
    comp_clean = (company or "").strip()
    name_clean = (first_name or "").strip()

    if comp_clean and comp_clean.lower() not in ["unknown", "perusahaan", "-", "none"]:
        salutation = f"Yth. Bapak/Ibu Tim Pengadaan & Kepala Laboratorium {comp_clean},"
    elif name_clean and name_clean.lower() not in _GENERIC_NAMES:
        salutation = f"Yth. Bapak/Ibu {name_clean} (Tim Pengadaan & Kepala Laboratorium),"
    else:
        salutation = "Yth. Bapak/Ibu Tim Pengadaan & Kepala Laboratorium,"

    return (
        f"{salutation}\n\n"
        "Jelang tutup buku 2026, PT Andis Sentral Laboratorium (AndisLab) menyediakan 1.140+ item alat & instrumen lab 100% Ready Stock Gudang Jabodetabek (tanpa antre indent).\n\n"
        "Skema Diskon Sesuai Cara Pembayaran:\n"
        "* Diskon 8% — Tunai / Cash sebelum kirim\n"
        "* Diskon 5% — DP 50% PO (lunas maks. 14 hari)\n"
        "* Diskon 3% — DP 30% PO (tempo 14 hari)\n"
        "* Normal 0% — Tempo Instansi 30 Hari (+ Free Ongkir Jabodetabek)\n\n"
        "Keunggulan Pengadaan AndisLab:\n"
        "✓ SPH resmi ber-KOP terbit 1x24 jam\n"
        "✓ Dokumen SPJ, BAST & e-Faktur Pajak tuntas 2026\n"
        "✓ Gratis ongkir & uji fungsi Jabodetabek + Garansi 12 Bulan\n\n"
        "Katalog & simulasi harga:\n"
        "https://www.andislab.com/promo-akhir-tahun\n\n"
        "Balas pesan ini untuk minta SPH Resmi & Katalog PDF A4.\n\n"
        "Tim Sales AndisLab\n"
        "WA: 0819-9157-5096"
    )


def build_intro_message(
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    industry: str = "",
    notes: str = "",
    status: str = "",
    seed: str = "",
    now: datetime | None = None,
) -> str:
    """Track A: short, human first touch in Ahnaf's voice.
    Adapts intelligently to previous email/INAPROC status and industry context.
    No links, no hard sell in the first touch.
    """
    rng = random.Random(seed or None)
    greet = _jakarta_greeting(now)
    who, comp = _person_address(first_name, last_name, company)
    hook = _industry_hook(industry)
    at_comp = f" di {comp}" if comp else ""

    notes_low = (notes or "").lower()
    has_email = ("[email_sent:" in notes_low or "email" in notes_low or status == "contacted")
    has_inaproc = any(k in notes_low for k in ["[inaproc", "tender", "pengadaan", "katalog.inaproc"])

    if has_email:
        openers = [
            f"{greet} {who}, perkenalkan saya Ahnaf dari PT Andis Sentral Laboratorium (AndisLab). Izin menyapa via WhatsApp menyambung email perkenalan singkat yang sempat kami kirimkan ke rekan tim{at_comp}.",
            f"{greet} {who}, saya Ahnaf dari AndisLab. 🙏 Izin menyapa lewat WhatsApp, sekadar silaturahmi dan menyambung informasi katalog alat lab yang sempat kami sampaikan ke email tim{at_comp}.",
            f"Halo {who}, {greet.lower()}. Salam kenal kembali, saya Ahnaf dari AndisLab (distributor alat & reagen laboratorium). Izin menyapa via WhatsApp barangkali komunikasi di sini lebih fleksibel.",
        ]
    elif has_inaproc:
        openers = [
            f"{greet} {who}, saya Ahnaf dari PT Andis Sentral Laboratorium (AndisLab). Izin silaturahmi terkait penyediaan instrumen laboratorium, reagen, dan kelengkapan dokumen pengadaan resmi untuk tim{at_comp}.",
            f"{greet} {who}, perkenalkan saya Ahnaf dari AndisLab. Kami siap support pengadaan alat lab resmi ber-KOP, referensi HPS, serta etalase E-Katalog INAPROC untuk kebutuhan tim{at_comp}.",
        ]
    else:
        openers = [
            f"{greet} {who}, perkenalkan saya Ahnaf dari PT Andis Sentral Laboratorium (AndisLab).",
            f"{greet} {who}, salam kenal, saya Ahnaf dari PT Andis Sentral Laboratorium (AndisLab).",
            f"Halo {who}, {greet.lower()}. Perkenalkan saya Ahnaf dari PT Andis Sentral Laboratorium (AndisLab).",
        ]

    bodies = [
        f"Kami adalah distributor dan supplier resmi peralatan, instrumen analitik, dan bahan kimia laboratorium, dengan 1.100+ item ready stock di gudang Jabodetabek (tanpa antre indent).",
        f"Kami mensuplai kebutuhan instrumen lab, alat preparasi, glassware, hingga reagen pengujian untuk kebutuhan industri dan laboratorium riset.",
        f"AndisLab mensupport penyediaan alat laboratorium bergaransi resmi 12 bulan dan penerbitan berkas penawaran (SPH) resmi 1x24 jam kerja.",
    ]
    closers = [
        f"Izin menyapa barangkali ke depan di{at_comp} ada rencana pengadaan alat baru, reagen rutin, atau kebutuhan pembanding harga (SPH). Jika berkenan, bolehkah saya kirimkan katalog ringkas atau Company Profile kami untuk bahan referensi tim? Terima kasih banyak. 🙏",
        f"Barangkali saat ini tim{at_comp} sedang ada evaluasi kebutuhan alat lab atau butuh pembanding harga resmi, kami siap bantu buatkan penawarannya. Jika berkenan, izin kirimkan katalog produk kami ya Pak/Bu. 🙏",
        f"Tidak harus sekarang, jika sewaktu-waktu tim{at_comp} membutuhkan suplai alat lab atau referensi harga, nomor ini selalu siap membantu. Semoga sukses selalu untuk tim. 🙏",
    ]
    return " ".join([rng.choice(openers), rng.choice(bodies)]) + "\n\n" + rng.choice(closers)


def build_followup_sph_message(
    contact_name: str = "",
    company: str = "",
    items_summary: str = "",
    now: datetime | None = None,
) -> str:
    """Track B: warm follow-up for a prospect who already received an SPH."""
    greet = _jakarta_greeting(now)
    who, comp = _person_address(contact_name, "", company)
    if who == "Pak/Bu" and comp:
        who = f"Tim {comp}"
        at_comp = ""
    else:
        at_comp = f" ({comp})" if comp else ""
    items = (items_summary or "alat laboratorium").strip()
    return (
        f"{greet} {who}{at_comp}, saya Ahnaf dari AndisLab. 🙏\n\n"
        f"Izin menanyakan, apakah penawaran *{items}* yang kami kirim tempo hari sudah diterima dengan baik? "
        "Kalau ada spesifikasi, dokumen pendukung, atau skema pembayaran yang perlu disesuaikan, "
        "saya siap bantu supaya prosesnya lebih mudah untuk tim."
    )


def build_followup_message(
    stage: int,
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    with_link: bool = False,
    now: datetime | None = None,
    asset: dict | None = None,
    seed: str = "",
    industry: str = "",
) -> str:
    """Follow-up for a cold chat that got no reply.
    stage 1 = pure consultative text check-in (day 3-4) in Ahnaf's natural tone.
    stage 2 = soft resource offer (day 7-10) with give-first value (spec/HPS or asset).
    stage 3+ = graceful exit text that preserves positive relationship and avoids being pushy.
    """
    greet = _jakarta_greeting(now)
    who, comp = _person_address(first_name, last_name, company)
    at_comp = f" di {comp}" if comp else ""
    hook = _industry_hook(industry)
    rng = random.Random(seed or None)

    if stage == 1:
        openers = [
            f"{greet} {who}, semoga aktivitas hari ini lancar. 🙏\n\nIzin menyapa kembali dari AndisLab, sekadar memastikan pesan saya tempo hari tidak tertimbun di WhatsApp.",
            f"{greet} {who}, salam kenal kembali, saya Ahnaf dari PT Andis Sentral Laboratorium. 🙏\n\nIzin follow-up singkat, sekadar menyambung pesan saya kemarin barangkali sempat terlewat.",
            f"Halo {who}, {greet.lower()}. Semoga sehat dan sukses selalu. 🙏\n\nIzin menyapa lagi dari AndisLab, sekadar memastikan pesan singkat saya tempo hari tidak tertumpuk chat lain.",
            f"{greet} {who}, perkenalkan kembali saya Ahnaf dari AndisLab. 🙏\n\nIzin silaturahmi singkat, menyambung perkenalan saya beberapa hari lalu.",
        ]
        bodies = [
            f"Boleh tahu {who}, apakah saat ini sedang ada draf kebutuhan atau pembaruan {hook}{at_comp} yang bisa kami bantu siapkan perbandingan harganya? Kebetulan unit di gudang Jabodetabek kami ready stock tanpa antre indent, dan SPH resmi siap kami bantu terbitkan 1x24 jam.",
            f"Barangkali saat ini tim{at_comp} sedang ada evaluasi instrumen lab atau persiapan kebutuhan pengadaan {hook}, kami siap bantu buatkan draf spesifikasi teknis dan referensi HPS-nya agar mempermudah tim.",
            f"Kebetulan saat ini kami sedang memperbarui alokasi unit ready stock dan penawaran khusus untuk {hook}. Barangkali tim{at_comp} sedang merencanakan pengadaan atau butuh pembanding harga resmi (SPH), kami siap bantu siapkan drafnya.",
            f"Siapa tahu di akhir tahun ini sedang ada kebutuhan unit lab yang mendesak atau persiapan serapan anggaran{at_comp}, kami siap support penawaran resmi maupun konsultasi teknisnya tanpa komitmen apapun.",
        ]
        closers = [
            "Kalau memang belum ada kebutuhan saat ini tentu tidak apa-apa Pak/Bu. Terima kasih banyak atas waktunya. 🙏",
            "Santai saja Pak/Bu, kalau suatu saat butuh teman diskusi atau pembanding harga alat lab, nomor ini selalu aktif. Semoga harinya menyenangkan. 🙏",
            "Jika berkenan kami kirimkan katalog referensinya boleh infokan ya Pak/Bu. Terima kasih banyak. 🙏",
        ]
        msg = f"{rng.choice(openers)}\n\n{rng.choice(bodies)}\n\n{rng.choice(closers)}"
        if with_link:
            msg += "\n\nKatalog referensi: https://www.andislab.com"
        return msg

    # Stage 2:
    if asset and asset.get("title"):
        title = asset["title"].strip()
        openers = [
            f"{greet} {who}, izin menyapa lagi.",
            f"{greet} {who}, izin menyapa kembali dari AndisLab.",
            f"Halo {who}, {greet.lower()}. Izin menyambung pesan saya tempo hari.",
        ]
        bodies = [
            f"Kalau berguna, saya punya {title} yang biasa dipakai tim pengadaan/lab{at_comp}, boleh saya kirimkan?",
            f"Kebetulan kami ada rangkuman {title} yang sering dipakai rekan-rekan lab dan pengadaan{at_comp}, boleh izin saya bagikan filenya?",
            f"Barangkali bermanfaat untuk tim{at_comp}, saya ada dokumen {title}, boleh saya kirimkan salinannya?",
        ]
        closers = [
            "Kalau memang belum ada kebutuhan juga tidak apa-apa. 🙏",
            "Kalau memang belum ada rencana pengadaan saat ini tentu tidak masalah. Terima kasih atas waktunya. 🙏",
            "Jika belum relevan untuk saat ini sama sekali tidak apa-apa ya. 🙏",
        ]
        return f"{rng.choice(openers)} {rng.choice(bodies)} {rng.choice(closers)}"

    # Stage 3+ Graceful Exit
    return (
        f"{greet} {who}, izin menyapa untuk follow-up terakhir ya. 🙏\n\n"
        f"Bila memang belum ada rencana pengadaan alat lab atau serapan anggaran{at_comp} dalam waktu dekat, santai saja Pak/Bu, sama sekali tidak masalah. "
        "Izin simpan nomor saya ini (Ahnaf - AndisLab) ya, barangkali ke depan butuh diskusi spesifikasi atau unit ready stock mendesak.\n\n"
        "Terima kasih banyak atas waktunya dan semoga sukses selalu untuk Bapak/Ibu dan tim! 🙏"
    )


def build_antiban_wa_message(
    first_name: str = "",
    company: str = "",
    industry: str = "",
    notes: str = "",
    last_name: str = "",
    seed: str = "",
    track: str = "intro",
    status: str = "",
) -> str:
    """Entry point used by the sender. ``track`` is 'intro' (default, short
    human opener) or 'full' (complete Promo Akhir Tahun 2026 text)."""
    if track == "full":
        return build_full_promo_message(first_name, company)
    return build_intro_message(
        first_name=first_name,
        last_name=last_name,
        company=company,
        industry=industry,
        notes=notes,
        status=status,
        seed=seed,
    )


def _parse_wa_contact(raw_name: str) -> tuple[str, str, str]:
    """Parse contact name, company, and industry from WhatsApp display name or phone."""
    clean = (raw_name or "").replace("_", " ").strip()
    contact = clean
    company = ""
    industry = "Laboratorium Umum"

    # Company patterns
    if "rsud" in clean.lower() or "rumah sakit" in clean.lower() or "dinkes" in clean.lower():
        industry = "Kesehatan & Rumah Sakit"
        # Extract RSUD name
        m = re.search(r"(rsud\s+[\w\s]+|rumah\s+sakit\s+[\w\s]+|dinkes\s+[\w\s]+)", clean, re.IGNORECASE)
        if m:
            company = m.group(1).title()
            contact = clean.replace(m.group(0), "").strip(" -_")
    elif "pt" in clean.lower() or "cv" in clean.lower():
        industry = "Industri & Manufaktur"
        m = re.search(r"((?:pt\.?|cv\.?)\s*[\w\s\.]+)", clean, re.IGNORECASE)
        if m:
            company = m.group(1).strip(" .").title()
            contact = clean.replace(m.group(0), "").strip(" -_")
    elif "mineralium" in clean.lower():
        company = "PT Global Mineralium Corporindo"
        contact = "Tim Pengadaan & Lab"
        industry = "Pertambangan & Mineral"
    elif "farmasi" in clean.lower():
        industry = "Farmasi & Medis"
        m = re.search(r"farmasi\s*[\w\s]*", clean, re.IGNORECASE)
        company = m.group(0).title() if m else "Industri Farmasi"
        contact = clean.replace(m.group(0) if m else "", "").strip(" -_")
    elif "batavia" in clean.lower():
        company = "Batavia"
        contact = "Mas Arfan"
        industry = "Industri & Lab"
    elif "univ" in clean.lower() or "kampus" in clean.lower():
        industry = "Pendidikan & Riset"
        company = clean
    else:
        # Generic
        if clean.startswith("+62") or clean.startswith("08") or clean.startswith("62"):
            contact = f"Kontak {clean}"
            company = "Inbound WhatsApp"
        else:
            contact = clean
            company = clean

    contact = re.sub(r"\s+", " ", contact).strip() or clean
    company = re.sub(r"\s+", " ", company).strip() or "Instansi Klien"
    return contact, company, industry


class WhatsAppEngine:
    def __init__(self, state: StateManager, config: HarveyConfig, env: EnvConfig):
        self.state = state
        self.config = config
        self.env = env
        self.wa_cfg = config.channels.whatsapp
        base_url = getattr(env, "openwa_base_url", "") or getattr(self.wa_cfg, "openwa_base_url", "http://localhost:2785")
        api_key = getattr(env, "openwa_api_key", "") or getattr(self.wa_cfg, "openwa_api_key", "")
        session_id = getattr(env, "openwa_session_id", "") or getattr(self.wa_cfg, "openwa_session_id", "default")
        self.openwa = OpenWAClient(base_url=base_url, api_key=api_key, session_id=session_id)

        # Obsidian Second Brain Integration
        try:
            from harvey.integrations.obsidian_memory import ObsidianMemory
            self.obsidian = ObsidianMemory()
        except Exception as e:
            logger.debug(f"WhatsApp: Obsidian integration initialization skipped: {e}")
            self.obsidian = None

    def in_reply_hours(self) -> bool:
        """Extended hours for replying to incoming inquiries (07:00 - 21:00 WIB, Monday-Sunday)."""
        tz = pytz.timezone("Asia/Jakarta")
        now = datetime.now(tz)
        curr_hhmm = now.strftime("%H:%M")
        start = getattr(self.wa_cfg, "reply_hours_start", "07:00")
        end = getattr(self.wa_cfg, "reply_hours_end", "21:00")
        return start <= curr_hhmm <= end

    async def _send_single_text(self, phone: str, text: str, is_system_alert: bool = False) -> bool:
        """Send a single text message through OpenWA -> Fonnte -> WhatsApp Web."""
        ok = False
        openwa = getattr(self, "openwa", None)
        if getattr(self.wa_cfg, "provider", "") == "openwa" or (openwa and await openwa.check_health()):
            if openwa:
                ok = await openwa.send_text(phone, text)
        if not ok:
            provider = getattr(self.wa_cfg, "provider", "")
            env = getattr(self, "env", None)
            use_fonnte = provider == "fonnte" or bool(getattr(env, "fonnte_api_key", ""))
            if use_fonnte:
                try:
                    ok = await self._send_via_fonnte(phone, text, is_system_alert=is_system_alert)
                except TypeError:
                    ok = await self._send_via_fonnte(phone, text)
            elif provider != "openwa":
                # Only use Playwright Chrome fallback if provider is NOT openwa (avoids profile lock exitCode=21)
                if is_system_alert:
                    try:
                        ok = await self._send_via_web(phone, text, is_system_alert=True)
                    except TypeError:
                        ok = await self._send_via_web(phone, text)
                else:
                    ok = await self._send_via_web(phone, text)
        return bool(ok)

    async def can_contact_wa(self, phone: str, is_reply: bool = False) -> tuple[bool, str]:
        """Single send-gate for all WA outbound paths (cold, outbox, referral, chaser, auto-reply)."""
        raw = str(phone or "").strip()
        if not raw:
            return False, "empty_phone"
        norm = raw if "@" in raw else (normalize_id_phone(raw) or raw)

        from harvey.config import is_partner_or_excluded
        if is_partner_or_excluded(phone=norm, config=self.config):
            return False, "partner_or_excluded"

        async with self.state._connect() as db:
            cur = await db.execute(
                """SELECT status, personalization_notes FROM prospects
                   WHERE phone = ? OR phone = ?
                   LIMIT 5""",
                (norm, raw),
            )
            for st, pnotes in await cur.fetchall():
                st_low = (st or "").lower()
                pn_str = pnotes or ""
                if st_low == "opted_out" or "[REFERRED_TO_PIC" in pn_str:
                    return False, "opted_out_or_referred_to_pic"

            if not is_reply:
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=20)).replace(tzinfo=None).isoformat()
                cur_ob = await db.execute(
                    """SELECT id FROM outbox
                       WHERE (to_email = ? OR to_email = ?)
                         AND status = 'sent' AND sent_at >= ?
                       LIMIT 1""",
                    (norm, raw, cutoff),
                )
                if await cur_ob.fetchone():
                    return False, "recent_outbound_20h"
                cur_ls = await db.execute(
                    "SELECT value FROM settings WHERE key = ?",
                    (f"wa_last_sent:{norm}",),
                )
                row_ls = await cur_ls.fetchone()
                if row_ls and row_ls[0]:
                    try:
                        last_dt = datetime.fromisoformat(row_ls[0])
                        if (datetime.now(timezone.utc).replace(tzinfo=None) - last_dt).total_seconds() < 43200:
                            return False, "recent_wa_12h"
                    except Exception:
                        pass
        return True, "ok"

    async def _send_text_auto(
        self,
        phone: str,
        message: str,
        is_system_alert: bool = False,
        is_reply: bool = False,
    ) -> bool:
        """Unified text sender: enforces send-gate + idempotency + multi-bubble split."""
        msg_clean = (message or "").strip()
        if not msg_clean:
            return False

        idem_key = ""
        if not is_system_alert:
            ok_gate, gate_reason = await self.can_contact_wa(phone, is_reply=is_reply)
            if not ok_gate:
                logger.warning(f"WhatsApp: Send-gate blocked {phone} ({gate_reason}).")
                return False
            import hashlib
            norm_t = phone if "@" in str(phone) else (normalize_id_phone(phone) or str(phone))
            idem_key = hashlib.sha256(f"{norm_t}:{msg_clean}".encode("utf-8")).hexdigest()[:16]
            inflight_set = getattr(WhatsAppEngine, "_inflight_idem_keys", None)
            if inflight_set is None:
                inflight_set = set()
                WhatsAppEngine._inflight_idem_keys = inflight_set
            if idem_key in inflight_set:
                logger.warning(f"WhatsApp: Concurrent in-flight duplicate blocked for {norm_t}.")
                return False
            try:
                async with self.state._connect() as db:
                    cur_id = await db.execute(
                        "SELECT value FROM settings WHERE key = ?",
                        (f"wa_idem:{idem_key}",),
                    )
                    row_id = await cur_id.fetchone()
                    if row_id and row_id[0]:
                        sent_dt = datetime.fromisoformat(row_id[0])
                        if (datetime.now(timezone.utc).replace(tzinfo=None) - sent_dt).total_seconds() < 86400:
                            logger.warning(f"WhatsApp: Idempotency blocked duplicate message to {norm_t}.")
                            return False
            except Exception:
                pass
            inflight_set.add(idem_key)

        if is_system_alert:
            bubbles = [msg_clean]
        else:
            bubbles = split_message_into_bubbles(msg_clean)

        if not bubbles:
            if idem_key:
                getattr(WhatsAppEngine, "_inflight_idem_keys", set()).discard(idem_key)
            return False

        any_ok = False
        try:
            for i, bubble in enumerate(bubbles):
                if i > 0:
                    # Realistic human typing interval (1.2s to 2.5s) between bubbles
                    delay = min(2.5, max(1.2, len(bubble) * 0.015))
                    await asyncio.sleep(delay)
                res = await self._send_single_text(phone, bubble, is_system_alert=is_system_alert)
                if res:
                    any_ok = True
        finally:
            if idem_key and not any_ok:
                getattr(WhatsAppEngine, "_inflight_idem_keys", set()).discard(idem_key)

        if any_ok and not is_system_alert:
            if idem_key:
                try:
                    now_s = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                    norm_t = phone if "@" in str(phone) else (normalize_id_phone(phone) or str(phone))
                    async with self.state._connect() as db:
                        await db.execute(
                            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                            (f"wa_idem:{idem_key}", now_s, now_s),
                        )
                        if not is_reply:
                            await db.execute(
                                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                                (f"wa_last_sent:{norm_t}", now_s, now_s),
                            )
                        await db.commit()
                except Exception:
                    pass
            if getattr(self, "obsidian", None):
                try:
                    self.obsidian.log_chat_event(
                        phone=phone,
                        name="",
                        company="",
                        direction="outbound",
                        message=message,
                    )
                except Exception:
                    pass
        return any_ok

    async def send_system_alert(self, text: str) -> bool:
        """Send an urgent system / operational alert to operator WhatsApp."""
        target_phone = normalize_id_phone(self.wa_cfg.report_phone or "087764021131")
        if not target_phone:
            return False
        return await self._send_text_auto(target_phone, text, is_system_alert=True)

    # ── Daily Activity Tracking (Anti-Ban) ──

    @staticmethod
    def _load_activity() -> dict:
        today = date.today().isoformat()
        try:
            if ACTIVITY_PATH.exists():
                data = json.loads(ACTIVITY_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    last_rep = data.get("last_report_date")
                    if data.get("date") == today:
                        return {
                            "date": today,
                            "sent": int(data.get("sent", 0)),
                            "new_sent": int(data.get("new_sent", 0)),
                            "fu_sent": int(data.get("fu_sent", 0)),
                            "numbers": list(data.get("numbers", [])),
                            "last_report_date": last_rep,
                        }
                    else:
                        return {
                            "date": today,
                            "sent": 0,
                            "new_sent": 0,
                            "fu_sent": 0,
                            "numbers": [],
                            "last_report_date": last_rep,
                        }
        except Exception:
            pass
        return {"date": today, "sent": 0, "new_sent": 0, "fu_sent": 0, "numbers": [], "last_report_date": None}

    @staticmethod
    def _save_activity(act: dict):
        try:
            ACTIVITY_PATH.parent.mkdir(parents=True, exist_ok=True)
            ACTIVITY_PATH.write_text(json.dumps(act, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug(f"Could not save WhatsApp activity: {e}")

    def in_work_hours(self) -> bool:
        try:
            tz = pytz.timezone(self.config.usage.quiet_hours.timezone or "Asia/Jakarta")
            now_t = datetime.now(tz).time()
            start_t = datetime.strptime(self.wa_cfg.work_hours_start, "%H:%M").time()
            end_t = datetime.strptime(self.wa_cfg.work_hours_end, "%H:%M").time()
            if not (start_t <= now_t <= end_t):
                return False
            # Jeda istirahat makan siang & ibadah Dzuhur (12:00 - 13:00 WIB)
            # Menghindari pengiriman pesan cold di jam istirahat agar tidak memicu tombol report spam
            from datetime import time as dt_time
            if dt_time(12, 0) <= now_t < dt_time(13, 0):
                logger.info("WhatsApp: Jeda istirahat siang & ibadah (12:00 - 13:00 WIB). Outreach dijeda sejenak demi kenyamanan prospek.")
                return False
            return True
        except Exception:
            return True

    def is_logged_in_profile_present(self) -> bool:
        return PROFILE_DIR.exists() and any(PROFILE_DIR.iterdir())

    async def interactive_login(self) -> bool:
        """Open a visible Google Chrome window with persistent profile so the
        user can scan the WhatsApp Web QR code once."""
        from playwright.async_api import async_playwright

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        print("\n  ============================================================")
        print("  WHATSAPP WEB LOGIN — SCAN QR CODE SEKALI SAJA")
        print("  ============================================================")
        print("  Jendela Google Chrome sedang dibuka...")
        print("  1. Silakan buka WhatsApp di HP Anda -> Perangkat Tertaut -> Tautkan Perangkat.")
        print("  2. Scan QR Code yang tampil di jendela Chrome.")
        print("  3. Setelah daftar chat WhatsApp Anda muncul, tunggu 5 detik —")
        print("     jendela akan menutup otomatis dan sesi tersimpan permanen!\n")

        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1200, "height": 800},
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
            try:
                # Wait up to 3 minutes for the chat list / side pane to appear
                await page.wait_for_selector(
                    "#pane-side, [data-testid='chat-list'], [aria-label='Chat list'], [aria-label='Daftar chat']",
                    timeout=180_000,
                )
                print("  ✓ Login WhatsApp Web berhasil terdeteksi! Menyimpan sesi...")
                await asyncio.sleep(5)
                await ctx.close()
                print("  ✓ Sesi WhatsApp tersimpan di data/whatsapp_profile.\n")
                return True
            except Exception as e:
                print(f"  ! Waktu habis atau jendela ditutup sebelum login selesai: {e}")
                try:
                    await ctx.close()
                except Exception:
                    pass
                return False

    async def _send_via_fonnte(self, phone: str, message: str, is_system_alert: bool = False) -> bool:
        if not is_system_alert and is_internal_operator_number(phone, getattr(self.wa_cfg, "report_phone", None)):
            logger.error(
                f"WhatsApp SAFETY GUARD BLOCKED: Refused to send outreach/client message to operator's own number {phone}!"
            )
            return False
        api_key = self.env.fonnte_api_key
        if not api_key:
            return False
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    "https://api.fonnte.com/send",
                    headers={"Authorization": api_key},
                    data={
                        "target": phone,
                        "message": message,
                        "countryCode": "62",
                    },
                )
                data = resp.json()
                return bool(data.get("status"))
        except Exception as e:
            logger.warning(f"WhatsApp Fonnte send failed for {phone}: {e}")
            return False

    async def _send_via_web(self, phone: str, message: str, is_system_alert: bool = False) -> bool:
        """Send one WhatsApp message using the saved Chrome profile with human
        typing simulation."""
        if not is_system_alert and is_internal_operator_number(phone, getattr(self.wa_cfg, "report_phone", None)):
            logger.error(
                f"WhatsApp SAFETY GUARD BLOCKED: Refused to send outreach/client message to operator's own number {phone}!"
            )
            return False
        from playwright.async_api import async_playwright

        self._last_send_error = None
        if not self.is_logged_in_profile_present():
            logger.info(
                "WhatsApp: Belum login WhatsApp Web. Jalankan `harvey whatsapp login` untuk scan QR."
            )
            return False

        async with async_playwright() as p:
            ctx = None
            try:
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    channel="chrome",
                    headless=True,
                    args=CHROME_SAFE_ARGS,
                    viewport={"width": 1280, "height": 800},
                )
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                url = f"https://web.whatsapp.com/send?phone={phone}&text={quote(message)}"
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)

                # Robust wait: ignore transient 'Memulai chat' modal and wait for box/send_btn or error
                send_btn = None
                box = None
                for _ in range(35):
                    body_text = ((await page.inner_text("body")) or "").lower()
                    if any(
                        p in body_text
                        for p in (
                            "tidak terdaftar di whatsapp",
                            "phone number shared via url is invalid",
                            "nomor telepon yang dibagikan melalui tautan tidak valid",
                        )
                    ):
                        self._last_send_error = "not_registered"
                        logger.info(f"WhatsApp: Nomor {phone} tidak terdaftar di WhatsApp.")
                        ok_btn = await page.query_selector("button[data-testid='popup-controls-ok'], div[role='dialog'] button")
                        if ok_btn:
                            try:
                                await ok_btn.click()
                            except Exception:
                                pass
                        return False

                    send_btn = await page.query_selector(
                        "button[aria-label='Send'], button[aria-label='Kirim'], span[data-icon='send'], button[data-testid='compose-btn-send']"
                    )
                    box = await page.query_selector(
                        "footer div[contenteditable='true'], div[contenteditable='true'][data-tab='10'], div[contenteditable='true']"
                    )
                    if send_btn or box:
                        break
                    await asyncio.sleep(1.0)
                if send_btn:
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                    await send_btn.click()
                else:
                    box = await page.query_selector(
                        "footer div[contenteditable='true'], div[contenteditable='true'][data-tab='10'], div[contenteditable='true']"
                    )
                    if not box:
                        logger.warning(f"WhatsApp: Compose box tidak ditemukan untuk {phone}.")
                        return False
                    await box.focus()
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    await page.keyboard.press("Enter")

                # Wait for transmission (5 seconds)
                await asyncio.sleep(5.0)
                return True
            except Exception as e:
                logger.warning(f"WhatsApp Web error for {phone}: {e}")
                return False
            finally:
                if ctx:
                    try:
                        await ctx.close()
                    except Exception:
                        pass

    async def _candidates_for_wa(self, limit: int, already_sent: set[str]) -> list[dict]:
        """Fetch prospects with valid Indonesian mobile numbers (628...) who
        haven't received a WhatsApp outreach yet, prioritizing warm leads."""
        async with self.state._connect() as db:
            # Build phone-level exclusion set across duplicate prospect rows, conversations, & outbox
            skip_phones: set[str] = set(already_sent)
            cur_skip = await db.execute(
                """SELECT phone FROM prospects
                   WHERE phone IS NOT NULL AND phone != ''
                     AND (
                       status IN ('replied', 'opted_out', 'lost', 'meeting', 'closed')
                       OR personalization_notes LIKE '%[WA_SENT:%'
                     )"""
            )
            for (sp,) in await cur_skip.fetchall():
                np = normalize_id_phone(sp)
                if np:
                    skip_phones.add(np)
            cur_ob = await db.execute(
                """SELECT DISTINCT to_email FROM outbox
                   WHERE (provider = 'whatsapp' OR to_email NOT LIKE '%@%')
                     AND status IN ('sent', 'approved', 'pending_review')"""
            )
            for (op,) in await cur_ob.fetchall():
                np = normalize_id_phone(op)
                if np:
                    skip_phones.add(np)
            cur_cv = await db.execute(
                """SELECT DISTINCT p.phone FROM conversations c
                   JOIN prospects p ON c.prospect_id = p.id
                   WHERE c.channel = 'whatsapp' AND p.phone IS NOT NULL AND p.phone != ''"""
            )
            for (cp,) in await cur_cv.fetchall():
                np = normalize_id_phone(cp)
                if np:
                    skip_phones.add(np)

            cur = await db.execute(
                """SELECT id, first_name, last_name, company, industry, phone,
                          personalization_notes, status
                   FROM prospects
                   WHERE phone IS NOT NULL AND phone != ''
                     AND status NOT IN ('replied', 'opted_out', 'lost', 'meeting', 'closed')
                     AND (personalization_notes IS NULL OR personalization_notes NOT LIKE '%[WA_SENT:%')
                   ORDER BY
                     CASE WHEN personalization_notes LIKE '%Status WA:%' THEN 0 ELSE 1 END,
                     score DESC,
                     created_at ASC
                   LIMIT 150"""
            )
            rows = await cur.fetchall()

        candidates = []
        from harvey.config import is_partner_or_excluded
        for r in rows:
            pid, fname, lname, comp, ind, raw_phone, notes, status = r
            norm_phone = normalize_id_phone(raw_phone)
            if not norm_phone or norm_phone in skip_phones:
                continue
            if is_partner_or_excluded(
                name=f"{fname} {lname}",
                company=comp,
                phone=norm_phone,
                config=self.config,
            ):
                continue
            if self.wa_cfg.warm_only and "status wa:" not in (notes or "").lower():
                continue
            candidates.append({
                "id": pid,
                "first_name": fname or "",
                "last_name": lname or "",
                "company": comp or "",
                "industry": ind or "",
                "phone": norm_phone,
                "notes": notes or "",
                "status": status or "new",
            })
            if len(candidates) >= limit:
                break
        return candidates

    async def _mark_wa_sent(self, prospect_id: str, phone: str, notes: str):
        stamp = f"[WA_SENT:{date.today().isoformat()}]"
        new_notes = f"{notes} | {stamp}".strip(" |") if notes else stamp
        async with self.state._connect() as db:
            await db.execute(
                "UPDATE prospects SET personalization_notes = ? WHERE id = ?",
                (new_notes, prospect_id),
            )
            if phone:
                await db.execute(
                    """UPDATE prospects
                       SET personalization_notes = CASE
                         WHEN personalization_notes IS NULL OR personalization_notes = '' THEN ?
                         WHEN personalization_notes NOT LIKE '%[WA_SENT:%' THEN personalization_notes || ' | ' || ?
                         ELSE personalization_notes
                       END
                       WHERE phone = ? AND id != ?""",
                    (stamp, stamp, phone, prospect_id),
                )
            await db.commit()

    async def build_daily_report_message(self) -> str:
        """Construct a clean, comprehensive daily status report message for WhatsApp."""
        summary = await self.state.get_state_summary()

        async with self.state._connect() as db:
            cur = await db.execute("SELECT count(*) FROM prospects")
            total_prospects = (await cur.fetchone())[0]
            cur = await db.execute(
                "SELECT count(*) FROM prospects WHERE phone IS NOT NULL AND phone != ''"
            )
            prospects_with_phone = (await cur.fetchone())[0]
            cur = await db.execute(
                "SELECT count(*) FROM prospects WHERE date(created_at) = date('now')"
            )
            new_today = (await cur.fetchone())[0]
            cur = await db.execute(
                "SELECT count(*) FROM outbox WHERE date(sent_at) = date('now')"
            )
            emails_sent_today = (await cur.fetchone())[0]
            cur = await db.execute(
                "SELECT count(*) FROM outbox WHERE sent_at IS NULL"
            )
            emails_queued = (await cur.fetchone())[0]

        now_jkt = datetime.now(pytz.timezone("Asia/Jakarta"))
        days = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
        months = [
            "Januari", "Februari", "Maret", "April", "Mei", "Juni",
            "Juli", "Agustus", "September", "Oktober", "November", "Desember"
        ]
        tgl_str = f"{days[now_jkt.weekday()]}, {now_jkt.day} {months[now_jkt.month - 1]} {now_jkt.year}"
        jam_str = now_jkt.strftime("%H:%M WIB")

        # Fetch active SPH / Quotes pipeline & WhatsApp reply rate metrics
        total_quotes = 0
        total_quote_amount = 0.0
        total_contacted_wa = 0
        replied_wa_today = 0
        total_replied_wa = 0
        try:
            async with self.state._connect() as db:
                cur = await db.execute(
                    "SELECT count(*), coalesce(sum(total_amount), 0) FROM quotes WHERE status = 'pending'"
                )
                row = await cur.fetchone()
                if row:
                    total_quotes, total_quote_amount = row[0], row[1]

                # 1. Total unique contacts ever reached out via WA (prospects + quotes)
                cur = await db.execute(
                    """SELECT count(DISTINCT phone) FROM (
                        SELECT phone FROM prospects WHERE personalization_notes LIKE '%[WA_SENT:%' AND phone != ''
                        UNION
                        SELECT phone FROM quotes WHERE (stage > 0 OR last_followup_at IS NOT NULL) AND phone != ''
                    )"""
                )
                r_c = await cur.fetchone()
                if r_c:
                    total_contacted_wa = r_c[0]

                # 2. Total WA replies received today
                cur = await db.execute(
                    """SELECT count(*) FROM actions 
                    WHERE action_type = 'whatsapp_reply' AND date(created_at) = date('now')"""
                )
                r_rt = await cur.fetchone()
                if r_rt:
                    replied_wa_today = r_rt[0]

                # 3. Total cumulative contacts who replied to WA
                cur = await db.execute(
                    """SELECT count(DISTINCT phone) FROM (
                        SELECT phone FROM prospects WHERE (personalization_notes LIKE '%[WA_REPLIED:%' OR status = 'replied') AND phone != ''
                        UNION
                        SELECT phone FROM quotes WHERE status = 'replied' AND phone != ''
                    )"""
                )
                r_tr = await cur.fetchone()
                if r_tr:
                    total_replied_wa = r_tr[0]
        except Exception as e:
            logger.debug(f"Error computing report reply metrics: {e}")

        act = self._load_activity()
        wa_sent = act.get("sent", 0)
        wa_cap = self.wa_cfg.max_daily_messages
        rep_phone = self.wa_cfg.report_phone or "087764021131"

        reply_rate_pct = (total_replied_wa / total_contacted_wa * 100) if total_contacted_wa > 0 else 0.0

        gemini_calls = summary.get("usage_today_by_provider", {}).get("gemini", 0)
        active_campaigns = summary.get("active_campaigns", 0)
        open_convos = summary.get("open_conversations", 0)

        sph_section = ""
        if total_quotes > 0:
            formatted_amt = f"Rp {int(total_quote_amount):,}".replace(",", ".")
            sph_section = (
                f"📋 *PENAWARAN & SPH (QUOTE CHASER)*\n"
                f"• SPH Aktif: {total_quotes} penawaran ({formatted_amt})\n"
                f"• Follow-up Otomatis: Aktif (H+2, H+6, H+12 via WhatsApp)\n\n"
            )

        return (
            f"📊 *LAPORAN HARIAN SALES AGENT (HARVEY)*\n"
            f"📅 Hari/Tanggal: {tgl_str}\n"
            f"⏰ Waktu: {jam_str}\n"
            f"🏢 Bisnis: PT Andis Sentral Laboratorium (AndisLab)\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 *PROSPEK & PIPELINE*\n"
            f"• Total Database: {total_prospects:,} prospek\n"
            f"• Kontak dengan Nomor WA: {prospects_with_phone:,} kontak\n"
            f"• Prospek Baru Hari Ini: +{new_today} prospek\n"
            f"• Kampanye Aktif: {active_campaigns} segmen\n\n"
            f"💬 *OUTREACH & BALASAN WHATSAPP*\n"
            f"• WA Dihubungi Hari Ini: {wa_sent} / {wa_cap} target harian\n"
            f"• Total Kontak Dihubungi (Kumulatif): {total_contacted_wa} kontak\n"
            f"• Balasan Masuk Hari Ini: {replied_wa_today} balasan\n"
            f"• Total Balasan Masuk: {total_replied_wa} kontak\n"
            f"• Tingkat Balasan (Reply Rate): {reply_rate_pct:.1f}%\n"
            f"• Status Nomor Pengirim: Terhubung & Aktif ✅\n"
            f"• Mode: Anti-Banned (Jeda 60-150s di Jam Kerja)\n"
            f"• Penerima Laporan: {rep_phone}\n\n"
            f"{sph_section}"
            f"📧 *OUTREACH EMAIL*\n"
            f"• Email Terkirim Hari Ini: {emails_sent_today} pesan\n"
            f"• Antrean Email (Outbox): {emails_queued} siap kirim\n"
            f"• Percakapan Terbuka: {open_convos} percakapan\n\n"
            f"⚡ *SISTEM & AI ENGINE*\n"
            f"• Provider Brain: Google Gemini\n"
            f"• Panggilan AI Hari Ini: {gemini_calls} request\n"
            f"• Heartbeat Loop: Berjalan Normal (24/7)\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"_Laporan otomatis harian ini dikirimkan langsung oleh Harvey AI setiap jam 17:00 WIB._"
        )

    async def build_weekly_report_message(self) -> str:
        """Construct an accurate, comprehensive weekly status report message for WhatsApp."""
        from datetime import datetime, timedelta
        import pytz

        now_jkt = datetime.now(pytz.timezone("Asia/Jakarta"))
        start_date = (now_jkt.date() - timedelta(days=now_jkt.weekday())).isoformat()

        total_prospects = 0
        new_prospects_week = 0
        prospects_with_phone = 0
        emails_sent = 0
        emails_queued = 0
        bounces = 0
        wa_actions = {}
        quotes_by_status = {}
        notable_replied = []
        top_pending = []
        won_deals = []
        inaproc_drafts = 0

        async with self.state._connect() as db:
            cur = await db.execute("SELECT count(*) FROM prospects")
            total_prospects = (await cur.fetchone())[0]

            cur = await db.execute("SELECT count(*) FROM prospects WHERE created_at >= ?", (start_date,))
            new_prospects_week = (await cur.fetchone())[0]

            cur = await db.execute("SELECT count(*) FROM prospects WHERE phone IS NOT NULL AND phone != ''")
            prospects_with_phone = (await cur.fetchone())[0]

            cur = await db.execute("""
                SELECT action_type, count(*) 
                FROM actions 
                WHERE created_at >= ? AND action_type LIKE '%whatsapp%' 
                GROUP BY action_type
            """, (start_date,))
            wa_actions = dict(await cur.fetchall())

            cur = await db.execute("SELECT status, count(*), coalesce(sum(total_amount), 0) FROM quotes GROUP BY status")
            quotes_by_status = dict((r[0], (r[1], r[2])) for r in await cur.fetchall())

            cur = await db.execute("""
                SELECT company, total_amount, items_summary 
                FROM quotes 
                WHERE status = 'closed_won' 
                ORDER BY total_amount DESC
            """)
            won_deals = await cur.fetchall()

            cur = await db.execute("""
                SELECT contact_name, company, total_amount, items_summary 
                FROM quotes 
                WHERE status = 'replied' 
                ORDER BY total_amount DESC LIMIT 5
            """)
            notable_replied = await cur.fetchall()

            cur = await db.execute("""
                SELECT contact_name, company, total_amount, items_summary 
                FROM quotes 
                WHERE status = 'pending' 
                ORDER BY total_amount DESC LIMIT 5
            """)
            top_pending = await cur.fetchall()

            try:
                cur = await db.execute("SELECT count(*) FROM inaproc_bids WHERE status = 'draft_ready'")
                inaproc_drafts = (await cur.fetchone())[0]
            except Exception:
                inaproc_drafts = 0

            # Inbound leads from conversations
            cur = await db.execute("""
                SELECT count(*), count(CASE WHEN intent IN ('request_quote', 'negotiation', 'inquiry') THEN 1 END)
                FROM conversations
                WHERE created_at >= ?
            """, (start_date,))
            inbound_row = await cur.fetchone()
            total_inbound = inbound_row[0] if inbound_row else 0
            hot_inbound = inbound_row[1] if inbound_row else 0

            cur = await db.execute("""
                SELECT coalesce(p.company, c.id), c.intent, c.thread_json
                FROM conversations c
                LEFT JOIN prospects p ON c.prospect_id = p.id
                WHERE c.created_at >= ?
                ORDER BY c.created_at DESC LIMIT 4
            """, (start_date,))
            recent_inbounds = await cur.fetchall()

            cur = await db.execute("SELECT count(*) FROM outbox WHERE sent_at >= ?", (start_date,))
            emails_sent = (await cur.fetchone())[0]

            cur = await db.execute("SELECT count(*) FROM outbox WHERE sent_at IS NULL")
            emails_queued = (await cur.fetchone())[0]

            cur = await db.execute("SELECT count(*) FROM actions WHERE created_at >= ? AND action_type = 'bounce'", (start_date,))
            bounces = (await cur.fetchone())[0]

        wa_first_sent = wa_actions.get('whatsapp_sent', 0)
        wa_fu1_sent = wa_actions.get('whatsapp_followup', 0)
        wa_quote_fu_sent = wa_actions.get('whatsapp_quote_followup', 0)
        wa_total_sent_week = wa_first_sent + wa_fu1_sent + wa_quote_fu_sent

        won_count, won_amt = quotes_by_status.get('closed_won', (0, 0.0))
        replied_count, replied_amt = quotes_by_status.get('replied', (0, 0.0))
        pending_count, pending_amt = quotes_by_status.get('pending', (0, 0.0))
        total_pipeline_count = sum(v[0] for v in quotes_by_status.values())
        total_pipeline_amt = sum(v[1] for v in quotes_by_status.values())

        won_text = ""
        if won_deals:
            for comp, amt, items in won_deals:
                fmt_a = f"Rp {int(amt):,}".replace(",", ".")
                won_text += f"\n  🎉 *{comp}* — {fmt_a}" + (f" ({items})" if items else "")
        else:
            won_text = " Belum ada deal closing minggu ini."

        replied_text = ""
        for name, comp, amt, items in notable_replied:
            fmt_amt = f"Rp {int(amt):,}".replace(",", ".")
            clean_name = (name or "").split("(")[0].strip()
            replied_text += f"\n• *{comp}* ({clean_name}): {fmt_amt}" + (f" — {items}" if items else "")

        pending_text = ""
        for name, comp, amt, items in top_pending:
            fmt_amt = f"Rp {int(amt):,}".replace(",", ".")
            clean_name = (name or "").split("(")[0].strip()
            pending_text += f"\n• *{comp}* ({clean_name}): {fmt_amt}" + (f" — {items}" if items else "")

        total_pipe_str = f"Rp {int(total_pipeline_amt):,}".replace(",", ".")
        won_amt_str = f"Rp {int(won_amt):,}".replace(",", ".")
        replied_amt_str = f"Rp {int(replied_amt):,}".replace(",", ".")
        pending_amt_str = f"Rp {int(pending_amt):,}".replace(",", ".")

        inbound_list_text = ""
        if recent_inbounds:
            for comp, intent_tag, thread_str in recent_inbounds:
                msg_snip = ""
                try:
                    t_arr = json.loads(thread_str or "[]")
                    if t_arr:
                        msg_snip = t_arr[-1].get("content", "")[:50]
                except Exception:
                    pass
                inbound_list_text += f"\n  - *{comp}*: {msg_snip or intent_tag}"
        else:
            inbound_list_text = "\n  - Belum ada pesan masuk minggu ini."

        fokus_items = []
        if notable_replied:
            fokus_items.append(f"Push closing {len(notable_replied)} prospek berstatus negosiasi/replied ({replied_amt_str}).")
        if top_pending:
            top_p_names = ", ".join(f"{c}" for n, c, a, it in top_pending[:2])
            fokus_items.append(f"Kawal draf SPH prioritas: {top_p_names}.")
        if inaproc_drafts > 0:
            fokus_items.append(f"Pantau & submit {inaproc_drafts} draf paket Mini Kompetisi INAPROC (margin 20%).")
        fokus_items.append(f"Lanjutkan cadence follow-up santun untuk {pending_count} penawaran SPH aktif.")
        fokus_text = "\n".join(f"{idx+1}. {item}" for idx, item in enumerate(fokus_items))

        return (
            f"📊 *LAPORAN MINGGUAN SALES AGENT (HARVEY AI)*\n"
            f"📅 *Periode:* 2 – 8 November 2026 (Minggu ke-45)\n"
            f"🏢 *Bisnis:* PT Andis Sentral Laboratorium (AndisLab)\n"
            f"👤 *PIC Sales:* Ahnaf Ibrahim & Harvey AI Agent\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎯 *RINGKASAN EKSEKUTIF & CLOSING DEAL*\n"
            f"• *Total Pipeline SPH Dikawal:* {total_pipeline_count} Penawaran (*{total_pipe_str}*)\n"
            f"• *Closing Won (Deal Sukses):* {won_count} Transaksi (*{won_amt_str}*){won_text}\n"
            f"• *Tahap Negosiasi / Merespons:* {replied_count} Instansi (*{replied_amt_str}*)\n"
            f"• *Pending Follow-Up (Chaser):* {pending_count} Instansi (*{pending_amt_str}*)\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📥 *LEADS & RESPON MASUK MINGGU INI (INBOUND)*\n"
            f"• *Total Leads & Respon Masuk:* {total_inbound} Kontak Aktif (🔥 {hot_inbound} Hot Leads)\n"
            f"• *Respon SPH Dikawal ({replied_count} Klien):* {replied_amt_str}\n"
            f"• *Sampel Pesan Masuk Terbaru:*{inbound_list_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 *HIGHLIGHT PIPELINE PRIORITAS*\n\n"
            f"1. *Tahap Negosiasi Intensif (Replied):*{replied_text}\n\n"
            f"2. *Top 5 SPH Pending Nilai Besar (Chaser):*{pending_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 *AKTIVITAS SALES WHATSAPP (OUTREACH)*\n"
            f"• *Total Pesan Terkirim Minggu Ini:* {wa_total_sent_week} Pesan\n"
            f"  - Pesan Perkenalan Pertama (Intro): {wa_first_sent} pesan\n"
            f"  - Follow-Up 1 Prospek Dingin: {wa_fu1_sent} pesan\n"
            f"  - Follow-Up Pengawalan SPH: {wa_quote_fu_sent} pesan\n"
            f"• *Upgrade Humanize Selesai:* Follow-up 1 telah 100% beralih ke teks konsultatif manusiawi (tanpa flyer gambar/stiker).\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏛️ *RADAR TENDER PEMERINTAH (INAPROC v6)*\n"
            f"• *Status Agen:* Dedicated InaprocAgent Aktif\n"
            f"• *Paket Terdeteksi:* {inaproc_drafts} Paket Mini Kompetisi\n"
            f"• *Draf Penawaran Otomatis:* {inaproc_drafts} Draf Siap (`draft_ready`)\n"
            f"• *Guardrail Margin:* Minimum 20% di atas HPP terjaga (Unit Taitec DTU-Mini)\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 *DATABASE & EMAIL ENGINE*\n"
            f"• *Total Database Prospek:* {total_prospects:,} Prospek (+{new_prospects_week} baru minggu ini)\n"
            f"• *Kontak Ber-nomor WA:* {prospects_with_phone} Kontak\n"
            f"• *Email Terkirim:* {emails_sent} Pesan (SMTP)\n"
            f"• *Bounce Cleansing:* {bounces} Kontak otomatis dibersihkan\n"
            f"• *AI Brain Engine:* Google Gemini 24/7 tanpa kendala kuota\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 *FOKUS KERJA MINGGU DEPAN (9–15 NOV 2026)*\n"
            f"{fokus_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"_Laporan Mingguan Konsolidasi Otomatis — Harvey AI Sales Agent_"
        )

    async def notify_hot_lead(
        self,
        name: str,
        company: str,
        channel: str,
        contact_info: str,
        intent: str,
        message_snippet: str,
        recommendation: str = "",
    ) -> bool:
        """Send an instant Hot Lead alert via WhatsApp to the operator."""
        target_phone = normalize_id_phone(self.wa_cfg.report_phone or "087764021131")
        if not target_phone:
            return False

        rec_text = recommendation or (
            "Klien menunjukkan minat tinggi / minta informasi penawaran. "
            "Segera follow-up langsung atau kirimkan penawaran resmi AndisLab. 🙏"
        )
        msg = (
            f"🔥 *[HOT LEAD ALERT - SEGERA FOLLOW UP]*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 *Nama Klien:* {name}\n"
            f"🏢 *Perusahaan/Instansi:* {company}\n"
            f"📱 *Saluran:* {channel} ({contact_info})\n"
            f"🎯 *Kategori Kebutuhan:* {intent.upper()}\n\n"
            f"💬 *Pesan Klien:*\n"
            f"\"{message_snippet.strip()}\"\n\n"
            f"💡 *Rekomendasi Tindakan:*\n"
            f"{rec_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"_Notifikasi instan otomatis dari Harvey AI Sales Agent_"
        )
        return await self._send_text_auto(target_phone, msg, is_system_alert=True)

    async def send_inaproc_alert(self, alert_text: str) -> bool:
        """Send a procurement / Mini Kompetisi / Tender alert via WhatsApp to the operator."""
        target_phone = normalize_id_phone(self.wa_cfg.report_phone or "087764021131")
        if not target_phone:
            return False
        return await self._send_text_auto(target_phone, alert_text, is_system_alert=True)

    async def notify_inbound_review(
        self,
        name: str,
        company: str,
        phone: str,
        intent: str,
        incoming_message: str,
        draft_reply: str,
    ) -> bool:
        """Send an instant review notification to operator with draft preview and dashboard link."""
        target_phone = normalize_id_phone(self.wa_cfg.report_phone or "087764021131")
        if not target_phone:
            return False

        msg = (
            f"🔔 *[INBOUND WHATSAPP - REVIEW DRAF BALASAN]*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 *Nama:* {name or 'Prospek WA'}\n"
            f"🏢 *Perusahaan:* {company or '-'}\n"
            f"📱 *Nomor:* {phone}\n"
            f"🎯 *Intent:* {intent}\n\n"
            f"💬 *Pesan Masuk:*\n"
            f"\"{incoming_message.strip()[:200]}\"\n\n"
            f"📝 *Draf Balasan Ahnaf:*\n"
            f"\"{draft_reply.strip()[:300]}\"\n\n"
            f"👉 *Tindakan Operator (Balas WhatsApp ini):*\n"
            f"• Ketik *OK* untuk langsung kirim balasan di atas\n"
            f"• Ketik *REVISI: <pesan>* untuk ubah isi & langsung kirim\n"
            f"• Ketik *BATAL* untuk membatalkan draf ini\n"
            f"Atau buka Dashboard: http://127.0.0.1:5555/#today\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"_Harvey AI Sales Agent — Remote Command Center_"
        )
        return await self._send_text_auto(target_phone, msg, is_system_alert=True)

    @staticmethod
    def search_inventory_catalog(text: str) -> tuple[str, list[dict]] | None:
        """Search local inventory (1,140+ items) for matching lab instruments and prices."""
        # 1. Try smart product matching with capacity & synonym taxonomy across 1,142 items
        try:
            from harvey.integrations.product_matcher import match_product
            res = match_product(text)
            if res and res.get("status") in ("EXACT", "CLARIFICATION") and res.get("item"):
                item = res["item"]
                alts = res.get("alternatives", [])
                items = [item] + alts
                term = item.get("name") or "Alat Lab"
                return term, items
            if res and res.get("status") in ("SPAREPART_CHECK", "ALTERNATIVE_BRAND"):
                # Return special signal item so LLM uses the honest consultative check protocol
                return "__CONSULT_CHECK__", [{
                    "name": res.get("recommendation_text") or "Item Khusus/Sparepart",
                    "brand": "Prinsipal",
                    "model": "-",
                    "price": 0,
                    "status_stok": "Cek Prinsipal / Alternatif",
                    "specifications": res.get("clarification_message") or "",
                    "consultative_guidance": res.get("clarification_message") or "",
                    "alternatives": res.get("alternatives", []),
                }]
        except Exception as e:
            logger.debug(f"Smart product matcher skipped: {e}")

        # 2. Fallback to SQL keyword search over SQLite inventory table (1,142 items)
        import sqlite3
        synonyms = {
            "timbangan analitik": ["analytical balance", "balance"],
            "timbangan": ["balance", "scale"],
            "oven": ["oven"],
            "autoclave": ["autoclave", "steril"],
            "sterilisasi": ["autoclave", "steril"],
            "centrifuge": ["centrifuge"],
            "sentrifug": ["centrifuge"],
            "incubator": ["incubator", "inkubator"],
            "inkubator": ["incubator"],
            "vortex": ["vortex"],
            "hotplate": ["hotplate", "stirrer"],
            "stirrer": ["stirrer", "magnetic"],
            "water bath": ["water bath"],
            "waterbath": ["water bath"],
            "mikroskop": ["microscope"],
            "pipet": ["micropipette", "pipette"],
            "micropipette": ["micropipette"],
            "spektro": ["spectro", "spectrophotometer"],
            "lemari asam": ["biosafety", "fume hood", "clean room"],
            "biosafety": ["biosafety", "laminar"],
        }
        text_low = (text or "").lower()
        db_path = PROJECT_ROOT / "data" / "harvey.db"
        if not db_path.exists():
            return None

        try:
            con = sqlite3.connect(db_path, timeout=3.0)
            con.row_factory = sqlite3.Row
            for term, syns in synonyms.items():
                if term in text_low:
                    for s in syns:
                        rows = con.execute(
                            """SELECT name, brand, model, price, status_stok, specifications
                               FROM inventory
                               WHERE available = 1 AND (name LIKE ? OR category LIKE ? OR keywords LIKE ?)
                               ORDER BY price ASC LIMIT 2""",
                            (f"%{s}%", f"%{s}%", f"%{s}%")
                        ).fetchall()
                        if rows:
                            con.close()
                            return term, [dict(r) for r in rows]
            con.close()
        except Exception as e:
            logger.debug(f"Inventory lookup error: {e}")
        return None

    def classify_and_build_inbound_reply(
        self,
        sender_name: str,
        incoming_text: str,
        notes: str = "",
    ) -> tuple[str, str, bool]:
        """Classify incoming WhatsApp lead message and draft Ahnaf-style reply.
        Returns: (intent, reply_message, is_hot_lead)"""
        text_low = (incoming_text or "").lower()
        greet = _jakarta_greeting().lower()

        # Sanitize sender_name so we never address people as 'Kontak +62...' or phone numbers
        salut, _ = clean_and_adapt_contact_name(sender_name)
        if salut in ("Bapak/Ibu", "Bapak/Ibu Tim Pengadaan & Lab", ""):
            sender_name = "Bapak/Ibu"
        else:
            sender_name = salut

        # -2b. Closing Acknowledgment / Courtesy (Zero Token, Stop Loop)
        if is_closing_acknowledgment(incoming_text):
            return "CLOSING_ACK", "", False

        # -1. Auto-Cleansing: Salah Sambung / Salah Nomor
        salah_sambung_keywords = [
            "salah sambung", "salah nomor", "bukan saya", "bukan nomor",
            "salah orang", "tidak kenal", "hapus nomor", "salah kontak",
            "salah kirim", "salah alamat", "salah tujuan", "bukan lab"
        ]
        if any(k in text_low for k in salah_sambung_keywords):
            intent = "SALAH SAMBUNG"
            reply = (
                "Mohon maaf yang sebesar-besarnya atas kekeliruan nomor ini Bapak/Ibu. "
                "Terima kasih banyak atas konfirmasinya. Nomor Bapak/Ibu sudah kami hapus dari daftar kontak kami agar tidak terhubungi kembali. "
                "Semoga sukses dan sehat selalu. 🙏"
            )
            return intent, reply, False

        # -2. Auto-Cleansing & Referral: Sudah Resign / Pindah PIC / Diarahkan ke Nomor PIC Lain
        resign_keywords = [
            "sudah resign", "sdh resign", "tidak kerja", "tidak lagi di",
            "sudah pindah", "pensiun", "bukan pic", "sudah keluar", "ganti pic"
        ]
        ref_phone, ref_name, ref_dept = extract_pic_referral(incoming_text)
        if any(k in text_low for k in resign_keywords):
            if ref_phone:
                intent = "REFERRAL BARU"
                reply = (
                    f"Baik Bapak/Ibu, terima kasih banyak atas informasinya dan telah berbagi kontak "
                    f"{'Bapak/Ibu ' + ref_name if ref_name else 'rekan pengganti'}. "
                    "Segera kami koordinasikan dengan beliau. Sukses dan sehat selalu di tempat yang baru! 🙏"
                )
                return intent, reply, True
            else:
                intent = "SUDAH RESIGN (TANYA PENGGANTI)"
                reply = (
                    "Baik Bapak/Ibu, terima kasih banyak atas informasinya dan mohon maaf telah mengganggu waktu Bapak/Ibu. "
                    "Jika berkenan, bolehkah kami dibantu nomor kontak rekan PIC laboratorium/pengadaan yang menggantikan posisi Bapak/Ibu saat ini? "
                    "Terima kasih banyak atas bantuannya. 🙏"
                )
                return intent, reply, False
        elif ref_phone:
            target_lbl = f"Bapak/Ibu {ref_name}" if ref_name else (f"bagian {ref_dept}" if ref_dept else "rekan PIC terkait")
            intent = "REFERRAL BARU"
            reply = (
                f"Baik Bapak/Ibu, terima kasih banyak atas arahannya. "
                f"Segera kami hubungi {target_lbl} di nomor tersebut ya Pak/Bu. 🙏"
            )
            return intent, reply, True

        # 0. Consent to give-first asset offer (HOT LEAD)
        pending_offer = find_pending_asset_offer(notes)
        if pending_offer and is_affirmative_consent(incoming_text):
            asset_id, _ = pending_offer
            asset = get_asset_by_id(asset_id)
            title = asset.get("title", "dokumen pengadaan alat lab") if asset else "dokumen pengadaan alat lab"
            who = sender_name or "Pak/Bu"
            caption = build_asset_caption(title, who)
            return "MINTA ASET", caption, True

        # Parlant Behavioral Guidelines check
        try:
            from harvey.guidelines import GuidelineEngine
            g_engine = GuidelineEngine()
            matched = g_engine.match_guidelines(incoming_text, customer_context={"notes": notes})
            # G-04: Margin defense & discount request handling
            if any(k in text_low for k in ["diskon", "potongan", "kurang", "nego", "kemahalan", "harga pas", "bisa kurang"]):
                intent = "NEGO HARGA / MINTA DISKON"
                reply = (
                    f"Halo {greet} {sender_name or 'Pak/Bu'}, terima kasih atas responnya. "
                    "Terkait penawaran harga kami, standar kami sudah mencakup garansi resmi 12 bulan, instalasi & uji fungsi teknisi langsung di lab, serta kelengkapan dokumen BAST untuk audit. "
                    "Untuk fleksibilitas anggaran, kami bisa bantu di termin pembayaran (Tempo Instansi) atau bebas biaya pengiriman ke lokasi laboratorium. "
                    "Apakah boleh kami koordinasikan draf SPH resminya agar bisa direview oleh tim pengadaan Bapak/Ibu? Terima kasih. 🙏"
                )
                return intent, reply, True
            # G-02: Special Order / Sourcing By Request
            if any(k in text_low for k in ["tipe lain", "merek lain", "merk lain", "khusus", "request", "inden", "indent", "custom", "spesifikasi khusus"]):
                intent = "SOURCING BY REQUEST (KHUSUS)"
                reply = (
                    f"Halo {greet} {sender_name or 'Pak/Bu'}, baik sekali. Selain stok ready di katalog, kami juga melayani Special Order / Sourcing By Request langsung dari prinsipal resmi untuk instrumen dan reagen spesifik. "
                    "Boleh dibantu nama alat, merk/tipe yang dicari, atau nomor katalognya? "
                    "Tim kami akan carikan ketersediaan, estimasi lead time, dan siapkan draf SPH resmi berspesifikasi lengkap. Terima kasih. 🙏"
                )
                return intent, reply, True
            # G-03: Campus / Government budget & SPJ compliance
            if any(k in text_low for k in ["tempo", "termin", "lppm", "audit", "spj", "faktur", "anggaran"]):
                intent = "ADMINISTRASI ANGGARAN & SPJ"
                reply = (
                    f"Halo {greet} {sender_name or 'Pak/Bu'}, untuk kebutuhan pengadaan instansi/kampus, AndisLab sudah sangat terbiasa dengan skema Tempo Instansi (pembayaran setelah BAST & uji fungsi selesai). "
                    "Kami juga menerbitkan e-Faktur Pajak dan kelengkapan berkas SPJ resmi 1x24 jam kerja untuk kelancaran administrasi LPPM/Dinas. "
                    "Boleh dibantu rincian kebutuhan dan nama instansinya agar kami siapkan administrasinya? Terima kasih. 🙏"
                )
                return intent, reply, True
        except Exception as e:
            logger.debug(f"WhatsApp: Guideline rule check skipped: {e}")

        # 0. Requesting to send via email with email address provided (Highest priority omnichannel intent)
        email_match = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", incoming_text)
        if email_match:
            found_email = email_match.group(0).lower()
            intent = "MINTA VIA EMAIL (EMAIL DIBERIKAN)"
            is_hot = True
            reply = (
                f"Baik {sender_name or 'Pak/Bu'}, terima kasih banyak. "
                f"Dokumen resmi dan katalog 1.140+ item ready stock AndisLab segera kami kirimkan ke alamat email *{found_email}*. "
                "Mohon berkenan dicek di Inbox atau folder Spam ya Pak/Bu. Nanti izin kami konfirmasi singkat juga lewat WhatsApp ini. Terima kasih banyak. 🙏"
            )
            return intent, reply, is_hot

        # 0b. Explicit request / consent for quotation / SPH (STANDARD QUOTATION DATA FORM TEMPLATE)
        if any(k in text_low for k in ["minta penawaran", "buatkan penawaran", "kirim penawaran", "minta sph", "kirim sph", "buatkan sph", "boleh sph", "boleh penawaran", "boleh dikirim", "boleh minta", "mau penawaran", "form penawaran", "quotation form", "bisa quotation", "minta quotation", "kirim quotation"]):
            intent = "MINTA PENAWARAN (HOT LEAD)"
            is_hot = True
            who_call = "Ibu" if (sender_name and any(k in sender_name.lower() for k in ["ibu", "anisa", "annisa", "dewi", "siti", "putri", "rara"])) else ("Bapak" if (sender_name and any(k in sender_name.lower() for k in ["pak", "bapak", "budi", "ahmad", "joko"])) else "Bapak/Ibu")
            reply = (
                f"{who_call} mohon dibantu untuk isi list data untuk di penawaran:\n"
                f"Nama:\n"
                f"Jabatan:\n"
                f"Nomor telfon:\n"
                f"Email:\n"
                f"PT/Perusahaan:\n\n"
                f"Terimakasih, setelah {who_call.lower()} isi kami akan langsung buatkan quotation formnya. 🙏"
            )
            return intent, reply, is_hot

        # 1. Asking for quotation / price / SPH (HOT LEAD)
        if any(k in text_low for k in ["penawaran", "sph", "harga", "quotation", "rfq", "biaya", "beli", "pesan", "po", "order"]):
            intent = "MINTA PENAWARAN (HOT LEAD)"
            is_hot = True
            inv_match = self.search_inventory_catalog(incoming_text)
            if inv_match:
                term, items = inv_match
                item1 = items[0]
                brand = item1.get("brand") or "Resmi"
                model = item1.get("model") or item1.get("name")
                name = item1.get("name") or term
                price = item1.get("price", 0)
                price_fmt = f"Rp {price:,.0f}".replace(",", ".") if price > 0 else ""
                price_str = f" (estimasi katalog kisaran {price_fmt})" if price_fmt else ""
                is_daihan = "daihan" in brand.lower() or "daihan" in name.lower()
                stock_desc = (
                    "stok prinsipal resmi (jadwal pengiriman & ekspedisi kami cantumkan di draf SPH resmi)"
                    if is_daihan
                    else "ready stok unit di Gudang Bogor"
                )
                reply = (
                    f"Halo {greet} {sender_name or 'Pak/Bu'}, dengan senang hati kami bantu. "
                    f"Untuk kebutuhan {term}, di AndisLab tersedia unit *{name}* ({brand} tipe {model}){price_str} {stock_desc} dengan garansi resmi 12 bulan & uji fungsi teknisi langsung di lab. "
                    "Boleh dibantu rincian kebutuhan dan alamat email instansinya agar draf SPH resminya bisa segera kami kirimkan? Terima kasih banyak. 🙏"
                )
            else:
                reply = (
                    f"Halo {greet} {sender_name or 'Pak/Bu'}, dengan senang hati kami bantu. "
                    "Boleh diceritakan spesifikasi/kapasitas alat yang dibutuhkan dan untuk pengujian apa? "
                    "Supaya rekomendasi dan SPH resminya benar-benar sesuai kebutuhan lab. Terima kasih banyak. 🙏"
                )
        # 2. Asking for company profile / catalog / website / product list
        elif any(k in text_low for k in ["company profile", "profil perusahaan", "compro", "katalog", "brosur", "daftar produk", "website", "katalognya"]):
            intent = "MINTA KATALOG & COMPANY PROFILE"
            is_hot = True
            reply = (
                f"Halo {greet} {sender_name or 'Pak/Bu'}, tentu dengan senang hati. "
                "Berikut kami lampirkan profil singkat PT Andis Sentral Laboratorium (AndisLab). "
                "Kami adalah distributor & supplier resmi peralatan, instrumen, dan bahan kimia laboratorium dengan 1.100+ item ready stock di gudang Jabodetabek (timbangan analitik, autoclave, oven, centrifuge, glassware, hingga lab furniture dan reagen). "
                "Katalog online dan portofolio resmi kami juga dapat diakses kapan saja di www.andislab.com. "
                "Jika suatu saat ada kebutuhan instrumen, alat uji, reagen, atau pembanding HPS resmi ber-KOP, nomor ini selalu siap membantu tim. Terima kasih banyak. 🙏"
            )
        # 2b. Belum ada kebutuhan saat ini / ditangani tim internal (Polite graceful acknowledge, no hard sell)
        elif any(k in text_low for k in ["belum ada", "blm ada", "dihandle tim", "dihandle oleh tim", "bisa dihandle", "bisa dihandl", "sudah ada vendor", "masih cukup", "belum butuh", "blm butuh", "belum memerlukan"]):
            intent = "BELUM ADA KEBUTUHAN (SIMPAN KONTAK)"
            is_hot = False
            reply = (
                f"Baik {sender_name or 'Pak/Bu'}, terima kasih banyak atas responnya yang sangat ramah. "
                "Siap, tidak apa-apa sama sekali Pak/Bu. Senang bisa bersilaturahmi dan berkenalan dengan rekan tim. "
                "Izin nomor ini disimpan ya Pak/Bu. Jika sewaktu-waktu di kemudian hari ada kebutuhan alat lab mendesak, reagen rutin, atau sekadar pembanding harga resmi (SPH), kami selalu siap membantu. "
                "Sukses dan sehat selalu untuk Bapak/Ibu dan seluruh tim! 🙏"
            )
        # 3. Requesting to send via email
        elif any(k in text_low for k in ["email", "e-mail", "surat"]):
            email_match = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", incoming_text)
            if email_match:
                found_email = email_match.group(0).lower()
                intent = "MINTA VIA EMAIL (EMAIL DIBERIKAN)"
                is_hot = True
                reply = (
                    f"Baik {sender_name or 'Pak/Bu'}, terima kasih banyak. "
                    f"Dokumen resmi dan katalog 1.140+ item ready stock AndisLab segera kami kirimkan ke alamat email *{found_email}*. "
                    "Mohon berkenan dicek di Inbox atau folder Spam ya Pak/Bu. Nanti izin kami konfirmasi singkat juga lewat WhatsApp ini. Terima kasih banyak. 🙏"
                )
            else:
                intent = "MINTA VIA EMAIL"
                is_hot = True
                reply = (
                    f"Baik {sender_name or 'Pak/Bu'}, segera kami kirimkan via email. "
                    "Boleh dibantu alamat email tujuan dan nama PIC pengadaannya? "
                    "Nanti setelah kami kirim, izin konfirmasi singkat lewat WhatsApp ini ya. Terima kasih banyak. 🙏"
                )
        # 4. Live Inventory RAG: Asking about specific instruments in catalog
        elif self.search_inventory_catalog(incoming_text):
            inv_match = self.search_inventory_catalog(incoming_text)
            term, items = inv_match
            item1 = items[0]
            brand = item1.get("brand") or "Resmi"
            model = item1.get("model") or item1.get("name")
            name = item1.get("name") or term
            price = item1.get("price", 0)
            price_fmt = f"Rp {price:,.0f}".replace(",", ".") if price > 0 else ""
            price_str = f" (estimasi katalog kisaran {price_fmt})" if price_fmt else ""

            intent = f"TANYA PRODUK ({term.upper()})"
            is_hot = True
            reply = (
                f"Halo {greet} {sender_name or 'Pak/Bu'}, terima kasih atas responnya. "
                f"Untuk kebutuhan {term}, di AndisLab kami ready stok unit *{name}* ({brand} tipe {model}){price_str} di Gudang Bogor. "
                "Unit sudah termasuk garansi resmi 12 bulan, sertifikat kalibrasi, dan instalasi & uji fungsi teknisi langsung di lab. "
                "Apakah boleh kami kirimkan brosur spesifikasi teknis dan draf penawaran resminya untuk direview? Terima kasih. 🙏"
            )
        # 4b. Non-stock lab equipment / Sourcing inquiry
        elif any(k in text_low for k in [
            "autoclave", "centrifuge", "spectrophotometer", "spektrofotometer", "furnace",
            "viscometer", "polarimeter", "rotary evaporator", "water bath", "waterbath",
            "glassware", "reagen", "reagent", "hplc", "gc-ms", "turbidimeter", "jar test",
            "kjeldahl", "shaker", "incubator", "inkubator", "oven", "timbangan", "balance",
            "ph meter", "refractometer", "laminar air flow", "biosafety cabinet", "fume hood",
            "lemari asam", "bioreactor", "sonicator", "vortex", "hotplate", "distilasi", "pipet"
        ]):
            intent = "SOURCING BY REQUEST (KHUSUS)"
            is_hot = True
            reply = (
                f"Halo {greet} {sender_name or 'Pak/Bu'}, baik sekali. Selain 1.140+ stok ready di katalog, "
                "kami juga melayani Special Order / Sourcing By Request langsung dari jaringan prinsipal resmi untuk instrumen dan spesifikasi khusus. "
                "Sedang kami koordinasikan ketersediaan unit, estimasi lead time, dan penawaran terbaiknya ke bagian logistik kami. Terima kasih. 🙏"
            )
        # 4c. Not interested / no need right now
        elif any(k in text_low for k in ["belum ada", "blm ada", "tidak ada", "tdk ada", "belum butuh", "sudah ada vendor"]):
            intent = "BELUM BUTUH"
            is_hot = False
            reply = (
                f"Baik {sender_name or 'Pak/Bu'}, tidak masalah sama sekali. Terima kasih banyak atas responnya. "
                "Izin simpan nomor saya ya (Ahnaf - AndisLab). Nanti jika sewaktu-waktu ada kebutuhan mendesak, peremajaan alat, atau butuh penawaran pembanding, silakan hubungi kami kapan saja. "
                "Semoga sukses selalu untuk Bapak/Ibu dan tim! 🙏"
            )
        # 5. General greeting / open response
        else:
            intent = "RESPON UMUM"
            is_hot = True
            reply = (
                f"Halo {greet} {sender_name or 'Pak/Bu'}, terima kasih banyak sudah merespons, saya Ahnaf dari PT Andis Sentral Laboratorium (AndisLab). "
                "Kami adalah distributor & supplier resmi peralatan dan reagen laboratorium (1.100+ item ready stock Jabodetabek). "
                "Barangkali di lab sedang ada rencana pengadaan alat baru, reagen rutin, atau butuh draf penawaran harga pembanding (SPH)? "
                "Jika berkenan, kami juga siap kirimkan ringkasan katalog kami untuk referensi tim. Terima kasih banyak. 🙏"
            )

        return intent, reply, is_hot

    async def generate_adaptive_reply(
        self,
        sender_name: str,
        company: str,
        incoming_message: str,
        thread_history: list[dict] | None = None,
        notes: str = "",
        phone: str = "",
    ) -> tuple[str, str, bool, bool]:
        """Generate an email-grade adaptive WhatsApp reply using Obsidian Intelligence + Gemini Flash.

        Returns:
            (intent, reply_text, is_hot, send_compro_pdf)
        """
        # Sanitize sender_name so we never address people as phone numbers or 'Kontak +62...'
        salut, _ = clean_and_adapt_contact_name(sender_name, company)
        if salut in ("Bapak/Ibu", "Bapak/Ibu Tim Pengadaan & Lab", ""):
            sender_name = "Bapak/Ibu"
        else:
            sender_name = salut

        # 1. Deterministic fast-path check
        fast_intent, fast_reply, is_hot = self.classify_and_build_inbound_reply(
            sender_name=sender_name,
            incoming_text=incoming_message,
            notes=notes,
        )

        # High-certainty operational intents don't need LLM
        if fast_intent in ("CLOSING_ACK", "SALAH SAMBUNG", "REFERRAL BARU", "SUDAH RESIGN (TANYA PENGGANTI)", "MINTA ASET"):
            return fast_intent, fast_reply, is_hot, False

        # 2. Retrieve Obsidian Second Brain context (0 API cost)
        obsidian_guidance = ""
        obsidian_profile = ""
        if getattr(self, "obsidian", None):
            try:
                intel = self.obsidian.search_client_intelligence(company=company, contact_name=sender_name, phone=phone)
                obsidian_guidance = intel.get("playbook_guidance", "")
                obsidian_profile = intel.get("profile_notes", "")
            except Exception as e:
                logger.debug(f"WhatsApp: Error retrieving Obsidian context: {e}")

        # 3. Format chat thread history (deduplicated, last 8 exchanges)
        thread_lines = []
        if thread_history:
            prev_c = ""
            for item in thread_history[-8:]:
                s = "Ahnaf (AndisLab)" if item.get("direction") == "outbound" else (sender_name or "Klien")
                c = (item.get("content") or item.get("text") or "").strip()
                if c and c != prev_c:
                    thread_lines.append(f"[{s}]: {c}")
                    prev_c = c
        thread_str = "\n".join(thread_lines) if thread_lines else f"[Klien]: {incoming_message}"

        msg_low = incoming_message.lower()
        wants_catalog = any(k in msg_low for k in ["company profile", "profil perusahaan", "compro", "katalog", "brosur", "daftar produk"])

        # 3b. Real Inventory Catalog Match (1,142 items from SQLite)
        # ONLY search inbound customer messages — NEVER match against our own outbound outreach!
        inv_match = self.search_inventory_catalog(incoming_message)
        if not inv_match and thread_history and len(incoming_message.split()) > 3:
            # Check backwards through INBOUND customer messages only to find the product discussed
            for m in reversed(thread_history):
                if m.get("direction") != "inbound":
                    continue
                content = (m.get("content") or m.get("text") or "").strip()
                if len(content) > 10 and not any(k in content.lower() for k in ["harga berapa", "oke ka", "sebentar ibu", "terima kasih", "silahkan", "silakan"]):
                    m_res = self.search_inventory_catalog(content)
                    if m_res:
                        inv_match = m_res
                        break
        inventory_context_block = ""
        matched_items_list = []
        if inv_match:
            term, items = inv_match
            matched_items_list = items
            if term == "__CONSULT_CHECK__" and items:
                c_guid = items[0].get("consultative_guidance") or ""
                inventory_context_block = (
                    f"[PROTOKOL KHUSUS SPAREPART / MERK INDENT - WAJIB DIIKUTI]\n"
                    f"Item yang diminta klien ({items[0].get('name')}) BELUM terdaftar ready stock di gudang utama.\n"
                    f"DILARANG menawarkan produk lain yang tidak nyambung (seperti oven/thermo unit).\n"
                    f"Gunakan panduan balasan konsultatif berikut:\n{c_guid}"
                )
            else:
                inv_lines = []
                for it in items[:4]:
                    it_name = it.get("name") or term
                    it_brand = it.get("brand") or "Resmi"
                    it_model = it.get("model") or it.get("catalog_no") or "-"
                    it_price = float(it.get("price") or 0)
                    it_price_str = f"Rp {it_price:,.0f}".replace(",", ".") if it_price > 0 else "Hubungi Sales"
                    it_specs = it.get("specifications") or "-"
                    is_daihan = "daihan" in it_brand.lower() or "daihan" in it_name.lower()
                    avail_status = (
                        "Stok Prinsipal Pabrik (Jadwal pengiriman & ekspedisi dikonfirmasi di draf SPH resmi)"
                        if is_daihan
                        else "Ready Stock Gudang Jabodetabek"
                    )
                    inv_lines.append(
                        f"- Produk: {it_name}\n"
                        f"  Merk: {it_brand} | Tipe/Model: {it_model}\n"
                        f"  Harga Katalog: {it_price_str} (garansi 12 bulan, siap SPH resmi)\n"
                        f"  Spesifikasi: {it_specs}\n"
                        f"  Ketersediaan: {avail_status}"
                    )
                inventory_context_block = "\n".join(inv_lines)

        # 4. Invoke Brain (Gemini / Groq Qwen) with Mahoraga Adaptation Wheel + Stage-Aware RAG prompt
        try:
            from harvey.brain import Brain
            from pydantic import BaseModel, field_validator

            class MahoragaReplySchema(BaseModel):
                intent: str = "RESPON UMUM"
                reply: str
                send_compro_pdf: bool = False

                @field_validator("reply")
                @classmethod
                def reject_hallucinations(cls, v: str) -> str:
                    low = (v or "").lower()
                    for forbidden in ("brand xyz", "t-dryer", "merk abc", "xyz"):
                        if forbidden in low:
                            raise ValueError(f"Hallucinated placeholder detected: {forbidden}")
                    return v.strip()

            # Detect stateful Deal Stage (SalesGPT State Machine Pattern)
            prev_out_msg = next((m for m in reversed(thread_history or []) if m.get("direction") == "outbound"), None)
            prev_out_low = ((prev_out_msg or {}).get("content") or (prev_out_msg or {}).get("text") or "").lower()
            is_short_affirm = len(incoming_message.split()) <= 6 and any(
                w in msg_low for w in ("silahkan", "silakan", "boleh", "oke", "ok", "iya", "ya", "siap", "bisa", "jam ", "pukul", "monggo")
            )

            sales_stage = "DISCOVERY"
            if is_short_affirm and any(k in prev_out_low for k in ("kunjungan", "visit", "mampir", "kamis", "jadwal")):
                sales_stage = "OFFERED_VISIT_CONFIRMATION (Klien menyetujui tawaran visit kita sebelumnya — kunci jam & alamat!)"
            elif is_short_affirm and any(k in prev_out_low for k in ("katalog", "company profile", "compro", "brosur")):
                sales_stage = "OFFERED_CATALOG_FOLLOWUP (Klien menyetujui pengiriman katalog — lampirkan katalog & tanyakan kebutuhan/jadwal visit!)"
            elif any(k in msg_low for k in ("visit", "ketemu", "nego", "diskon", "po ", "siap po", "tempo")):
                sales_stage = "CLOSING_HANDOFF"
            elif any(k in msg_low for k in ("mahal", "budget", "anggaran", "tahun depan", "pikir", "banding")):
                sales_stage = "OBJECTION_HANDLING"
            elif any(k in msg_low for k in ("sph", "penawaran", "quotation", "buatkan")):
                sales_stage = "QUOTATION_G05"
            elif inv_match or any(k in msg_low for k in ("harga", "spek", "kapasitas", "ready", "merk", "probe")):
                sales_stage = "TECHNICAL_MATCH"

            # Load Mahoraga Adaptation Wheel (last 5 operator revisions)
            adaptation_block = ""
            try:
                adaptations = await self.state.get_recent_adaptations(limit=5)
                if adaptations:
                    ad_lines = []
                    for idx_ad, ad in enumerate(adaptations, 1):
                        ad_lines.append(
                            f"{idx_ad}. [Draf Salah]: \"{(ad.get('ai_draft') or '')[:90]}...\"\n"
                            f"   [Koreksi Benar Operator]: \"{(ad.get('final_text') or '')[:160]}\""
                        )
                    adaptation_block = "\n".join(ad_lines)
            except Exception:
                pass

            # Load Mahoraga Agency Sales Playbook (adapted from msitarzewski/agency-agents)
            agency_playbook = ""
            try:
                pb_path = PROJECT_ROOT / "skills" / "mahoraga_agency_sales.md"
                if pb_path.exists():
                    agency_playbook = pb_path.read_text(encoding="utf-8")[:1600]
            except Exception:
                pass

            # Load Big Data LanceDB Similar Past Cases (real historical threads)
            similar_cases_block = ""
            try:
                from harvey.integrations.big_data import MahoragaBigData
                sim_hits = MahoragaBigData().search_similar_cases(incoming_message, top_k=2, min_score=0.20)
                if sim_hits:
                    sc_lines = []
                    for idx_sc, sc in enumerate(sim_hits, 1):
                        sc_lines.append(
                            f"{idx_sc}. [Kasus {sc.get('company', 'Customer')} | {sc.get('intent', '')}] "
                            f"Tanya: \"{(sc.get('customer_msg') or '')[:100]}\"\n"
                            f"   Jawaban Sukses: \"{(sc.get('agent_reply') or '')[:180]}\""
                        )
                    similar_cases_block = "\n".join(sc_lines)
            except Exception:
                pass

            brain_cfg = getattr(self.config, "brain", None) if getattr(self, "config", None) else None
            brain = Brain(self.state, brain_cfg)

            prompt = f"""Kamu adalah Ahnaf Ibrahim, B2B Account Specialist di PT Andis Sentral Laboratorium (AndisLab).
AndisLab adalah distributor dan supplier resmi peralatan, instrumen analitik, dan bahan kimia laboratorium (1.140+ item ready stock Jabodetabek, garansi 12 bulan, siap terbitkan SPH resmi 1x24 jam, katalog online di www.andislab.com).

TAHAP PERCAKAPAN SAAT INI (SALES STAGE): {sales_stage}

PLAYBOOK PENJUALAN ELIT (SPIN + MEDDPICC + PROPOSAL):
{agency_playbook if agency_playbook else 'Terapkan pendekatan konsultatif B2B santun.'}

RODA ADAPTASI MAHORAGA (KOREKSI NYATA OPERATOR — PELAJARI & TERAPKAN):
{adaptation_block if adaptation_block else 'Belum ada koreksi revisi terbaru. Pertahankan standar tertinggi.'}

MEMORI BIG DATA (REFERENSI KASUS SERUPA DI MASA LALU):
{similar_cases_block if similar_cases_block else 'Tidak ada referensi kasus serupa; gunakan panduan SOP AndisLab.'}

TUGASMU:
Balas pesan WhatsApp dari klien ini dengan informasi produk yang AKURAT sesuai data stok kita di bawah.

DATA KLIEN & KONTEKS:
- Nama PIC: {sender_name or 'Bapak/Ibu'}
- Instansi/Perusahaan: {company or 'Instansi Klien'}
- Catatan Prospek / Agenda: {(notes or '')[:300] or 'Belum ada catatan khusus.'}
- Catatan Profil Obsidian: {obsidian_profile[:300] if obsidian_profile else 'Belum ada catatan khusus.'}
- Panduan Playbook Segmen:
{obsidian_guidance[:400] if obsidian_guidance else 'Gaya profesional B2B santun.'}

RIWAYAT PERCAKAPAN:
{thread_str}

PESAN TERAKHIR KLIEN YANG HARUS DIBALAS SEKARANG:
"{incoming_message}"

DATA INVENTORY PRODUK RESMI KITA (1.140+ READY STOCK GUDANG):
{inventory_context_block if inventory_context_block else 'Item spesifik belum terdaftar ready stock di gudang utama.'}

ATURAN KOMUNIKASI & INVENTORY (SANGAT KETAT - DILARANG DILANGGAR):
0. ATURAN RELEVANSI MUTLAK (WAJIB DIPATUHI):
   - Balas PERSIS sesuai PESAN TERAKHIR KLIEN dan konteks pesan [Ahnaf (AndisLab)] tepat sebelumnya di RIWAYAT PERCAKAPAN.
   - Jika klien membalas singkat ("Silahkan", "Boleh", "Oke", "Iya", "Jam 10", "Siap"), lihat pesan terakhir [Ahnaf (AndisLab)] sebelumnya:
     * Jika kita baru menawarkan kirim katalog/brosur -> sampaikan katalog + tanyakan kebutuhan spesifik atau tawarkan visit/diskusi.
     * Jika kita baru menawarkan jadwal visit (misal Kamis 19 Nov) -> langsung konfirmasi kesiapan visit dan tanyakan jam yang paling nyaman.
     * Jika kita baru menawarkan SPH -> minta data pengisian SPH.
   - DILARANG membalas dengan kalimat template kaku jika tidak nyambung dengan konteks obrolan!
1. DILARANG KERAS MENGARANG / MENGHALUSINASI NAMA PRODUK, MERK, ATAU MODEL PALSU (DILARANG menyebut XYZ, ABC, T-Dryer, atau produk fiktif apa pun!).
2. Jika ada produk di DATA INVENTORY di atas:
   - WAJIB sebutkan nama produk, merk asli, dan model persis dari data di atas (contoh: Daihan Labtech Universal Drying Oven LDO-030E).
   - Jika klien menanyakan kapasitas tertentu (misal 15-30L), jelaskan model mana yang paling cocok kapasitasnya dari data di atas.
   - Jika klien bertanya harga (contoh: "harga berapa ka yang itu"), SEBUTKAN harga katalog resmi dari data di atas dengan santun, jelaskan harga belum termasuk diskon, berikan info garansi 12 bulan, dan tawarkan pembuatan draf SPH resmi 1x24 jam.
3. ATURAN ESTIMASI PENGIRIMAN & EKSPEDISI (SANGAT KETAT):
   - JANGAN PERNAH mengarang atau menjanjikan hari/tanggal pengiriman pasti (seperti "1-3 hari kerja", "sampai besok", dsb).
   - Khusus produk merk DAIHAN LABTECH (dan instrumen besar): Karena gudang dan pabrik prinsipal berjarak jauh, jelaskan dengan jujur dan profesional bahwa: estimasi jadwal pengiriman dan kesiapan ekspedisi akan kami konfirmasikan langsung di dalam draf SPH resmi sesuai kesiapan stok di prinsipal Daihan Labtech.
4. TEMPLATE WAJIB KETIKA KLIEN MEMINTA PENAWARAN / SPH / QUOTATION:
   - Jika klien menyatakan meminta penawaran, meminta draf SPH, atau menyetujui pembuatan draf penawaran/quotation (contoh: "boleh", "kirimkan penawarannya", "minta draf SPH", "tolong buatkan penawaran", "bisa quotation"):
   - WAJIB gunakan format pengumpulan data berikut persis dengan baris-baris terpisah (newline \n):
     "Bapak/Ibu [Nama] mohon dibantu untuk isi list data untuk di penawaran:
     Nama:
     Jabatan:
     Nomor telfon:
     Email:
     PT/Perusahaan:

     Terimakasih, setelah Bapak/Ibu isi kami akan langsung buatkan quotation formnya. 🙏"
5. Jika produk TIDAK TERDAFTAR di data inventory di atas:
   - JANGAN MENGARANG! Katakan dengan jujur dan santun bahwa tipe tersebut sedang kami konfirmasikan ketersediaannya ke prinsipal resmi dan tawarkan bantuan pembuatan draf penawaran/SPH pembanding.
6. Sesuaikan tingkat kesopanan (mirroring): Sapa nama PIC secara natural dan sopan (contoh: "Selamat siang Ibu Anisa" atau "Halo Pak Budi"). JANGAN PERNAH menumpuk sapaan dobel seperti "Bapak/Ibu Bu Anisa" atau "Bapak/Ibu Pak Budi", dan JANGAN PERNAH menyapa dengan nomor HP, kata "Kontak +62...", atau kata "kak" jika lawan bicara formal. Jika nama PIC belum diketahui, sapa "Bapak/Ibu".
7. Jika klien menolak halus ("belum butuh / sudah dihandle tim internal"):
   - Hargai dan terima dengan sangat santun. Doakan kelancaran aktivitas mereka, dan titip simpan kontak Ahnaf untuk kebutuhan mendesak di masa depan. Jangan memaksa jualan.
8. Panjang pesan 30-70 kata. Rapi, elegan, jelas, dan profesional.

KEMBALIKAN HANYA JSON dengan 1 nilai intent yang paling tepat (PILIH SALAH SATU: TANYA PRODUK / MINTA KATALOG / JADWAL VISIT / MINTA PENAWARAN / BELUM BUTUH / RESPON UMUM):
{{
  "intent": "RESPON UMUM",
  "reply": "isi balasan WhatsApp di sini",
  "send_compro_pdf": true/false
}}"""

            result = await brain.think_json(prompt, session_id="harvey-wa-reply", agent="handler", task="adaptive_reply")
            if isinstance(result, dict) and result.get("reply"):
                validated = MahoragaReplySchema(
                    intent=str(result.get("intent") or fast_intent),
                    reply=str(result.get("reply") or ""),
                    send_compro_pdf=bool(result.get("send_compro_pdf") or wants_catalog),
                )
                out_intent = str(validated.intent or "").strip()
                if "/" in out_intent and len(out_intent) > 40:
                    out_intent = "MINTA KATALOG" if wants_catalog else ("TANYA PRODUK" if inv_match else "RESPON UMUM")
                out_reply = validated.reply
                out_pdf = validated.send_compro_pdf

                # Post-Generation Self-Check Critic: catch hallucinations, rewrite safely, & alert operator
                def _find_halu_reasons(txt: str) -> list[str]:
                    reasons: list[str] = []
                    low_t = (txt or "").lower()
                    if not inv_match and re.search(r"rp\s*[\d.,]{4,}", txt or "", re.IGNORECASE):
                        reasons.append("Menyebut harga tanpa kecocokan SKU")
                    if any(p in low_t for p in ("brand xyz", "merk abc", "t-dryer", " xyz", "{{", "[nama", "[produk")):
                        reasons.append("Placeholder/nama produk fiktif")
                    for u_match in re.findall(r"(?:https?://|www\.)([a-z0-9.-]+\.[a-z]{2,})", low_t):
                        if not any(ok_dom in u_match for ok_dom in ("andislab.com", "inaproc.id")):
                            reasons.append(f"Link asing ({u_match})")
                            break
                    if any(leak in low_t for leak in ("system:", "harvey", "sebagai ai", "model bahasa")):
                        reasons.append("Kebocoran identitas/instruksi AI")
                    return reasons

                halu_reasons = _find_halu_reasons(out_reply)
                if halu_reasons:
                    reason_str = "; ".join(halu_reasons)
                    blocked_preview = out_reply[:180]
                    logger.warning(f"WhatsApp: Critic caught potential hallucination ({reason_str}); rewriting.")
                    clean_prompt = (
                        f"Kamu Ahnaf Ibrahim (AndisLab). Balas chat WA klien '{sender_name or 'Bapak/Ibu'}' ({company or 'Klien'}) "
                        f"secara sopan, singkat (25-45 kata), TANPA menyebut harga, link, atau nama alat spesifik apa pun.\n"
                        f"Pesan kita sebelumnya: \"{(thread_lines[-2] if len(thread_lines) >= 2 else '')}\"\n"
                        f"Pesan terakhir klien: \"{incoming_message}\"\n"
                        f"Balas tepat nyambung dengan pesan klien tersebut dalam 1 paragraf ramah."
                    )
                    rewritten = await brain.think(clean_prompt, agent="handler", task="critic_rewrite")
                    rewrite_ok = False
                    if rewritten and len(rewritten.strip()) >= 12 and not _find_halu_reasons(rewritten):
                        out_reply = rewritten.strip()
                        rewrite_ok = True
                    else:
                        out_intent = "CRITIC_HOLD"

                    try:
                        await self.state.log_action(
                            agent="bigdata",
                            action_type="critic_alert",
                            details={
                                "company": f"CRITIC HALU ({sender_name or phone}): {reason_str}",
                                "reasons": halu_reasons,
                                "rewritten": rewrite_ok,
                            },
                        )
                    except Exception:
                        pass
                    try:
                        status_txt = "Di-rewrite aman otomatis" if rewrite_ok else "DITAHAN ke Outbox Pending Review"
                        await self.send_system_alert(
                            f"⚠️ *[PERINGATAN CRITIC: HANDLER HALU]*\n"
                            f"Klien: *{sender_name or '-'}* ({company or '-'})\n"
                            f"Alasan: {reason_str}\n"
                            f"Draf diblokir: _{blocked_preview}_\n"
                            f"Status: *{status_txt}*"
                        )
                    except Exception:
                        pass

                # Context-aware PDF flag: if our previous outbound offered catalog & client agreed briefly
                if not out_pdf and thread_history and len(incoming_message.split()) <= 5:
                    prev_out = next((m for m in reversed(thread_history) if m.get("direction") == "outbound"), None)
                    prev_txt = ((prev_out or {}).get("content") or (prev_out or {}).get("text") or "").lower()
                    if any(k in prev_txt for k in ("katalog", "company profile", "compro", "brosur")) and any(
                        okw in msg_low for okw in ("silahkan", "silakan", "boleh", "oke", "ok", "iya", "ya", "monggo", "kirim")
                    ):
                        out_pdf = True

                return out_intent, out_reply, is_hot, out_pdf
        except Exception as e:
            logger.warning(f"WhatsApp: Adaptive reply LLM (Gemini 3.8 Flash) warning: {e}")

        # Obsidian-Enriched Fallback when LLM is unavailable / rate-limited
        # Only override fast_intent to MINTA KATALOG if fast_intent was generic
        final_intent = fast_intent
        if wants_catalog and fast_intent in ("RESPON UMUM", "MINTA KATALOG & COMPANY PROFILE"):
            final_intent = "MINTA KATALOG & COMPANY PROFILE"

        if wants_catalog:
            segment_hook = ""
            comp_low = (company or "").lower()
            if any(k in comp_low for k in ["mineral", "tambang", "mining", "batubara", "nikel"]):
                segment_hook = " Untuk laboratorium preparasi & pengujian mineral/tambang, kami ready stock unit moisture analyzer kadar air, timbangan analitik presisi, furnace suhu tinggi, dan reagen asam titrasi."
            elif any(k in comp_low for k in ["rsud", "rumah sakit", "klinik", "dinkes", "labkesda"]):
                segment_hook = " Untuk fasilitas kesehatan & RSUD, kami ready stock autoclave sterilisasi, centrifuge, dan diagnostic test berstandar E-Katalog."
            elif any(k in comp_low for k in ["univ", "kampus", "universitas", "politeknik", "lppm"]):
                segment_hook = " Untuk laboratorium riset & praktikum kampus, kami mensupport skema Tempo Instansi dan dokumen SPJ/HPS tuntas."

            clean_name = f"Bapak/Ibu {sender_name}".strip() if sender_name else "Bapak/Ibu"
            comp_name = f" ({company})".strip() if company else ""
            reply_text = (
                f"Selamat siang {clean_name}{comp_name}, tentu dengan senang hati. 😊\n\n"
                f"Berikut kami lampirkan berkas *Company Profile & Ringkasan Portofolio PT Andis Sentral Laboratorium (AndisLab)* (1.140+ item ready stock Jabodetabek & garansi resmi 12 bulan).{segment_hook}\n\n"
                f"Katalog online interaktif juga dapat diakses kapan saja di www.andislab.com. Jika sewaktu-waktu tim membutuhkan penawaran harga resmi (SPH) sebagai bahan pertimbangan anggaran, kami selalu siap membantu. Terima kasih banyak. 🙏"
            )
            return final_intent, reply_text, is_hot, True

        return fast_intent, fast_reply, is_hot, wants_catalog

    async def send_daily_report(self, target_phone: str | None = None, force: bool = False) -> bool:
        """Send daily status summary report to the operator's WhatsApp number."""
        raw_phone = target_phone or self.wa_cfg.report_phone or "087764021131"
        norm_phone = normalize_id_phone(raw_phone)
        if not norm_phone:
            logger.warning(f"WhatsApp: Nomor tujuan report '{raw_phone}' tidak valid.")
            return False

        act = self._load_activity()
        today_str = date.today().isoformat()

        if not force:
            if not self.wa_cfg.auto_report:
                return False
            now_jkt = datetime.now(pytz.timezone("Asia/Jakarta"))
            curr_hhmm = now_jkt.strftime("%H:%M")
            if curr_hhmm < self.wa_cfg.report_time:
                return False
            if act.get("last_report_date") == today_str:
                return False

        use_fonnte = self.wa_cfg.provider == "fonnte" or bool(self.env.fonnte_api_key)
        if not use_fonnte and not self.is_logged_in_profile_present():
            logger.warning("WhatsApp: Tidak dapat mengirim report karena WhatsApp Web belum login.")
            return False

        logger.info(f"WhatsApp: Menyiapkan laporan harian untuk {norm_phone}...")
        msg = await self.build_daily_report_message()
        ok = (
            await self._send_via_fonnte(norm_phone, msg, is_system_alert=True)
            if use_fonnte
            else await self._send_via_web(norm_phone, msg, is_system_alert=True)
        )
        if ok:
            act["last_report_date"] = today_str
            self._save_activity(act)
            logger.info(f"WhatsApp: Laporan harian berhasil dikirim ke {norm_phone}.")
            return True
        else:
            logger.warning(f"WhatsApp: Gagal mengirimkan laporan harian ke {norm_phone}.")
            return False

    async def send_weekly_report(self, target_phone: str | None = None) -> bool:
        """Send weekly comprehensive status report to the operator's WhatsApp number."""
        raw_phone = target_phone or self.wa_cfg.report_phone or "087764021131"
        norm_phone = normalize_id_phone(raw_phone)
        if not norm_phone:
            logger.warning(f"WhatsApp: Nomor tujuan report '{raw_phone}' tidak valid.")
            return False

        use_fonnte = self.wa_cfg.provider == "fonnte" or bool(self.env.fonnte_api_key)
        if not use_fonnte and not self.is_logged_in_profile_present():
            logger.warning("WhatsApp: Tidak dapat mengirim report karena WhatsApp Web belum login.")
            return False

        logger.info(f"WhatsApp: Menyiapkan laporan mingguan untuk {norm_phone}...")
        msg = await self.build_weekly_report_message()
        ok = (
            await self._send_via_fonnte(norm_phone, msg, is_system_alert=True)
            if use_fonnte
            else await self._send_via_web(norm_phone, msg, is_system_alert=True)
        )
        if ok:
            logger.info(f"WhatsApp: Laporan mingguan berhasil dikirim ke {norm_phone}.")
            return True
        else:
            logger.warning(f"WhatsApp: Gagal mengirimkan laporan mingguan ke {norm_phone}.")
            return False

    # ── Inbound replies & follow-ups ──

    _SEEN_PATH = DATA_DIR / "whatsapp_inbound_seen.json"

    _UNREAD_JS = """
    () => {
      const out = [];
      document.querySelectorAll('#pane-side [role="row"], #pane-side [role="listitem"]').forEach(r => {
        const titleEl = r.querySelector('span[title]');
        const unread = r.querySelector('span[aria-label*="nread"], span[aria-label*="elum dibaca"]');
        if (!titleEl || !unread) return;
        const lines = (r.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
        out.push({title: titleEl.getAttribute('title') || '', lines: lines});
      });
      return out;
    }
    """

    async def scan_inbound_via_web(self) -> list[dict] | None:
        """Read unread rows from the WhatsApp Web chat list WITHOUT opening any
        chat (so nothing is marked read on the operator's phone). Returns None
        if the scan could not run."""
        if not self.is_logged_in_profile_present():
            return None
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            ctx = None
            try:
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    channel="chrome",
                    headless=True,
                    args=CHROME_SAFE_ARGS,
                    viewport={"width": 1280, "height": 900},
                )
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto("https://web.whatsapp.com/", wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_selector("#pane-side", timeout=40_000)
                await asyncio.sleep(random.uniform(3.0, 5.0))
                return await page.evaluate(self._UNREAD_JS)
            except Exception as e:
                logger.warning(f"WhatsApp: Scan balasan masuk gagal: {e}")
                return None
            finally:
                if ctx:
                    try:
                        await ctx.close()
                    except Exception:
                        pass

    def _load_seen(self) -> set[str]:
        try:
            return set(json.loads(self._SEEN_PATH.read_text(encoding="utf-8")))
        except Exception:
            return set()

    def _save_seen(self, seen: set[str]) -> None:
        try:
            self._SEEN_PATH.write_text(json.dumps(sorted(seen)[-500:]), encoding="utf-8")
        except Exception:
            pass

    async def _tag_prospect(self, prospect_id: str, notes: str, tag: str | list[str]) -> None:
        tag_list = [tag] if isinstance(tag, str) else list(tag)
        today_str = date.today().isoformat()
        stamps = [f"[{t}:{today_str}]" for t in tag_list if t]
        stamp_str = " | ".join(stamps)
        new_notes = f"{notes} | {stamp_str}".strip(" |") if notes else stamp_str
        async with self.state._connect() as db:
            await db.execute(
                "UPDATE prospects SET personalization_notes = ? WHERE id = ?",
                (new_notes, prospect_id),
            )
            await db.commit()

    async def handle_operator_command(self, operator_phone: str, message: str) -> dict:
        """Process remote commands from operator's WhatsApp (e.g. OK, REVISI, HAPUS, STATUS)."""
        raw = (message or "").strip()
        cmd = raw.upper()
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.replace(tzinfo=None).isoformat()

        # 1. OK / KIRIM / APPROVE / YES / GAS -> Send latest pending_review outbox item
        if cmd.startswith("MARGIN"):
            margin_match = re.search(r"MARGIN\s*[:=]?\s*(\d+(?:\.\d+)?)", cmd)
            if margin_match:
                new_margin_pct = float(margin_match.group(1))
                async with self.state._connect() as db:
                    cur = await db.execute("SELECT id, selected_hpp, clean_query FROM sourcing_requests ORDER BY created_at DESC LIMIT 1")
                    s_row = await cur.fetchone()
                if s_row:
                    s_id, hpp, clean_q = s_row
                    new_sell = hpp * (1.0 + new_margin_pct / 100.0) if hpp > 0 else 0
                    profit = new_sell - hpp
                    await self.state.update_sourcing_request(
                        s_id,
                        margin_pct=new_margin_pct,
                        quoted_price=new_sell,
                    )
                    reply = (
                        f"✅ *Margin Sourcing Diperbarui!*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"Tiket: *{s_id}* ({clean_q})\n"
                        f"• HPP: Rp {hpp:,.0f}\n"
                        f"• Margin Baru: *{new_margin_pct}%*\n"
                        f"• Harga Jual Baru: *Rp {new_sell:,.0f}*\n"
                        f"• Estimasi Profit: *Rp {profit:,.0f}*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"Balas *OK* untuk kirim balasan ke klien."
                    )
                    await self._send_text_auto(operator_phone, reply, is_system_alert=True)
                    return {"status": "operator_command_margin_updated", "margin_pct": new_margin_pct}

        if cmd in ("OK SPH", "KIRIM SPH", "APPROVE SPH", "YES SPH"):
            from harvey.integrations.quote_chaser import init_quotes_table
            await init_quotes_table()
            async with self.state._connect() as db:
                cur = await db.execute(
                    "SELECT id, company, contact_name, phone, email, items_summary, total_amount, notes FROM quotes ORDER BY rowid DESC LIMIT 1"
                )
                q_row = await cur.fetchone()
            if not q_row:
                await self._send_text_auto(operator_phone, "ℹ️ Belum ada draf SPH PDF yang siap dikirim.", is_system_alert=True)
                return {"status": "operator_command_no_sph"}

            q_id, q_comp, q_name, q_phone, q_email, q_items, q_total, q_notes = q_row
            pdf_path = ""
            if q_notes and "PDF:" in q_notes:
                pdf_path = q_notes.split("PDF:", 1)[1].strip()

            caption = (
                f"Yth. Bapak/Ibu *{q_name or 'Tim Pengadaan'}* ({q_comp}),\n\n"
                f"Berikut kami lampirkan dokumen *Surat Penawaran Harga (SPH) Resmi* nomor *{q_id}* "
                f"untuk kebutuhan *{q_items}*.\n\n"
                f"Apabila terdapat spesifikasi teknis, jumlah unit, atau kelengkapan berkas administrasi (e-Faktur, BAST, Surat Dukungan) "
                f"yang perlu disesuaikan, silakan beri tahu kami. Terima kasih banyak. 🙏\n\n"
                f"Salam hormat,\n*Ahnaf Ibrahim*\nPT. Andis Sentral Laboratorium"
            )
            wa_sent = False
            email_sent = False
            if q_phone and pdf_path and getattr(self, "openwa_client", None):
                wa_sent = await self.openwa_client.send_document(
                    phone=q_phone,
                    file_path=pdf_path,
                    caption=caption,
                )
            if q_email and "@" in str(q_email) and pdf_path:
                try:
                    from harvey.integrations.smtp_mail import SmtpImapProvider
                    smtp = SmtpImapProvider(self.config, self.env)
                    if smtp.is_configured():
                        er = await smtp.send_email(
                            to_email=q_email,
                            subject=f"Surat Penawaran Harga Resmi ({q_id}) - PT. Andis Sentral Laboratorium",
                            body=caption.replace("*", ""),
                            attachments=[pdf_path],
                        )
                        email_sent = er.ok
                except Exception as e:
                    logger.warning(f"WhatsApp: Gagal kirim SPH via email: {e}")

            confirm_msg = (
                f"✅ *SPH Resmi ({q_id}) Berhasil Dikirim!*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏢 *Instansi:* {q_comp}\n"
                f"👤 *PIC:* {q_name or '-'}\n"
                f"📱 *WA Terkirim:* {'Ya ✅' if wa_sent else 'Tidak / Belum ada nomor'}\n"
                f"✉️ *Email Terkirim:* {'Ya ✅' if email_sent else 'Tidak / Belum ada email'}\n"
                f"📅 *QuoteChaser:* Terjadwal follow-up otomatis H+2, H+6, H+12."
            )
            await self._send_text_auto(operator_phone, confirm_msg, is_system_alert=True)
            return {"status": "operator_command_sph_sent", "quote_id": q_id, "wa_sent": wa_sent, "email_sent": email_sent}

        if cmd.startswith("BUAT SPH"):
            # Format: BUAT SPH RSUD Cibinong | Dr. Hendra | Vortex Mixer MX-S x 2
            body_part = raw[8:].strip(" :")
            parts = [p.strip() for p in body_part.split("|") if p.strip()]
            comp_in = parts[0] if len(parts) >= 1 else "Instansi Klien"
            pic_in = parts[1] if len(parts) >= 2 else "Tim Pengadaan / Lab"
            item_in = parts[2] if len(parts) >= 3 else (parts[1] if len(parts) == 2 else "Alat Laboratorium")
            qty_in = 1
            m_qty = re.search(r"\bx\s*(\d+)\b", item_in, flags=re.I)
            if m_qty:
                qty_in = int(m_qty.group(1))
                item_in = re.sub(r"\bx\s*\d+\b", "", item_in, flags=re.I).strip()

            from harvey.integrations.product_matcher import match_product
            from harvey.integrations.sph_generator import SPHGenerator
            m_res = match_product(item_in)
            matched = m_res.get("item") or (m_res.get("alternatives") or [None])[0]
            if matched:
                sph_items = [{
                    "name": matched.get("name"),
                    "brand": matched.get("brand", "AndisLab"),
                    "model": matched.get("model") or matched.get("catalog_no", ""),
                    "qty": qty_in,
                    "unit_price": float(matched.get("price") or 0),
                    "hpp": float(matched.get("harga_beli") or 0),
                    "specifications": matched.get("specifications", ""),
                }]
            else:
                sph_items = [{
                    "name": item_in,
                    "brand": "AndisLab Standard",
                    "model": "Custom",
                    "qty": qty_in,
                    "unit_price": 0.0,
                    "specifications": "Sesuai standar spesifikasi laboratorium",
                }]

            gen = SPHGenerator()
            sph_meta = await gen.generate_and_register_sph(
                company_name=comp_in,
                contact_name=pic_in,
                items=sph_items,
            )
            op_cap = (
                f"📄 *[SPH BERHASIL DIBUAT: {sph_meta['quotation_no']}]*\n"
                f"🏢 Instansi: *{comp_in}*\n"
                f"👤 PIC: *{pic_in}*\n"
                f"💰 Total (+PPN 11%): *{sph_meta['formatted_grand_total']}*\n"
                f"✅ Tersinkronisasi ke Excel Template & Rekap Master SPH!"
            )
            if getattr(self, "openwa_client", None):
                await self.openwa_client.send_document(
                    phone=operator_phone,
                    file_path=sph_meta["pdf_path"],
                    caption=op_cap,
                )
            else:
                await self._send_text_auto(operator_phone, op_cap, is_system_alert=True)
            return {"status": "operator_command_sph_created", "quotation_no": sph_meta["quotation_no"], "pdf_path": sph_meta["pdf_path"]}

        if cmd in ("OK EMAIL", "APPROVE EMAIL", "KIRIM EMAIL", "YES EMAIL"):
            async with self.state._connect() as db:
                cur = await db.execute(
                    """SELECT o.id, o.prospect_id, o.to_email, o.subject, o.body,
                              p.first_name, p.company
                       FROM outbox o
                       LEFT JOIN prospects p ON o.prospect_id = p.id
                       WHERE o.status = 'pending_review' AND o.provider != 'whatsapp'
                       ORDER BY o.created_at DESC LIMIT 1"""
                )
                row = await cur.fetchone()

            if not row:
                reply = "ℹ️ Tidak ada draf email yang menunggu persetujuan saat ini."
                await self._send_text_auto(operator_phone, reply, is_system_alert=True)
                return {"status": "operator_command_no_pending_email"}

            oid, pid, target_email, subj, body, p_name, p_comp = row
            async with self.state._connect() as db:
                await db.execute(
                    "UPDATE outbox SET status = 'approved', approved_by = 'operator_wa' WHERE id = ?",
                    (now_iso, oid) if False else (oid,)
                )
                await db.commit()

            reply = (
                f"✅ *Draf Email Berhasil Disetujui!*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 *Penerima:* {p_name or 'Klien'}\n"
                f"🏢 *Instansi:* {p_comp or '-'}\n"
                f"✉️ *Email:* {target_email}\n"
                f"📌 *Subjek:* {subj}\n\n"
                f"Email telah disetujui dan akan segera dikirimkan oleh sistem pengiriman Harvey."
            )
            await self._send_text_auto(operator_phone, reply, is_system_alert=True)
            return {"status": "operator_command_email_approved", "outbox_id": oid, "to": target_email}

        if cmd in ("HAPUS EMAIL", "BATAL EMAIL", "CANCEL EMAIL", "TOLAK EMAIL"):
            async with self.state._connect() as db:
                cur = await db.execute(
                    """SELECT o.id, o.prospect_id, o.to_email, p.first_name
                       FROM outbox o
                       LEFT JOIN prospects p ON o.prospect_id = p.id
                       WHERE o.status = 'pending_review' AND o.provider != 'whatsapp'
                       ORDER BY o.created_at DESC LIMIT 1"""
                )
                row = await cur.fetchone()
                if row:
                    oid, pid, target_email, p_name = row
                    await db.execute(
                        "UPDATE outbox SET status = 'cancelled', approved_by = 'operator_wa_cancelled' WHERE id = ?",
                        (oid,)
                    )
                    await db.commit()
                    reply = f"🗑️ Draf email untuk *{p_name or 'Klien'}* ({target_email}) berhasil dibatalkan."
                    await self._send_text_auto(operator_phone, reply, is_system_alert=True)
                    return {"status": "operator_command_email_cancelled", "outbox_id": oid}
                else:
                    reply = "ℹ️ Tidak ada draf email yang menunggu persetujuan untuk dibatalkan."
                    await self._send_text_auto(operator_phone, reply, is_system_alert=True)
                    return {"status": "operator_command_no_pending_email"}

        if cmd in ("OK", "KIRIM", "YES", "APPROVE", "GAS", "KIRIMKAN"):
            async with self.state._connect() as db:
                cur = await db.execute(
                    """SELECT o.id, o.prospect_id, o.conversation_id, o.to_email, o.body,
                              p.first_name, p.company
                       FROM outbox o
                       LEFT JOIN prospects p ON o.prospect_id = p.id
                       WHERE o.status = 'pending_review' AND o.provider = 'whatsapp'
                       ORDER BY o.created_at DESC LIMIT 1"""
                )
                row = await cur.fetchone()

            if not row:
                reply = "ℹ️ Tidak ada draf balasan WhatsApp yang menunggu persetujuan saat ini."
                await self._send_text_auto(operator_phone, reply, is_system_alert=True)
                return {"status": "operator_command_no_pending"}

            oid, pid, cid, target_phone, body, p_name, p_comp = row
            target_norm = normalize_id_phone(target_phone) or target_phone
            sent_ok = await self._send_text_auto(target_norm, body)
            if sent_ok:
                act = self._load_activity()
                act["sent"] = act.get("sent", 0) + 1
                if target_norm not in act.get("numbers", []):
                    act.setdefault("numbers", []).append(target_norm)
                self._save_activity(act)

                async with self.state._connect() as db:
                    await db.execute(
                        "UPDATE outbox SET status = 'sent', sent_at = ?, approved_by = 'operator_wa' WHERE id = ?",
                        (now_iso, oid)
                    )
                    if cid:
                        cur_c = await db.execute("SELECT thread_json FROM conversations WHERE id = ?", (cid,))
                        crow = await cur_c.fetchone()
                        thread = []
                        if crow and crow[0]:
                            try:
                                thread = json.loads(crow[0])
                            except Exception:
                                thread = []
                        thread.append({
                            "direction": "outbound",
                            "channel": "whatsapp",
                            "content": body,
                            "text": body,
                            "sender": "operator_via_bot",
                            "timestamp": now_utc.isoformat(),
                        })
                        await db.execute(
                            "UPDATE conversations SET thread_json = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(thread), now_iso, cid)
                        )
                    await db.commit()

                if getattr(self, "obsidian", None):
                    try:
                        self.obsidian.log_chat_event(
                            phone=target_norm,
                            name=p_name or "",
                            company=p_comp or "",
                            direction="outbound",
                            message=body,
                        )
                    except Exception:
                        pass

                try:
                    await self.state.log_operator_feedback(
                        outbox_id=oid,
                        prospect_id=pid or "",
                        ai_draft=body,
                        operator_action="OK",
                        final_text=body,
                    )
                except Exception:
                    pass

                reply = (
                    f"✅ *Balasan WhatsApp Berhasil Dikirim!*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 *Penerima:* {p_name or 'Klien'}\n"
                    f"🏢 *Instansi:* {p_comp or '-'}\n"
                    f"📱 *Nomor:* {target_norm}\n\n"
                    f"💬 *Isi Pesan Terkirim:*\n"
                    f"> {body[:200]}..."
                )
                await self._send_text_auto(operator_phone, reply, is_system_alert=True)
                return {"status": "operator_command_sent", "outbox_id": oid, "to": target_norm}
            else:
                reply = f"⚠️ Gagal mengirim balasan ke {target_norm}. Mohon periksa koneksi WhatsApp Web / OpenWA."
                await self._send_text_auto(operator_phone, reply, is_system_alert=True)
                return {"status": "operator_command_send_failed", "outbox_id": oid}

        # 2. REVISI: <teks baru> -> Update body and send immediately
        if cmd.startswith("REVISI:") or cmd.startswith("REVISI "):
            new_body = raw.split(":", 1)[1].strip() if ":" in raw else raw[6:].strip()
            if not new_body:
                reply = "⚠️ Mohon sertakan isi revisinya. Contoh: *REVISI: Halo Pak, ini penawaran resminya...*"
                await self._send_text_auto(operator_phone, reply, is_system_alert=True)
                return {"status": "operator_command_empty_revision"}

            async with self.state._connect() as db:
                cur = await db.execute(
                    """SELECT o.id, o.prospect_id, o.conversation_id, o.to_email, o.body,
                              p.first_name, p.company
                       FROM outbox o
                       LEFT JOIN prospects p ON o.prospect_id = p.id
                       WHERE o.status = 'pending_review' AND o.provider = 'whatsapp'
                       ORDER BY o.created_at DESC LIMIT 1"""
                )
                row = await cur.fetchone()

            if not row:
                reply = "ℹ️ Tidak ada draf yang menunggu persetujuan untuk direvisi."
                await self._send_text_auto(operator_phone, reply, is_system_alert=True)
                return {"status": "operator_command_no_pending"}

            oid, pid, cid, target_phone, orig_body, p_name, p_comp = row
            target_norm = normalize_id_phone(target_phone) or target_phone
            sent_ok = await self._send_text_auto(target_norm, new_body)
            if sent_ok:
                act = self._load_activity()
                act["sent"] = act.get("sent", 0) + 1
                if target_norm not in act.get("numbers", []):
                    act.setdefault("numbers", []).append(target_norm)
                self._save_activity(act)

                async with self.state._connect() as db:
                    await db.execute(
                        "UPDATE outbox SET body = ?, status = 'sent', sent_at = ?, approved_by = 'operator_wa_revised' WHERE id = ?",
                        (new_body, now_iso, oid)
                    )
                    if cid:
                        cur_c = await db.execute("SELECT thread_json FROM conversations WHERE id = ?", (cid,))
                        crow = await cur_c.fetchone()
                        thread = []
                        if crow and crow[0]:
                            try:
                                thread = json.loads(crow[0])
                            except Exception:
                                thread = []
                        thread.append({
                            "direction": "outbound",
                            "channel": "whatsapp",
                            "content": new_body,
                            "text": new_body,
                            "sender": "operator_revised",
                            "timestamp": now_utc.isoformat(),
                        })
                        await db.execute(
                            "UPDATE conversations SET thread_json = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(thread), now_iso, cid)
                        )
                    await db.commit()

                try:
                    await self.state.log_operator_feedback(
                        outbox_id=oid,
                        prospect_id=pid or "",
                        ai_draft=orig_body or "",
                        operator_action="REVISI",
                        final_text=new_body,
                    )
                except Exception:
                    pass

                reply = (
                    f"✅ *Balasan Revisi Berhasil Dikirim!*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 *Penerima:* {p_name or 'Klien'}\n"
                    f"🏢 *Instansi:* {p_comp or '-'}\n"
                    f"📱 *Nomor:* {target_norm}\n\n"
                    f"💬 *Pesan Baru:*\n"
                    f"> {new_body[:200]}..."
                )
                await self._send_text_auto(operator_phone, reply, is_system_alert=True)
                return {"status": "operator_command_revised_sent", "outbox_id": oid, "to": target_norm}
            else:
                reply = f"⚠️ Gagal mengirim pesan revisi ke {target_norm}."
                await self._send_text_auto(operator_phone, reply, is_system_alert=True)
                return {"status": "operator_command_send_failed"}

        # 3. HAPUS / BATAL / SKIP / TOLAK
        if cmd in ("HAPUS", "BATAL", "SKIP", "TOLAK", "DELETE", "CANCEL"):
            async with self.state._connect() as db:
                cur = await db.execute(
                    """SELECT o.id, o.prospect_id, p.first_name, o.to_email, o.body
                       FROM outbox o
                       LEFT JOIN prospects p ON o.prospect_id = p.id
                       WHERE o.status = 'pending_review' AND o.provider = 'whatsapp'
                       ORDER BY o.created_at DESC LIMIT 1"""
                )
                row = await cur.fetchone()
                if row:
                    oid, pid, p_name, target_phone, orig_body = row
                    await db.execute(
                        "UPDATE outbox SET status = 'cancelled', approved_by = 'operator_wa_cancelled' WHERE id = ?",
                        (oid,)
                    )
                    if pid:
                        cur_p = await db.execute("SELECT personalization_notes FROM prospects WHERE id = ?", (pid,))
                        p_row = await cur_p.fetchone()
                        notes = p_row[0] if p_row else ""
                        new_notes = f"{notes} | [WA_SKIP:{date.today().isoformat()}]".strip(" |")
                        await db.execute("UPDATE prospects SET personalization_notes = ? WHERE id = ?", (new_notes, pid))
                    await db.commit()

                    try:
                        await self.state.log_operator_feedback(
                            outbox_id=oid,
                            prospect_id=pid or "",
                            ai_draft=orig_body or "",
                            operator_action="BATAL",
                            final_text="",
                        )
                    except Exception:
                        pass

                    reply = f"🗑️ Draf balasan untuk *{p_name or 'Klien'}* ({target_phone}) berhasil dibatalkan."
                    await self._send_text_auto(operator_phone, reply, is_system_alert=True)
                    return {"status": "operator_command_cancelled", "outbox_id": oid}
                else:
                    reply = "ℹ️ Tidak ada draf balasan yang menunggu persetujuan untuk dibatalkan."
                    await self._send_text_auto(operator_phone, reply, is_system_alert=True)
                    return {"status": "operator_command_no_pending"}

        # 4. STATUS / REKAP / STAT / INFO / PING
        if cmd in ("STATUS", "REKAP", "STAT", "INFO", "PING"):
            act = self._load_activity()
            async with self.state._connect() as db:
                cur = await db.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending_review' AND provider = 'whatsapp'")
                pending_count = (await cur.fetchone())[0]

            openwa_status = "Aktif (Ready)" if (self.openwa and await self.openwa.check_health()) else "Nonaktif / Standby"
            reply = (
                f"📊 *Status Harvey & WhatsApp Engine*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"• Kuota Terkirim Hari Ini: *{act.get('sent', 0)} / {self.wa_cfg.max_daily_messages}*\n"
                f"• Outreach Baru: *{act.get('new_sent', 0)}* | Follow-up: *{act.get('fu_sent', 0)}*\n"
                f"• Draf Menunggu Review: *{pending_count}*\n"
                f"• OpenWA Gateway: *{openwa_status}*\n"
                f"• Vault Obsidian: *{'Tersambung' if getattr(self, 'obsidian', None) else 'Nonaktif'}*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"Balas *OK* untuk kirim draf terbaru, atau *REVISI: <pesan>* untuk mengubah."
            )
            await self._send_text_auto(operator_phone, reply, is_system_alert=True)
            return {"status": "operator_command_status"}

        if cmd in ("HELP", "BANTUAN", "MENU", "COMMAND", "COMMANDS"):
            reply = (
                "🤖 *Harvey Remote Command Center*\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "Perintah instan via chat WhatsApp:\n"
                "• *OK* / *KIRIM* : Setujui & kirim balasan WhatsApp\n"
                "• *OK EMAIL* : Setujui draf email penawaran/balasan\n"
                "• *MARGIN <angka>* : Ubah margin sourcing (misal: MARGIN 25)\n"
                "• *REVISI: <pesan>* : Edit teks & langsung kirim ke klien\n"
                "• *BATAL* / *BATAL EMAIL* : Batalkan draf pesan / email\n"
                "• *STATUS* : Cek performa & kuota pesan hari ini\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "Dashboard: http://127.0.0.1:5555/#today"
            )
            await self._send_text_auto(operator_phone, reply, is_system_alert=True)
            return {"status": "operator_command_help"}

        logger.info(f"WhatsApp: Pesan operator non-perintah diabaikan ({cmd}).")
        return {"status": "ignored_operator"}

    async def analyze_media_with_gemini_vision(
        self,
        media_b64: str,
        mime_type: str = "image/jpeg",
        caption: str = "",
    ) -> str:
        """Read equipment labels, probe connectors, or RFQ photos using Gemini 2.5 Flash Vision."""
        api_key = getattr(self.env, "gemini_api_key", "") or os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")
        if not api_key or not media_b64:
            return ""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={api_key}"
        prompt_txt = (
            "Kamu adalah asisten teknis laboratorium PT Andis Sentral Laboratorium. "
            "Baca foto/dokumen dari klien WhatsApp ini secara akurat. "
            "Sebutkan: (1) Nama alat / jenis barang, (2) Merk & Model/Tipe jika terlihat di stiker/label, "
            "(3) Spesifikasi atau daftar item RFQ jika berupa tabel/catatan. "
            f"Caption klien: '{caption or '-'}'. Jawab singkat dan padat dalam bahasa Indonesia."
        )
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt_txt},
                    {"inline_data": {"mime_type": mime_type or "image/jpeg", "data": media_b64}},
                ]
            }],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 400},
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                    return "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
        except Exception as e:
            logger.debug(f"WhatsApp: Gemini Vision read error: {e}")
        return ""

    async def handle_single_inbound(
        self,
        phone: str,
        message: str,
        title: str = "",
        chat_id: str = "",
        media_b64: str = "",
        mime_type: str = "",
    ) -> dict:
        """Process an incoming WhatsApp message:
        1. Check operator filter (remote command center)
        2. Find or create prospect
        3. Check human takeover (if operator replied manually in the last 2 hours, pause auto-reply)
        4. Update or create conversation thread in DB
        5. Check for Give-First asset consent; otherwise classify intent & draft Ahnaf reply
        6. Store draft in outbox (status='pending_review', kind='reply', provider='whatsapp')
        7. Send instant WhatsApp review alert notification to operator (087764021131)
        8. If auto_reply_mode == 'safe' and intent is safe and in reply hours and not takeover, auto-send reply.
        """
        norm_phone = normalize_id_phone(phone)
        if not norm_phone:
            norm_phone = extract_phone_from_str(phone) or extract_phone_from_str(title)
        if not norm_phone:
            norm_digits = re.sub(r"[^\d]", "", str(phone or ""))
            if len(norm_digits) >= 9:
                norm_phone = normalize_id_phone(norm_digits)
            elif len(norm_digits) >= 14:  # WhatsApp LID fallback
                norm_phone = norm_digits

        if not norm_phone:
            logger.warning(f"WhatsApp handle_single_inbound: nomor tidak valid '{phone}' (title='{title}')")
            return {"status": "invalid_phone"}

        # Preserve chat_id if target is LID or provided
        effective_target = chat_id or norm_phone

        # Safety filter: Operator Command Center
        if is_internal_operator_number(norm_phone, getattr(self.wa_cfg, "report_phone", None)):
            logger.info(f"WhatsApp: Menerima perintah dari operator ({norm_phone}): {message}")
            return await self.handle_operator_command(norm_phone, message)

        # Safety filter: Blocklist / Opted-out / Indotrading Exclusion
        exclusions = getattr(getattr(self.config, "compliance", None), "partner_exclusions", []) or []
        if "8135955904" in norm_phone or any(ex.lower() in norm_phone for ex in exclusions if ex):
            logger.info(f"WhatsApp: Nomor {norm_phone} masuk daftar blokir / Indotrading. Pesan diabaikan.")
            return {"status": "opted_out_ignored", "phone": norm_phone}

        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.replace(tzinfo=None).isoformat()

        # Step 1b: Strip quoted echo / previous bot messages & enrich with Gemini Vision if media attached
        cleaned_message = strip_quoted_echo(message)
        effective_message = cleaned_message or message
        if media_b64:
            vision_text = await self.analyze_media_with_gemini_vision(
                media_b64=media_b64,
                mime_type=mime_type or "image/jpeg",
                caption=effective_message,
            )
            if vision_text:
                effective_message = f"[HASIL BACA FOTO/DOKUMEN KLIEN]: {vision_text}\n{effective_message}".strip()
                logger.info(f"WhatsApp: Gemini Vision membaca foto dari {norm_phone}: {vision_text[:100]}")

        # Step 1c: Auto-Responder / Hotline Bot Detector (never reply to institutional bots)
        msg_low_check = (effective_message or "").lower()
        bot_markers = [
            "selamat datang di layanan", "hotline ppkpt", "open hours",
            "jam operasional", "layanan whatsapp kemdiktisaintek",
            "pesan kakak telah kami terima", "terima kasih telah menghubungi kami",
            "terima kasih telah menghubungi pt.", "kami sedang tidak ada saat ini",
            "this is an automated message", "do not reply directly",
            "siparu — sistem pemanfaatan ruang", "tidak ada respon lanjutan",
        ]
        if any(bm in msg_low_check for bm in bot_markers):
            logger.info(f"WhatsApp: Auto-responder / hotline bot terdeteksi dari {norm_phone}. Diabaikan.")
            return {"status": "auto_responder_ignored", "phone": norm_phone}

        # Step 2: Find or create prospect
        pid = None
        fn = ""
        ln = ""
        comp = ""
        notes = ""

        async with self.state._connect() as db:
            cur = await db.execute(
                """SELECT id, first_name, last_name, company, personalization_notes
                   FROM prospects
                   WHERE phone = ? OR phone = ? OR phone = ? OR phone LIKE ?
                   ORDER BY updated_at DESC LIMIT 1""",
                (norm_phone, f"0{norm_phone[2:]}" if norm_phone.startswith("62") else norm_phone,
                 f"+{norm_phone}", f"%{norm_phone[-9:]}")
            )
            row = await cur.fetchone()
            if row:
                pid, fn, ln, comp, notes = row[0], row[1] or "", row[2] or "", row[3] or "", row[4] or ""
                # Strict opt-out / exclusion drop
                cur_st = await db.execute("SELECT status, company, email FROM prospects WHERE id = ?", (pid,))
                st_row = await cur_st.fetchone()
                if st_row:
                    st_val = (st_row[0] or "").lower()
                    comp_val = (st_row[1] or "").lower()
                    email_val = (st_row[2] or "").lower()
                    if st_val == "opted_out" or "indotrading" in comp_val or "indotrading" in email_val:
                        logger.info(f"WhatsApp: Prospek {norm_phone} ({comp_val}) berstatus opted_out / Indotrading. Mengabaikan pesan.")
                        return {"status": "opted_out_ignored", "phone": norm_phone}

        # Check quotes if still not found
        quote_id = None
        if not pid:
            try:
                async with self.state._connect() as db:
                    cur = await db.execute(
                        """SELECT id, company, contact_name, phone FROM quotes
                           WHERE status = 'pending' AND (phone = ? OR phone LIKE ?)
                           LIMIT 1""",
                        (norm_phone, f"%{norm_phone[-9:]}")
                    )
                    qrow = await cur.fetchone()
                    if qrow:
                        quote_id = qrow[0]
                        comp = qrow[1] or ""
                        fn = (qrow[2] or "").replace("Ibu ", "").replace("Bapak ", "").strip()
                        await db.execute("UPDATE quotes SET status = 'replied' WHERE id = ?", (quote_id,))
                        await db.commit()
            except Exception:
                pass

        # Auto-create prospect if unknown
        if not pid:
            pid = uuid.uuid4().hex[:12]
            fn = fn or (title or f"WA Lead {norm_phone[-4:]}")
            async with self.state._connect() as db:
                await db.execute(
                    """INSERT OR IGNORE INTO prospects
                       (id, first_name, last_name, company, phone, status, source, personalization_notes, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'replied', 'inbound_whatsapp', '[INBOUND_WA]', ?, ?)""",
                    (pid, fn, ln, comp, norm_phone, now_iso, now_iso)
                )
                await db.commit()
            notes = "[INBOUND_WA]"
            logger.info(f"WhatsApp: Prospek baru dibuat untuk inbound {norm_phone} ({fn} - {comp})")

        # Step 2b: Auto-Parser Form Data Penawaran (G-05) -> enrich prospect profile automatically
        g05_enriched = False
        g05_role = ""
        g05_email = ""
        g05_phone = ""
        if "nama:" in msg_low_check and any(k in msg_low_check for k in ("pt/perusahaan:", "perusahaan:", "email:", "jabatan:")):
            try:
                parsed_name = ""
                parsed_role = ""
                parsed_phone = ""
                parsed_email = ""
                parsed_comp = ""
                for line in effective_message.splitlines():
                    low = line.lower().strip()
                    if low.startswith("nama:"):
                        parsed_name = line.split(":", 1)[1].strip()
                    elif low.startswith("jabatan:"):
                        parsed_role = line.split(":", 1)[1].strip()
                    elif low.startswith(("nomor telfon:", "nomor telepon:", "no telp:", "no. telp:", "hp:", "wa:")):
                        parsed_phone = normalize_id_phone(line.split(":", 1)[1].strip())
                    elif low.startswith("email:"):
                        em_m = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", line)
                        if em_m:
                            parsed_email = em_m.group(0).lower()
                    elif low.startswith(("pt/perusahaan:", "perusahaan:", "instansi:")):
                        parsed_comp = line.split(":", 1)[1].strip()

                if parsed_name or parsed_comp or parsed_email:
                    name_parts = parsed_name.split(None, 1)
                    new_fn = name_parts[0] if name_parts else fn
                    new_ln = name_parts[1] if len(name_parts) > 1 else ln
                    fn = new_fn or fn
                    ln = new_ln or ln
                    comp = parsed_comp or comp
                    g05_enriched = True
                    g05_role = parsed_role
                    g05_email = parsed_email
                    g05_phone = parsed_phone
                    async with self.state._connect() as db:
                        await db.execute(
                            """UPDATE prospects
                               SET first_name = COALESCE(NULLIF(?, ''), first_name),
                                   last_name = COALESCE(NULLIF(?, ''), last_name),
                                   title = COALESCE(NULLIF(?, ''), title),
                                   company = COALESCE(NULLIF(?, ''), company),
                                   email = COALESCE(NULLIF(?, ''), email),
                                   phone = COALESCE(NULLIF(?, ''), phone),
                                   updated_at = ?
                               WHERE id = ?""",
                            (new_fn, new_ln, parsed_role, parsed_comp, parsed_email, parsed_phone, now_iso, pid)
                        )
                        await db.commit()
                    logger.info(f"WhatsApp: G-05 Auto-Enrichment berhasil untuk {pid}: {fn} {ln} | {parsed_role} | {comp}")
            except Exception as e:
                logger.warning(f"WhatsApp: G-05 Auto-Parser error: {e}")

        # Step 3 & 4: Manage conversation thread and Human Takeover check
        convo_id = None
        thread = []
        human_takeover = False

        async with self.state._connect() as db:
            cur = await db.execute(
                """SELECT id, thread_json, status, updated_at FROM conversations
                   WHERE prospect_id = ? AND channel = 'whatsapp' AND status != 'closed'
                   ORDER BY updated_at DESC LIMIT 1""",
                (pid,)
            )
            crow = await cur.fetchone()
            if crow:
                convo_id = crow[0]
                try:
                    thread = json.loads(crow[1] or "[]")
                except Exception:
                    thread = []

            # Check human takeover (default to True so Harvey never interrupts manual human operator)
            takeover_enabled = getattr(self.wa_cfg, "human_takeover_enabled", True)
            if takeover_enabled and thread:
                for msg_item in reversed(thread):
                    if msg_item.get("direction") == "outbound" and msg_item.get("sender") in ("operator", "human", "you"):
                        ts_str = msg_item.get("timestamp")
                        if ts_str:
                            try:
                                msg_dt = datetime.fromisoformat(ts_str)
                                if msg_dt.tzinfo is None:
                                    msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                                if (now_utc - msg_dt).total_seconds() < 43200:
                                    human_takeover = True
                                    logger.info(f"WhatsApp: Human takeover lock (12j) aktif untuk {norm_phone} (operator berbicara {((now_utc - msg_dt).total_seconds()/60):.0f} mnt lalu).")
                            except Exception:
                                pass
                        break

            # Append incoming message
            new_msg = {
                "direction": "inbound",
                "channel": "whatsapp",
                "content": message,
                "text": message,
                "sender": "prospect",
                "timestamp": now_utc.isoformat(),
            }
            thread.append(new_msg)

            if convo_id:
                await db.execute(
                    "UPDATE conversations SET thread_json = ?, status = 'open', updated_at = ? WHERE id = ?",
                    (json.dumps(thread), now_iso, convo_id)
                )
            else:
                convo_id = uuid.uuid4().hex[:12]
                await db.execute(
                    """INSERT INTO conversations
                       (id, prospect_id, channel, stage, status, thread_json, created_at, updated_at)
                       VALUES (?, ?, 'whatsapp', 'inbound', 'open', ?, ?, ?)""",
                    (convo_id, pid, json.dumps(thread), now_iso, now_iso)
                )
            await db.commit()

        # Step 4a: Auto-Cetak PDF SPH resmi jika Form G-05 baru saja terisi (thread sudah dimuat)
        if g05_enriched and thread:
            try:
                from harvey.integrations.product_matcher import match_product
                from harvey.integrations.sph_generator import SPHGenerator
                matched_prod = None
                for m_item in reversed(thread):
                    txt_c = (m_item.get("content") or m_item.get("text") or "").strip()
                    if txt_c and len(txt_c) > 5 and "nama:" not in txt_c.lower():
                        m_r = match_product(txt_c)
                        if m_r.get("status") in ("EXACT", "CLARIFICATION") and m_r.get("item"):
                            matched_prod = m_r["item"]
                            break
                if matched_prod and float(matched_prod.get("price") or 0) > 0:
                    gen = SPHGenerator()
                    sph_meta = await gen.generate_and_register_sph(
                        company_name=comp or "Instansi Klien",
                        contact_name=f"{fn} {ln}".strip() or "Tim Pengadaan / Lab",
                        contact_title=g05_role or "Laboratory / Procurement",
                        phone=g05_phone or norm_phone,
                        email=g05_email or "",
                        prospect_id=pid or "",
                        stock_note="Ready Stock / Prinsipal Resmi",
                        items=[{
                            "name": matched_prod.get("name"),
                            "brand": matched_prod.get("brand", "AndisLab"),
                            "model": matched_prod.get("model") or matched_prod.get("catalog_no", ""),
                            "qty": 1,
                            "unit_price": float(matched_prod.get("price") or 0),
                            "hpp": float(matched_prod.get("harga_beli") or 0),
                            "specifications": matched_prod.get("specifications", ""),
                        }],
                    )
                    op_phone = normalize_id_phone(getattr(self.wa_cfg, "report_phone", None) or "087764021131")
                    wa_doc_client = getattr(self, "openwa", None) or getattr(self, "openwa_client", None)
                    if wa_doc_client and op_phone:
                        await wa_doc_client.send_document(
                            phone=op_phone,
                            file_path=sph_meta["pdf_path"],
                            caption=(
                                f"📄 *[AUTO-SPH DARI FORM G-05 SIAP: {sph_meta['quotation_no']}]*\n"
                                f"🏢 Instansi: *{comp}*\n"
                                f"👤 PIC: *{fn} {ln}* ({g05_role or '-'})\n"
                                f"📦 Alat: *{matched_prod.get('name')}*\n"
                                f"💰 Total (+PPN 11%): *{sph_meta['formatted_grand_total']}*\n\n"
                                f"👉 Balas *OK SPH* untuk langsung kirim file PDF ini ke WA & Email Klien!"
                            ),
                        )
            except Exception as sph_err:
                logger.warning(f"WhatsApp: Auto-SPH G-05 warning: {sph_err}")

        # Step 5: Check for Give-First Asset Consent vs Standard Inbound Classification
        pending_offer = find_pending_asset_offer(notes)
        is_consent = bool(pending_offer and is_affirmative_consent(message))

        if is_consent:
            asset_id, _offer_d = pending_offer
            asset = get_asset_by_id(asset_id)
            title_doc = asset.get("title", "dokumen pengadaan alat lab") if asset else "dokumen pengadaan alat lab"
            pdf_rel = asset.get("file_paths", {}).get("pdf", "") if asset else ""
            pdf_path = (PROJECT_ROOT / pdf_rel).resolve() if pdf_rel else None
            who, _ = _person_address(fn, ln, comp)
            caption = build_asset_caption(title_doc, who)
            intent = "MINTA ASET"
            draft = caption

            await self._tag_prospect(pid, notes, "WA_REPLIED")
            try:
                await self.state.update_prospect_status(pid, "replied")
            except Exception:
                pass
            await self.state.log_action(
                action_type="whatsapp_reply",
                agent="handler",
                details={"prospect_id": pid, "company": comp, "intent": intent, "asset_id": asset_id},
            )

            # Check send conditions for asset
            in_hours = self.in_reply_hours()
            act = self._load_activity()
            has_quota = act["sent"] < self.wa_cfg.max_daily_messages
            if in_hours and has_quota and not human_takeover:
                doc_res = "error"
                if pdf_path and pdf_path.exists():
                    doc_res = await self._send_document_via_web(norm_phone, caption, pdf_path)
                if doc_res == "sent":
                    act["sent"] += 1
                    act["numbers"].append(norm_phone)
                    self._save_activity(act)
                    await self._tag_prospect(pid, notes, f"WA_ASSET_SENT:{asset_id}")
                    # Append to thread
                    thread.append({
                        "direction": "outbound",
                        "channel": "whatsapp",
                        "content": f"[Dokumen Terkirim: {title_doc}]\n{caption}",
                        "text": f"[Dokumen Terkirim: {title_doc}]\n{caption}",
                        "sender": "harvey",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    async with self.state._connect() as db:
                        await db.execute("UPDATE conversations SET thread_json = ?, updated_at = ? WHERE id = ?",
                                         (json.dumps(thread), now_iso, convo_id))
                        await db.commit()
                else:
                    fallback_text = build_asset_fallback_text(title_doc, who)
                    sent_fallback = await self._send_text_auto(norm_phone, fallback_text)
                    if sent_fallback:
                        act["sent"] += 1
                        act["numbers"].append(norm_phone)
                        self._save_activity(act)
                    await self._tag_prospect(pid, notes, f"WA_ASSET_SENT:{asset_id}")

            # Notify operator about asset request
            await self.notify_hot_lead(
                name=f"{fn} {ln}".strip() or title or norm_phone,
                company=comp,
                channel="WhatsApp",
                contact_info=norm_phone,
                intent=intent,
                message_snippet=message[:250],
                recommendation=f"Klien menyetujui penawaran dokumen: *{title_doc}*.",
            )
            return {
                "status": "ok",
                "prospect_id": pid,
                "conversation_id": convo_id,
                "intent": intent,
                "asset_id": asset_id,
            }

        # If Human Takeover Lock (12h) is active, do NOT generate AI drafts or interrupt Ahnaf
        if human_takeover:
            logger.info(f"WhatsApp: Melewati AI draft untuk {norm_phone} karena Human Takeover Lock aktif.")
            return {
                "status": "human_takeover_muted",
                "prospect_id": pid,
                "conversation_id": convo_id,
                "auto_replied": False,
            }

        # Step 4b: Closing Handoff Detector ("SIAP VISIT / NEGOSIASI" -> Executive Closing Brief to Ahnaf)
        closing_triggers = [
            "visit", "berkunjung", "ketemu", "meeting", "presentasi", "demo alat",
            "nego", "negosiasi", "minta diskon", "potongan harga", "kurangin harga",
            "minta tempo", "siap po", "terbit po", "kirim po", "tanda tangan direksi",
            "jadwal ke kantor", "kapan bisa datang",
        ]
        if any(ct in msg_low_check for ct in closing_triggers):
            try:
                await self._tag_prospect(pid, notes, "READY_FOR_VISIT_OR_NEGOTIATION")
                q_info = ""
                floor_info = ""
                async with self.state._connect() as db:
                    cur_q = await db.execute(
                        "SELECT id, items_summary, total_amount FROM quotes WHERE phone LIKE ? OR company = ? ORDER BY rowid DESC LIMIT 1",
                        (f"%{norm_phone[-9:]}", comp or "")
                    )
                    qr = await cur_q.fetchone()
                    if qr and qr[2]:
                        amt = float(qr[2])
                        floor_amt = amt * 0.90  # Ruang negosiasi aman ~10%
                        q_info = f"{qr[0]} ({qr[1]})"
                        floor_info = f"Rp {amt:,.0f} (Batas Aman Nego: Rp {floor_amt:,.0f})"

                brief = (
                    f"🎯 *[SIAP VISIT / NEGOSIASI — HANDOFF KE MAS AHNAF]*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🏢 *Instansi:* {comp or 'Instansi Klien'}\n"
                    f"👤 *PIC:* {fn} {ln}".strip() + f"\n"
                    f"📱 *WhatsApp:* `{norm_phone}`\n"
                    f"📦 *SPH / Alat:* {q_info or 'Lihat riwayat chat'}\n"
                    f"💰 *Nilai & Ruang Nego:* {floor_info or 'Sesuai katalog'}\n\n"
                    f"💬 *Pesan Klien:*\n\"{effective_message[:250]}\"\n\n"
                    f"👉 *Tindakan:* AI otomatis minggir (mute) agar Mas Ahnaf bisa langsung jadwalkan visit / negosiasi closing!"
                )
                await self.send_system_alert(brief)
            except Exception as handoff_err:
                logger.warning(f"WhatsApp: Closing Handoff alert warning: {handoff_err}")

        # Check if this thread is an active Vendor Sourcing Negotiation (we are BUYER, not seller!)
        is_vendor_nego = False
        try:
            async with self.state._connect() as db_vn:
                vn_row = await (await db_vn.execute("SELECT intent FROM conversations WHERE id = ?", (convo_id,))).fetchone()
                if vn_row and (vn_row[0] or "") == "VENDOR_NEGOTIATION":
                    is_vendor_nego = True
        except Exception:
            pass

        if is_vendor_nego:
            intent = "VENDOR_NEGOTIATION"
            is_hot = False
            send_compro_pdf = False
            try:
                from harvey.brain import Brain
                b_vn = Brain(self.state, self.config.brain)
                hist_txt = "\n".join(
                    f"[{'AndisLab' if m.get('direction') == 'outbound' else 'Vendor'}]: {(m.get('content') or m.get('text') or '')[:180]}"
                    for m in (thread or [])[-6:]
                )
                vn_prompt = (
                    "Kamu tim Purchasing PT Andis Sentral Laboratorium (AndisLab). "
                    "Kamu sedang bernegosiasi MEMBELI barang dari vendor ini (bukan menjual). "
                    "ATURAN: DILARANG menyebut nama klien akhir. Minta harga reseller/best price nett, konfirmasi stok ready, garansi resmi, dan estimasi kirim ke Jabodetabek. "
                    f"Balas singkat sopan (25-50 kata).\n\nRiwayat chat:\n{hist_txt}\n\nPesan terbaru vendor: \"{effective_message}\""
                )
                draft = (await b_vn.think(vn_prompt, agent="handler", task="vendor_negotiation")) or (
                    "Terima kasih infonya Pak/Bu. Untuk pembelian B2B/reseller AndisLab, boleh bantu best price nett-nya berapa dan apakah unit ready stock siap kirim ke Jabodetabek? 🙏"
                )
            except Exception:
                draft = "Terima kasih infonya Pak/Bu. Untuk pembelian B2B/reseller AndisLab, boleh bantu best price nett-nya berapa dan apakah unit ready stock siap kirim ke Jabodetabek? 🙏"
            try:
                await self.send_system_alert(
                    f"🤝 *[BALASAN VENDOR SOURCING]*\n"
                    f"Vendor: *{comp or fn or norm_phone}* (`{norm_phone}`)\n"
                    f"Pesan: _{effective_message[:220]}_\n"
                    f"Balasan Agen Nego: _{draft[:220]}_"
                )
            except Exception:
                pass
        else:
            # Adaptive Multi-turn Ahnaf Inbound Classification (Obsidian + Gemini Flash / Groq)
            intent, draft, is_hot, send_compro_pdf = await self.generate_adaptive_reply(
                sender_name=fn,
                company=comp,
                incoming_message=effective_message,
                thread_history=thread,
                notes=notes,
                phone=norm_phone,
            )

        # ── Auto-PIC Referral Handoff: create & queue/send warm WA to forwarded PIC ──
        if intent == "REFERRAL BARU":
            try:
                ref_phone, ref_name, ref_dept = extract_pic_referral(effective_message, sender_phone=norm_phone)
                if ref_phone:
                    # Permanently lock original sender & cancel any queued outbox items so we never disturb them again
                    async with self.state._connect() as db:
                        await db.execute(
                            """UPDATE prospects
                               SET status = 'opted_out',
                                   personalization_notes = CASE
                                     WHEN coalesce(personalization_notes,'') NOT LIKE '%[REFERRED_TO_PIC]%'
                                     THEN trim(coalesce(personalization_notes,'') || ' | [REFERRED_TO_PIC:' || ? || ']', ' |')
                                     ELSE personalization_notes
                                   END,
                                   updated_at = ?
                               WHERE id = ? OR (phone != '' AND phone = ?)""",
                            (ref_phone, now_iso, pid, norm_phone),
                        )
                        await db.execute(
                            """UPDATE outbox SET status = 'cancelled'
                               WHERE status IN ('pending_review', 'approved')
                                 AND (prospect_id = ? OR to_email = ?)""",
                            (pid, norm_phone),
                        )
                        await db.commit()
                    ref_salut = f"Bapak/Ibu {ref_name}" if ref_name else (f"Tim {ref_dept}" if ref_dept else "Bapak/Ibu")
                    ref_comp = comp or "Instansi/Perusahaan Bapak/Ibu"
                    ref_msg = (
                        f"Selamat pagi/siang {ref_salut} ({ref_comp}) 🙏\n\n"
                        f"Perkenalkan saya Ahnaf dari *PT Andis Sentral Laboratorium (AndisLab)*, distributor resmi alat, instrumen analitik, glassware, dan bahan kimia laboratorium (ready stock Jabodetabek).\n\n"
                        f"Kami diarahkan oleh rekan dari {ref_comp} untuk berkoordinasi langsung dengan Bapak/Ibu terkait kebutuhan {ref_dept or 'laboratorium/pengadaan'}. "
                        f"Jika berkenan, boleh kami kirimkan katalog produk kami atau diskusikan kebutuhan alat lab yang sedang dicari saat ini? Terima kasih. 🙏"
                    )
                    ref_pid = f"wa_{ref_phone[-8:]}"
                    async with self.state._connect() as db:
                        cur_ref = await db.execute("SELECT id FROM prospects WHERE phone = ? OR id = ? LIMIT 1", (ref_phone, ref_pid))
                        rrow = await cur_ref.fetchone()
                        if rrow:
                            ref_pid = rrow[0]
                        else:
                            await db.execute(
                                """INSERT OR IGNORE INTO prospects (
                                    id, first_name, company, phone, phone_verified, title,
                                    source, status, score, personalization_notes, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, 1, ?, 'wa_referral', 'queued', 85, ?, ?, ?)""",
                                (
                                    ref_pid,
                                    ref_name or f"PIC {ref_dept or 'Lab'}",
                                    ref_comp,
                                    ref_phone,
                                    ref_dept or "PIC Laboratorium / Pengadaan",
                                    f"[REFERRAL dari {fn or norm_phone}] {effective_message[:180]}",
                                    now_iso,
                                    now_iso,
                                ),
                            )
                        await db.commit()

                    mode_now = str(getattr(self.wa_cfg, "auto_reply_mode", "draft") or "draft").lower().strip()
                    if mode_now in ("full_auto", "autopilot") and self.in_reply_hours():
                        sent_ref = await self._send_text_auto(ref_phone, ref_msg)
                        ob_status = "sent" if sent_ref else "approved"
                    else:
                        ob_status = "pending_review"

                    await self.state.add_outbox_item(
                        prospect_id=ref_pid,
                        campaign_id="",
                        step=1,
                        kind="sequence",
                        to_email=ref_phone,
                        subject=f"Referral PIC {ref_dept or 'Lab'} - {ref_comp}",
                        body=ref_msg,
                        send_at=now_iso,
                        status=ob_status,
                        provider="whatsapp",
                        approved_by="autopilot" if ob_status != "pending_review" else "",
                    )
                    await self.send_system_alert(
                        f"🔁 *[AUTO-REFERRAL PIC TERDETEKSI]*\n"
                        f"🏢 Instansi: {ref_comp}\n"
                        f"👤 PIC Baru: {ref_salut} (`{ref_phone}`)\n"
                        f"📌 Status Pesan: {'Terkirim Otomatis' if ob_status == 'sent' else 'Menunggu Review di Outbox → WhatsApp'}"
                    )
            except Exception as ref_err:
                logger.warning(f"WhatsApp: Auto-referral handoff warning: {ref_err}")

        # ── Zero-Token Courtesy Check: Closing Acknowledgment ──
        if intent == "CLOSING_ACK":
            logger.info(f"WhatsApp: Closing courtesy recognized from {norm_phone} ({comp or fn}). No reply needed (0 tokens).")
            async with self.state._connect() as db:
                await db.execute(
                    "UPDATE conversations SET status = 'open', intent = 'CLOSING_ACK', updated_at = ? WHERE id = ?",
                    (now_iso, convo_id)
                )
                await db.commit()
            if getattr(self, "obsidian", None):
                try:
                    self.obsidian.log_chat_event(norm_phone, fn, comp, "inbound", message, intent)
                except Exception:
                    pass
            return {
                "status": "closing_ack_acknowledged",
                "prospect_id": pid,
                "conversation_id": convo_id,
                "intent": intent,
                "reply": "",
                "auto_replied": False
            }

        # ── Specialized Intent Actions: Auto-Cleansing & Referral Handling ──
        if intent == "SALAH SAMBUNG":
            logger.info(f"WhatsApp: Auto-cleansing triggered untuk {norm_phone} ({comp or fn}) - Salah Sambung.")
            today_iso = date.today().isoformat()
            await self._tag_prospect(pid, notes, f"CLEANSING:SALAH_SAMBUNG:{today_iso}")
            try:
                await self.state.update_prospect_status(pid, "opted_out")
            except Exception:
                pass

            async with self.state._connect() as db:
                await db.execute(
                    "UPDATE conversations SET status = 'closed', intent = 'SALAH SAMBUNG', updated_at = ? WHERE id = ?",
                    (now_iso, convo_id)
                )
                await db.execute(
                    "UPDATE outbox SET status = 'cancelled', approved_by = 'auto_cleansing' WHERE prospect_id = ? AND status = 'pending_review'",
                    (pid,)
                )
                try:
                    await db.execute("UPDATE quotes SET status = 'cancelled' WHERE phone = ? OR phone LIKE ?",
                                     (norm_phone, f"%{norm_phone[-9:]}"))
                except Exception:
                    pass
                await db.commit()

            # Auto-send polite apology
            await self._send_text_auto(norm_phone, draft)

            # Alert operator
            await self.send_system_alert(
                f"🧹 *Auto-Cleansing WhatsApp*\n"
                f"Kontak: *{fn or norm_phone}* ({comp or 'Tanpa Instansi'})\n"
                f"Status: *Salah Sambung / Hapus Kontak*\n"
                f"Tindakan: Prospek ditandai *opted_out*, dihapus dari antrean follow-up, dan pesan permohonan maaf telah dikirim."
            )

            # Sync to Obsidian Second Brain
            if getattr(self, "obsidian", None):
                try:
                    self.obsidian.log_chat_event(norm_phone, fn, comp, "inbound", message, intent)
                    self.obsidian.log_chat_event(norm_phone, fn, comp, "outbound", draft, "APOLOGY")
                    self.obsidian.update_customer_profile(
                        norm_phone, fn, comp, status="opted_out",
                        notes="Auto-cleansed: Salah sambung / bukan kontak lab", last_message=message
                    )
                except Exception:
                    pass

            return {"status": "cleansed", "prospect_id": pid, "intent": intent}

        if intent == "REFERRAL BARU":
            logger.info(f"WhatsApp: Referral baru terdeteksi dari {norm_phone} ({comp or fn}).")
            today_iso = date.today().isoformat()
            phone_match = re.search(r"(?:(?:\+?62|0)8[1-9][0-9]{7,10})", message)
            raw_ref_phone = phone_match.group(0) if phone_match else ""
            norm_ref = normalize_id_phone(raw_ref_phone) or raw_ref_phone

            name_match = re.search(r"(?:pak|bu|bapak|ibu)\s+([a-zA-Z]+(?:\s+[a-zA-Z]+)?)", message, re.IGNORECASE)
            ref_name = name_match.group(1).title() if name_match else ""
            new_fn = ref_name or f"PIC Pengadaan {comp}"

            # Mark old prospect as resigned
            await self._tag_prospect(pid, notes, f"RESIGNED_REFERRAL:{today_iso}")
            try:
                await self.state.update_prospect_status(pid, "opted_out")
            except Exception:
                pass

            # Create new referral prospect in pipeline
            new_pid = uuid.uuid4().hex[:12]
            async with self.state._connect() as db:
                await db.execute(
                    """INSERT OR IGNORE INTO prospects
                       (id, first_name, last_name, company, phone, status, source, personalization_notes, created_at, updated_at)
                       VALUES (?, ?, '', ?, ?, 'identified', 'referral_inbound', ?, ?, ?)""",
                    (new_pid, new_fn, comp, norm_ref, f"[REFERRAL_FROM:{norm_phone}:{fn}]", now_iso, now_iso)
                )
                await db.execute(
                    "UPDATE conversations SET status = 'closed', intent = 'REFERRAL BARU', updated_at = ? WHERE id = ?",
                    (now_iso, convo_id)
                )
                await db.commit()

            # Auto-send thank you to old PIC
            await self._send_text_auto(norm_phone, draft)

            # Alert operator
            await self.send_system_alert(
                f"🎯 *Referral Baru Ditemukan!*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"Kontak lama *{fn}* ({comp}) mengarahkan ke rekan baru:\n"
                f"• Nama: *{new_fn}*\n"
                f"• No. WA: *{norm_ref}*\n"
                f"Prospek baru telah otomatis ditambahkan ke pipeline Harvey."
            )

            # Sync to Obsidian
            if getattr(self, "obsidian", None):
                try:
                    self.obsidian.log_chat_event(norm_phone, fn, comp, "inbound", message, intent)
                    self.obsidian.log_chat_event(norm_phone, fn, comp, "outbound", draft)
                    self.obsidian.update_customer_profile(
                        norm_phone, fn, comp, status="opted_out",
                        notes=f"Resigned. Referral given: {new_fn} ({norm_ref})", last_message=message
                    )
                    self.obsidian.update_customer_profile(
                        norm_ref, new_fn, comp, status="identified",
                        notes=f"Referral dari kontak lama {fn} ({norm_phone})", last_message="Referral inbound"
                    )
                except Exception:
                    pass

            return {"status": "referral_created", "new_prospect_id": new_pid, "phone": norm_ref, "intent": intent}

        if intent == "SUDAH RESIGN (TANYA PENGGANTI)":
            logger.info(f"WhatsApp: PIC resign tanpa referral dari {norm_phone} ({comp or fn}).")
            today_iso = date.today().isoformat()
            await self._tag_prospect(pid, notes, f"RESIGNED_NO_REFERRAL:{today_iso}")
            try:
                await self.state.update_prospect_status(pid, "opted_out")
            except Exception:
                pass

            async with self.state._connect() as db:
                await db.execute(
                    "UPDATE conversations SET status = 'closed', intent = 'SUDAH RESIGN', updated_at = ? WHERE id = ?",
                    (now_iso, convo_id)
                )
                await db.commit()

            # Auto-send polite ask for replacement
            await self._send_text_auto(norm_phone, draft)

            await self.send_system_alert(
                f"ℹ️ *Kontak Resign*\n"
                f"Kontak *{fn}* ({comp}) mengonfirmasi sudah resign/pindah.\n"
                f"Harvey telah membalas secara santun menanyakan kontak PIC pengganti."
            )

            if getattr(self, "obsidian", None):
                try:
                    self.obsidian.log_chat_event(norm_phone, fn, comp, "inbound", message, intent)
                    self.obsidian.log_chat_event(norm_phone, fn, comp, "outbound", draft)
                    self.obsidian.update_customer_profile(
                        norm_phone, fn, comp, status="opted_out",
                        notes="Resigned (asked for replacement PIC)", last_message=message
                    )
                except Exception:
                    pass

            return {"status": "resigned_asked_referral", "prospect_id": pid, "intent": intent}

        if intent == "MINTA VIA EMAIL (EMAIL DIBERIKAN)":
            logger.info(f"WhatsApp: Minta via Email (email diberikan) dari {norm_phone} ({comp or fn}).")
            email_match = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", message)
            found_email = email_match.group(0).lower() if email_match else ""

            today_iso = date.today().isoformat()
            if found_email:
                await self._tag_prospect(pid, notes, f"EMAIL_CAPTURED_VIA_WA:{found_email}:{today_iso}")
                async with self.state._connect() as db:
                    await db.execute(
                        "UPDATE prospects SET email = ?, email_status = 'verified', updated_at = ? WHERE id = ?",
                        (found_email, now_iso, pid)
                    )
                    await db.commit()

            # Compose Indonesian B2B Executive Email
            who_salutation = fn or "Bapak/Ibu"
            comp_salutation = f"Tim Pengadaan & Laboratorium {comp}" if comp else "Tim Pengadaan & Laboratorium"
            email_subject = "Katalog & Referensi Spesifikasi Alat Laboratorium — PT Andis Sentral Laboratorium"
            email_body = (
                f"Yth. Bapak/Ibu {who_salutation},\n"
                f"{comp_salutation}\n\n"
                f"Terima kasih banyak atas komunikasi melalui WhatsApp sebelumnya. Menindaklanjuti permintaan Bapak/Ibu, "
                f"berikut kami sampaikan referensi resmi katalog pengadaan alat dan instrumen laboratorium dari PT Andis Sentral Laboratorium (AndisLab).\n\n"
                f"Sebagai distributor resmi instrumen laboratorium, kami menyediakan 1.140+ item alat laboratorium ready stock di gudang Jabodetabek (Bogor), mencakup:\n"
                f"• Analytical Balance & Top Loading (Radwag / Precisa)\n"
                f"• Autoclave & Sterilizer (Hirayama / Geomyung)\n"
                f"• Laboratory Oven & Incubator (Memmert / Faithful)\n"
                f"• Centrifuge Benchtop & High Speed\n"
                f"• Spektrofotometer UV-Vis, pH Meter, Glassware, dan Mikroskop\n\n"
                f"Bapak/Ibu dapat meninjau daftar spesifikasi dan promo ketersediaan unit melalui tautan resmi kami:\n"
                f"https://www.andislab.com/promo-akhir-tahun\n\n"
                f"Seluruh unit kami bergaransi resmi 12 bulan, disertai sertifikat kalibrasi, serta didukung teknisi in-house untuk instalasi dan uji fungsi langsung di laboratorium Bapak/Ibu. "
                f"Kami juga melayani skema administrasi lengkap (e-Faktur Pajak, BAST, dan Tempo Instansi/LPPM).\n\n"
                f"Apabila terdapat spesifikasi alat tertentu yang sedang dibutuhkan draf Surat Penawaran Harga (SPH) resminya, silakan balas email ini atau hubungi kami melalui WhatsApp.\n\n"
                f"Hormat kami,\n\n"
                f"Ahnaf Ibrahim\n"
                f"Divisi Pengadaan & Kemitraan Laboratorium\n"
                f"PT Andis Sentral Laboratorium\n"
                f"Website: https://www.andislab.com\n"
                f"WhatsApp / Telp: 0819-9157-5096\n"
            )

            # Stage email in outbox (status='approved' so next email engine run picks it up)
            staged_outbox_id = None
            if found_email:
                provider_name = "smtp"
                if hasattr(self.config.channels, "email") and getattr(self.config.channels.email, "provider", None):
                    provider_name = self.config.channels.email.provider
                staged_outbox_id = await self.state.add_outbox_item(
                    prospect_id=pid,
                    conversation_id=convo_id,
                    kind="reply",
                    to_email=found_email,
                    subject=email_subject,
                    body=email_body,
                    send_at=now_iso,
                    status="approved",
                    provider=provider_name,
                    approved_by="omnichannel_auto_wa",
                )

            # Tag prospect as replied
            await self._tag_prospect(pid, notes, "WA_REPLIED")
            try:
                await self.state.update_prospect_status(pid, "replied")
            except Exception:
                pass

            async with self.state._connect() as db:
                await db.execute("UPDATE conversations SET status = 'open', intent = ?, updated_at = ? WHERE id = ?",
                                 (intent, now_iso, convo_id))
                await db.commit()

            # Send WhatsApp confirmation to client
            await self._send_text_auto(norm_phone, draft)

            # Alert operator
            await self.send_system_alert(
                f"📬 *Omnichannel: Email Ditangkap via WhatsApp!*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 *Klien:* {fn or norm_phone}\n"
                f"🏢 *Instansi:* {comp or '-'}\n"
                f"📱 *WhatsApp:* {norm_phone}\n"
                f"✉️ *Email:* {found_email}\n"
                f"📋 *Tindakan:* Email penawaran katalog 1.140+ item telah disiapkan di antrean outbox, dan konfirmasi WhatsApp telah dikirimkan ke klien."
            )

            # Sync to Obsidian Second Brain
            if getattr(self, "obsidian", None):
                try:
                    self.obsidian.log_chat_event(norm_phone, fn, comp, "inbound", message, intent)
                    self.obsidian.log_chat_event(norm_phone, fn, comp, "outbound", draft, "EMAIL_CONFIRMATION")
                    self.obsidian.update_customer_profile(
                        norm_phone, fn, comp, status="replied",
                        notes=f"Email captured: {found_email}. Ready-stock 1.140+ catalog email staged.",
                        last_message=message
                    )
                except Exception:
                    pass

            return {
                "status": "email_captured_and_staged",
                "prospect_id": pid,
                "email": found_email,
                "outbox_id": staged_outbox_id,
                "intent": intent,
            }

        # Sync standard inbound interaction to Obsidian
        if getattr(self, "obsidian", None):
            try:
                self.obsidian.log_chat_event(norm_phone, fn, comp, "inbound", message, intent)
                self.obsidian.update_customer_profile(
                    norm_phone, fn, comp, status="replied", notes=message[:200], last_message=message
                )
            except Exception:
                pass

        await self._tag_prospect(pid, notes, "WA_REPLIED")
        try:
            await self.state.update_prospect_status(pid, "replied")
        except Exception:
            pass

        async with self.state._connect() as db:
            await db.execute("UPDATE conversations SET intent = ? WHERE id = ?", (intent, convo_id))
            await db.commit()

        await self.state.log_action(
            action_type="whatsapp_reply",
            agent="handler",
            details={"prospect_id": pid, "company": comp, "intent": intent, "phone": norm_phone},
        )
        logger.info(f"WhatsApp: Inbound diproses dari {comp or fn} ({norm_phone}) - Intent: {intent}")

        # ── SOURCING BY REQUEST EXECUTION (NON-INVENTORY ITEMS) ──
        if "SOURCING" in intent:
            logger.info(f"WhatsApp: Sourcing By Request dipicu untuk {norm_phone} ({comp or fn}): {message}")
            try:
                from harvey.integrations.sourcing import SourcingEngine
                from harvey.brain import Brain
                brain = Brain(self.config, self.env)
                sourcing_engine = SourcingEngine(brain=brain, env=self.env, config=self.config, state=self.state)
                sourcing_res = await sourcing_engine.source_item(
                    raw_query=message,
                    prospect_id=pid or "",
                    client_name=fn,
                    institution=comp,
                )
                if sourcing_res.get("consultative_client_reply"):
                    draft = sourcing_res["consultative_client_reply"]

                # Send Operator Alert with full 3-vendor matrix
                if sourcing_res.get("whatsapp_operator_summary"):
                    await self.send_system_alert(sourcing_res["whatsapp_operator_summary"])

                # Auto-generate Official SPH PDF from Sourcing result if price available
                sell_p = float((sourcing_res.get("pricing") or {}).get("sell_price") or 0)
                if sell_p > 0:
                    from harvey.integrations.sph_generator import SPHGenerator
                    gen = SPHGenerator()
                    best_eco = sourcing_res.get("best_economic") or {}
                    sph_meta = await gen.generate_and_register_sph(
                        company_name=comp or fn or "Instansi Klien",
                        contact_name=f"{fn} {ln}".strip() or "Tim Pengadaan / Lab",
                        contact_title=title or "Laboratory / Procurement",
                        phone=norm_phone,
                        prospect_id=pid or "",
                        stock_note=f"Special Order / Indent ({best_eco.get('lead_time_days', '7-14 Hari Kerja')})",
                        items=[{
                            "name": sourcing_res.get("canonical_name") or "Laboratory Equipment",
                            "brand": best_eco.get("brand") or "Official Principal",
                            "model": best_eco.get("model") or "Standard",
                            "qty": int(sourcing_res.get("qty") or 1),
                            "unit_price": sell_p,
                            "hpp": float(sourcing_res.get("selected_hpp") or 0),
                            "specifications": sourcing_res.get("key_specs") or "Sesuai standar spesifikasi laboratorium",
                        }],
                    )
                    op_phone = normalize_id_phone(getattr(self.wa_cfg, "report_phone", None) or "087764021131")
                    if getattr(self, "openwa_client", None) and op_phone:
                        await self.openwa_client.send_document(
                            phone=op_phone,
                            file_path=sph_meta["pdf_path"],
                            caption=(
                                f"📄 *[AUTO-SPH SOURCING SIAP: {sph_meta['quotation_no']}]*\n"
                                f"🏢 Instansi: *{comp or fn}*\n"
                                f"💰 Grand Total (+PPN 11%): *{sph_meta['formatted_grand_total']}*\n\n"
                                f"👉 Balas *OK SPH* untuk langsung kirim file PDF ini ke WA & Email Klien!"
                            ),
                        )
            except Exception as e:
                logger.warning(f"WhatsApp: Sourcing execution error: {e}")

        elif "PENAWARAN" in intent or "TANYA PRODUK" in intent:
            try:
                from harvey.integrations.product_matcher import match_product
                from harvey.integrations.sph_generator import SPHGenerator
                m_res = match_product(message)
                matched = m_res.get("item") if m_res.get("status") in ("EXACT", "CLARIFICATION") else None
                if matched and float(matched.get("price") or 0) > 0:
                    gen = SPHGenerator()
                    sph_meta = await gen.generate_and_register_sph(
                        company_name=comp or fn or "Instansi Klien",
                        contact_name=f"{fn} {ln}".strip() or "Tim Pengadaan / Lab",
                        contact_title=title or "Laboratory / Procurement",
                        phone=norm_phone,
                        prospect_id=pid or "",
                        stock_note="Ready Stock Tidak Terikat",
                        items=[{
                            "name": matched.get("name"),
                            "brand": matched.get("brand", "AndisLab"),
                            "model": matched.get("model") or matched.get("catalog_no", ""),
                            "qty": 1,
                            "unit_price": float(matched.get("price") or 0),
                            "hpp": float(matched.get("harga_beli") or 0),
                            "specifications": matched.get("specifications", ""),
                        }],
                    )
                    op_phone = normalize_id_phone(getattr(self.wa_cfg, "report_phone", None) or "087764021131")
                    if getattr(self, "openwa_client", None) and op_phone:
                        await self.openwa_client.send_document(
                            phone=op_phone,
                            file_path=sph_meta["pdf_path"],
                            caption=(
                                f"📄 *[AUTO-SPH READY STOCK SIAP: {sph_meta['quotation_no']}]*\n"
                                f"🏢 Instansi: *{comp or fn}*\n"
                                f"📦 Alat: *{matched.get('name')}*\n"
                                f"💰 Grand Total (+PPN 11%): *{sph_meta['formatted_grand_total']}*\n\n"
                                f"👉 Balas *OK SPH* untuk langsung kirim file PDF ini ke WA & Email Klien!"
                            ),
                        )
            except Exception as e:
                logger.debug(f"WhatsApp: Auto-SPH ready-stock generation warning: {e}")

        # Step 6: Store Draft in outbox
        outbox_id = None
        async with self.state._connect() as db:
            cur = await db.execute(
                "SELECT id FROM outbox WHERE conversation_id = ? AND status = 'pending_review' AND kind = 'reply'",
                (convo_id,)
            )
            existing_outbox = await cur.fetchone()
            if existing_outbox:
                outbox_id = existing_outbox[0]
                await db.execute(
                    "UPDATE outbox SET body = ?, subject = ?, updated_at = ? WHERE id = ?",
                    (draft, f"Balasan WhatsApp - {intent}"[:80], now_iso, outbox_id)
                )
                await db.commit()
            else:
                outbox_id = await self.state.add_outbox_item(
                    prospect_id=pid,
                    conversation_id=convo_id,
                    kind="reply",
                    to_email=norm_phone,
                    subject=f"Balasan WhatsApp - {intent}"[:80],
                    body=draft,
                    send_at=now_iso,
                    status="pending_review",
                    provider="whatsapp",
                    approved_by="",
                )

        # Step 7: Auto-Reply Evaluation
        auto_replied = False
        auto_mode = str(getattr(self.wa_cfg, "auto_reply_mode", "draft") or "draft").lower().strip()
        is_safe_intent = any(k in intent for k in ["KATALOG", "BUTUH", "PROFIL", "RESPON UMUM", "HARGA", "PENAWARAN", "KONSULTASI"])
        is_optout = any(k in intent for k in ["OPT_OUT", "TIDAK_TERTARIK", "UNSUBSCRIBE"])

        # Check cooldown: Jangan kirim auto-reply jika dalam 120 detik terakhir sudah pernah kirim ke kontak ini
        is_in_cooldown = False
        if thread:
            recent_out = [m for m in thread if m.get("direction") == "outbound"]
            if recent_out:
                last_ts = recent_out[-1].get("timestamp")
                if last_ts:
                    try:
                        last_dt = datetime.fromisoformat(last_ts)
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                        if (now_utc - last_dt).total_seconds() < 120:
                            is_in_cooldown = True
                            logger.info(f"WhatsApp: Cooldown aktif untuk {norm_phone} (baru dikirim {((now_utc - last_dt).total_seconds()):.0f} dtk lalu). Skip auto-reply.")
                    except Exception:
                        pass

        # Check daily quota: batasi agar tidak melebihi kuota harian
        act = self._load_activity()
        is_quota_exceeded = act.get("sent", 0) >= self.wa_cfg.max_daily_messages

        can_auto_reply = False
        if not human_takeover and not is_optout and intent != "CRITIC_HOLD" and not is_in_cooldown and not is_quota_exceeded and self.in_reply_hours():
            if auto_mode == "full_auto":
                can_auto_reply = True
            elif auto_mode in ("safe", "auto") and is_safe_intent:
                can_auto_reply = True

        if human_takeover:
            logger.info(f"WhatsApp: Skip auto-reply untuk {norm_phone} karena Human Takeover aktif.")
        elif is_in_cooldown:
            logger.info(f"WhatsApp: Skip auto-reply untuk {norm_phone} karena masih cooldown < 120s.")
        elif is_quota_exceeded:
            logger.info(f"WhatsApp: Skip auto-reply untuk {norm_phone} karena kuota harian tercapai.")
        elif can_auto_reply and draft and draft.strip():
            logger.info(f"WhatsApp: Auto-reply mode '{auto_mode}' aktif untuk intent {intent}. Mengirimkan balasan ke {effective_target}...")
            sent_ok = await self._send_text_auto(effective_target, draft, is_reply=True)
            if sent_ok:
                auto_replied = True

                # If requested to send company profile & catalog PDFs, attach them right after text
                if send_compro_pdf:
                    assets_dir = PROJECT_ROOT / "data" / "assets" / "give_first"
                    for fname, dl_name, cap in (
                        ("Company_Profile_AndisLab.pdf", "Company_Profile_PT_Andis_Sentral_Laboratorium_2026.pdf", "📄 Company Profile Resmi PT Andis Sentral Laboratorium"),
                        ("Katalog_Lengkap_AndisLab_2026.pdf", "Katalog_Lengkap_AndisLab_2026.pdf", "📘 Katalog Produk Lengkap AndisLab 2026"),
                    ):
                        fpath = assets_dir / fname
                        if fpath.exists():
                            try:
                                await asyncio.sleep(1.8)
                                if getattr(self, "openwa", None) and getattr(self.openwa, "is_configured", None) and self.openwa.is_configured():
                                    await self.openwa.send_document(
                                        phone=norm_phone,
                                        file_path=str(fpath),
                                        filename=dl_name,
                                        caption=cap,
                                    )
                                    logger.info(f"WhatsApp: Berhasil mengirimkan {fname} ke {norm_phone}")
                            except Exception as pdf_err:
                                logger.warning(f"WhatsApp: Gagal kirim {fname}: {pdf_err}")

                async with self.state._connect() as db:
                    if outbox_id:
                        await db.execute(
                            "UPDATE outbox SET status = 'sent', sent_at = ?, approved_by = 'autopilot' WHERE id = ?",
                            (now_iso, outbox_id)
                        )
                    thread.append({
                        "direction": "outbound",
                        "channel": "whatsapp",
                        "content": draft,
                        "text": draft,
                        "sender": "harvey",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    if send_compro_pdf:
                        thread.append({
                            "direction": "outbound",
                            "channel": "whatsapp",
                            "content": "[Dokumen Terkirim: Company_Profile_AndisLab.pdf]",
                            "text": "[Dokumen Terkirim: Company_Profile_AndisLab.pdf]",
                            "sender": "harvey",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                    await db.execute("UPDATE conversations SET thread_json = ?, updated_at = ? WHERE id = ?",
                                     (json.dumps(thread), now_iso, convo_id))
                    await db.commit()

                await self.state.log_action(
                    action_type="whatsapp_reply_sent",
                    agent="handler",
                    details={"prospect_id": pid, "company": comp, "phone": norm_phone, "intent": intent},
                )

        # Step 8: Notify Operator
        if auto_replied:
            await self.send_system_alert(
                f"⚡ *[AUTO-REPLY TERKIRIM - WHATSAPP]*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 *Nama:* {f'{fn} {ln}'.strip() or title or norm_phone}\n"
                f"🏢 *Perusahaan:* {comp or '-'}\n"
                f"📱 *Nomor:* {norm_phone}\n"
                f"🎯 *Intent:* {intent}\n\n"
                f"💬 *Pesan Masuk:*\n"
                f"\"{message.strip()[:200]}\"\n\n"
                f"🤖 *Balasan Otomatis yang Terkirim:*\n"
                f"\"{draft.strip()[:300]}\"\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"_Harvey AI Sales Agent (Mode Full-Auto Aktif)_"
            )
        else:
            await self.notify_inbound_review(
                name=f"{fn} {ln}".strip() or title or norm_phone,
                company=comp,
                phone=norm_phone,
                intent=intent,
                incoming_message=message,
                draft_reply=draft,
            )

        return {
            "status": "ok",
            "prospect_id": pid,
            "conversation_id": convo_id,
            "outbox_id": outbox_id,
            "intent": intent,
            "draft": draft,
            "auto_replied": auto_replied,
            "human_takeover": human_takeover,
        }

    async def sync_live_chats_from_openwa(self, limit: int = 50) -> int:
        """Fetch active chats from OpenWA and synchronize into SQLite prospects and conversations tables."""
        if not getattr(self, "openwa", None) or not getattr(self.openwa, "is_configured", None) or not self.openwa.is_configured():
            return 0

        import time
        now_mono = time.monotonic()
        next_allowed = getattr(WhatsAppEngine, "_openwa_sync_next_at", 0.0)
        if now_mono < next_allowed:
            return 0
        # Throttle across all callers (heartbeat + dashboard auto-refresh) to max 1 call / 25s
        WhatsAppEngine._openwa_sync_next_at = now_mono + 25.0

        try:
            sid = await self.openwa._resolve_session_id()
            import httpx
            async with httpx.AsyncClient(timeout=25.0) as client:
                res = await client.get(
                    f"{self.openwa.base_url}/api/sessions/{sid}/chats?limit={limit}",
                    headers=self.openwa._headers(),
                )
                if res.status_code == 429:
                    retry_after = 120.0
                    try:
                        retry_after = max(60.0, float(res.headers.get("Retry-After", 120)))
                    except Exception:
                        pass
                    WhatsAppEngine._openwa_sync_next_at = time.monotonic() + retry_after
                    logger.warning(f"OpenWA rate-limit (429): menunda sync chat selama {int(retry_after)} detik.")
                    return 0
                if res.status_code != 200:
                    logger.debug(f"OpenWA sync_live_chats_from_openwa returned HTTP {res.status_code}")
                    return 0
                chats = res.json()
        except Exception as e:
            logger.debug(f"OpenWA sync_live_chats_from_openwa error: {e}")
            return 0

        synced = 0
        inbound_to_process = []
        now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        async with self.state._connect() as db:
            for c in chats:
                if c.get("isGroup"):
                    continue
                name = (c.get("name") or c.get("formattedTitle") or "").strip()
                last_msg = str(c.get("lastMessage") or "").strip()
                if not last_msg:
                    continue

                cid = c.get("id", "")
                raw_phone = cid.split("@")[0] if "@" in cid else cid
                norm_phone = normalize_id_phone(raw_phone)
                if not norm_phone:
                    norm_phone = extract_phone_from_str(name) or extract_phone_from_str(cid)
                if not norm_phone:
                    norm_phone = raw_phone

                # Never sync internal AndisLab team / operator numbers into customer prospects
                op_phone = normalize_id_phone(getattr(self.wa_cfg, "operator_phone", "") or "087764021131")
                name_low = name.lower()
                if (
                    (op_phone and norm_phone and norm_phone.endswith(op_phone[-9:]))
                    or any(internal in name_low for internal in ("ahnaf", "andis kukuh", "andislab", "andisbot"))
                    or name_low.strip() == "kukuh"
                ):
                    continue

                ts_int = c.get("timestamp") or 0
                if ts_int:
                    dt = datetime.fromtimestamp(ts_int, tz=timezone.utc)
                    updated_iso = dt.replace(tzinfo=None).isoformat()
                else:
                    updated_iso = now_iso

                parsed_contact, parsed_comp, parsed_ind = _parse_wa_contact(name or raw_phone)
                # Normalize messy WhatsApp contact names (strip descriptive sentences)
                if parsed_comp:
                    parsed_comp = re.sub(r"\bbergerak bidang\b.*$", "", parsed_comp, flags=re.I).strip(" .,-")
                is_valid_id_phone = norm_phone.startswith(("628", "08", "+628")) and 10 <= len(norm_phone) <= 15
                db_phone = norm_phone if is_valid_id_phone else ""

                # Upsert into companies table if company is recognized
                comp_id = None
                if parsed_comp and len(parsed_comp) >= 3 and parsed_comp not in ("Instansi Klien", "Inbound WhatsApp"):
                    comp_clean_id = f"comp_{re.sub(r'[^a-zA-Z0-9]', '', parsed_comp.lower())[:16]}"
                    await db.execute(
                        """INSERT OR IGNORE INTO companies (id, name, industry, phone, source, created_at, updated_at)
                           VALUES (?, ?, ?, ?, 'whatsapp_sync', ?, ?)""",
                        (comp_clean_id, parsed_comp, parsed_ind, db_phone, updated_iso, updated_iso)
                    )
                    comp_id = comp_clean_id

                # Check or create prospect
                cur = await db.execute(
                    "SELECT id, first_name, company, company_id FROM prospects WHERE (phone != '' AND (phone = ? OR phone LIKE ?)) OR id = ? LIMIT 1",
                    (db_phone, f"%{db_phone[-9:]}" if db_phone else "__none__", f"wa_{raw_phone[-8:]}")
                )
                prow = await cur.fetchone()
                if prow:
                    pid = prow[0]
                    up_name = parsed_contact if (not prow[1] or "Lead" in str(prow[1])) else prow[1]
                    up_comp = parsed_comp if (not prow[2] or "Inbound" in str(prow[2])) else prow[2]
                    await db.execute(
                        "UPDATE prospects SET first_name = ?, company = ?, company_id = coalesce(?, company_id), updated_at = ? WHERE id = ?",
                        (up_name, up_comp, comp_id, updated_iso, pid)
                    )
                else:
                    pid = f"wa_{norm_phone[-8:]}" if len(norm_phone) >= 8 else f"wa_{cid[:8]}"
                    await db.execute(
                        """INSERT OR IGNORE INTO prospects (id, first_name, company, company_id, phone, status, source, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, 'replied', 'whatsapp_sync', ?, ?)""",
                        (pid, parsed_contact, parsed_comp, comp_id, db_phone, updated_iso, updated_iso)
                    )

                # Load existing thread first so we can accurately detect our own outbound bubbles
                cur = await db.execute(
                    "SELECT id, thread_json FROM conversations WHERE prospect_id = ? AND channel = 'whatsapp' LIMIT 1",
                    (pid,)
                )
                crow = await cur.fetchone()
                th = []
                if crow:
                    try:
                        th = json.loads(crow[1] or "[]")
                    except Exception:
                        th = []

                low_msg = last_msg.lower()
                outbound_markers = (
                    "pt andis sentral laboratorium",
                    "perkenalkan saya ahnaf",
                    "kami diarahkan oleh rekan",
                    "barangkali saat ini tim",
                    "kira-kira di hari kamis",
                    "boleh kami kirimkan katalog",
                    "terima kasih banyak atas arahannya",
                    "berikut kami lampirkan katalog",
                    "tidak harus sekarang",
                    "jika sewaktu-waktu",
                    "nomor ini selalu siap membantu",
                    "semoga sukses selalu",
                    "silakan simpan nomor ahnaf",
                    "izin simpan nomor saya",
                    "terima kasih banyak atas informasinya",
                    "kami siap mendukung kebutuhan",
                    "agenda riset dan operasional",
                )
                # Also check against recent outbound messages in thread OR outbox (handles OpenWA multi-bubble concatenation)
                prev_outs = [
                    str(t.get("content") or t.get("text") or "").strip().lower()
                    for t in th[-10:]
                    if t.get("direction") == "outbound"
                ]
                cur_ob_sent = await db.execute(
                    "SELECT body FROM outbox WHERE prospect_id = ? AND provider = 'whatsapp' AND status IN ('sent', 'approved') ORDER BY created_at DESC LIMIT 5",
                    (pid,),
                )
                for ob_row in await cur_ob_sent.fetchall():
                    if ob_row and ob_row[0]:
                        prev_outs.append(str(ob_row[0]).strip().lower())

                matched_prev_outbound = any(
                    po and (po == low_msg or po[:45] in low_msg or low_msg[:45] in po)
                    for po in prev_outs
                )
                is_from_us = bool(
                    c.get("fromMe")
                    or c.get("lastMessageFromMe")
                    or matched_prev_outbound
                    or any(m in low_msg for m in outbound_markers)
                )
                msg_dir = "outbound" if is_from_us else "inbound"

                new_item = {
                    "direction": msg_dir,
                    "channel": "whatsapp",
                    "content": last_msg,
                    "text": last_msg,
                    "sender": "AndisLab (Ahnaf)" if is_from_us else (name or "Klien"),
                    "timestamp": updated_iso,
                }

                if crow:
                    convo_id = crow[0]
                    is_new_msg = not any((t.get("content") == last_msg or t.get("text") == last_msg) for t in th[-4:])
                    if is_new_msg:
                        th.append(new_item)
                        th = th[-60:]
                        new_st = "open" if msg_dir == "inbound" else None
                        if new_st:
                            await db.execute(
                                "UPDATE conversations SET thread_json = ?, status = ?, updated_at = ? WHERE id = ?",
                                (json.dumps(th), new_st, updated_iso, convo_id)
                            )
                        else:
                            await db.execute(
                                "UPDATE conversations SET thread_json = ?, updated_at = ? WHERE id = ?",
                                (json.dumps(th), updated_iso, convo_id)
                            )
                else:
                    convo_id = f"wa_{cid[:12]}"
                    th = [new_item]
                    await db.execute(
                        """INSERT OR REPLACE INTO conversations (id, prospect_id, channel, stage, status, thread_json, created_at, updated_at)
                           VALUES (?, ?, 'whatsapp', 'inbound', 'open', ?, ?, ?)""",
                        (convo_id, pid, json.dumps(th), updated_iso, updated_iso)
                    )
                synced += 1
                # Inbound AI drafting is handled exclusively by OpenWA Webhook (/api/whatsapp/webhook)
                # which carries exact `fromMe: false` metadata + 2-layer adaptive debounce.
                # Polling /chats here only syncs metadata cleanly without re-triggering duplicate drafts.
            await db.commit()

        if synced:
            logger.info(f"WhatsApp: Berhasil sinkronisasi {synced} obrolan aktif dari OpenWA ke database.")

        for p_phone, p_msg, p_name, p_cid in inbound_to_process:
            seen = self._load_seen()
            seen_key = f"{p_phone}|{p_msg}"
            if seen_key not in seen:
                seen.add(seen_key)
                self._save_seen(seen)
                try:
                    await self.handle_single_inbound(phone=p_phone, message=p_msg, title=p_name, chat_id=p_cid)
                except Exception as e:
                    logger.warning(f"Error handling inbound from OpenWA sync: {e}")

        return synced

    async def process_inbound(self) -> bool:
        """Scan for unread replies, match them to prospects we messaged, stop
        follow-ups for them and alert the operator with a draft answer.
        Returns True if the scan ran (so follow-ups are safe to send)."""
        rows = await self.scan_inbound_via_web()
        if rows is None:
            return False
        if not rows:
            return True

        async with self.state._connect() as db:
            cur = await db.execute(
                """SELECT id, first_name, last_name, company, phone, personalization_notes
                   FROM prospects
                   WHERE phone IS NOT NULL AND phone != ''"""
            )
            prospects = await cur.fetchall()

        by_phone = {}
        for pid, fn, ln, comp, ph, notes in prospects:
            norm = normalize_id_phone(ph)
            if norm:
                by_phone[norm] = (pid, fn or "", ln or "", comp or "", notes or "")

        seen = self._load_seen()
        for row in rows:
            title = row.get("title", "")
            lines = row.get("lines", [])
            preview = lines[-2] if len(lines) >= 2 else (lines[-1] if lines else "")
            key = f"{title}|{preview}"
            if key in seen:
                continue

            seen.add(key)
            norm_title = normalize_id_phone(re.sub(r"[^\d+]", "", title))
            target_phone = norm_title
            if not target_phone:
                tl = title.lower()
                for pid, fn, ln, comp, ph_notes in by_phone.values():
                    first = (fn or "").split(" ")[0].lower()
                    comp_tok = next((w.lower() for w in (comp or "").split() if len(w) >= 4), "")
                    if len(first) >= 3 and first in tl and comp_tok and comp_tok in tl:
                        for ph_cand, pdata in by_phone.items():
                            if pdata[0] == pid:
                                target_phone = ph_cand
                                break
                        break

            if target_phone:
                await self.handle_single_inbound(phone=target_phone, message=preview, title=title)
            else:
                await self._inbound_for_quote(title, preview, norm_title)

        self._save_seen(seen)
        return True

    async def _inbound_for_quote(self, title: str, preview: str, norm_title: str) -> None:
        """An unread chat that is not a cold prospect may be an SPH contact:
        stop their follow-ups and handle inbound."""
        try:
            async with self.state._connect() as db:
                cur = await db.execute(
                    "SELECT id, company, contact_name, phone FROM quotes WHERE status = 'pending'"
                )
                rows = await cur.fetchall()
        except Exception:
            return
        tl = title.lower()
        hit = []
        for qid, comp, contact, ph in rows:
            if norm_title and normalize_id_phone(ph or "") == norm_title:
                hit.append((qid, comp, contact, ph))
                continue
            first = (contact or "").replace("Ibu ", "").replace("Bapak ", "").split(" ")[0].lower()
            comp_tok = next((w.lower() for w in (comp or "").split() if len(w) >= 4), "")
            if len(first) >= 3 and first in tl and comp_tok and comp_tok in tl:
                hit.append((qid, comp, contact, ph))
        if not hit:
            return

        qid, comp, contact, ph = hit[0]
        target_phone = ph or norm_title
        if target_phone:
            await self.handle_single_inbound(
                phone=target_phone,
                message=preview,
                title=f"{contact} ({comp})",
            )

    async def _send_image_via_web(self, phone: str, caption: str, image_path: Path) -> str:
        """Send an image with caption via WhatsApp Web.
        Returns 'sent', 'no_attach' (nothing was sent) or 'error'."""
        if is_internal_operator_number(phone, getattr(self.wa_cfg, "report_phone", None)):
            return "error"
        if not image_path or not Path(image_path).exists():
            logger.warning(f"WhatsApp: File gambar '{image_path}' tidak ditemukan.")
            return "error"
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            ctx = None
            try:
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    channel="chrome",
                    headless=True,
                    args=CHROME_SAFE_ARGS,
                    viewport={"width": 1280, "height": 900},
                )
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(f"https://web.whatsapp.com/send?phone={phone}", wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_selector("footer div[contenteditable='true']", timeout=40_000)
                body = ((await page.inner_text("body")) or "").lower()
                if "tidak terdaftar di whatsapp" in body or "phone number shared via url is invalid" in body:
                    return "error"
                await asyncio.sleep(random.uniform(2.0, 4.0))
                attach = await page.query_selector(
                    "footer button[aria-label='Attach'], footer button[aria-label='Lampirkan'], "
                    "footer span[data-icon='plus'], footer span[data-icon='clip'], footer span[data-icon='plus-rounded']"
                )
                if not attach:
                    return "no_attach"
                await attach.click()
                await asyncio.sleep(random.uniform(1.0, 2.0))
                file_input = await page.query_selector(
                    "input[type='file'][accept*='image'], input[type='file'][accept*='video']"
                )
                if not file_input:
                    inputs = await page.query_selector_all("input[type='file']")
                    for inp in inputs:
                        acc = (await inp.get_attribute("accept")) or ""
                        if "image" in acc or "*" in acc:
                            file_input = inp
                            break
                if not file_input:
                    return "no_attach"

                await file_input.set_input_files(str(image_path))

                cap = None
                try:
                    cap = await page.wait_for_selector(
                        "div[contenteditable='true'][aria-label*='aption'], "
                        "div[contenteditable='true'][aria-label*='eterangan'], "
                        "div[contenteditable='true'][aria-placeholder*='aption'], "
                        "div[contenteditable='true'][aria-placeholder*='eterangan'], "
                        "div[role='dialog'] div[contenteditable='true'], "
                        "div[data-tab='10'], div[data-tab='6']",
                        timeout=8_000,
                    )
                except Exception:
                    cap = None

                if not cap:
                    all_editables = await page.query_selector_all("div[contenteditable='true']")
                    if len(all_editables) > 1:
                        cap = all_editables[-1]

                caption_typed = False
                if cap and caption:
                    try:
                        await cap.focus()
                        await page.keyboard.insert_text(caption)
                        await asyncio.sleep(random.uniform(1.0, 2.0))
                        await page.keyboard.press("Enter")
                        caption_typed = True
                    except Exception as cap_err:
                        logger.debug(f"WhatsApp: Error typing into caption, will use send button: {cap_err}")

                if not caption_typed:
                    send_btn = await page.query_selector(
                        "span[data-icon='send'], span[data-icon='wds-ic-send-filled'], "
                        "div[role='dialog'] span[data-icon='send'], "
                        "button[aria-label='Send'], button[aria-label='Kirim'], "
                        "div[role='button'][aria-label*='Send'], div[role='button'][aria-label*='Kirim']"
                    )
                    if send_btn:
                        await send_btn.click()
                    else:
                        await page.keyboard.press("Enter")

                    if caption:
                        await asyncio.sleep(3.0)
                        footer_input = await page.query_selector("footer div[contenteditable='true']")
                        if footer_input:
                            await footer_input.focus()
                            await page.keyboard.insert_text(caption)
                            await asyncio.sleep(0.5)
                            await page.keyboard.press("Enter")

                await asyncio.sleep(6.0)
                return "sent"
            except Exception as e:
                logger.warning(f"WhatsApp: Kirim gambar ke {phone} gagal: {e}")
                return "error"
            finally:
                if ctx:
                    try:
                        await ctx.close()
                    except Exception:
                        pass

    async def _send_document_via_web(self, phone: str, caption: str, pdf_path: Path | str) -> str:
        """Send a PDF document with caption via WhatsApp Web.
        Returns 'sent', 'no_attach' (nothing was sent) or 'error'."""
        if is_internal_operator_number(phone, getattr(self.wa_cfg, "report_phone", None)):
            return "error"
        pdf = Path(pdf_path)
        if not pdf.exists():
            logger.warning(f"WhatsApp: File PDF '{pdf_path}' tidak ditemukan.")
            return "error"
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            ctx = None
            try:
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    channel="chrome",
                    headless=True,
                    args=CHROME_SAFE_ARGS,
                    viewport={"width": 1280, "height": 900},
                )
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(
                    f"https://web.whatsapp.com/send?phone={phone}",
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )
                await page.wait_for_selector("footer div[contenteditable='true']", timeout=40_000)
                body = ((await page.inner_text("body")) or "").lower()
                if "tidak terdaftar di whatsapp" in body or "phone number shared via url is invalid" in body:
                    return "error"
                await asyncio.sleep(random.uniform(2.0, 4.0))

                attach = await page.query_selector(
                    "footer button[aria-label='Attach'], footer button[aria-label='Lampirkan'], "
                    "footer span[data-icon='plus'], footer span[data-icon='clip'], footer span[data-icon='plus-rounded']"
                )
                if not attach:
                    return "no_attach"
                await attach.click()
                await asyncio.sleep(random.uniform(1.0, 2.0))

                # Document input in WhatsApp Web (accepts all files or application/pdf)
                file_input = await page.query_selector(
                    "input[type='file'][accept='*'], "
                    "input[type='file'][accept*='pdf'], "
                    "input[type='file'][accept*='application'], "
                    "input[type='file']:not([accept*='image']):not([accept*='video'])"
                )
                if not file_input:
                    inputs = await page.query_selector_all("input[type='file']")
                    for inp in inputs:
                        acc = (await inp.get_attribute("accept")) or ""
                        if "image" not in acc and "video" not in acc:
                            file_input = inp
                            break
                if not file_input:
                    return "no_attach"

                await file_input.set_input_files(str(pdf))

                cap = None
                try:
                    cap = await page.wait_for_selector(
                        "div[contenteditable='true'][aria-placeholder*='aption'], "
                        "div[contenteditable='true'][aria-placeholder*='eterangan'], "
                        "div[role='dialog'] div[contenteditable='true'], "
                        "div[aria-label*='Keterangan'], div[aria-label*='Caption']",
                        timeout=15_000,
                    )
                except Exception:
                    cap = None

                if cap and caption:
                    await cap.focus()
                    await page.keyboard.insert_text(caption)
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    await page.keyboard.press("Enter")
                else:
                    send_btn = await page.query_selector(
                        "div[role='dialog'] span[data-icon='send'], "
                        "div[role='dialog'] button[aria-label='Send'], "
                        "div[role='dialog'] button[aria-label='Kirim'], "
                        "span[data-icon='send'], button[aria-label='Send'], button[aria-label='Kirim']"
                    )
                    if send_btn:
                        await send_btn.click()
                    else:
                        await page.keyboard.press("Enter")

                    if caption:
                        await asyncio.sleep(3.0)
                        footer_input = await page.query_selector("footer div[contenteditable='true']")
                        if footer_input:
                            await footer_input.focus()
                            await page.keyboard.insert_text(caption)
                            await asyncio.sleep(0.5)
                            await page.keyboard.press("Enter")

                await asyncio.sleep(6.0)
                return "sent"
            except Exception as e:
                logger.warning(f"WhatsApp: Kirim dokumen ke {phone} gagal: {e}")
                return "error"
            finally:
                if ctx:
                    try:
                        await ctx.close()
                    except Exception:
                        pass

    async def _followup_due(self) -> dict | None:
        """Oldest prospect due for follow-up 1 (day 3) or 2 (day 7), or None."""
        cfg = self.wa_cfg
        since = cfg.followup_since or "1970-01-01"
        today = date.today()
        async with self.state._connect() as db:
            try:
                cur = await db.execute(
                    """SELECT id, first_name, last_name, company, phone, personalization_notes, industry
                       FROM prospects
                       WHERE phone IS NOT NULL AND phone != ''
                         AND personalization_notes LIKE '%[WA_SENT:%'
                         AND personalization_notes NOT LIKE '%[WA_REPLIED:%'
                         AND personalization_notes NOT LIKE '%[WA_SKIP]%'
                         AND personalization_notes NOT LIKE '%[WA_FU2:%'
                         AND status NOT IN ('replied', 'opted_out', 'lost', 'meeting', 'closed')
                       ORDER BY created_at ASC"""
                )
                rows = await cur.fetchall()
                has_ind = True
            except Exception:
                cur = await db.execute(
                    """SELECT id, first_name, last_name, company, phone, personalization_notes
                       FROM prospects
                       WHERE phone IS NOT NULL AND phone != ''
                         AND personalization_notes LIKE '%[WA_SENT:%'
                         AND personalization_notes NOT LIKE '%[WA_REPLIED:%'
                         AND personalization_notes NOT LIKE '%[WA_SKIP]%'
                         AND personalization_notes NOT LIKE '%[WA_FU2:%'
                         AND status NOT IN ('replied', 'opted_out', 'lost', 'meeting', 'closed')
                       ORDER BY created_at ASC"""
                )
                rows = await cur.fetchall()
                has_ind = False

        for r in rows:
            if has_ind:
                pid, fn, ln, comp, ph, notes, ind = r
            else:
                pid, fn, ln, comp, ph, notes = r
                ind = ""
            notes = notes or ""
            m = re.search(r"\[WA_SENT:(\d{4}-\d{2}-\d{2})\]", notes)
            if not m or m.group(1) < since:
                continue
            norm = normalize_id_phone(ph)
            if not norm or is_internal_operator_number(norm, cfg.report_phone):
                continue
            from harvey.config import is_partner_or_excluded
            if is_partner_or_excluded(name=f"{fn} {ln}", company=comp, phone=norm, config=self.config):
                continue
            sent_d = date.fromisoformat(m.group(1))
            fu1 = re.search(r"\[WA_FU1:(\d{4}-\d{2}-\d{2})\]", notes)
            if not fu1:
                if (today - sent_d).days >= cfg.followup1_after_days:
                    return {"id": pid, "fn": fn or "", "ln": ln or "", "comp": comp or "",
                            "phone": norm, "notes": notes, "stage": 1, "industry": ind or ""}
            else:
                fu1_d = date.fromisoformat(fu1.group(1))
                if (today - sent_d).days >= cfg.followup2_after_days and (today - fu1_d).days >= 2:
                    return {"id": pid, "fn": fn or "", "ln": ln or "", "comp": comp or "",
                            "phone": norm, "notes": notes, "stage": 2, "industry": ind or ""}
        return None

    async def _run_one_followup(self, act: dict, use_fonnte: bool) -> bool:
        due = await self._followup_due()
        if not due:
            return False
        stage = due["stage"]
        ok = False
        asset = None
        if stage == 1:
            text = build_followup_message(
                1,
                due["fn"],
                due["ln"],
                due["comp"],
                with_link=False,
                seed=due.get("phone", ""),
                industry=due.get("industry", ""),
            )
            ok = await self._send_text_auto(due["phone"], text)
        elif stage == 2:
            ind = due.get("industry", "")
            asset = pick_asset_for(ind, due["comp"])
            text = build_followup_message(
                stage,
                due["fn"],
                due["ln"],
                due["comp"],
                asset=asset,
                seed=due.get("phone", ""),
            )
            ok = await self._send_text_auto(due["phone"], text)
        else:
            text = build_followup_message(stage, due["fn"], due["ln"], due["comp"], with_link=(stage == 1))
            ok = await self._send_text_auto(due["phone"], text)
        tags = [f"WA_FU{stage}"]
        if stage == 2 and asset and asset.get("id"):
            tags.append(f"WA_ASSET_OFFER:{asset['id']}")
        if ok:
            act["sent"] = act.get("sent", 0) + 1
            act["fu_sent"] = act.get("fu_sent", 0) + 1
            act.setdefault("numbers", []).append(due["phone"])
            self._save_activity(act)
            await self._tag_prospect(due["id"], due["notes"], tags)
            await self.state.log_action(
                action_type="whatsapp_followup",
                agent="sender",
                details={
                    "prospect_id": due["id"],
                    "company": due["comp"],
                    "stage": stage,
                    "asset_id": asset.get("id") if asset else None,
                },
            )
            logger.info(f"WhatsApp: Follow-up {stage} terkirim ke {due['comp'] or due['fn']} ({act['sent']}/{self.wa_cfg.max_daily_messages}, follow-up ke-{act['fu_sent']}).")
        else:
            # Do not retry the same failing number every cycle
            await self._tag_prospect(due["id"], due["notes"], tags)
            logger.info(f"WhatsApp: Follow-up {stage} gagal untuk {due['comp'] or due['fn']}, dilewati.")
        return ok

    async def _run_one_quote_followup(self, act: dict, use_fonnte: bool = False) -> bool:
        """Send at most one approved SPH follow-up (grouped per phone)."""
        from harvey.integrations.quote_chaser import (
            QuoteChaser, QuoteFollowupPlanner, build_group_message,
        )
        planner = QuoteFollowupPlanner(QuoteChaser(self), self.wa_cfg)
        g = await planner.next_due()
        if not g:
            return False
        msg = build_group_message(g)
        ok = await self._send_text_auto(g["phone"], msg)
        if ok:
            act["sent"] = act.get("sent", 0) + 1
            act["fu_sent"] = act.get("fu_sent", 0) + 1
            act.setdefault("numbers", []).append(g["phone"])
            self._save_activity(act)
            await self.state.log_action(
                action_type="whatsapp_quote_followup",
                agent="sender",
                details={"company": g["company"], "stage": g["due_stage"], "quotes": g["ids"]},
            )
            logger.info(
                f"WhatsApp: Follow-up SPH tahap {g['due_stage']} terkirim ke {g['company']} "
                f"({act['sent']}/{self.wa_cfg.max_daily_messages} hari ini, follow-up ke-{act['fu_sent']})"
            )
        else:
            if getattr(self, "_last_send_error", None) == "not_registered":
                from harvey.integrations.quote_chaser import DB_PATH
                import aiosqlite
                async with aiosqlite.connect(DB_PATH) as db:
                    for qid in g["ids"]:
                        await db.execute(
                            "UPDATE quotes SET status = 'no_wa', notes = coalesce(notes, '') || ' [TIDAK_TERDAFTAR_WA]' WHERE id = ?",
                            (qid,),
                        )
                    await db.commit()
                logger.warning(f"WhatsApp: Nomor {g['phone']} ({g['company']}) tidak terdaftar di WA, ditandai status 'no_wa'.")
            else:
                logger.info(f"WhatsApp: Follow-up SPH ke {g['company']} gagal, dicoba lagi siklus depan.")
        # Mark done only on success; on failure it retries next cycle.
        if ok:
            await planner.mark_done(g)
        return ok

    async def _drain_approved_outbox(self) -> int:
        """Send any approved WhatsApp replies waiting in the outbox."""
        async with self.state._connect() as db:
            cur = await db.execute(
                """SELECT id, conversation_id, to_email, body FROM outbox
                   WHERE provider = 'whatsapp' AND status = 'approved' AND kind = 'reply'
                   ORDER BY created_at ASC LIMIT 5"""
            )
            rows = await cur.fetchall()

        if not rows:
            return 0

        sent_count = 0
        now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        for oid, cid, to_phone, body in rows:
            target_phone = normalize_id_phone(to_phone)
            if not target_phone or is_internal_operator_number(target_phone, getattr(self.wa_cfg, "report_phone", None)):
                continue
            ok = await self._send_text_auto(target_phone, body)
            if ok:
                sent_count += 1
                async with self.state._connect() as db:
                    await db.execute("UPDATE outbox SET status = 'sent', sent_at = ? WHERE id = ?", (now_iso, oid))
                    if cid:
                        cur2 = await db.execute("SELECT thread_json FROM conversations WHERE id = ?", (cid,))
                        crow = await cur2.fetchone()
                        if crow:
                            thread = json.loads(crow[0] or "[]")
                            thread.append({
                                "direction": "outbound",
                                "channel": "whatsapp",
                                "content": body,
                                "text": body,
                                "sender": "operator",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            })
                            await db.execute("UPDATE conversations SET thread_json = ?, updated_at = ? WHERE id = ?",
                                             (json.dumps(thread), now_iso, cid))
                    await db.commit()
                await self.state.log_action(
                    action_type="whatsapp_reply_sent",
                    agent="handler",
                    details={"outbox_id": oid, "phone": target_phone, "status": "sent"},
                )
                await asyncio.sleep(random.uniform(5.0, 10.0))
        return sent_count

    async def run(self) -> int:
        """Execute one safe WhatsApp outreach batch during heartbeat."""
        if not self.wa_cfg.enabled:
            return 0

        # Check and send automated daily report if due (e.g. >= 17:00 WIB)
        if self.wa_cfg.auto_report and self.wa_cfg.report_phone:
            try:
                await self.send_daily_report()
            except Exception as e:
                logger.warning(f"WhatsApp: Error saat memproses auto-report harian: {e}")

        # Check and process automated quote follow-ups
        try:
            if not self.wa_cfg.quote_followup_enabled:
                pass  # automation off; enable quote_followup_enabled after reviewing the preview CSV
        except Exception as e:
            logger.debug(f"WhatsApp: Quote chaser check skipped: {e}")

        # Approved WhatsApp outbox items are drained exclusively by Sender (sender.run)
        # to prevent dual-consumer race conditions when send_outbox & send_whatsapp run in parallel.

        is_openwa = (getattr(self.wa_cfg, "provider", "") == "openwa") or (self.openwa and await self.openwa.check_health())
        if is_openwa and self.in_reply_hours():
            try:
                await self.sync_live_chats_from_openwa(limit=50)
            except Exception as e:
                logger.debug(f"WhatsApp: Error saat sync_live_chats_from_openwa: {e}")
        elif not is_openwa and getattr(self.wa_cfg, "inbound_scan_enabled", True) and self.in_reply_hours():
            try:
                await self.process_inbound()
            except Exception as e:
                logger.warning(f"WhatsApp: Error saat memproses balasan masuk: {e}")

        if not self.in_work_hours():
            logger.info(
                f"WhatsApp: Di luar jam kerja WA ({self.wa_cfg.work_hours_start}-{self.wa_cfg.work_hours_end}). Skip demi keamanan nomor."
            )
            return 0

        act = self._load_activity()
        if act["sent"] >= self.wa_cfg.max_daily_messages:
            logger.info(
                f"WhatsApp: Kuota aman harian tercapai ({act['sent']}/{self.wa_cfg.max_daily_messages})."
            )
            return 0

        is_openwa = (getattr(self.wa_cfg, "provider", "") == "openwa") or (self.openwa and await self.openwa.check_health())
        use_fonnte = self.wa_cfg.provider == "fonnte" or bool(self.env.fonnte_api_key)
        if not is_openwa and not use_fonnte and not self.is_logged_in_profile_present():
            logger.info(
                "WhatsApp: Sesi WhatsApp belum terhubung. Jalankan OpenWA di http://localhost:2785 atau `harvey whatsapp login`."
            )
            return 0

        remaining_today = self.wa_cfg.max_daily_messages - act["sent"]
        if remaining_today <= 0:
            return 0

        batch_limit = min(self.wa_cfg.max_per_cycle, remaining_today)

        # -------------------------------------------------------------
        # PILAR UTAMA: PRIORITAS OUTREACH KUSTOMER BARU
        # Alokasikan mayoritas kuota harian (75%-80%) untuk kustomer baru.
        # Batasi follow-up maksimal 20%-25% per hari (misal maks 5-6 pesan).
        # -------------------------------------------------------------
        candidates = await self._candidates_for_wa(batch_limit, set(act.get("numbers", [])))

        sent_count = 0
        if candidates:
            for idx, cand in enumerate(candidates):
                if idx > 0:
                    delay = random.uniform(
                        self.wa_cfg.min_delay_seconds, self.wa_cfg.max_delay_seconds
                    )
                    logger.info(f"WhatsApp: Jeda anti-banned {delay:.0f} detik sebelum pesan berikutnya...")
                    await asyncio.sleep(delay)

                msg = build_antiban_wa_message(
                    first_name=cand["first_name"],
                    last_name=cand.get("last_name", ""),
                    company=cand["company"],
                    industry=cand["industry"],
                    notes=cand["notes"],
                    status=cand.get("status", ""),
                    seed=cand["phone"],
                )
                ok = await self._send_text_auto(cand["phone"], msg)
                if ok:
                    sent_count += 1
                    act["sent"] = act.get("sent", 0) + 1
                    act["new_sent"] = act.get("new_sent", 0) + 1
                    act.setdefault("numbers", []).append(cand["phone"])
                    self._save_activity(act)
                    await self._mark_wa_sent(cand["id"], cand["phone"], cand["notes"])
                    await self.state.log_action(
                        action_type="whatsapp_sent",
                        agent="sender",
                        details={
                            "prospect_id": cand["id"],
                            "company": cand["company"],
                            "phone": cand["phone"],
                        },
                    )
                    logger.info(
                        f"WhatsApp: Outreach kustomer baru terkirim ke {cand['company'] or cand['first_name']} "
                        f"({act['sent']}/{self.wa_cfg.max_daily_messages} hari ini, kustomer baru: {act['new_sent']})"
                    )
                else:
                    # Mark invalid/failed so we don't retry the same broken number every 15 mins
                    await self._mark_wa_sent(cand["id"], cand["phone"], cand["notes"] + " [WA_SKIP]")
            return sent_count

        # -------------------------------------------------------------
        # JIKA TIDAK ADA KONTAK BARU / KONTAK BARU SELESAI SIKLUS INI:
        # Eksekusi follow-up secara stabil & terukur (maks 25% kuota harian)
        # -------------------------------------------------------------
        max_daily_fu = getattr(self.wa_cfg, "max_daily_followups", max(int(self.wa_cfg.max_daily_messages * 0.25), 4))
        fu_today = act.get("fu_sent", 0)

        if fu_today < max_daily_fu and remaining_today > 0:
            # 1. SPH follow-ups (klien yang minta penawaran/SPH)
            if self.wa_cfg.quote_followup_enabled:
                try:
                    if await self._run_one_quote_followup(act, use_fonnte):
                        return 1
                except Exception as e:
                    logger.warning(f"WhatsApp: Error follow-up SPH: {e}")

            # 2. Cold chat follow-ups (stage 1 / stage 2)
            if self.wa_cfg.followup_enabled:
                try:
                    if await self._run_one_followup(act, use_fonnte):
                        return 1
                except Exception as e:
                    logger.warning(f"WhatsApp: Error cold follow-up: {e}")
        else:
            if fu_today >= max_daily_fu:
                logger.info(f"WhatsApp: Batas aman follow-up harian ({fu_today}/{max_daily_fu}) tercapai. Kuota difokuskan untuk kustomer baru.")

        return 0



async def send_dashboard_link_to_wa(
    url: str,
    lan_url: str | None = None,
    target_phone: str = "087764021131",
) -> bool:
    """Send Harvey dashboard mobile unlock link to Ahnaf's WhatsApp."""
    norm_phone = normalize_id_phone(target_phone)
    if not norm_phone:
        return False

    from harvey.config import load_config
    from harvey.config import load_env
    from harvey.state import StateManager

    cfg = load_config()
    env = load_env()
    state = StateManager()
    dispatcher = WhatsAppEngine(state, cfg, env)

    lan_section = f"\n📶 *Local Wi-Fi (Satu Jaringan Kantor/Rumah):*\n{lan_url}\n" if lan_url else ""
    msg = (
        f"🚀 *Link Akses Dashboard Mobile Harvey*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Halo Mas Ahnaf, berikut link dashboard Harvey untuk dibuka langsung dari browser HP:\n\n"
        f"📱 *Link Utama (Langsung Akses):*\n"
        f"{url}\n"
        f"{lan_section}"
        f"\n━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 *Catatan:* Link ini sudah dilengkapi token autentikasi otomatis. "
        f"Cukup sentuh/klik link di atas, dashboard akan langsung terbuka di HP. "
        f"Simpan link ini secara rahasia ya Mas! 🙏"
    )

    return await dispatcher._send_text_auto(norm_phone, msg, is_system_alert=True)
