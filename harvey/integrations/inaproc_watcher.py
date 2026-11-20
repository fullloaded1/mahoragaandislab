"""Agent Pemantau INAPROC & Pengadaan Pemerintah untuk PT Andis Sentral Laboratorium.

Memantau dan memfilter paket pengadaan alat laboratorium dari portal LPSE / SiRUP / e-Katalog
INAPROC, mencocokkan dengan 1.140+ item ready stock AndisLab, dan mengirimkan alert
peluang bernilai tinggi ke WhatsApp operator.
"""

from dataclasses import asdict, dataclass
from datetime import date, datetime
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any

import httpx

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.inaproc")

# 10 Kategori Utama Alat Laboratorium AndisLab di INAPROC
ANDISLAB_CORE_CATEGORIES = {
    "timbangan": {
        "keywords": ["timbangan analitik", "analytical balance", "precision balance", "neraca analitik", "moisture analyzer", "timbangan digital lab"],
        "typical_models": ["Aczet CY224C", "Aczet CY223", "Aczet MB100", "Infitek BA-N503"],
        "min_budget": 15_000_000,
        "max_budget": 120_000_000,
    },
    "autoclave": {
        "keywords": ["autoclave", "autoklaf", "sterilisator uap", "steam sterilizer", "sterilisasi lab", "alat sterilisasi"],
        "typical_models": ["Daihan Labtech LAC-50455P", "LAC-1021SV", "LAC-5065SP"],
        "min_budget": 40_000_000,
        "max_budget": 150_000_000,
    },
    "oven_inkubator": {
        "keywords": ["oven laboratorium", "drying oven", "incubator", "inkubator", "co2 incubator", "shaking incubator", "furnace"],
        "typical_models": ["Daihan Labtech LDO-060F", "LIB-030M", "LCO-150B"],
        "min_budget": 15_000_000,
        "max_budget": 180_000_000,
    },
    "safety_cabinet": {
        "keywords": ["biosafety cabinet", "bsc", "laminar air flow", "laf", "clean bench", "fume hood", "lemari asam", "lemari b3"],
        "typical_models": ["Batavialab BSC Class II", "LAF 1000 Vertikal", "Fume Hood SUS 304", "Lemari B3 Standar OSHA"],
        "min_budget": 25_000_000,
        "max_budget": 220_000_000,
    },
    "cold_chain": {
        "keywords": ["kulkas lab", "laboratory refrigerator", "ult freezer", "freezer lab", "biomedical freezer", "-86 freezer"],
        "typical_models": ["Daihan Labtech LUF-080F", "Infitek BDF-86V", "Medical Refrigerator 2-8C"],
        "min_budget": 20_000_000,
        "max_budget": 250_000_000,
    },
    "sentrifus": {
        "keywords": ["centrifuge", "sentrifus", "refrigerated centrifuge", "microcentrifuge"],
        "typical_models": ["DLAB DM0412", "D1524R Refrigerated", "Clinical Centrifuge"],
        "min_budget": 5_000_000,
        "max_budget": 150_000_000,
    },
    "ph_meter": {
        "keywords": ["ph meter", "ph/ion meter", "multiparameter", "conductivity meter", "konduktivitas"],
        "typical_models": ["Horiba LAQUA-PH1100", "Infitek Benchtop pH Meter"],
        "min_budget": 2_000_000,
        "max_budget": 50_000_000,
    },
    "spektrofotometer": {
        "keywords": ["spektrofotometer", "spectrophotometer", "uv-vis", "visible spectrophotometer"],
        "typical_models": ["UV-Vis Single Beam", "Double Beam High Precision"],
        "min_budget": 45_000_000,
        "max_budget": 200_000_000,
    },
    "laboratorium_umum": {
        "keywords": ["alat laboratorium", "alkat laboratorium", "peralatan laboratorium", "laboratorium lingkungan", "laboratorium mikrobiologi", "laboratorium kimia", "meubelair laboratorium"],
        "typical_models": ["Unit Katalog & Custom Fabrikasi AndisLab"],
        "min_budget": 10_000_000,
        "max_budget": 500_000_000,
    },
    "reagen_bahan_kimia": {
        "keywords": ["reagen", "reagent", "bahan laboratorium", "bahan kimia labkes", "reagen polio", "bahan kimia laboratorium", "consumable lab", "bahan habis pakai laboratorium", "labkes"],
        "typical_models": ["Merck Reagent Grade", "DLAB Reagent Dispenser", "Larutan Kimia & Reagen Lab AndisLab"],
        "min_budget": 5_000_000,
        "max_budget": 500_000_000,
    },
    "rapid_test_sanitarian": {
        "keywords": ["rapid test", "sanitarian kit", "water test kit", "food security kit", "chlorine comparator", "thermohygrobarometer", "kesling kit", "sanitarian"],
        "typical_models": ["Sanitarian Field Kit", "Chlorine Comparator Kit", "Rapid Test Diagnostic Labkes"],
        "min_budget": 10_000_000,
        "max_budget": 2_000_000_000,
    },
}


