"""Moli Headless Browser Integration for Harvey.

Connects to Moli (https://github.com/lexmount/moli) — a lightweight, structure-first
headless browser engine written in Rust that consumes ~20-50MB RAM compared to Chrome's 1.5GB+.
Communicates via Chrome DevTools Protocol (CDP) on localhost:9222.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.moli")

MOLI_BIN_PATH = Path(os.environ.get("LOCALAPPDATA", "")) / "Moli" / "bin" / "moli.exe"
MOLI_CDP_PORT = 9222
MOLI_CDP_URL = f"http://127.0.0.1:{MOLI_CDP_PORT}"
COOKIES_FILE = PROJECT_ROOT / "data" / "inaproc" / "inaproc_cookies.json"


def is_moli_available() -> bool:
    """Check if Moli executable exists on system."""
    if MOLI_BIN_PATH.exists():
        return True
    import shutil
    return bool(shutil.which("moli"))


async def is_moli_running() -> bool:
    """Check if Moli CDP server is responding on port 9222."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            resp = await client.get(f"{MOLI_CDP_URL}/json/version")
            return resp.status_code == 200
    except Exception:
        return False


async def ensure_moli_daemon() -> bool:
    """Ensure Moli serve daemon is running on port 9222."""
    if await is_moli_running():
        return True

    bin_path = str(MOLI_BIN_PATH) if MOLI_BIN_PATH.exists() else "moli"
    logger.info("Moli: Memulai Moli CDP server daemon di port 9222...")
    try:
        # Launch non-blocking daemon process
        subprocess.Popen(
            [bin_path, "serve", "--port", str(MOLI_CDP_PORT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        for _ in range(15):
            await asyncio.sleep(0.5)
            if await is_moli_running():
                logger.info("Moli: Server siap di 127.0.0.1:9222.")
                return True
    except Exception as e:
        logger.warning(f"Moli: Gagal menjalankan daemon: {e}")

    return False


async def get_moli_browser(playwright_instance) -> tuple[Browser, BrowserContext]:
    """Connect to Moli via Playwright CDP and return (browser, context).
    Automatically injects saved INAPROC session cookies.
    """
    await ensure_moli_daemon()
    browser = await playwright_instance.chromium.connect_over_cdp(MOLI_CDP_URL)
    context = browser.contexts[0] if browser.contexts else await browser.new_context(
        viewport={"width": 1280, "height": 850},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36",
    )

    if COOKIES_FILE.exists():
        try:
            cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
            await context.add_cookies(cookies)
        except Exception as e:
            logger.warning(f"Moli: Gagal menginjeksi cookies: {e}")

    return browser, context

