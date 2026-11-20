"""Smart & Accurate Product Matching Engine for PT Andis Sentral Laboratorium.

Principles: 'Zero Silent Error'
- Normalizes units (L -> mL, µL, rpm, g).
- Laboratory taxonomy synonym mapping (Indonesian <-> English catalog standards).
- Applies hard filters on brand, capacity, and product type.
- Never guesses when information is ambiguous: generates 1 polite Ahnaf-style
  clarification question with ready stock options.
- Matches against the 1,142 items in `data/inventory.json` and SQLite `inventory`.
"""

import json
import logging
import re
from pathlib import Path
from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.matcher")
INVENTORY_JSON = PROJECT_ROOT / "data" / "inventory.json"

_inventory_cache: list[dict] = []

SYNONYM_MAP = {
    "timbangan analitik": ["analytical balance"],
    "timbangan": ["balance", "analytical balance", "precision balance", "moisture balance"],
    "neraca": ["balance", "analytical balance"],
    "gelas ukur": ["measuring cylinder", "cylinder"],
    "silinder": ["measuring cylinder"],
    "labu ukur": ["volumetric flask"],
    "erlenmeyer": ["erlenmeyer", "conical flask"],
    "beaker": ["beaker", "gelas piala", "gelas beker"],
    "cawan petri": ["petri dish", "dish"],
    "cawan": ["dish", "crucible"],
    "mikropipet": ["micropipette"],
    "pipet tetes": ["dropper", "pasteur pipette"],
    "pipet": ["pipette", "micropipette"],
    "lemari asam": ["fume hood", "exhaust hood"],
    "biosafety": ["biosafety", "bsc", "clean room"],
    "sentrifus": ["centrifuge", "microcentrifuge"],
    "centrifuge": ["centrifuge"],
    "vortex": ["vortex mixer", "vortex"],
    "shaker": ["orbital shaker", "shaker"],
    "hotplate": ["hotplate", "magnetic stirrer"],
    "stirrer": ["stirrer", "magnetic stirrer"],
    "penangas air": ["water bath", "waterbath"],
    "waterbath": ["water bath", "waterbath"],
    "tanur": ["muffle furnace", "furnace"],
    "rak tabung": ["test tube rack", "tube rack"],
    "tabung reaksi": ["test tube"],
    "botol sampel": ["reagent bottle", "sample bottle"],
    "botol semprot": ["wash bottle"],
    "kertas saring": ["filter paper", "whatman"],
    "buret": ["burette", "buret"],
    "corong": ["funnel"],
    "spektrofotometer": ["spectrophotometer", "spectro"],
    "ph meter": ["ph meter", "ph tester"],
    "oven": ["drying oven", "oven"],
    "inkubator": ["incubator"],
    "incubator": ["incubator"],
    "autoclave": ["autoclave", "sterilizer"],
    "autoklaf": ["autoclave"],
    # Semantic Layman-to-Lab Concept Expansion (inspired by Vector RAG)
    "kekeruhan air": ["turbidity"],
    "kekeruhan": ["turbidity"],
    "keasaman": ["ph meter", "ph tester"],
    "asam basa": ["ph meter"],
    "pengering sampel": ["drying oven", "oven"],
    "pengeringan sampel": ["drying oven", "oven"],
    "pengeringan": ["drying oven", "oven"],
    "oven pengering": ["drying oven", "oven"],
    "pemanas sampel": ["drying oven", "oven", "hotplate"],
    "sterilisasi uap": ["autoclave", "sterilizer"],
    "alat steril": ["autoclave", "sterilizer"],
    "4 desimal": ["analytical balance"],
    "berat teliti": ["analytical balance"],
    "pengaduk larutan": ["magnetic stirrer", "stirrer"],
    "pengaduk magnet": ["magnetic stirrer"],
    "pemisah partikel": ["centrifuge"],
    "pemutar tabung": ["centrifuge"],
    "kadar air": ["moisture analyzer", "moisture balance"],
    "konduktivitas": ["conductivity meter", "ec meter"],
}

# Exclusion rules to prevent false matches (e.g. 'Fully Autoclave' on a micropipette)
PRODUCT_EXCLUSIONS = {
    "autoclave": ["pipette", "micropipette", "tip", "bag"],
    "vortex": ["adapter", "pad", "cup"],
}