@dataclass
class InaprocPackage:
    package_id: str
    package_name: str
    institution: str
    hps_budget: float
    category_matched: str
    matched_models: list[str]
    match_score: int
    procurement_type: str  # e.g., "Mini Kompetisi", "Pengadaan Langsung", "Tender Cepat"
    location: str
    source_url: str
    deadline: str
    status: str = "new"  # new, alerted, archived


def match_package_to_andislab(package_name: str, hps_budget: float = 0.0, procurement_type: str = "") -> tuple[str, list[str], int]:
    """Score and match a procurement package against AndisLab catalog categories.

    Returns: (category_name, matched_models, score_from_0_to_100)
    """
    text_clean = f"{package_name} {procurement_type}".lower()
    best_cat = ""
    best_models: list[str] = []
    highest_score = 0

    for cat_name, cat_data in ANDISLAB_CORE_CATEGORIES.items():
        score = 0
        matched_kw = []
        for kw in cat_data["keywords"]:
            if re.search(rf"\b{re.escape(kw)}\b", text_clean):
                score += 35
                matched_kw.append(kw)
            elif kw in text_clean:
                score += 20
                matched_kw.append(kw)

        if matched_kw:
            # Budget check: if budget falls within typical range, increase confidence score
            if hps_budget > 0:
                if cat_data["min_budget"] <= hps_budget <= cat_data["max_budget"]:
                    score += 25
                elif hps_budget < cat_data["min_budget"]:
                    score += 5

            # Context boost
            if any(term in text_clean for term in ["laboratorium", "lab", "pengujian", "uji mutu", "qc", "riset", "lingkungan hidup"]):
                score += 15

            # Boost specifically for actionable procurement types:
            # 1. Mini Kompetisi E-Katalog (Fast turnaround 1-3 days, high win-rate!)
            if "mini kompetisi" in text_clean:
                score += 25
            # 2. Pengadaan Langsung (< Rp 200jt, direct quotation, no auction)
            elif "pengadaan langsung" in text_clean or "pl-lab" in text_clean:
                score += 20
            # 3. Tender Cepat (short listing)
            elif "tender cepat" in text_clean:
                score += 15

            score = min(100, score)
            if score > highest_score:
                highest_score = score
                best_cat = cat_name
                best_models = cat_data["typical_models"]

    return best_cat, best_models, highest_score


