"""Autonomous Script to join the Sleman Rapid Test Mini-Competition at 12:00 WIB.

Target:
- Package: Pengadaan rapid tes kolesterol UPTD POAK Kab. Sleman Ulang 2
- ID: MC-01M743M2FWZB8AV3GCKQ9XA1DZ
- Budget: Rp 500.040.000,00
- Opens: Wednesday, 11 November 2026 @ 12:00 WIB
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
from pathlib import Path

import pytz
from playwright.async_api import async_playwright

from harvey.config import load_config, load_env
from harvey.integrations.moli_browser import get_moli_browser, is_moli_available
from harvey.integrations.whatsapp import WhatsAppEngine
from harvey.paths import PROJECT_ROOT
from harvey.state import StateManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("harvey.sleman_auto_join")

TARGET_URL = "https://penyedia.inaproc.id/mini-competition/public-competition/01M743M2FWZB8AV3GCKQ9XA1DZ?key=01M743M2H4FX1ERZGT4VS6V79Y"
SCREENSHOTS_DIR = PROJECT_ROOT / "data" / "inaproc" / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


async def execute_join_sleman() -> bool:
    """Execute opt-in for Sleman tender."""
    state = StateManager()
    await state.init_db()
    config = load_config()
    env = load_env()
    wa = WhatsAppEngine(state, config, env)

    logger.info("Sleman Auto-Join: Menyiapkan koneksi browser Moli...")
    success = False
    notes = ""

    async with async_playwright() as p:
        browser = None
        ctx = None
        try:
            if is_moli_available():
                browser, ctx = await get_moli_browser(p)
            else:
                profile_dir = PROJECT_ROOT / "data" / "inaproc_profile"
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    channel="chrome",
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--mute-audio"],
                )

            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

            logger.info(f"Sleman Auto-Join: Membuka {TARGET_URL}...")
            await page.goto(TARGET_URL, wait_until="networkidle", timeout=45000)
            await asyncio.sleep(2)

            # Check if button 'Ikuti Kompetisi' is ready
            btn_ikuti = await page.query_selector("button:has-text('Ikuti Kompetisi')")
            if not btn_ikuti:
                # Might already be joined or page not yet updated, try reload
                await page.reload(wait_until="networkidle")
                await asyncio.sleep(2)
                btn_ikuti = await page.query_selector("button:has-text('Ikuti Kompetisi')")

            if btn_ikuti:
                logger.info("Sleman Auto-Join: Tombol 'Ikuti Kompetisi' ditemukan. Mengklik via DOM evaluate...")
                try:
                    await page.evaluate("(el) => el.click()", btn_ikuti)
                except Exception:
                    await btn_ikuti.click(force=True)
                await asyncio.sleep(2)

                confirm_btn = page.locator("#undefined-confirm-btn, button:has-text('Ya, Setuju'), button:has-text('Ya'), button:has-text('Konfirmasi')")
                if await confirm_btn.count() > 0:
                    logger.info("Sleman Auto-Join: Mengonfirmasi modal...")
                    try:
                        await page.evaluate("(el) => el.click()", await confirm_btn.first.element_handle())
                    except Exception:
                        await confirm_btn.first.click(force=True)
                    await asyncio.sleep(4)

                success = True
                notes = "Berhasil ikut kompetisi Sleman Rp 500 Juta."
                try:
                    shot_path = SCREENSHOTS_DIR / "sleman_joined_success.png"
                    await page.screenshot(path=str(shot_path))
                    logger.info(f"Sleman Auto-Join: Screenshot tersimpan di {shot_path}")
                except Exception as se:
                    logger.debug(f"Screenshot skipped (Moli headless mode): {se}")
            else:
                body_text = await page.inner_text("body")
                body_lower = body_text.lower()
                if any(k in body_lower for k in ["kompetisi saya", "penawaran diajukan", "batalkan keikutsertaan", "kirim penawaran", "terdaftar"]):
                    success = True
                    notes = "Kompetisi sudah terdaftar/diikuti sebelumnya."
                else:
                    notes = "Tombol 'Ikuti Kompetisi' belum aktif atau jadwal belum dibuka."
        except Exception as e:
            logger.error(f"Sleman Auto-Join: Gagal: {e}")
            notes = f"Error: {e}"
        finally:
            if browser:
                await browser.close()
            elif ctx:
                await ctx.close()

    # WhatsApp Alert to Operator
    status_icon = "🎉 *[SUKSES IKUTI KOMPETISI SLEMAN]*" if success else "⚠️ *[STATUS AUTO-JOIN SLEMAN]*"
    alert_msg = (
        f"{status_icon}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 *Instansi:* Dinas Kesehatan Kab. Sleman\n"
        f"📦 *Paket:* Pengadaan rapid tes kolesterol UPTD POAK Kab. Sleman Ulang 2\n"
        f"💰 *Pagu / HPS:* Rp 500.040.000 (Setengah Miliar)\n"
        f"📋 *Item:* 27.780 pcs EMTECH MULTIMAX Cholesterol Test Strip\n"
        f"🏷️ *Rekomendasi Tawaran:* Rp 16.500 / pcs (Total Rp 458.370.000)\n"
        f"📊 *Keterangan:* {notes}\n"
        f"🔗 *Tautan:* {TARGET_URL}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_Laporan otomatis dari Harvey INAPROC Agent._ 🙏"
    )
    try:
        await wa.send_inaproc_alert(alert_msg)
    except Exception as e:
        logger.warning(f"Gagal kirim alert WA: {e}")

    return success


async def run_scheduled() -> bool:
    """Wait until 12:00:00 WIB today, then execute opt-in with retries."""
    tz = pytz.timezone("Asia/Jakarta")
    now = datetime.now(tz)
    target_time = now.replace(hour=12, minute=0, second=0, microsecond=0)

    if now < target_time:
        wait_seconds = (target_time - now).total_seconds()
        logger.info(
            f"Auto-Join Scheduled: Target {target_time.strftime('%Y-%m-%d %H:%M:%S WIB')}. "
            f"Menunggu {int(wait_seconds)} detik (~{wait_seconds / 60:.1f} menit)..."
        )
        # Sleep in chunks to allow heartbeat logging
        while True:
            remaining = (target_time - datetime.now(tz)).total_seconds()
            if remaining <= 5:
                break
            sleep_step = min(remaining - 5, 300)  # log every 5 mins
            await asyncio.sleep(sleep_step)
            now_curr = datetime.now(tz)
            rem = (target_time - now_curr).total_seconds()
            logger.info(f"Countdown Sleman Tender: {int(rem)} detik tersisa ({rem / 60:.1f} menit)...")

        # Precision wait for the final seconds
        while datetime.now(tz) < target_time:
            await asyncio.sleep(0.5)

    logger.info("Waktu target 12:00:00 WIB TERCAPAI! Memulai eksekusi opt-in Sleman...")

    # Retry up to 5 times (every 15s) in case INAPROC server flips open status with a slight delay
    for attempt in range(1, 6):
        logger.info(f"Percobaan ke-{attempt} opt-in Sleman...")
        success = await execute_join_sleman()
        if success:
            logger.info("Auto-join Sleman BERHASIL pada percobaan ke-%d!", attempt)
            return True
        logger.warning("Percobaan %d belum berhasil / belum terbuka. Coba lagi dalam 15 detik...", attempt)
        await asyncio.sleep(15)

    return False


if __name__ == "__main__":
    import sys
    if "--now" in sys.argv:
        asyncio.run(execute_join_sleman())
    else:
        asyncio.run(run_scheduled())
