"""Parlant-style Behavioral Guideline & Interaction Control Engine for Harvey.

Adopts Parlant's interaction control schema (Condition -> Action) directly into
Harvey's native Brain and Handler pipelines. Eliminates external server overhead
while guaranteeing strict brand alignment, margin defense, and audit compliance.
"""

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from typing import Any

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.guidelines")
GUIDELINES_FILE = PROJECT_ROOT / "data" / "guidelines.json"


@dataclass
class Guideline:
    id: str
    name: str
    condition: str   # When ...
    action: str      # Then ...
    category: str    # "sales" | "procurement" | "pricing" | "compliance"
    tools: list[str] = field(default_factory=list)
    enabled: bool = True
    priority: int = 10


DEFAULT_GUIDELINES: list[dict[str, Any]] = [
    {
        "id": "G-01-STOCK-READY",
        "name": "Ketersediaan Stok Ready Gudang",
        "condition": "Pelanggan menanyakan ketersediaan alat, reagen, atau bahan laboratorium umum.",
        "action": "Konfirmasi status ready stock, tawarkan free delivery area Bandung/Cimahi/Jabodetabek, dan jelaskan kesiapan uji fungsi teknisi serta dokumen resmi SPJ/BAST.",
        "category": "sales",
        "tools": ["search_inventory"],
        "enabled": True,
        "priority": 10,
    },
    {
        "id": "G-02-SOURCING-BY-REQUEST",
        "name": "Pengadaan Khusus / By Request (Indent)",
        "condition": "Pelanggan menanyakan alat, instrumen khusus, atau nomor katalog spesifik yang TIDAK TERSEDIA di ready stock gudang.",
        "action": "Jelaskan dengan ramah bahwa barang berstatus Special Order / By Request dari prinsipal resmi. Berikan estimasi waktu inden pengiriman, dan pastikan pengadaan resmi melalui PT Andis Sentral Laboratorium mencakup garansi 12 bulan serta berkas SPJ/BAST siap audit 2026.",
        "category": "procurement",
        "tools": ["sourcing_by_request"],
        "enabled": True,
        "priority": 10,
    },
    {
        "id": "G-03-INSTITUTION-BUDGET",
        "name": "Administrasi Anggaran Kampus & Dinas",
        "condition": "Pelanggan berasal dari universitas (kampus/dosen/LPPM), rumah sakit daerah, atau dinas pemerintah.",
        "action": "Tawarkan skema pembayaran fleksibel Tempo Instansi (pelunasan setelah BAST ditandatangani) dan garansi penerbitan e-Faktur Pajak serta berkas SPJ lengkap 1x24 jam untuk kelancaran pelaporan audit anggaran.",
        "category": "compliance",
        "tools": [],
        "enabled": True,
        "priority": 20,
    },
    {
        "id": "G-04-MARGIN-PROTECTION",
        "name": "Proteksi Margin & Penanganan Minta Diskon",
        "condition": "Pelanggan meminta potongan harga, diskon khusus, atau membandingkan dengan harga pasar murah.",
        "action": "Pertahankan margin resmi dengan sopan. Jelaskan bahwa harga AndisLab sudah meng-cover kalibrasi, uji fungsi teknisi langsung di lab, garansi 12 bulan, dan legalitas BAST audit. Tawarkan free ongkir atau tempo pembayaran alih-alih menurunkan harga unit.",
        "category": "pricing",
        "tools": [],
        "enabled": True,
        "priority": 15,
    },
    {
        "id": "G-05-SPH-ESCALATION",
        "name": "Penerbitan Surat Penawaran Harga (SPH)",
        "condition": "Pelanggan meminta surat penawaran tertulis, mengajukan pengadaan formal, atau menyetujui perkiraan harga.",
        "action": "Kirimkan template pengumpulan data formal: 'Bapak/Ibu mohon dibantu untuk isi list data untuk di penawaran:\nNama:\nJabatan:\nNomor telfon:\nEmail:\nPT/Perusahaan:\n\nTerimakasih, setelah Bapak/Ibu isi kami akan langsung buatkan quotation formnya.'",
        "category": "sales",
        "tools": ["create_quote"],
        "enabled": True,
        "priority": 25,
    },
]


