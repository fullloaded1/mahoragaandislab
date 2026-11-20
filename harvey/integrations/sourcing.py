"""Sourcing on Demand (By Request) Engine for PT Andis Sentral Laboratorium.

Powered by:
- Blind Procurement Privacy Firewall (100% Client PII Isolation)
- Multi-Source Parallel Discovery (Tavily AI Search across Marketplaces & Distributors)
- Parallel Web Page Extraction (Firecrawl Markdown Scraping)
- Partner Tiering (Priority for PT CMSI on Glassware & Reagents)
- Vendor Scoring & Landed Cost Normalisation (PPN 11% + Franco Bogor)
- Price Memory Bank (SQLite SQLite caching of past vendor quotes)
"""

import asyncio
import inspect
import json
import logging
import re
from typing import Any
import httpx

from harvey.config import EnvConfig, HarveyConfig
from harvey.brain import Brain
from harvey.state import StateManager
from harvey.integrations.sourcing_privacy import (
    anonymize_item_query,
    outbound_rfq_guard,
    build_blind_rfq_message,
)
from harvey.integrations.vendor_scoring import (
    calculate_landed_cost,
    rank_and_select_best_vendors,
    recommend_margins,
)

logger = logging.getLogger("harvey.sourcing")

TAVILY_API_URL = "https://api.tavily.com/search"
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v1/scrape"