def format_inaproc_alert_message(pkg: InaprocPackage) -> str:
    """Format a clean, high-priority WhatsApp alert for Ahnaf Ibrahim."""
    formatted_budget = f"Rp {int(pkg.hps_budget):,}".replace(",", ".") if pkg.hps_budget > 0 else "Belum tertera di pengumuman"
    models_str = ", ".join(pkg.matched_models[:2]) if pkg.matched_models else "Unit katalog AndisLab"

    # Dynamic headline and action recommendation based on procurement type
    proc_lower = f"{pkg.procurement_type} {pkg.package_name}".lower()
    if "mini kompetisi" in proc_lower:
        headline = "⚡ *[PELUANG MINI KOMPETISI E-KATALOG DITEMUKAN]*"
        action_note = (
            "Segera cek etalase E-Katalog LKPP v5/v6 untuk submit harga bersaing atau hubungi PPK sebelum batas waktu. "
            "1.140+ item alat & instrumen ready stock gudang Jabodetabek tanpa indent!"
        )
    elif "pengadaan langsung" in proc_lower:
        headline = "🏷️ *[PELUANG PENGADAAN LANGSUNG (PL) DITEMUKAN]*"
        action_note = (
            "Siapkan SPH resmi ber-KOP AndisLab (nilai < Rp 200 Juta) dan hubungi Pejabat Pengadaan instansi. "
            "Dokumen SPJ, BAST & e-Faktur tuntas tahun anggaran 2026."
        )
    else:
        headline = "🔔 *[PELUANG TENDER LPSE / INAPROC DITEMUKAN]*"
        action_note = (
            "Cek persyaratan kualifikasi LPSE & jadwal aanwijzing. "
            "Unit fisik bergaransi 12 bulan siap supply dan uji fungsi Jabodetabek/Nasional."
        )

    return (
        f"{headline}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 *Instansi:* {pkg.institution}\n"
        f"📦 *Paket:* {pkg.package_name}\n"
        f"💰 *Pagu / HPS:* {formatted_budget}\n"
        f"📍 *Lokasi:* {pkg.location}\n"
        f"🏷️ *Metode:* {pkg.procurement_type}\n"
        f"⏰ *Batas Waktu:* {pkg.deadline}\n"
        f"🎯 *Kategori Cocok:* {pkg.category_matched.upper()} (Skor {pkg.match_score}%)\n"
        f"📦 *Rekomendasi Ready Stock:* {models_str}\n"
        f"🔗 *Tautan:* {pkg.source_url}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 *Tindakan Ahnaf:* {action_note} 🙏"
    )