class GuidelineEngine:
    """Manages and dynamically matches Parlant-style behavioral guidelines."""

    def __init__(self, storage_path: Path | None = None):
        self.storage_path = storage_path or GUIDELINES_FILE
        self._guidelines: list[Guideline] = []
        self._load()

    def _load(self) -> None:
        if self.storage_path.exists():
            try:
                data = json.loads(self.storage_path.read_text(encoding="utf-8"))
                self._guidelines = [Guideline(**g) for g in data]
                return
            except Exception as e:
                logger.warning(f"Guidelines: Gagal membaca {self.storage_path}: {e}")

        # Fallback to default
        self._guidelines = [Guideline(**g) for g in DEFAULT_GUIDELINES]
        self._save()

    def _save(self) -> None:
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            dicts = [g.__dict__ for g in self._guidelines]
            self.storage_path.write_text(json.dumps(dicts, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Guidelines: Gagal menyimpan {self.storage_path}: {e}")

    def list_guidelines(self, enabled_only: bool = True) -> list[Guideline]:
        if enabled_only:
            return [g for g in self._guidelines if g.enabled]
        return list(self._guidelines)

    def match_guidelines(self, message: str, customer_context: dict[str, Any] | None = None) -> list[Guideline]:
        """Dynamically select applicable guidelines based on keywords, role, and intent."""
        msg = message.lower()
        ctx = customer_context or {}
        matched = []

        is_campus_or_gov = any(k in msg for k in ["kampus", "univ", "lppm", "dosen", "rsud", "dinas", "pemerintah", "audit", "spj", "bast"]) or \
                           any(k in str(ctx.get("company", "")).lower() for k in ["universitas", "institut", "politeknik", "rsud", "dinas", "balai"])

        is_discount = any(k in msg for k in ["diskon", "potongan", "kurang", "nego", "kemahalan", "harga pas", "bisa kurang"])

        is_quote_req = any(k in msg for k in ["sph", "penawaran", "surat penawaran", "proforma", "buatkan penawaran", "kirim sph", "minta penawaran"])

        is_product_ask = any(k in msg for k in ["ready", "stok", "ada gak", "punya gak", "harga berapa", "spek", "jual", "katalog"])

        for g in self.list_guidelines(enabled_only=True):
            if g.id == "G-03-INSTITUTION-BUDGET" and is_campus_or_gov:
                matched.append(g)
            elif g.id == "G-04-MARGIN-PROTECTION" and is_discount:
                matched.append(g)
            elif g.id == "G-05-SPH-ESCALATION" and is_quote_req:
                matched.append(g)
            elif g.id in ("G-01-STOCK-READY", "G-02-SOURCING-BY-REQUEST") and is_product_ask:
                matched.append(g)

        # If none specifically triggered, supply standard sales and compliance guidelines
        if not matched:
            matched = [g for g in self.list_guidelines(enabled_only=True) if g.id in ("G-01-STOCK-READY", "G-02-SOURCING-BY-REQUEST", "G-03-INSTITUTION-BUDGET")]

        # Sort by priority descending
        matched.sort(key=lambda g: g.priority, reverse=True)
        return matched

    def format_prompt_block(self, matched_guidelines: list[Guideline]) -> str:
        """Format matched guidelines into an authoritative instruction block for Gemini."""
        if not matched_guidelines:
            return ""

        lines = [
            "### ATURAN PERILAKU INTERAKSI KONSULTATIF (PARLANT BEHAVIORAL GUIDELINES):",
            "Ikuti pedoman 'When [Kondisi] Then [Tindakan]' berikut secara ketat agar reputasi dan margin perusahaan terlindungi:"
        ]
        for i, g in enumerate(matched_guidelines, 1):
            lines.append(f"{i}. [{g.name}]")
            lines.append(f"   - KONDISI (When): {g.condition}")
            lines.append(f"   - TINDAKAN (Then): {g.action}")
            if g.tools:
                lines.append(f"   - TOOL TERKAIT: {', '.join(g.tools)}")

        return "\n".join(lines)

