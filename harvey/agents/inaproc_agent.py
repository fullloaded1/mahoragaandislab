"""Dedicated Agent for INAPROC E-Katalog v6 & Mini Kompetisi Co-Pilot.

Manages persistent authenticated sessions to E-Katalog v6 (katalog.inaproc.id),
monitors invited/open Mini Kompetisi, matches requirements against AndisLab's
1,100+ inventory catalog items, enforces minimum 20% margin guardrail, and
executes automated/co-pilot bid submissions.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
from pathlib import Path
import re
from typing import Any
import urllib.parse

from harvey.brain import Brain
from harvey.config import EnvConfig, HarveyConfig
from harvey.paths import PROJECT_ROOT
from harvey.state import StateManager
from harvey.integrations.spec_bank import SpecBank

logger = logging.getLogger("harvey.inaproc_agent")

DEFAULT_SEARCH_KEYWORDS = [
    "Laboratorium",
    "Alat Lab",
    "Reagen",
    "Rapid Test",
    "Timbangan",
    "Oven",
    "Inkubator",
    "Centrifuge",
    "Biosafety Cabinet",
    "Autoclave",
    "Kulkas Lab",
]

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


class InaprocAgent:
    """Autonomous and Co-Pilot agent for INAPROC E-Katalog v6 Mini Kompetisi."""

    def __init__(
        self,
        state: StateManager,
        config: HarveyConfig,
        env: EnvConfig,
        brain: Brain | None = None,
        min_margin: float = 0.20,
    ):
        self.state = state
        self.config = config
        self.env = env
        self.brain = brain
        self.min_margin = min_margin  # Enforce 20% minimum margin above HPP

        self.portal_url = "https://penyedia.inaproc.id/mini-competition/public-competition"
        self.portal_version = "v6_penyedia"

        self.data_dir = PROJECT_ROOT / "data" / "inaproc"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir = PROJECT_ROOT / "data" / "inaproc_profile"
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir = self.data_dir / "screenshots"
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.session_file = self.data_dir / "session_info.json"

    def is_profile_present(self) -> bool:
        """Check if persistent browser profile exists and has saved session files."""
        if not self.profile_dir.exists():
            return False
        # Profile directory should contain Default or cookies/Storage
        default_dir = self.profile_dir / "Default"
        return default_dir.exists() and any(default_dir.iterdir())

    def load_session_info(self) -> dict[str, Any]:
        """Read session metadata file."""
        if not self.session_file.exists():
            return {
                "logged_in": False,
                "portal": self.portal_version,
                "last_login": "",
                "status_text": "Belum pernah login",
            }
        try:
            return json.loads(self.session_file.read_text(encoding="utf-8"))
        except Exception:
            return {"logged_in": False, "portal": self.portal_version, "last_login": "", "status_text": "Error membaca sesi"}

    def save_session_info(self, data: dict[str, Any]) -> None:
        """Save session metadata file."""
        try:
            self.session_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"InaprocAgent: Gagal menyimpan info sesi: {e}")

    async def interactive_login(self) -> bool:
        """Open visible Google Chrome window for operator to log in once to E-Katalog v6.
        Session and cookies are persisted in data/inaproc_profile/.
        """
        from playwright.async_api import async_playwright

        print("\n  " + "=" * 62)
        print("  INAPROC E-KATALOG V6 LOGIN — AKUN PENYEDIA ANDISLAB")
        print("  " + "=" * 62)
        print("  Jendela Google Chrome sedang dibuka...")
        print("  1. Masuk ke akun Penyedia PT Andis Sentral Laboratorium")
        print("     (masukkan akun INAPROC / SSO LKPP dan verifikasi jika diminta).")
        print("  2. Setelah halaman utama Dashboard Penyedia E-Katalog v6 muncul,")
        print("     tunggu beberapa saat — sesi akan tersimpan secara otomatis!\n")

        async with async_playwright() as p:
            ctx = None
            try:
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    channel="chrome",
                    headless=False,
                    args=CHROME_SAFE_ARGS,
                    viewport={"width": 1280, "height": 850},
                )
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(self.portal_url, wait_until="domcontentloaded", timeout=60_000)

                # Poll up to 5 minutes for login indicators
                logged_in = False
                for _ in range(60):
                    await asyncio.sleep(5)
                    url = page.url.lower()
                    # Check URL and common elements in INAPROC v6 for logged in state
                    body = ((await page.inner_text("body")) or "").lower()

                    is_dashboard = any(x in url for x in ["/dashboard", "/penyedia", "/paket", "/kompetisi", "/beranda"])
                    has_profile_menu = (
                        "keluar" in body or "logout" in body or "profil" in body
                        or bool(await page.query_selector("a[href*='logout'], button[id*='user'], div[class*='user-menu']"))
                    )

                    if is_dashboard or has_profile_menu:
                        logged_in = True
                        break

                if logged_in:
                    info = {
                        "logged_in": True,
                        "portal": self.portal_version,
                        "last_login": datetime.now().isoformat(),
                        "status_text": "Sesi Aktif (E-Katalog v6)",
                    }
                    self.save_session_info(info)
                    print("\n  [SUKSES] Sesi login E-Katalog v6 berhasil disimpan permanen!")
                    print("  Agent sekarang dapat memantau dan submit Mini Kompetisi secara otonom.\n")
                    return True
                else:
                    print("\n  [TIMEOUT] Batas waktu login (5 menit) berakhir sebelum login terdeteksi.\n")
                    return False
            except Exception as e:
                logger.error(f"InaprocAgent: Error saat interactive login: {e}")
                print(f"\n  [ERROR] Gagal membuka browser login: {e}\n")
                return False
            finally:
                if ctx:
                    try:
                        await ctx.close()
                    except Exception:
                        pass

    async def check_session_status(self) -> dict[str, Any]:
        """Verify whether the INAPROC v6 session is currently authenticated."""
        if not self.is_profile_present():
            return {
                "logged_in": False,
                "portal": self.portal_version,
                "last_login": "",
                "status_text": "Belum Login (Profil Browser Kosong)",
            }

        info = self.load_session_info()
        return info

    async def match_product_and_pricing(
        self,
        package_name: str,
        hps_budget: float = 0.0,
        qty: int = 1,
    ) -> dict[str, Any] | None:
        """Find best matching inventory product and calculate price respecting 20% margin guardrail."""
        name_clean = package_name.lower()
        qty = max(qty, 1)

        # Keyword mapping for quick categorisation
        category_search_terms = {
            "timbangan": ["timbangan", "analytical balance", "precision balance", "neraca", "moisture"],
            "autoclave": ["autoclave", "autoklaf", "sterilisator", "steam sterilizer"],
            "oven": ["oven", "drying oven", "incubator", "inkubator", "furnace"],
            "safety_cabinet": ["biosafety", "bsc", "laminar", "fume hood", "lemari asam"],
            "freezer": ["freezer", "kulkas lab", "refrigerator", "cold storage"],
            "centrifuge": ["centrifuge", "sentrifus", "microcentrifuge"],
            "mixer": ["vortex", "mixer", "stirrer", "shaker"],
            "spectro": ["spektrofotometer", "spectrophotometer", "uv-vis"],
            "ph": ["ph meter", "multiparameter", "conductivity"],
        }

        search_tokens = []
        for cat, tokens in category_search_terms.items():
            if any(t in name_clean for t in tokens):
                search_tokens.extend(tokens)
                break

        if not search_tokens:
            search_tokens = [w for w in name_clean.split() if len(w) > 3][:3]

        best_item = None
        best_score = 0

        async with self.state._connect() as db:
            db.row_factory = None
            async with db.execute("SELECT id, name, brand, model, harga_beli, price, status_stok, specifications FROM inventory WHERE harga_beli > 0") as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    iid, iname, ibrand, imodel, ihpp, iprice, istatus, ispecs = r
                    comb_text = f"{iname} {ibrand} {imodel} {ispecs}".lower()
                    score = 0
                    for token in search_tokens:
                        if token in comb_text:
                            score += 25
                    if "ready stock" in str(istatus).lower():
                        score += 10
                    if score > best_score:
                        best_score = score
                        best_item = {
                            "id": iid,
                            "name": iname,
                            "brand": ibrand,
                            "model": imodel,
                            "harga_beli": float(ihpp or 0.0),
                            "price": float(iprice or 0.0),
                            "status_stok": istatus,
                        }

        if not best_item or best_score < 20:
            return None

        hpp = best_item["harga_beli"]
        floor_price = hpp * (1.0 + self.min_margin)  # HPP + 20% minimum margin

        if hps_budget > 0:
            per_unit_hps = hps_budget / qty
            if floor_price > per_unit_hps:
                return {
                    "eligible": False,
                    "reason": f"Floor price Rp {int(floor_price):,} (margin 20%) melebihi HPS Rp {int(per_unit_hps):,}",
                    "matched_item": best_item,
                    "cost_hpp": hpp,
                    "floor_price": floor_price,
                }
            # Target competitive price: 4% below HPS to maximize win chance, capped above floor_price
            target_price = round(min(per_unit_hps * 0.96, max(floor_price, hpp * 1.25)), 2)
        else:
            target_price = round(max(floor_price, best_item["price"]), 2)

        margin_pct = round(((target_price - hpp) / target_price) * 100, 2)

        return {
            "eligible": True,
            "matched_id": best_item["id"],
            "matched_name": best_item["name"],
            "brand": best_item["brand"],
            "model": best_item["model"],
            "cost_hpp": hpp,
            "target_price": target_price,
            "floor_price": floor_price,
            "margin_pct": margin_pct,
            "stock_status": best_item["status_stok"],
            "score": best_score,
        }

    async def submit_bid(self, bid_id: str, bid_data: dict[str, Any]) -> dict[str, Any]:
        """Co-Pilot Bid Preparation for Mini Kompetisi on E-Katalog v6.
        Calculates HPP + minimum 20% margin guardrail, marks as 'draft_ready',
        and alerts operator via WhatsApp with the direct portal link for safe manual submission.
        """
        pkg_name = bid_data.get("package_name", "Paket Mini Kompetisi")
        institution = bid_data.get("institution", "Instansi")
        url = bid_data.get("source_url", self.portal_url)
        target_price = float(bid_data.get("target_price", 0.0))
        hpp = float(bid_data.get("cost_hpp", 0.0))
        margin_pct = float(bid_data.get("margin_pct", 20.0))
        matched_prod = bid_data.get("matched_product_name", "Produk AndisLab")

        logger.info(f"InaprocAgent [Co-Pilot]: Menyiapkan draf penawaran Mini Kompetisi {bid_id} ({pkg_name})...")

        new_status = "draft_ready"
        notes = (
            "Co-Pilot siap: harga & margin 20% telah dihitung. "
            "Silakan buka link portal di browser Anda untuk klik 'Ikuti Kompetisi', pilih produk toko, dan kirim penawaran."
        )

        await self.state.update_inaproc_bid_status(
            bid_id=bid_id,
            status=new_status,
            notes=notes,
            submitted_at=None,
        )

        # Send WhatsApp Co-Pilot Brief to operator
        await self._send_bid_notification(
            status=new_status,
            pkg_name=pkg_name,
            institution=institution,
            matched_prod=matched_prod,
            hpp=hpp,
            target_price=target_price,
            margin_pct=margin_pct,
            hps_budget=float(bid_data.get("hps_budget", 0.0)),
            deadline=bid_data.get("deadline", "Segera"),
            url=url,
        )

        return {"status": new_status, "notes": notes}

    def _parse_card_container(self, text: str, links: list[str], keyword: str = "") -> dict[str, Any] | None:
        """Parse raw text and links from an INAPROC mini-competition card container."""
        if not text or ("Penawaran" not in text and "HPS" not in text):
            return None

        lines = [l.strip() for l in text.split("\n") if l.strip()]

        # 1. Package ID (ULID)
        ulid_match = re.search(r"\b([0-9A-HJ-KM-NP-TV-Z]{26})\b", text)
        pkg_id = ulid_match.group(1) if ulid_match else ""

        # 2. Detail URL
        url = ""
        for l in links:
            if "/public-competition/" in l:
                url = l
                break
        if not pkg_id and url:
            m = re.search(r"/public-competition/([0-9A-HJ-KM-NP-TV-Z]{26})", url)
            if m:
                pkg_id = m.group(1)

        if not pkg_id:
            return None

        # 3. Dates
        date_match = re.search(r"Tanggal Penawaran:\s*([^\n\r]+)", text)
        dates = date_match.group(1).strip() if date_match else ""

        # 4. Countdown / Time Left
        time_left_match = re.search(r"(\d+\s*Hari[\s\S]*?\d+\s*Menit)", text)
        time_left = re.sub(r"\s+", " ", time_left_match.group(1)).strip() if time_left_match else ""

        # 5. Location
        location = "Indonesia"
        if "Lokasi" in lines:
            idx = lines.index("Lokasi")
            if idx + 1 < len(lines):
                location = lines[idx + 1]

        # 6. Institution
        institution = "Instansi Pemerintah"
        if "Instansi" in lines:
            idx = lines.index("Instansi")
            if idx + 1 < len(lines):
                institution = lines[idx + 1]

        # 7. Jenis Penawaran
        bid_type = "Itemized" if "itemized" in text.lower() else "Non Itemized"
        if "Jenis Penawaran" in lines:
            idx = lines.index("Jenis Penawaran")
            if idx + 1 < len(lines):
                bid_type = lines[idx + 1]

        # 8. Total HPS
        hps = 0.0
        hps_match = re.search(r"Total HPS Kompetisi\s*Rp\s*([\d\.]+)", text)
        if hps_match:
            raw_num = hps_match.group(1).replace(".", "")
            try:
                hps = float(raw_num)
            except ValueError:
                pass

        # 9. Package Title
        title = "Paket Mini Kompetisi Laboratorium"
        if "Lokasi" in lines:
            loc_idx = lines.index("Lokasi")
            if loc_idx > 0:
                title = lines[loc_idx - 1]

        return {
            "id": pkg_id,
            "package_id": pkg_id,
            "package_name": title,
            "institution": institution,
            "location": location,
            "dates": dates,
            "deadline": dates.split("-")[-1].strip() if "-" in dates else dates,
            "time_left": time_left,
            "bid_type": bid_type,
            "procurement_type": f"Mini Kompetisi ({bid_type})",
            "hps_budget": hps,
            "budget": hps,
            "source_url": url or f"https://penyedia.inaproc.id/mini-competition/public-competition/{pkg_id}",
            "keyword": keyword,
            "discovered_at": datetime.now().isoformat(),
        }

    async def fetch_competition_details(self, page, detail_url: str) -> dict[str, Any]:
        """Fetch package detail page to extract exact product line items requested."""
        if not detail_url:
            return {}
        try:
            await page.goto(detail_url, wait_until="networkidle", timeout=35000)
            await asyncio.sleep(1.5)
            text = await page.inner_text("body")
            lines = [line.strip() for line in text.split("\n") if line.strip()]

            # Extract Nomor Kompetisi
            comp_num = ""
            if "Nomor Kompetisi" in lines:
                idx = lines.index("Nomor Kompetisi")
                if idx + 1 < len(lines):
                    comp_num = lines[idx + 1]

            # Extract products list
            products = []
            cur_prod: dict[str, Any] = {}
            for i, l in enumerate(lines):
                if re.match(r"^Produk\s+\d+", l):
                    if cur_prod and cur_prod.get("name"):
                        products.append(cur_prod)
                    cur_prod = {"item_label": l, "name": "", "qty": 1, "unit": "Unit", "hps_unit": 0.0, "total_hps": 0.0}
                elif l == "Spesifikasi Produk" and i + 2 < len(lines) and lines[i + 1] == ":":
                    cur_prod["name"] = lines[i + 2]
                elif l == "Jumlah" and i + 2 < len(lines) and lines[i + 1] == ":":
                    try:
                        cur_prod["qty"] = float(lines[i + 2].replace(",", "."))
                    except ValueError:
                        pass
                elif l == "Satuan" and i + 2 < len(lines) and lines[i + 1] == ":":
                    cur_prod["unit"] = lines[i + 2]
                elif l == "HPS Produk" and i + 2 < len(lines) and lines[i + 1] == ":":
                    raw_hps = re.sub(r"[^\d]", "", lines[i + 2])
                    if raw_hps:
                        cur_prod["hps_unit"] = float(raw_hps)
                elif l == "Total HPS Produk" and i + 2 < len(lines) and lines[i + 1] == ":":
                    raw_tot = re.sub(r"[^\d]", "", lines[i + 2])
                    if raw_tot:
                        cur_prod["total_hps"] = float(raw_tot)

            if cur_prod and cur_prod.get("name"):
                products.append(cur_prod)

            return {
                "competition_number": comp_num,
                "products": products,
                "total_items": len(products),
            }
        except Exception as e:
            logger.warning(f"InaprocAgent: Error fetching details from {detail_url}: {e}")
            return {}

    async def search_public_competitions(
        self,
        keywords: list[str] | None = None,
        max_pages: int = 1,
        fetch_details: bool = True,
    ) -> list[dict[str, Any]]:
        """Autonomously patrol INAPROC mini-competition portal for tender opportunities.
        Uses persistent authenticated session, evaluates cards, matches against Spec Bank,
        and saves discovered packages to database.
        """
        from playwright.async_api import async_playwright
        from harvey.integrations.inaproc_watcher import InaprocWatcher, match_package_to_andislab

        if not self.is_profile_present():
            logger.warning("InaprocAgent: Profil browser belum ada. Harap login terlebih dahulu.")
            return []

        search_kws = keywords if keywords else DEFAULT_SEARCH_KEYWORDS
        spec_bank = SpecBank()

        discovered: dict[str, dict[str, Any]] = {}
        logger.info(f"InaprocAgent: Memulai pencarian otonom untuk {len(search_kws)} kata kunci...")

        async with async_playwright() as p:
            ctx = None
            try:
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    channel="chrome",
                    headless=False,
                    args=CHROME_SAFE_ARGS + ["--start-minimized"],
                    viewport={"width": 1280, "height": 850},
                )
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

                for kw in search_kws:
                    for page_num in range(1, max_pages + 1):
                        search_url = f"{self.portal_url}?search={urllib.parse.quote(kw)}&page={page_num}"
                        try:
                            await page.goto(search_url, wait_until="networkidle", timeout=40000)
                            await asyncio.sleep(2.0)

                            # Evaluate all card elements in DOM
                            raw_cards = await page.evaluate('''() => {
                                const detailButtons = Array.from(document.querySelectorAll("button, a")).filter(el => 
                                    el.innerText && (el.innerText.includes("Lihat Detail") || el.innerText.includes("Ikuti Kompetisi"))
                                );
                                const cards = new Set();
                                for (const btn of detailButtons) {
                                    let parent = btn.parentElement;
                                    while (parent && parent.innerText && parent.innerText.length < 2500) {
                                        if (parent.innerText.includes("Total HPS") || parent.innerText.includes("Penawaran Dibuka")) {
                                            cards.add(parent);
                                            break;
                                        }
                                        parent = parent.parentElement;
                                    }
                                }
                                return Array.from(cards).map(c => ({
                                    text: c.innerText || "",
                                    links: Array.from(c.querySelectorAll("a")).map(a => a.href)
                                }));
                            }''')

                            for c in raw_cards:
                                parsed = self._parse_card_container(c.get("text", ""), c.get("links", []), keyword=kw)
                                if parsed and parsed["id"] not in discovered:
                                    discovered[parsed["id"]] = parsed

                        except Exception as e:
                            logger.warning(f"InaprocAgent: Gagal memindai keyword '{kw}' page {page_num}: {e}")

                # Deep parse detail page for discovered itemized packages
                if fetch_details:
                    for pid, pkg in list(discovered.items()):
                        d_url = pkg.get("source_url")
                        if d_url and "public-competition" in d_url and "?key=" in d_url:
                            dt = await self.fetch_competition_details(page, d_url)
                            if dt:
                                pkg["competition_number"] = dt.get("competition_number", "")
                                pkg["products"] = dt.get("products", [])
                                pkg["total_items"] = dt.get("total_items", len(dt.get("products", [])))

            finally:
                if ctx:
                    try:
                        await ctx.close()
                    except Exception:
                        pass

        # Match packages against Spec Bank & calculate margins
        watcher = InaprocWatcher()
        known_pkgs = watcher.load_known_packages()

        matched_list = []
        for pid, pkg in discovered.items():
            cat, models, score = match_package_to_andislab(
                pkg["package_name"],
                pkg["hps_budget"],
                pkg["procurement_type"],
            )
            pkg["category_matched"] = cat
            pkg["matched_models"] = models
            pkg["match_score"] = score

            # Check specific product matches from Spec Bank
            matched_spec = spec_bank.search_spec(pkg["package_name"], limit=1)
            if matched_spec:
                spec_item = matched_spec[0]
                pkg["spec_bank_match"] = spec_item["name"]
                pkg["cost_hpp"] = spec_item.get("harga_beli", 0.0)
                pkg["target_price"] = round(spec_item.get("price", 0.0), 2)
                if pkg["target_price"] > 0 and pkg["cost_hpp"] > 0:
                    pkg["margin_pct"] = round(((pkg["target_price"] - pkg["cost_hpp"]) / pkg["target_price"]) * 100, 2)
            else:
                # Inventory fallback
                inv_match = await self.match_product_and_pricing(pkg["package_name"], hps_budget=pkg["hps_budget"])
                if inv_match:
                    pkg["spec_bank_match"] = inv_match.get("matched_name", "")
                    pkg["cost_hpp"] = inv_match.get("cost_hpp", 0.0)
                    pkg["target_price"] = inv_match.get("target_price", 0.0)
                    pkg["margin_pct"] = inv_match.get("margin_pct", 20.0)

            # Persist to JSON database
            known_pkgs[pid] = pkg
            matched_list.append(pkg)

            # Insert/update bid candidate into SQLite
            await self.state.record_inaproc_bid({
                "id": pid,
                "package_name": pkg["package_name"],
                "institution": pkg["institution"],
                "hps_budget": pkg["hps_budget"],
                "item_requirements_json": json.dumps(pkg.get("products", [{"name": pkg["package_name"]}]), ensure_ascii=False),
                "matched_inventory_id": "",
                "matched_product_name": pkg.get("spec_bank_match") or (models[0] if models else "Unit Laboratorium"),
                "cost_hpp": pkg.get("cost_hpp", 0.0),
                "target_price": pkg.get("target_price", 0.0),
                "margin_pct": pkg.get("margin_pct", 20.0),
                "status": "discovered",
                "source_url": pkg["source_url"],
                "deadline": pkg["deadline"],
                "notes": f"Pencarian otonom keyword: {pkg.get('keyword')}, Skor {score}%",
            })

        watcher.save_packages(known_pkgs)
        logger.info(f"InaprocAgent: Pencarian selesai. {len(matched_list)} paket kompetisi berhasil diekstrak dan disimpan.")
        return matched_list

    async def scan_and_alert_competitions(
        self,
        keywords: list[str] | None = None,
        notify_wa: bool = True,
        auto_submit: bool = False,
    ) -> list[dict[str, Any]]:
        """Run autonomous search, match with Spec Bank, and dispatch WhatsApp alerts for hot leads."""
        from harvey.integrations.whatsapp import WhatsAppEngine
        from harvey.integrations.inaproc_watcher import format_inaproc_alert_message, InaprocPackage

        results = await self.search_public_competitions(keywords=keywords, fetch_details=True)
        wa = WhatsAppEngine(self.state, self.config, self.env) if notify_wa else None

        alerted_file = self.data_dir / "alerted_ids.json"
        try:
            alerted_ids = set(json.loads(alerted_file.read_text(encoding="utf-8"))) if alerted_file.exists() else set()
        except Exception:
            alerted_ids = set()

        new_alerts_count = 0
        for pkg in results:
            pid = str(pkg.get("id") or "")
            if pid in alerted_ids:
                continue  # Deduplication: never spam the same package alert twice!

            score = pkg.get("match_score", 0)
            if score >= 35 and notify_wa and wa:
                ip_pkg = InaprocPackage(
                    package_id=pid,
                    package_name=pkg["package_name"],
                    institution=pkg["institution"],
                    hps_budget=pkg["hps_budget"],
                    category_matched=pkg.get("category_matched", "laboratorium"),
                    matched_models=pkg.get("matched_models", []),
                    match_score=score,
                    procurement_type=pkg.get("procurement_type", "Mini Kompetisi"),
                    location=pkg.get("location", ""),
                    source_url=pkg["source_url"],
                    deadline=pkg["deadline"],
                )
                msg = format_inaproc_alert_message(ip_pkg)
                if pkg.get("time_left"):
                    msg += f"\n⏳ *Sisa Waktu:* {pkg['time_left']}"
                if pkg.get("products"):
                    msg += f"\n📋 *Item Dibutuhkan:* {len(pkg['products'])} macam produk terdaftar"
                try:
                    await wa.send_inaproc_alert(msg)
                    alerted_ids.add(pid)
                    new_alerts_count += 1
                    logger.info(f"InaprocAgent: Alert WhatsApp BARU terkirim untuk {pid}")
                except Exception as e:
                    logger.warning(f"InaprocAgent: Gagal kirim WA alert untuk {pid}: {e}")

            if auto_submit and pkg.get("hps_budget", 0) > 0 and score >= 40:
                try:
                    await self.submit_bid(pid, pkg)
                except Exception as e:
                    logger.warning(f"InaprocAgent: Auto-submit gagal untuk {pid}: {e}")

        if new_alerts_count:
            try:
                alerted_file.write_text(json.dumps(sorted(alerted_ids), indent=2), encoding="utf-8")
            except Exception:
                pass

        return results

    async def _send_bid_notification(
        self,
        status: str,
        pkg_name: str,
        institution: str,
        matched_prod: str,
        hpp: float,
        target_price: float,
        margin_pct: float,
        hps_budget: float,
        deadline: str,
        url: str,
    ) -> None:
        """Send formatted WhatsApp notification to operator about bid status."""
        from harvey.integrations.whatsapp import WhatsAppEngine

        wa = WhatsAppEngine(self.state, self.config, self.env)
        target_phone = getattr(self.config.channels.whatsapp, "report_phone", "087764021131")

        budget_str = f"Rp {int(hps_budget):,}".replace(",", ".") if hps_budget > 0 else "Belum tertera"
        hpp_str = f"Rp {int(hpp):,}".replace(",", ".")
        price_str = f"Rp {int(target_price):,}".replace(",", ".")

        if status == "submitted":
            headline = "⚡ *[MINI KOMPETISI E-KATALOG V6 BERHASIL DISUBMIT]*"
            footer_note = "_Penawaran telah otomatis diajukan oleh Harvey INAPROC Agent._"
        elif status == "draft_ready":
            headline = "⚠️ *[DRAFT PENAWARAN MINI KOMPETISI DISIAPKAN]*"
            footer_note = "👉 *Tindakan Ahnaf:* Silakan buka link untuk memasukkan OTP / konfirmasi akhir di portal E-Katalog v6."
        else:
            headline = "🚨 *[KENDALA PENGAJUAN MINI KOMPETISI]*"
            footer_note = "👉 Silakan tinjau paket ini secara manual di portal E-Katalog v6."

        msg = (
            f"{headline}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏢 *Instansi:* {institution}\n"
            f"📦 *Paket:* {pkg_name}\n"
            f"🎯 *Produk Cocok:* {matched_prod}\n"
            f"💰 *Estimasi HPP:* {hpp_str}\n"
            f"🏷️ *Harga Penawaran:* {price_str} (Margin {margin_pct}%)\n"
            f"📊 *Pagu / HPS:* {budget_str}\n"
            f"⏰ *Batas Waktu:* {deadline}\n"
            f"🔗 *Tautan:* {url}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{footer_note} 🙏"
        )
        try:
            await wa.send_inaproc_alert(msg)
        except Exception as e:
            logger.warning(f"InaprocAgent: Gagal kirim WA alert: {e}")

    async def run_copilot_cycle(self, auto_submit: bool = False) -> list[dict[str, Any]]:
        """Run Co-Pilot scanning, matching, margin calculation, and operator briefing."""
        from harvey.integrations.inaproc_watcher import InaprocWatcher

        watcher = InaprocWatcher()
        logger.info("InaprocAgent: Memulai siklus Co-Pilot Mini Kompetisi E-Katalog v6...")

        # 1. Run direct autonomous portal search across core lab categories
        try:
            await self.search_public_competitions(fetch_details=True)
        except Exception as e:
            logger.warning(f"InaprocAgent: Direct portal search encountered an error: {e}")

        # Also discover any external LPSE packages
        try:
            await watcher.scan_live_procurement(self.env, self.brain)
        except Exception as e:
            logger.warning(f"InaprocAgent: Live procurement scan encountered an error: {e}")

        results = []

        # Retrieve all discovered candidates from watcher store
        known_pkgs = watcher.load_known_packages()
        all_candidates = list(known_pkgs.values())

        # Fetch already processed bid IDs from SQLite so we NEVER re-submit or spam WA
        processed_ids: set[str] = set()
        try:
            async with self.state._connect() as db:
                cur = await db.execute(
                    "SELECT id FROM inaproc_bids WHERE status IN ('submitted', 'draft_ready', 'won', 'lost', 'skipped')"
                )
                processed_ids = {str(r[0]) for r in await cur.fetchall()}
        except Exception:
            pass

        for raw in all_candidates:
            pid = str(raw.get("package_id") or raw.get("id"))
            if pid in processed_ids:
                continue
            pname = raw.get("package_name") or raw.get("name", "")
            proc_type = (raw.get("procurement_type") or "").lower()

            # Target Mini Kompetisi & Pengadaan Langsung
            if "mini" not in proc_type and "langsung" not in proc_type and "mini" not in pname.lower():
                continue

            hps = float(raw.get("hps_budget") or raw.get("budget", 0.0))

            # 2. Check product match with 20% margin guardrail
            match_res = await self.match_product_and_pricing(pname, hps_budget=hps)
            if not match_res or not match_res.get("eligible"):
                continue

            bid_record = {
                "id": pid,
                "package_name": pname,
                "institution": raw.get("institution", "Instansi"),
                "hps_budget": hps,
                "item_requirements_json": json.dumps([{"name": pname}], ensure_ascii=False),
                "matched_inventory_id": str(match_res.get("matched_id", "")),
                "matched_product_name": f"{match_res.get('matched_name')} ({match_res.get('brand')} {match_res.get('model')})",
                "cost_hpp": match_res["cost_hpp"],
                "target_price": match_res["target_price"],
                "margin_pct": match_res["margin_pct"],
                "status": "ready_to_submit",
                "source_url": raw.get("source_url") or raw.get("url", self.portal_url),
                "deadline": raw.get("deadline", "Segera"),
                "notes": f"Margin {match_res['margin_pct']}% terpenuhi (stok: {match_res.get('stock_status')})",
            }

            await self.state.record_inaproc_bid(bid_record)

            # 3. Auto-submit (max 2 packages per cycle with human pacing to avoid WAF throttling)
            if auto_submit and len(results) < 2:
                sub_res = await self.submit_bid(pid, bid_record)
                bid_record["submission_result"] = sub_res
                await asyncio.sleep(10.0)

            results.append(bid_record)

        logger.info(f"InaprocAgent: Siklus selesai. {len(results)} paket Mini Kompetisi diproses.")
        return results