class InaprocWatcher:
    """Autonomous agent monitoring procurement packages from INAPROC & LPSE."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = data_dir or (PROJECT_ROOT / "data" / "inaproc")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "inaproc_packages.json"

    def load_known_packages(self) -> dict[str, dict]:
        if not self.db_path.exists():
            return {}
        try:
            return json.loads(self.db_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Failed to read inaproc db: {e}")
            return {}

    def save_packages(self, packages: dict[str, dict]) -> None:
        try:
            self.db_path.write_text(json.dumps(packages, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to save inaproc db: {e}")

    def filter_and_record_packages(self, raw_packages: list[dict[str, Any]]) -> list[InaprocPackage]:
        """Process incoming raw tender/e-katalog items and return newly discovered high-priority matches."""
        known = self.load_known_packages()
        new_high_matches: list[InaprocPackage] = []

        for raw in raw_packages:
            pid = str(raw.get("id") or raw.get("package_id") or "")
            if not pid or pid in known:
                continue

            name = raw.get("name") or raw.get("package_name") or ""
            budget = float(raw.get("budget") or raw.get("hps_budget") or 0.0)
            institution = raw.get("institution") or raw.get("instansi") or "Instansi Pemerintah"
            location = raw.get("location") or raw.get("lokasi") or "Jabodetabek / Nasional"
            proc_type = raw.get("procurement_type") or "e-Purchasing INAPROC"
            url = raw.get("url") or "https://katalog.inaproc.id"
            deadline = raw.get("deadline") or date.today().isoformat()

            cat, models, score = match_package_to_andislab(name, budget, proc_type)
            if score >= 40:  # Confident match with AndisLab inventory
                pkg = InaprocPackage(
                    package_id=pid,
                    package_name=name,
                    institution=institution,
                    hps_budget=budget,
                    category_matched=cat,
                    matched_models=models,
                    match_score=score,
                    procurement_type=proc_type,
                    location=location,
                    source_url=url,
                    deadline=deadline,
                    status="new",
                )
                known[pid] = asdict(pkg)
                new_high_matches.append(pkg)

        self.save_packages(known)
        return new_high_matches

    def load_activity(self) -> dict[str, Any]:
        """Load last scan status and metrics."""
        act_path = self.data_dir / "inaproc_activity.json"
        if not act_path.exists():
            return {}
        try:
            return json.loads(act_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _record_scan_activity(self, items_found: int, packages_parsed: int, new_matches: int) -> None:
        """Update last scan date and counts."""
        act_path = self.data_dir / "inaproc_activity.json"
        act = self.load_activity()
        now_iso = datetime.now().isoformat()
        act["last_scan_time"] = now_iso
        act["last_scan_date"] = date.today().isoformat()
        act["last_items_found"] = items_found
        act["last_packages_parsed"] = packages_parsed
        act["last_new_matches"] = new_matches
        act["total_scans"] = act.get("total_scans", 0) + 1
        try:
            act_path.write_text(json.dumps(act, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Could not write inaproc activity: {e}")

    async def _search_web(self, query: str, env: Any) -> list[dict[str, str]]:
        """Search using Serper API with Tavily fallback."""
        results: list[dict[str, str]] = []
        serper_key = getattr(env, "serper_api_key", "") or ""
        tavily_key = getattr(env, "tavily_api_key", "") or ""

        if serper_key:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        "https://google.serper.dev/search",
                        json={"q": query, "num": 10},
                        headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        for item in data.get("organic", []) or []:
                            link = item.get("link", "")
                            title = item.get("title", "")
                            snippet = item.get("snippet", "")
                            if link and (title or snippet):
                                results.append({"title": title, "link": link, "snippet": snippet})
            except Exception as e:
                logger.warning(f"Serper search failed for '{query}': {e}")

        if not results and tavily_key:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        "https://api.tavily.com/search",
                        json={"api_key": tavily_key, "query": query, "max_results": 10},
                        headers={"Content-Type": "application/json"},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        for item in data.get("results", []) or []:
                            link = item.get("url", "")
                            title = item.get("title", "")
                            snippet = item.get("content", "")
                            if link and (title or snippet):
                                results.append({"title": title, "link": link, "snippet": snippet})
            except Exception as e:
                logger.warning(f"Tavily search failed for '{query}': {e}")

        return results

    async def _extract_packages_with_brain(self, search_results: list[dict[str, str]], brain: Any) -> list[dict[str, Any]]:
        """Use Brain (Gemini) to extract structured procurement packages from search snippets."""
        if not search_results:
            return []

        if brain:
            prompt = (
                "Kamu adalah asisten analisis data pengadaan pemerintah Indonesia (LPSE, SiRUP, INAPROC, e-Katalog LKPP).\n"
                "Tugasmu mengekstrak paket pengadaan/tender/mini kompetisi/pengadaan langsung dari hasil pencarian web berikut ke dalam format JSON.\n\n"
                f"Daftar Hasil Pencarian:\n{json.dumps(search_results[:15], indent=2, ensure_ascii=False)}\n\n"
                "Ekstrak setiap paket yang relevan dengan alat laboratorium / timbangan / autoclave / oven / lemari asam / biosafety cabinet / freezer lab / mikrobiologi / kimia / rumah sakit.\n"
                "Keluarkan HANYA JSON list valid dengan struktur tiap item:\n"
                "[\n"
                "  {\n"
                '    "id": "ID paket atau nomor unik di URL, jika tidak ada buat slug unik",\n'
                '    "name": "Nama lengkap paket pengadaan",\n'
                '    "institution": "Nama instansi pemerintah / dinas / rumah sakit / universitas",\n'
                '    "budget": 150000000.0,  // float nilai pagu atau HPS dalam rupiah (0 jika belum tertera)\n'
                '    "procurement_type": "Mini Kompetisi E-Katalog" ATAU "Pengadaan Langsung" ATAU "Tender Cepat" ATAU "Tender",\n'
                '    "location": "Kota / Kabupaten / Provinsi",\n'
                '    "deadline": "YYYY-MM-DD atau estimasi tanggal",\n'
                '    "url": "URL sumber paket"\n'
                "  }\n"
                "]\n"
            )
            try:
                resp = await brain.think(prompt, expect_json=True, agent="inaproc")
                clean_resp = resp.strip()
                if clean_resp.startswith("```json"):
                    clean_resp = clean_resp[7:]
                if clean_resp.startswith("```"):
                    clean_resp = clean_resp[3:]
                if clean_resp.endswith("```"):
                    clean_resp = clean_resp[:-3]
                parsed = json.loads(clean_resp.strip())
                if isinstance(parsed, list):
                    return parsed
                elif isinstance(parsed, dict) and "packages" in parsed:
                    return parsed["packages"]
            except Exception as e:
                logger.warning(f"Brain failed to extract inaproc packages: {e}")

        # Fallback heuristic parser if brain fails or not provided
        return self._heuristic_package_extraction(search_results)

    def _heuristic_package_extraction(self, search_results: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Fallback rule-based extractor if brain is unavailable."""
        packages = []
        for item in search_results:
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            url = item.get("link", "")
            combined = f"{title} {snippet}"

            # Check if mentions lab equipment
            if not any(k in combined.lower() for k in ["laboratorium", "lab", "timbangan analitik", "autoclave", "lemari asam", "alat peraga"]):
                continue

            # Determine type
            if "mini kompetisi" in combined.lower():
                ptype = "Mini Kompetisi E-Katalog"
            elif "pengadaan langsung" in combined.lower():
                ptype = "Pengadaan Langsung"
            elif "tender cepat" in combined.lower():
                ptype = "Tender Cepat"
            else:
                ptype = "Pengadaan LPSE"

            # Budget heuristic
            budget = 0.0
            budget_match = re.search(r"Rp\.?\s*([\d\.,]+)", snippet)
            if budget_match:
                try:
                    num_str = budget_match.group(1).replace(".", "").replace(",", ".")
                    budget = float(num_str)
                except ValueError:
                    budget = 0.0

            # Generate stable ID
            pid = hashlib.md5(url.encode()).hexdigest()[:12]
            url_match = re.search(r"/(\d{7,15})", url)
            if url_match:
                pid = url_match.group(1)

            packages.append({
                "id": pid,
                "name": title,
                "institution": "Instansi Pemerintah",
                "budget": budget,
                "procurement_type": ptype,
                "location": "Indonesia",
                "deadline": date.today().isoformat(),
                "url": url,
            })
        return packages

    async def scan_live_procurement(
        self,
        env: Any,
        brain: Any | None = None,
        max_queries: int = 4,
    ) -> list[InaprocPackage]:
        """Perform a focused live web scan for Mini Kompetisi, Pengadaan Langsung & Tender lab equipment."""
        queries = [
            'site:spse.inaproc.id "mini kompetisi" ("laboratorium" OR "alat lab" OR "timbangan" OR "autoclave" OR "mikrobiologi")',
            '"mini kompetisi" ("alat laboratorium" OR "alat lab" OR "instrumen laboratorium") e-katalog',
            'site:lpse.*.go.id "pengadaan langsung" laboratorium',
            'site:lpse.*.go.id "tender cepat" ("laboratorium" OR "alat lab")',
        ][:max_queries]

        seen_links = set()
        all_search_items = []

        for q in queries:
            items = await self._search_web(q, env)
            for it in items:
                link = it.get("link", "")
                if link and link not in seen_links:
                    seen_links.add(link)
                    all_search_items.append(it)

        logger.info(f"InaprocWatcher: Gathered {len(all_search_items)} raw web results across {len(queries)} queries.")
        if not all_search_items:
            return []

        raw_packages = await self._extract_packages_with_brain(all_search_items, brain)
        new_matches = self.filter_and_record_packages(raw_packages)

        # Record activity
        self._record_scan_activity(len(all_search_items), len(raw_packages), len(new_matches))

        return new_matches


