"""
Mahoraga Multi-Segment Lab Universe Harvester (Tahap 2).

Discovers & enriches Indonesian laboratory buyers across ALL 4 balanced verticals
(NOT just government/INAPROC):
  1. Industri Swasta (QC/R&D): Farmasi, Kosmetik Maklon, F&B, Sawit/Agro, Tambang, Kimia
  2. Kesehatan & Rumah Sakit: RS Swasta, RSUD, Lab Klinik & Patologi
  3. Pendidikan & Riset: Universitas (FMIPA, Farmasi, Teknik Kimia, Kedokteran), SMK Analis
  4. Pemerintah & Pengadaan: SIRUP LKPP, Labkesda, BPOM, Balai Uji, PDAM

Strictly enforces the Indonesian End-User ICP Gate and Partner Exclusion list
(Batavia, Indotrading, CMSI, competitor lab vendors).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SQLITE_PATH = PROJECT_ROOT / "data" / "harvey.db"

PARTNER_OR_COMPETITOR_TERMS = (
    "batavia",
    "indotrading",
    "cahayatiara",
    "cmsi",
    "tokopedia",
    "shopee",
    "bukalapak",
    "blibli",
    "lazada",
    "alibaba",
    "indonetwork",
    "distributor alat lab",
    "jual alat laboratorium",
    "supplier alat lab",
    "andislab",
)

UNIVERSE_BLUEPRINTS: dict[str, dict[str, Any]] = {
    "industri_swasta": {
        "label": "Industri Swasta (QC/R&D)",
        "target_pics": ["QC Manager", "R&D Manager", "Kepala Laboratorium", "Purchasing Manager"],
        "typical_equipment": ["Oven", "pH Meter", "Viscometer", "Autoclave", "Spectrophotometer", "Moisture Analyzer"],
        "queries": [
            '"quality control" laboratorium pabrik farmasi kosmetik maklon indonesia kontak',
            'lowongan "qc lab" OR "analis kimia" pabrik makanan minuman jabodetabek karawang bekasi',
            'laboratorium kelapa sawit CPO "quality control" sumatera kalimantan kontak',
            'laboratorium uji kualitas air limbah industri kimia cikarang surabaya',
        ],
    },
    "kesehatan_rs": {
        "label": "Kesehatan & Rumah Sakit",
        "target_pics": ["Kepala Instalasi Laboratorium", "Penanggung Jawab Lab", "Bagian Pengadaan / Logistik RS"],
        "typical_equipment": ["Centrifuge", "Mikroskop", "Autoclave", "Incubator", "Biosafety Cabinet", "Pipette"],
        "queries": [
            '"instalasi laboratorium" rumah sakit swasta jakarta bandung surabaya kontak',
            'laboratorium klinik patologi akreditasi indonesia kontak email',
            'RSUD instalasi laboratorium patologi klinik pengadaan alat kesehatan',
        ],
    },
    "pendidikan_riset": {
        "label": "Pendidikan & Riset",
        "target_pics": ["Kepala Laboratorium", "Laboran", "Dekan / Kaprodi Kimia / Farmasi", "Unit Pengadaan"],
        "typical_equipment": ["Hotplate Stirrer", "Timbangan Analitik", "Oven", "Furnace", "Water Bath", "Glassware"],
        "queries": [
            'laboratorium kimia farmasi universitas indonesia kontak email laboran',
            'SMK analis kimia laboratorium praktikum indonesia kontak',
            'fakultas mipa teknik kimia "kepala laboratorium" .ac.id',
        ],
    },
    "pemerintah_pengadaan": {
        "label": "Pemerintah & Pengadaan",
        "target_pics": ["PPK (Pejabat Pembuat Komitmen)", "Kepala UPTD Labkesda", "Tim Pengadaan"],
        "typical_equipment": ["Spectrophotometer", "Incubator", "Laminar Air Flow", "Autoclave", "Turbidity Meter"],
        "queries": [
            'UPTD Labkesda laboratorium kesehatan daerah pengadaan alat kontak',
            'balai pengujian mutu barang lingkungan hidup laboratorium kontak',
            'PDAM laboratorium uji kualitas air minum kontak',
        ],
    },
}


def _is_clean_target_domain(url: str, title: str, snippet: str) -> bool:
    blob = f"{url} {title} {snippet}".lower()
    if any(bad in blob for bad in PARTNER_OR_COMPETITOR_TERMS):
        return False
    parsed = urlparse(url if url.startswith("http") else f"https://{url}")
    host = (parsed.netloc or "").lower()
    if not host:
        return False
    blocked_hosts = (
        "youtube.com",
        "facebook.com",
        "instagram.com",
        "tiktok.com",
        "wikipedia.org",
        "scribd.com",
        "academia.edu",
        "researchgate.net",
        "jobstreet.co.id",
        "glints.com",
        "kalibrr.com",
        "indeed.com",
    )
    if any(bh in host for bh in blocked_hosts):
        return False
    return True


def _extract_company_name(title: str, url: str) -> str:
    clean = re.split(r"\s*[\|\-–—:]\s*", title or "")[0].strip()
    if len(clean) >= 4 and len(clean) <= 80:
        return clean
    host = urlparse(url).netloc.replace("www.", "")
    return host.split(".")[0].title() if host else "Unknown Lab"


class UniverseHarvester:
    """Multi-segment Indonesian laboratory universe discovery & coverage tracker."""

    def __init__(self, sqlite_path: Path | str | None = None) -> None:
        self.sqlite_path = Path(sqlite_path) if sqlite_path else SQLITE_PATH

    def get_segment_blueprints(self) -> dict[str, dict[str, Any]]:
        return UNIVERSE_BLUEPRINTS

    async def search_ddg_html(self, query: str, limit: int = 8) -> list[dict[str, str]]:
        """Lightweight DuckDuckGo HTML search for Indonesian lab universe discovery."""
        url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
        }
        results: list[dict[str, str]] = []
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.post(url, data={"q": query, "kl": "id-id"}, headers=headers)
                if resp.status_code != 200:
                    return []
                html = resp.text
                # Parse result blocks cleanly without heavy dependencies
                links = re.findall(
                    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                    html,
                    flags=re.DOTALL | re.IGNORECASE,
                )
                snippets = re.findall(
                    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
                    html,
                    flags=re.DOTALL | re.IGNORECASE,
                )
                for idx, (href, raw_title) in enumerate(links[: limit * 2]):
                    title = re.sub(r"<[^>]+>", "", raw_title).strip()
                    snip = re.sub(r"<[^>]+>", "", snippets[idx]).strip() if idx < len(snippets) else ""
                    if not _is_clean_target_domain(href, title, snip):
                        continue
                    results.append({"url": href, "title": title, "snippet": snip})
                    if len(results) >= limit:
                        break
        except Exception as exc:
            logger.debug(f"[UniverseHarvester] DDG search failed for '{query}': {exc}")
        return results

    async def harvest_balanced_universe(
        self, segments: list[str] | None = None, max_per_segment: int = 5
    ) -> dict[str, Any]:
        """
        Harvest fresh Indonesian lab prospects across all 4 balanced verticals
        and store unique organizations into `companies` + `prospects`.
        """
        target_segments = segments or list(UNIVERSE_BLUEPRINTS.keys())
        inserted_by_seg: dict[str, int] = {s: 0 for s in target_segments}
        discovered_samples: list[dict[str, str]] = []

        conn = sqlite3.connect(str(self.sqlite_path))
        conn.row_factory = sqlite3.Row
        try:
            existing_names = {
                (r["company"] or "").strip().lower()
                for r in conn.execute("SELECT company FROM prospects WHERE company IS NOT NULL").fetchall()
            }

            for seg_key in target_segments:
                bp = UNIVERSE_BLUEPRINTS.get(seg_key)
                if not bp:
                    continue
                for q in bp["queries"]:
                    if inserted_by_seg[seg_key] >= max_per_segment:
                        break
                    hits = await self.search_ddg_html(q, limit=6)
                    for hit in hits:
                        if inserted_by_seg[seg_key] >= max_per_segment:
                            break
                        comp_name = _extract_company_name(hit["title"], hit["url"])
                        if comp_name.lower() in existing_names or len(comp_name) < 4:
                            continue
                        existing_names.add(comp_name.lower())

                        emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", hit["snippet"])
                        email = emails[0] if emails else ""

                        conn.execute(
                            """
                            INSERT INTO prospects (
                                id, first_name, last_name, email, company, industry,
                                source, source_url, status, score, personalization_notes
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', 60, ?)
                            """,
                            (
                                uuid.uuid4().hex[:12],
                                "Tim Lab / Purchasing",
                                "",
                                email,
                                comp_name,
                                bp["label"],
                                f"universe_{seg_key}",
                                hit["url"][:300],
                                f"[Segment: {bp['label']}] {hit['snippet'][:220]}",
                            ),
                        )
                        inserted_by_seg[seg_key] += 1
                        discovered_samples.append(
                            {
                                "segment": bp["label"],
                                "company": comp_name,
                                "url": hit["url"][:120],
                            }
                        )
            conn.commit()
        finally:
            conn.close()

        return {
            "inserted_by_segment": inserted_by_seg,
            "total_inserted": sum(inserted_by_seg.values()),
            "samples": discovered_samples[:12],
        }