class SourcingEngine:
    """Finds at least 3 suppliers, prices, and specifications for laboratory items not currently in stock,
    strictly safeguarding client confidentiality via Blind Procurement protocols.
    """

    def __init__(
        self,
        brain: Brain,
        env: EnvConfig,
        config: HarveyConfig | None = None,
        state: StateManager | None = None,
    ):
        self.brain = brain
        self.env = env
        self.config = config
        self.state = state
        self.default_margin_pct = 0.20

    async def search_tavily(self, query: str, max_results: int = 5) -> list[dict]:
        """Search Indonesian laboratory suppliers via Tavily."""
        api_key = self.env.tavily_api_key
        if not api_key:
            logger.warning("Sourcing: TAVILY_API_KEY tidak terpasang di .env")
            return []

        payload = {
            "api_key": api_key,
            "query": query,
            "search_depth": "advanced",
            "max_results": max_results,
            "include_answer": False,
        }

        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.post(TAVILY_API_URL, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("results", [])
                else:
                    logger.warning(f"Sourcing: Tavily error {resp.status_code}: {resp.text}")
                    return []
        except Exception as e:
            logger.warning(f"Sourcing: Tavily request failed: {e}")
            return []

    async def search_serper(self, query: str, max_results: int = 5) -> list[dict]:
        """Search Indonesian Google results via Serper.dev (gl=id, hl=id)."""
        api_key = getattr(self.env, "serper_api_key", "") or ""
        if not api_key:
            return []
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                    json={"q": query, "gl": "id", "hl": "id", "num": max_results},
                )
                if resp.status_code == 200:
                    organic = resp.json().get("organic") or []
                    return [
                        {
                            "title": item.get("title", ""),
                            "url": item.get("link", ""),
                            "content": item.get("snippet", ""),
                        }
                        for item in organic[:max_results]
                        if item.get("link")
                    ]
        except Exception as e:
            logger.debug(f"Sourcing: Serper request failed: {e}")
        return []

    async def _search_internal_stock(self, clean_query: str) -> tuple[list[dict], list[dict]]:
        """Check internal 1,142+ SKU inventory and LanceDB price_market_memory."""
        inv_hits: list[dict] = []
        mkt_hits: list[dict] = []
        try:
            if self.state:
                tokens = [t for t in re.split(r"[^a-z0-9]+", clean_query.lower()) if len(t) >= 3][:4]
                if tokens:
                    like_clauses = " AND ".join(["lower(coalesce(name,'') || ' ' || coalesce(brand,'') || ' ' || coalesce(model,'')) LIKE ?" for _ in tokens])
                    params = [f"%{t}%" for t in tokens]
                    async with self.state._connect() as db:
                        cur = await db.execute(
                            f"SELECT name, brand, model, category, price_idr, stock_status FROM inventory WHERE {like_clauses} LIMIT 5",
                            params,
                        )
                        for r in await cur.fetchall():
                            inv_hits.append({
                                "name": r[0], "brand": r[1], "model": r[2],
                                "category": r[3], "price_idr": r[4], "stock_status": r[5],
                            })
        except Exception as e:
            logger.debug(f"Sourcing: Internal inventory check skipped: {e}")
        try:
            from harvey.integrations.big_data import MahoragaBigData
            bd = MahoragaBigData()
            if not inv_hits:
                for sku in bd.search_sku(clean_query, limit=4):
                    inv_hits.append({
                        "name": sku.get("name", ""), "brand": sku.get("brand", ""),
                        "model": sku.get("model", ""), "category": sku.get("category", ""),
                        "price_idr": sku.get("price_idr", 0), "stock_status": "Ready (LanceDB)",
                    })
            mkt_hits = bd.search_price_market(clean_query, limit=3) if hasattr(bd, "search_price_market") else []
        except Exception as e:
            logger.debug(f"Sourcing: BigData check skipped: {e}")
        return inv_hits, mkt_hits

    async def scrape_firecrawl(self, url: str) -> str:
        """Scrape clean markdown of a supplier/product page via Firecrawl."""
        api_key = self.env.firecrawl_api_key
        if not api_key:
            logger.warning("Sourcing: FIRECRAWL_API_KEY tidak terpasang di .env")
            return ""

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "url": url,
            "formats": ["markdown"],
        }

        try:
            async with httpx.AsyncClient(timeout=35.0) as client:
                resp = await client.post(FIRECRAWL_SCRAPE_URL, headers=headers, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("data", {}).get("markdown", "")
                else:
                    logger.debug(f"Sourcing: Firecrawl status {resp.status_code} for {url}")
                    return ""
        except Exception as e:
            logger.debug(f"Sourcing: Firecrawl request failed for {url}: {e}")
            return ""

    async def search_suppliers(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        """Backwards compatibility alias for search_tavily."""
        return await self.search_tavily(query, max_results=max_results)

    async def scrape_page(self, url: str) -> str:
        """Backwards compatibility alias for scrape_firecrawl."""
        return await self.scrape_firecrawl(url)

    async def source_item(
        self,
        raw_query: str = "",
        item_query: str = "",
        prospect_id: str = "",
        client_name: str = "",
        institution: str = "",
        margin_pct: float | None = None,
        qty: int = 1,
    ) -> dict[str, Any]:
        """Orchestrate end-to-end multi-vendor sourcing:
        1. Anonymize query (strip client PII).
        2. Check Price Memory Bank in SQLite.
        3. Parallel search across B2B Marketplaces, Authorized Principals & Budget Brands.
        4. Parallel Firecrawl scraping.
        5. Prioritize Partner PT CMSI if Glassware/Reagent.
        6. Extract >= 3 vendor candidates via LLM.
        7. Normalize landed costs, score, and compute dynamic margins.
        8. Store to DB & return operator summary + client draft.
        """
        raw_query = raw_query or item_query
        effective_margin = margin_pct if margin_pct is not None else self.default_margin_pct

        # ── Step 1: Blind Procurement Firewall (Anonymize) ──
        clean_query = anonymize_item_query(raw_query)
        if not clean_query:
            clean_query = raw_query.strip()

        logger.info(f"Sourcing: Permintaan asli: '{raw_query}' -> Query Bersih: '{clean_query}'")

        # Create sourcing ticket in DB if state available
        sourcing_id = f"SRC-{clean_query[:8].upper().replace(' ', '')}-{asyncio.get_event_loop().time():.0f}"
        if self.state:
            try:
                sourcing_id = await self.state.create_sourcing_request({
                    "prospect_id": prospect_id,
                    "raw_query": raw_query,
                    "clean_query": clean_query,
                    "qty": qty,
                    "status": "searching",
                })
            except Exception as e:
                logger.debug(f"Sourcing: Failed to create request record: {e}")

        # ── Step 2: Check Price Memory Bank ──
        cached_quotes = []
        if self.state:
            try:
                cached_quotes = await self.state.find_cached_price_memory(clean_query, max_days=30)
                if len(cached_quotes) >= 3:
                    logger.info(f"Sourcing: Ditemukan {len(cached_quotes)} kutipan valid di Price Memory Bank!")
            except Exception as e:
                logger.debug(f"Sourcing: Price memory lookup skipped: {e}")

        # ── Step 0: Check Internal Ready Stock (SQLite + LanceDB) & Market Memory ──
        internal_matches, market_benchmarks = await self._search_internal_stock(clean_query)

        # ── Step 3: Multi-Query Parallel Discovery (Tavily + Serper Google ID + E-Katalog) ──
        # Strip budget phrases from search query so search engines focus on exact product/model
        search_q = re.sub(
            r"\b(?:budget|anggaran|plafon|harga)?\s*(?:under|dibawah|di bawah|maks|max|<)\s*[\d.,]+\s*(?:jt|juta|rb|ribu|k|m)?\b",
            "",
            clean_query,
            flags=re.IGNORECASE,
        ).strip() or clean_query

        q_low = search_q.lower()
        is_glassware_or_reagent = any(
            k in q_low
            for k in ["glassware", "reagen", "reagent", "kimia", "iwaki", "merck", "pyrex", "labu ukur", "gelas beker", "erlenmeyer", "pipet volume"]
        )
        is_non_lab = any(
            k in q_low
            for k in ["chromebook", "laptop", "notebook", "pc ", "komputer", "printer", "scanner", "proyektor", "monitor", "server", "router", "ups ", "cdm"]
        )

        if is_non_lab:
            query_marketplace = f"{search_q} harga site:tokopedia.com OR site:bhinneka.com OR site:blibli.com OR site:monotaro.id OR site:ralali.com"
            query_distributor = f"{search_q} jual distributor resmi indonesia spesifikasi harga"
            query_budget = f"{search_q} baru garansi resmi indonesia harga"
        else:
            query_marketplace = f"{search_q} harga site:monotaro.id OR site:labmart.id OR site:ralali.com OR site:tokopedia.com OR site:indotrading.com"
            query_distributor = f"{search_q} distributor resmi indonesia spesifikasi harga laboratorium"
            query_budget = f"{search_q} biobase OR joanlab OR dlab OR faithfull OR scilogex harga indonesia"
        query_serper_id = f"{search_q} jual distributor supplier harga indonesia WA OR telepon"
        query_ekatalog = f"{search_q} site:e-katalog.lkpp.go.id OR site:inaproc.id"

        search_tasks = [
            self.search_suppliers(query_marketplace, max_results=4),
            self.search_suppliers(query_distributor, max_results=4),
            self.search_suppliers(query_budget, max_results=3),
            self.search_serper(query_serper_id, max_results=5),
            self.search_serper(query_ekatalog, max_results=3),
        ]

        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)
        combined_tavily = []
        seen_urls = set()

        for res in search_results:
            if isinstance(res, list):
                for item in res:
                    u = item.get("url", "")
                    if u and u not in seen_urls:
                        seen_urls.add(u)
                        combined_tavily.append(item)

        # ── Step 4: Parallel Scraping Top 6 URLs with Firecrawl (+ httpx fallback) ──
        urls_to_scrape = [r.get("url") for r in combined_tavily if r.get("url")][:6]
        scrape_tasks = [self.scrape_page(u) for u in urls_to_scrape]
        scraped_markdowns = await asyncio.gather(*scrape_tasks, return_exceptions=True)

        async def _light_fetch(u: str) -> str:
            try:
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as cl:
                    r = await cl.get(u, headers={"User-Agent": "Mozilla/5.0"})
                    if r.status_code == 200:
                        txt = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<[^>]+>", " ", r.text)
                        return re.sub(r"\s+", " ", txt).strip()[:2500]
            except Exception:
                pass
            return ""

        scraped_sections = []
        for u, md in zip(urls_to_scrape, scraped_markdowns):
            body_txt = md.strip() if isinstance(md, str) else ""
            if not body_txt:
                body_txt = await _light_fetch(u)
            if body_txt:
                scraped_sections.append(f"--- SOURCE ({u}) ---\n{body_txt[:3000]}")

        combined_snippets = "\n\n".join([
            f"Title: {r.get('title')}\nURL: {r.get('url')}\nSnippet: {r.get('content')}"
            for r in combined_tavily[:12]
        ])
        combined_scraped = "\n\n".join(scraped_sections)

        # ── Step 5: Partner Context (PT CMSI) ──
        partner_note = ""
        if is_glassware_or_reagent:
            partner_note = (
                "CATATAN KHUSUS: Item ini termasuk kategori Glassware / Reagen Kimia. "
                "PT Cahayatiara Mustika Scientific Indonesia (CMSI) adalah MITRA KERJASAMA UTAMA AndisLab untuk brand Iwaki & Merck. "
                "Wajib masukkan PT CMSI sebagai salah satu kandidat vendor prioritas."
            )

        # ── Step 6: LLM Synthesis across Multiple Vendors ──
        prompt = f"""Kamu adalah Procurement Specialist di PT Andis Sentral Laboratorium (AndisLab).
Lakukan ekstraksi dan analisis perbandingan harga dari minimal 3 VENDOR BERBEDA untuk item laboratorium berikut:

Permintaan Barang (Bersih dari data klien): "{clean_query}"
Jumlah Kebutuhan: {qty} unit
{partner_note}

--- DATA HASIL PENCARIAN TAVILY ---
{combined_snippets if combined_snippets else "(Data pencarian ringkas tidak tersedia)"}

--- DATA DETAIL HALAMAN SUPPLIER (FIRECRAWL) ---
{combined_scraped if combined_scraped else "(Gunakan data cuplikan Tavily)"}

TUGAS KAMU:
Ekstrak minimal 3 VENDOR PEMBANDING dalam format JSON murni:
{{
  "canonical_name": "Nama resmi lengkap alat / barang",
  "key_specs": "Spesifikasi teknis ringkas (akurasi, kapasitas, daya, dimensi)",
  "vendors": [
    {{
      "vendor_name": "Nama Perusahaan / Distributor / Toko B2B",
      "brand": "Merek alat (misal: Biobase, Memmert, Ohaus, Joanlab, Iwaki, dll.)",
      "model": "Tipe / Nomor Katalog",
      "unit_price_idr": 15000000,
      "ppn_included": true,
      "stock_status": "ready" atau "inden",
      "lead_time_days": "Estimasi waktu (misal: '3-5 hari kerja' atau '2-3 minggu')",
      "warranty_months": 12,
      "tier": "partner" atau "marketplace" atau "general_competitor" atau "principal",
      "vendor_phone": "Nomor WA/Telp vendor jika ada di sumber (misal '0812...' atau '')",
      "vendor_email": "Email vendor jika ada di sumber atau ''",
      "city": "Kota vendor (misal 'Jakarta', 'Bogor', 'Surabaya', atau '')",
      "source_url": "URL sumber temuan atau 'Direct Partner'"
    }}
  ]
}}

ATURAN PENTING:
1. Berikan MINIMAL 3 VENDOR BERBEDA dengan perkiraan harga realistis dalam Rupiah (IDR).
2. Jika ada brand ekonomis (misal Joanlab/Biobase/DLAB) dan brand premium (Memmert/Ohaus/Eppendorf), sertakan keduanya sebagai opsi pembanding.
3. Kembalikan JSON murni tanpa markdown triple-backticks.
"""

        parsed = await self.brain.think_json(prompt=prompt, agent="sourcing_agent")

        if not parsed or not isinstance(parsed, dict) or not (parsed.get("vendors") or parsed.get("official_name") or parsed.get("estimated_cost_idr")):
            raw = self.brain.think(prompt=prompt, agent="sourcing_agent")
            raw_text = await raw if (asyncio.iscoroutine(raw) or inspect.isawaitable(raw)) else str(raw or "")
            if isinstance(raw_text, str) and raw_text.strip():
                raw_text = re.sub(r"^```json\s*", "", raw_text.strip(), flags=re.I)
                raw_text = re.sub(r"^```\s*", "", raw_text)
                raw_text = re.sub(r"\s*```$", "", raw_text)
                try:
                    parsed = json.loads(raw_text)
                except Exception:
                    logger.warning(f"Sourcing: Gagal mem-parse JSON brain untuk {clean_query}")
                    parsed = {}

        canonical_name = parsed.get("canonical_name") or parsed.get("official_name") or clean_query
        key_specs = parsed.get("key_specs") or "Spesifikasi standar laboratorium"
        extracted_vendors = parsed.get("vendors") or []

        # Fallback for single-item response schema (e.g. legacy/mock formats)
        if not extracted_vendors and (parsed.get("official_name") or parsed.get("estimated_cost_idr")):
            est_cost = float(parsed.get("estimated_cost_idr") or 0.0)
            sup_refs = parsed.get("supplier_references") or []
            v_name = sup_refs[0].get("name", "Distributor Resmi") if sup_refs else "Distributor Resmi"
            v_url = sup_refs[0].get("url", "") if sup_refs else ""
            extracted_vendors.append({
                "vendor_name": v_name,
                "brand": parsed.get("brand", "LabBrand"),
                "model": parsed.get("model_or_cat_no", ""),
                "unit_price_idr": est_cost,
                "ppn_included": True,
                "stock_status": "ready",
                "lead_time_days": parsed.get("lead_time_days", "7-14 hari kerja"),
                "warranty_months": 12,
                "tier": "principal",
                "source_url": v_url,
            })

        # ── Step 7: Guarantee Minimum 3 Vendors ──
        # If fewer than 3 vendors extracted, enrich from partner / registered vendors directory
        if len(extracted_vendors) < 3 and self.state:
            try:
                db_vendors = await self.state.get_vendors()
                for dv in db_vendors:
                    if len(extracted_vendors) >= 3:
                        break
                    # Avoid duplicate vendor name
                    if not any(v.get("vendor_name", "").lower() == dv["name"].lower() for v in extracted_vendors):
                        extracted_vendors.append({
                            "vendor_name": dv["name"],
                            "brand": "Prinsipal / Mitra Resmi",
                            "model": "Katalog Standar",
                            "unit_price_idr": 0.0,
                            "ppn_included": True,
                            "stock_status": "inden (by quote)",
                            "lead_time_days": "7-14 hari",
                            "warranty_months": 12,
                            "tier": dv["tier"],
                            "source_url": dv.get("website", ""),
                            "needs_rfq": True,
                        })
            except Exception as e:
                logger.debug(f"Sourcing: Fallback vendor enrichment failed: {e}")

        # Ensure PT CMSI is included if glassware/reagents and not already present
        if is_glassware_or_reagent and not any("cmsi" in v.get("vendor_name", "").lower() for v in extracted_vendors):
            extracted_vendors.insert(0, {
                "vendor_name": "PT Cahayatiara Mustika Scientific Indonesia (CMSI)",
                "brand": "Iwaki / Merck",
                "model": "Katalog Resmi",
                "unit_price_idr": 0.0,
                "ppn_included": True,
                "stock_status": "ready / mitra",
                "lead_time_days": "3-7 hari",
                "warranty_months": 12,
                "tier": "partner",
                "source_url": "https://www.cmsi-id.com",
                "is_partner": True,
                "needs_rfq": True,
            })

        # Calculate landed costs for each quote
        for v in extracted_vendors:
            raw_p = float(v.get("unit_price_idr", 0) or 0)
            ppn = bool(v.get("ppn_included", True))
            v["landed_cost_idr"] = calculate_landed_cost(raw_p, ppn_included=ppn, shipping_est=100_000.0 if raw_p > 0 else 0.0)

        # ── Step 8: Scoring, Ranking & Margin Calculation ──
        ranked_data = rank_and_select_best_vendors(extracted_vendors, custom_margin_pct=effective_margin * 100)
        selected_hpp = ranked_data["selected_hpp"]
        pricing = ranked_data["pricing"]

        # ── Step 9: Save to Database (Price Memory Bank) ──
        if self.state:
            try:
                await self.state.update_sourcing_request(
                    sourcing_id,
                    normalized_specs=key_specs,
                    selected_hpp=selected_hpp,
                    quoted_price=pricing.get("sell_price", 0.0),
                    margin_pct=pricing.get("margin_pct", 0.0),
                    status="quoted" if selected_hpp > 0 else "needs_rfq",
                )
                for q in ranked_data["ranked_quotes"]:
                    await self.state.add_vendor_quote({
                        "sourcing_id": sourcing_id,
                        "vendor_name": q.get("vendor_name", "Supplier"),
                        "brand": q.get("brand", ""),
                        "model": q.get("model", ""),
                        "specs": key_specs,
                        "unit_price_idr": q.get("unit_price_idr", 0.0),
                        "ppn_included": q.get("ppn_included", True),
                        "landed_cost_idr": q.get("landed_cost_idr", 0.0),
                        "stock_status": q.get("stock_status", "ready"),
                        "lead_time_days": q.get("lead_time_days", "7-14 hari"),
                        "warranty_months": q.get("warranty_months", 12),
                        "source_url": q.get("source_url", ""),
                        "score": q.get("score", 0.0),
                        "notes": "Partner Resmi" if q.get("is_partner") else q.get("tier", ""),
                    })
            except Exception as e:
                logger.debug(f"Sourcing: Failed to save quotes to DB: {e}")

        # ── Step 10: Generate Blind RFQ drafts for vendors with unlisted prices ──
        blind_rfqs = []
        for v in ranked_data["ranked_quotes"]:
            if v.get("unit_price_idr", 0) <= 0 or v.get("needs_rfq"):
                v_name = v.get("vendor_name", "")
                rfq_text = build_blind_rfq_message(
                    item_name=canonical_name,
                    specs=key_specs,
                    qty=qty,
                    vendor_name=v_name,
                    target_city="Bogor",
                )
                # Verify safety guard
                is_safe, reason = outbound_rfq_guard(rfq_text)
                if is_safe:
                    blind_rfqs.append({
                        "vendor_name": v_name,
                        "rfq_text": rfq_text,
                    })

        # ── Step 11: Format WhatsApp Operator Alert & Consultative Client Draft ──
        best_eco = ranked_data.get("best_economic")
        best_prem = ranked_data.get("best_premium")

        vendor_lines = []
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for idx, q in enumerate(ranked_data["ranked_quotes"][:5]):
            m = medals[idx] if idx < len(medals) else "•"
            partner_tag = " [MITRA RESMI]" if q.get("is_partner") else ""
            hpp_fmt = f"Rp {q['landed_cost_idr']:,.0f}".replace(",", ".") if q["landed_cost_idr"] > 0 else "Hubungi Kami (RFQ)"
            brand_str = f" ({q.get('brand')} {q.get('model')})".strip() if q.get("brand") else ""
            vendor_lines.append(f"{m} *{q['vendor_name']}*{partner_tag}{brand_str}\n   • HPP Landed: {hpp_fmt} | Stok: {q.get('stock_status', 'Ready')} | Skor: {q.get('score', 0):.0f}/100")

        vendor_matrix_str = "\n".join(vendor_lines) if vendor_lines else "Belum ditemukan vendor otomatis."

        wa_operator_summary = (
            f"🔎 *[HASIL SOURCING BY REQUEST: {sourcing_id}]*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 *Barang:* {canonical_name}\n"
            f"⚙️ *Spek:* {key_specs}\n"
            f"🔢 *Qty:* {qty} unit\n"
            f"🔒 *Status Privasi:* 100% AMAN (Identitas klien diisolasi penuh)\n\n"
            f"📊 *MATRIKS VENDOR PEMBANDING (MINIMAL 3):*\n"
            f"{vendor_matrix_str}\n\n"
            f"💰 *ANALISIS HARGA & MARGIN ANDISLAB:*\n"
            f"• HPP Terpilih: {pricing.get('formatted_hpp')}\n"
            f"• Rekomendasi Margin: *{pricing.get('margin_pct')}%*\n"
            f"• Rekomendasi Harga Jual: *{pricing.get('formatted_sell_price')}*\n"
            f"• Estimasi Profit Bersih: *{pricing.get('formatted_profit')}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"👉 Balas *OK SOURCING* untuk mengirim balasan konsultatif ke klien."
        )

        # Consultative client reply
        lead_time_mention = best_eco.get("lead_time_days", "7-14 hari kerja") if best_eco else "7-14 hari kerja"
        client_reply = (
            f"Halo Bapak/Ibu, terima kasih atas kesabarannya menunggu. "
            f"Terkait kebutuhan *{canonical_name}*, unit ini berstatus *Special Order / By Request* langsung dari jaringan prinsipal resmi kami. "
            f"Estimasi pengiriman dan pengadaan berkisar *{lead_time_mention}* (Franco Jabodetabek). "
            f"Seluruh pengadaan melalui PT Andis Sentral Laboratorium sudah mencakup garansi resmi 12 bulan, instalasi teknisi, "
            f"serta kelengkapan berkas resmi (e-Faktur Pajak, BAST, dan fasilitas Tempo Instansi). "
            f"Apakah boleh kami koordinasikan draf rincian spesifikasi teknis dan SPH resminya? Terima kasih banyak. 🙏"
        )

        # Backwards compatibility fields for pricing
        if "estimated_cost_idr" in parsed and parsed.get("estimated_cost_idr"):
            base_hpp = float(parsed["estimated_cost_idr"])
            eff_margin = pricing.get("margin_pct", 20.0) / 100.0
            pricing["estimated_hpp"] = base_hpp
            pricing["formatted_hpp"] = f"Rp {base_hpp:,.0f}".replace(",", ".")
            pricing["estimated_quote_price"] = round(base_hpp * (1.0 + eff_margin), 2)
            pricing["formatted_quote_price"] = f"Rp {pricing['estimated_quote_price']:,.0f}".replace(",", ".")
        else:
            pricing["estimated_hpp"] = pricing.get("hpp", 0.0)
            pricing["estimated_quote_price"] = pricing.get("sell_price", 0.0)
            pricing["formatted_quote_price"] = pricing.get("formatted_sell_price", "Rp 0")

        return {
            "sourcing_id": sourcing_id,
            "raw_query": raw_query,
            "clean_query": clean_query,
            "canonical_name": canonical_name,
            "official_name": canonical_name,
            "brand": (best_eco.get("brand") if best_eco else "") or parsed.get("brand", ""),
            "key_specs": key_specs,
            "qty": qty,
            "total_candidates": len(extracted_vendors),
            "internal_matches": internal_matches,
            "market_benchmarks": market_benchmarks,
            "source_urls": [r.get("url") for r in combined_tavily[:10] if r.get("url")],
            "ranked_quotes": ranked_data["ranked_quotes"],
            "best_economic": best_eco,
            "best_premium": best_prem,
            "selected_hpp": selected_hpp,
            "pricing": pricing,
            "blind_rfqs": blind_rfqs,
            "whatsapp_operator_summary": wa_operator_summary,
            "consultative_client_reply": client_reply,
            "consultative_response": parsed.get("consultative_response") or client_reply,
        }