if __name__ == "__main__":
    watcher = InaprocWatcher()
    sample = [
        {
            "id": "TND-2026-001",
            "name": "Pengadaan Timbangan Analitik Kapasitas 220g dan Moisture Analyzer Laboratorium",
            "institution": "Dinas Ketahanan Pangan & Pertanian Jawa Barat",
            "budget": 65000000,
            "location": "Bandung / Jawa Barat",
            "deadline": "2026-11-20",
            "procurement_type": "e-Purchasing INAPROC",
            "url": "https://katalog.inaproc.id/paket/TND-2026-001",
        },
        {
            "id": "TND-2026-002",
            "name": "Belanja Modal Pengadaan Autoclave Sterilisasi Uap 50 Liter Lab Mikrobiologi",
            "institution": "Universitas Negeri Jakarta (UNJ)",
            "budget": 85000000,
            "location": "Jakarta Timur",
            "deadline": "2026-11-25",
            "procurement_type": "Tender Cepat",
            "url": "https://lpse.unj.ac.id/eproc4/lelang/TND-2026-002",
        },
    ]
    matches = watcher.filter_and_record_packages(sample)
    print(f"Discovered {len(matches)} matching procurement opportunities!")
    for m in matches:
        print("\n" + format_inaproc_alert_message(m))

