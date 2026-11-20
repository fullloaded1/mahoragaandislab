"""Vendor Directory & INAPROC Real Chrome Co-Pilot for Harvey.

Manages laboratory equipment principals/distributors, WhatsApp RFQ generation,
and 1-click execution for INAPROC E-Katalog submissions via real Google Chrome.
"""

import json
import logging
import os
import subprocess
import webbrowser
from pathlib import Path
from typing import Any

import pyperclip

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.vendor_directory")
VENDORS_FILE = PROJECT_ROOT / "data" / "vendors.json"


def load_vendors() -> list[dict[str, Any]]:
    """Load verified laboratory principals and distributors."""
    if not VENDORS_FILE.exists():
        return []
    try:
        data = json.loads(VENDORS_FILE.read_text(encoding="utf-8"))
        return data.get("vendors", [])
    except Exception as e:
        logger.warning(f"Error loading vendors from {VENDORS_FILE}: {e}")
        return []


def find_vendor_for_item(item_name: str, brand: str = "") -> dict[str, Any] | None:
    """Find the best matching principal/distributor for an item."""
    vendors = load_vendors()
    if not vendors:
        return None

    query = f"{brand or ''} {item_name or ''}".lower()

    # 1. Exact brand match
    if brand:
        b_low = brand.strip().lower()
        for v in vendors:
            for vb in v.get("brands", []):
                if vb.lower() in b_low or b_low in vb.lower():
                    return v

    # 2. Category / keyword match
    for v in vendors:
        for cat in v.get("categories", []):
            if cat.lower() in query:
                return v

    # 3. Vendor name match
    for v in vendors:
        if v.get("name", "").lower() in query:
            return v

    return None


def build_whatsapp_rfq(
    vendor: dict[str, Any],
    package_name: str,
    item_name: str,
    brand: str = "",
    model: str = "",
    qty: int = 1,
) -> str:
    """Generate a formal, ready-to-send B2B WhatsApp RFQ to a principal."""
    v_name = vendor.get("name", "Bapak/Ibu Tim Sales")
    item_desc = f"{item_name}"
    if brand or model:
        item_desc += f" ({brand} {model})".strip()

    return (
        f"Selamat siang Bapak/Ibu Tim Sales *{v_name}*,\n\n"
        f"Perkenalkan kami dari *PT Andis Sentral Laboratorium (AndisLab)*, distributor penyedia alat laboratorium di E-Katalog LKPP.\n\n"
        f"Saat ini kami sedang menyiapkan penawaran pengadaan Mini-Kompetisi E-Katalog untuk paket:\n"
        f"📦 *{package_name}*\n\n"
        f"Kebutuhan unit:\n"
        f"• Nama Barang: *{item_desc}*\n"
        f"• Jumlah: *{qty} unit/set*\n\n"
        f"Mohon bantuan informasi:\n"
        f"1. Ketersediaan stok / estimasi lead time pengiriman\n"
        f"2. Surat Dukungan Pabrik/Distributor Resmi untuk tender LKPP\n"
        f"3. Harga Net Rekanan / Distributor Resmi (Exclude PPN)\n\n"
        f"Terima kasih banyak atas kerjasamanya. 🙏\n\n"
        f"—\n"
        f"Ahnaf Ibrahim\n"
        f"Divisi Pengadaan & Tender LKPP\n"
        f"PT Andis Sentral Laboratorium\n"
        f"WhatsApp / Telp: 0819-9157-5096\n"
        f"Kantor: (021) 3874-0154"
    )


def open_inaproc_in_chrome(
    package_url: str,
    bid_price: float,
    pdf_path: str | Path = "",
) -> dict[str, Any]:
    """Co-Pilot Mode: Opens tender package in real Google Chrome, copies bid price

    to clipboard, and reveals the SPH PDF in Windows Explorer.
    100% bypasses Cloudflare WAF, totally legal, and takes only 10 seconds.
    """
    res = {
        "status": "ok",
        "price_copied": False,
        "browser_opened": False,
        "explorer_opened": False,
        "bid_price": bid_price,
    }

    # 1. Copy price (plain integer) to clipboard
    try:
        int_price = int(round(bid_price))
        pyperclip.copy(str(int_price))
        res["price_copied"] = True
        logger.info(f"INAPROC Co-Pilot: Harga Rp {int_price:,} disalin ke clipboard.")
    except Exception as e:
        logger.warning(f"INAPROC Co-Pilot: Gagal salin ke clipboard: {e}")

    # 2. Open URL in real Chrome
    try:
        opened = False
        # Try locating chrome.exe on Windows
        chrome_paths = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
        for cp in chrome_paths:
            if os.path.exists(cp):
                subprocess.Popen([cp, package_url])
                opened = True
                break
        if not opened:
            webbrowser.open(package_url)
        res["browser_opened"] = True
    except Exception as e:
        logger.warning(f"INAPROC Co-Pilot: Gagal membuka browser: {e}")

    # 3. Highlight PDF in Windows Explorer
    if pdf_path:
        p = Path(pdf_path).resolve()
        if p.exists():
            try:
                subprocess.Popen(f'explorer /select,"{p}"')
                res["explorer_opened"] = True
            except Exception as e:
                logger.warning(f"INAPROC Co-Pilot: Gagal membuka file explorer: {e}")

    return res

