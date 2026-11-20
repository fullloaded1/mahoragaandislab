"""Vendor Scoring, Landed Cost Normalisation, and Dynamic Margin Engine for Sourcing.

Provides:
- Landed cost calculation (PPN 11% + Franco Bogor handling)
- Anomaly filtering (rejecting spare parts / accessories mistaken for full equipment)
- Multi-criteria vendor scoring (pricing, partner status, warranty, lead time)
- Dynamic margin pricing (higher margin on low-ticket items, competitive on high-ticket)
- Dual tier selection: Economic Option vs Premium Option
"""

import statistics
import logging
from typing import Any

logger = logging.getLogger("harvey.vendor_scoring")


def calculate_landed_cost(
    unit_price: float,
    ppn_included: bool = True,
    shipping_est: float = 0.0,
) -> float:
    """Normalize vendor price to Landed Cost (inclusive PPN 11% + delivery to Bogor)."""
    if unit_price <= 0:
        return 0.0
    
    # If price excludes PPN, add standard 11% VAT
    base_with_ppn = unit_price if ppn_included else unit_price * 1.11
    
    # Add estimated shipping/insurance to Gudang Bogor (default 50k - 150k for lab parcels)
    landed = base_with_ppn + max(0.0, shipping_est)
    return round(landed, 2)


def detect_price_anomalies(quotes: list[dict]) -> list[dict]:
    """Flag quotes that are suspiciously low (< 35% of median, e.g. spare parts/accessories)
    or suspiciously high (> 350% of median).
    """
    valid_prices = [q.get("landed_cost_idr", 0) for q in quotes if q.get("landed_cost_idr", 0) > 0]
    if len(valid_prices) < 2:
        for q in quotes:
            q["is_anomaly"] = False
        return quotes

    median_price = statistics.median(valid_prices)

    for q in quotes:
        price = q.get("landed_cost_idr", 0)
        if price <= 0:
            if q.get("needs_rfq"):
                q["is_anomaly"] = False
                q["anomaly_reason"] = ""
            else:
                q["is_anomaly"] = True
                q["anomaly_reason"] = "Harga nol atau tidak tertera"
        elif price < (0.35 * median_price):
            q["is_anomaly"] = True
            q["anomaly_reason"] = f"Harga terlalu rendah (kemungkinan sparepart/aksesori: Rp {price:,.0f} vs median Rp {median_price:,.0f})"
        elif price > (3.5 * median_price):
            q["is_anomaly"] = True
            q["anomaly_reason"] = f"Harga terlalu tinggi (anomali harga eceran)"
        else:
            q["is_anomaly"] = False
            q["anomaly_reason"] = ""

    return quotes


def score_vendor_quote(quote: dict, is_partner: bool = False, min_price: float = 0.0) -> float:
    """Calculate vendor reliability score (0 - 100 points).
    
    Criteria:
    - Price Competitiveness (up to 40 pts)
    - Trust & Relationship Tier (up to 25 pts)
    - Stock Availability & Lead Time (up to 20 pts)
    - Warranty & Compliance (up to 15 pts)
    """
    score = 0.0
    price = quote.get("landed_cost_idr", 0)

    # 1. Price Competitiveness (40 pts)
    if min_price > 0 and price > 0:
        ratio = min_price / price
        score += ratio * 40.0
    elif price > 0:
        score += 30.0

    # 2. Trust Tier (25 pts)
    tier = quote.get("tier", "general")
    if is_partner or tier == "partner":
        score += 25.0  # PT CMSI or contracted partner
    elif tier == "principal":
        score += 20.0
    elif tier == "marketplace":
        score += 16.0
    else:
        score += 12.0

    # 3. Stock Availability & Lead Time (20 pts)
    stock_status = (quote.get("stock_status") or "").lower()
    if "ready" in stock_status:
        score += 20.0
    elif "inden" in stock_status or "indent" in stock_status:
        score += 12.0
    else:
        score += 8.0

    # 4. Warranty & Completeness (15 pts)
    raw_w = quote.get("warranty_months")
    try:
        warranty_months = int(raw_w) if raw_w is not None else 12
    except (TypeError, ValueError):
        warranty_months = 0
    if warranty_months >= 12:
        score += 15.0
    elif warranty_months >= 6:
        score += 10.0
    else:
        score += 5.0

    return round(min(100.0, score), 1)