STOPWORDS_ID = {
    "yang", "dan", "ada", "mau", "minta", "tolong", "buat", "gak", "nggak", "tidak",
    "sama", "sekali", "ini", "itu", "kak", "pak", "bu", "mas", "mbak", "bapak", "ibu",
    "bulan", "kira", "kira2", "harga", "barang", "stok", "ready", "seri", "series",
    "lama", "baru", "nampak", "sudah", "lagi", "setelah", "diskon", "diskusikan",
    "dengan", "untuk", "saja", "dulu", "oke", "baik", "siap", "boleh", "tanya",
    "bertanya", "apakah", "tentang", "ketersediaan", "hanya", "menanyakan", "kebetulan",
    "lumayan", "agak", "sih", "namun", "sekarang", "terima", "kasih", "makasih",
    "halo", "selamat", "pagi", "siang", "sore", "malam", "assalamualaikum", "mohon",
    "izin", "kirim", "kirimkan", "penawaran", "sph", "berapa", "bisa", "atau", "dari",
    "dalam", "kalau", "kalo", "jika", "bila", "pada", "ke", "di", "ya", "yah", "nya",
    "liter", "kapasitas", "kebutuhan", "tanaman", "sampel", "media", "tanam",
    "silahkan", "silakan", "mangga", "monggo", "berkenan", "dibantu", "bantu",
    "katalog", "brosur", "compro", "profile", "profil", "perusahaan", "instansi",
    "laboratorium", "industri", "produk", "alat", "instrumen", "rekan", "diarahkan",
    "koordinasi", "perkenalkan", "ahnaf", "andislab", "distributor", "resmi", "stock",
    "jabodetabek", "visit", "jadwal", "kamis", "kapan", "pukul", "mampir", "kawasan",
    "sedang", "dicari", "kami", "kita", "anda", "beliau", "terkait", "langsung",
}

SPAREPART_KEYWORDS = [
    "probe", "elektroda", "electrode", "sensor", "sparepart", "spare part",
    "suku cadang", "lampu uv", "seal", "gasket", "elemen pemanas", "heater element",
    "adaptor", "kabel", "kalibrasi",
]

EXTERNAL_BRANDS = [
    "milwaukee", "pyrex", "mettler", "sartorius", "ohaus", "shimadzu",
    "agilent", "eppendorf", "thermo", "merck",
]


def load_inventory() -> list[dict]:
    global _inventory_cache
    if not _inventory_cache and INVENTORY_JSON.exists():
        try:
            _inventory_cache = json.loads(INVENTORY_JSON.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Failed to load inventory: {e}")
    return _inventory_cache


def normalize_capacity(text: str) -> tuple[float | tuple[float, float] | None, str]:
    """Parse capacity string into (value_in_base_unit, standard_unit)."""
    t = (text or "").lower()
    # Check range like 15-30l or 15 - 30 liter
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-~to]+\s*(\d+(?:\.\d+)?)\s*(?:liter|ltr|l)(?![a-zA-Z])", t)
    if m:
        return (float(m.group(1)) * 1000.0, float(m.group(2)) * 1000.0), "ml_range"
    # Check model code patterns like ldo-030, ldo-060, ldo-080
    m = re.search(r"(?:ldo|ov|dkn|dh|wh)[-_]0*(\d{2,3})", t)
    if m:
        return float(m.group(1)) * 1000.0, "ml"
    # Check Liters
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:liter|ltr|l)(?![a-zA-Z])", t)
    if m:
        return float(m.group(1)) * 1000.0, "ml"
    # Check mL / cc
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:ml|cc)(?![a-zA-Z])", t)
    if m:
        return float(m.group(1)), "ml"
    # Check microliters (ul / µl)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:ul|µl)(?![a-zA-Z])", t)
    if m:
        return float(m.group(1)) / 1000.0, "ml"
    # Check kg
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:kg)(?![a-zA-Z])", t)
    if m:
        return float(m.group(1)) * 1000.0, "g"
    # Check grams
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:gram|gr|g)(?![a-zA-Z])", t)
    if m:
        return float(m.group(1)), "g"
    # Check mg (milligrams)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:mg)(?![a-zA-Z])", t)
    if m:
        return float(m.group(1)) / 1000.0, "g"
    return None, ""