def recommend_margins(hpp: float, custom_margin_pct: float | None = None) -> dict[str, Any]:
    """Calculate selling price and dynamic margin.
    
    Default brackets:
    - Low ticket (< Rp 5,000,000)  -> 30% margin
    - Mid ticket (Rp 5M - Rp 50M)   -> 20% margin
    - High ticket (> Rp 50,000,000) -> 15% margin
    """
    if hpp <= 0:
        return {
            "hpp": 0.0,
            "margin_pct": 0.0,
            "sell_price": 0.0,
            "profit_idr": 0.0,
            "formatted_hpp": "Rp 0",
            "formatted_sell_price": "Rp 0",
        }

    if custom_margin_pct is not None and custom_margin_pct > 0:
        eff_margin = custom_margin_pct / 100.0 if custom_margin_pct > 1.0 else custom_margin_pct
    else:
        if hpp < 5_000_000:
            eff_margin = 0.30
        elif hpp <= 50_000_000:
            eff_margin = 0.20
        else:
            eff_margin = 0.15

    sell_price = round(hpp * (1.0 + eff_margin), 2)
    profit = round(sell_price - hpp, 2)

    return {
        "hpp": hpp,
        "margin_pct": round(eff_margin * 100, 1),
        "sell_price": sell_price,
        "profit_idr": profit,
        "formatted_hpp": f"Rp {hpp:,.0f}".replace(",", "."),
        "formatted_sell_price": f"Rp {sell_price:,.0f}".replace(",", "."),
        "formatted_profit": f"Rp {profit:,.0f}".replace(",", "."),
    }


def rank_and_select_best_vendors(quotes: list[dict], custom_margin_pct: float | None = None) -> dict[str, Any]:
    """Filter anomalies, score, and rank quotes into Economic and Premium options."""
    if not quotes:
        return {
            "total_candidates": 0,
            "valid_candidates": 0,
            "ranked_quotes": [],
            "best_economic": None,
            "best_premium": None,
            "selected_hpp": 0.0,
            "pricing": recommend_margins(0.0),
        }

    # Step 1: Anomaly check
    screened = detect_price_anomalies(quotes)
    valid = [q for q in screened if not q.get("is_anomaly") and (q.get("landed_cost_idr", 0) > 0 or q.get("needs_rfq"))]
    
    # If all flagged as anomaly, fall back to non-zero quotes or quotes needing RFQ
    if not valid:
        valid = [q for q in screened if q.get("landed_cost_idr", 0) > 0 or q.get("needs_rfq")]

    priced_quotes = [q for q in valid if q.get("landed_cost_idr", 0) > 0]
    min_price = min([q["landed_cost_idr"] for q in priced_quotes]) if priced_quotes else 0.0

    # Step 2: Score quotes
    for q in valid:
        is_partner = bool(q.get("is_partner") or q.get("tier") == "partner")
        q["score"] = score_vendor_quote(q, is_partner=is_partner, min_price=min_price)

    # Sort: highest score first, then lowest landed cost (prefer priced quotes over 0/RFQ)
    valid.sort(key=lambda x: (-x.get("score", 0), x.get("landed_cost_idr", 0) if x.get("landed_cost_idr", 0) > 0 else 999_999_999))

    # Step 3: Identify Economic vs Premium
    best_quote = next((q for q in valid if q.get("landed_cost_idr", 0) > 0), (valid[0] if valid else None))
    best_economic = best_quote
    best_premium = None

    # Check for a premium alternative (different brand or higher tier)
    if len(priced_quotes) > 1 and best_quote:
        first_brand = (best_quote.get("brand") or "").lower()
        for cand in priced_quotes[1:]:
            cand_brand = (cand.get("brand") or "").lower()
            if cand_brand and cand_brand != first_brand and cand.get("landed_cost_idr", 0) > best_quote.get("landed_cost_idr", 0):
                best_premium = cand
                break

    selected_hpp = best_quote.get("landed_cost_idr", 0.0) if best_quote else 0.0
    pricing = recommend_margins(selected_hpp, custom_margin_pct=custom_margin_pct)

    return {
        "total_candidates": len(quotes),
        "valid_candidates": len(valid),
        "ranked_quotes": valid,
        "anomalies": [q for q in screened if q.get("is_anomaly")],
        "best_economic": best_economic,
        "best_premium": best_premium,
        "selected_hpp": selected_hpp,
        "pricing": pricing,
    }