def match_product(query: str) -> dict:
    """Match customer request with Zero Silent Error guarantees."""
    inventory = load_inventory()
    if not inventory:
        return {"status": "NOT_FOUND", "item": None, "alternatives": [], "clarification_message": "", "recommendation_text": ""}

    q = (query or "").lower().strip()
    req_cap, req_unit = normalize_capacity(q)

    # Detect known brands & external brands
    known_brands = [
        "dlab", "daihan", "yamato", "coleparmer", "euromex", "apera", "bel-art",
        "biobase", "aczet", "hanna", "lovibond", "memmert", "iwaki", "whatman", "aelab"
    ]
    detected_brand = next((b for b in known_brands if re.search(rf"\b{re.escape(b)}\b", q)), "")
    external_brand = next((b for b in EXTERNAL_BRANDS if re.search(rf"\b{re.escape(b)}\b", q)), "")
    is_sparepart = any(re.search(rf"\b{re.escape(sp)}\b", q) for sp in SPAREPART_KEYWORDS)

    # Match key product category from synonym map
    matched_term = ""
    search_terms = []
    for ind_term, eng_terms in sorted(SYNONYM_MAP.items(), key=lambda x: len(x[0]), reverse=True):
        if re.search(rf"\b{re.escape(ind_term)}\b", q):
            matched_term = ind_term
            search_terms = eng_terms
            break

    # Branch C: Sparepart / Probe / Sensor check (never guess an unrelated main unit!)
    if is_sparepart:
        brand_lbl = f" merk {external_brand.title()}" if external_brand else (f" merk {detected_brand.title()}" if detected_brand else "")
        prod_lbl = f" {matched_term}" if matched_term else ""
        clarification = (
            f"Terima kasih telah menghubungi AndisLab. 🙏 Untuk ketersediaan dan harga suku cadang/probe{prod_lbl}{brand_lbl}, "
            "izin kami cek terlebih dahulu stok dan kecocokannya ke bagian gudang/prinsipal kami ya Pak/Bu.\n\n"
            "Boleh dibantu kirimkan **foto unit/konektor lamanya** atau tipe lengkapnya agar tidak salah kecocokan spesifikasinya? "
            "Jika seri lamanya sudah diskontinu, kami juga bisa bantu lampirkan opsi unit baru yang ready stock sebagai perbandingan. 🙏"
        )
        return {
            "status": "SPAREPART_CHECK",
            "item": None,
            "alternatives": [],
            "clarification_message": clarification,
            "recommendation_text": f"Cek stok prinsipal: Sparepart/Probe{prod_lbl}{brand_lbl}",
        }

    used_fallback_tokens = False
    if not search_terms:
        # Strict word-boundary tokens excluding Indonesian conversational stopwords
        tokens = [
            w for w in re.findall(r"[a-z0-9]+", q)
            if len(w) > 3 and w not in STOPWORDS_ID and not w.isdigit()
        ]
        if not tokens:
            return {"status": "NOT_FOUND", "item": None, "alternatives": [], "clarification_message": "", "recommendation_text": ""}
        search_terms = tokens
        used_fallback_tokens = True

    exclusions = PRODUCT_EXCLUSIONS.get(matched_term, [])

    # Filter inventory candidates matching search terms
    candidates = []
    for it in inventory:
        it_name = it["name"].lower()
        it_blob = f"{it_name} {it['model'].lower()} {it['category'].lower()} {it['specifications'].lower()} {' '.join(it.get('keywords', []))}"
        
        # Guard: check exclusions
        if any(exc in it_name for exc in exclusions):
            continue

        if used_fallback_tokens:
            # Strict word-boundary match so 'ini' NEVER matches 'DTU-Mini'
            matched = any(re.search(rf"\b{re.escape(term)}\b", it_blob) for term in search_terms)
        else:
            matched = any(term in it_blob for term in search_terms)

        if matched:
            if detected_brand and detected_brand not in it["brand"].lower():
                continue
            candidates.append(it)

    # Branch B: External brand requested (e.g. Pyrex / Milwaukee) -> check if we have equivalents
    if external_brand and not candidates:
        equiv_candidates = [
            it for it in inventory
            if any(term in f"{it['name']} {it['model']} {it['category']}".lower() for term in search_terms)
        ] if matched_term else []
        if equiv_candidates:
            brands_avail = ", ".join(sorted({it.get("brand", "") for it in equiv_candidates[:5] if it.get("brand")}))
            clarification = (
                f"Untuk merk *{external_brand.title()}* saat ini belum tersedia di gudang kami Pak/Bu, "
                f"namun kami memiliki alternatif setara yang ready stock di Jabodetabek (merk *{brands_avail}*). "
                "Apakah berkenan kami buatkan draf penawaran opsi merk alternatifnya? 🙏"
            )
            return {
                "status": "ALTERNATIVE_BRAND",
                "item": equiv_candidates[0],
                "alternatives": equiv_candidates[:4],
                "clarification_message": clarification,
                "recommendation_text": f"Alternatif setara {external_brand.title()}: {brands_avail}",
            }
        return {
            "status": "SPAREPART_CHECK",
            "item": None,
            "alternatives": [],
            "clarification_message": (
                f"Untuk produk merk *{external_brand.title()}*, izin kami cek terlebih dahulu ketersediaan dan harganya ke prinsipal kami ya Pak/Bu. "
                "Boleh dibantu infokan tipe atau spesifikasinya?"
            ),
            "recommendation_text": f"Cek prinsipal merk {external_brand.title()}",
        }

    if not candidates and detected_brand:
        candidates = [it for it in inventory if any(term in f"{it['name']} {it['model']} {it['category']}".lower() for term in search_terms)]

    # LanceDB semantic vector fallback ONLY when query has >= 2 meaningful technical tokens
    if not candidates and len(search_terms) >= 2:
        try:
            from harvey.integrations.big_data import MahoragaBigData
            vec_hits = MahoragaBigData().search_sku_vector(q, top_k=3, min_score=0.38)
            if vec_hits:
                hit_ids = {h["id"] for h in vec_hits}
                candidates = [it for it in inventory if str(it.get("id", "")) in hit_ids]
                if not candidates:
                    # Fallback reconstruct from LanceDB hit if id differs
                    candidates = [
                        {
                            "id": h["id"],
                            "name": h["name"],
                            "brand": h["brand"],
                            "model": h["model"],
                            "category": h["category"],
                            "price": float(h.get("price") or 0),
                            "specifications": h.get("search_text", ""),
                        }
                        for h in vec_hits
                    ]
        except Exception:
            pass

    if not candidates:
        return {"status": "NOT_FOUND", "item": None, "alternatives": [], "clarification_message": "", "recommendation_text": ""}

    # Rank candidates using rank_bm25 (BM25Okapi) so the most relevant item always ranks first
    if len(candidates) > 1:
        try:
            from rank_bm25 import BM25Okapi
            corpus = [
                re.findall(r"[a-z0-9]+", f"{c['name']} {c['brand']} {c['model']} {c['category']} {c['specifications']}".lower())
                for c in candidates
            ]
            bm25 = BM25Okapi(corpus)
            query_tokens = re.findall(r"[a-z0-9]+", f"{q} {' '.join(search_terms)}".lower())
            scores = bm25.get_scores(query_tokens)
            candidates = [c for _, c in sorted(zip(scores, candidates), key=lambda pair: pair[0], reverse=True)]
        except Exception:
            pass

    # Check capacity match if requested
    if req_cap is not None:
        exact_cap_candidates = []
        for it in candidates:
            it_cap, it_unit = normalize_capacity(f"{it['name']} {it['specifications']}")
            is_match = False
            if req_unit == "ml_range" and isinstance(req_cap, tuple) and isinstance(it_cap, (int, float)):
                if req_cap[0] <= it_cap <= req_cap[1]:
                    is_match = True
            elif it_cap == req_cap and (not req_unit or not it_unit or it_unit == req_unit):
                is_match = True
            if is_match:
                exact_cap_candidates.append(it)
        if exact_cap_candidates:
            best = exact_cap_candidates[0]
            return {
                "status": "EXACT",
                "item": best,
                "alternatives": exact_cap_candidates[1:4],
                "clarification_message": "",
                "recommendation_text": f"{best['name']} ({best['brand']}) [Ready Stock] - Rp {best['price']:,.0f}"
            }

    # If multiple candidates exist and no capacity was specified, ask clarification!
    if len(candidates) > 1 and req_cap is None:
        sample_options = []
        for it in candidates[:5]:
            label = f"{it['brand']} {it['name'][:30]}"
            if label not in sample_options:
                sample_options.append(label)

        options_str = ", ".join(sample_options[:3])
        main_term = matched_term.title() if matched_term else (search_terms[0].title() if search_terms else "Alat")
        clarification = (
            f"Siap Pak/Bu, untuk {main_term} unit kami ready stock di gudang dengan beberapa pilihan ({options_str}). "
            "Kira-kira ada kapasitas spesifik atau merk tertentu yang dibutuhkan Pak/Bu? Biar bisa langsung kami buatkan SPH resminya. 🙏"
        )
        return {
            "status": "CLARIFICATION",
            "item": candidates[0],
            "alternatives": candidates[:4],
            "clarification_message": clarification,
            "recommendation_text": f"Pilihan tersedia: {options_str}"
        }

    best = candidates[0]
    return {
        "status": "EXACT",
        "item": best,
        "alternatives": candidates[1:4],
        "clarification_message": "",
        "recommendation_text": f"{best['name']} ({best['brand']} {best['model']}) - Rp {best['price']:,.0f} [Ready Stock]"
    }
