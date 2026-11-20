import pytest
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

from harvey.state import StateManager
from harvey.integrations.sourcing_privacy import (
    anonymize_item_query,
    outbound_rfq_guard,
    build_blind_rfq_message,
)
from harvey.integrations.vendor_scoring import (
    calculate_landed_cost,
    detect_price_anomalies,
    score_vendor_quote,
    recommend_margins,
    rank_and_select_best_vendors,
)
from harvey.integrations.sourcing import SourcingEngine
from harvey.integrations.whatsapp import WhatsAppEngine


def test_anonymize_item_query_strips_pii_and_institutions():
    raw_query = (
        "Halo selamat siang Pak Ahnaf, saya Dr. Budi Santoso dari Universitas Indonesia Fakultas Farmasi. "
        "Kami butuh segera Centrifuge 12 lubang 4000 rpm untuk lab kami. "
        "Bisa kirim penawaran ke budi.santoso@farmasi.ui.ac.id atau WA ke 0812987654321? "
        "Paket tender RUP 998877 pagu anggaran Rp 35.000.000."
    )
    cleaned = anonymize_item_query(raw_query)

    # Must NOT contain client PII or institution
    assert "Budi" not in cleaned
    assert "Santoso" not in cleaned
    assert "Universitas Indonesia" not in cleaned
    assert "Farmasi" not in cleaned
    assert "0812987654321" not in cleaned
    assert "budi.santoso@farmasi.ui.ac.id" not in cleaned
    assert "998877" not in cleaned
    assert "35.000.000" not in cleaned

    # MUST contain core equipment specifications
    clean_low = cleaned.lower()
    assert "centrifuge" in clean_low
    assert "4000 rpm" in clean_low or "4000" in clean_low


def test_outbound_rfq_guard_blocks_client_data():
    forbidden = ["Universitas Airlangga", "dr. Hendra", "hendra@unair.ac.id"]

    unsafe_rfq = "Selamat siang, kami butuh unit untuk kebutuhan Universitas Airlangga."
    is_safe, reason = outbound_rfq_guard(unsafe_rfq, forbidden)
    assert is_safe is False
    assert "Universitas Airlangga" in reason or "instansi" in reason

    safe_rfq = build_blind_rfq_message(
        item_name="Oven Memmert UN55",
        specs="Kapasitas 53L",
        qty=1,
        vendor_name="PT SAKA",
    )
    is_safe, _ = outbound_rfq_guard(safe_rfq, forbidden)
    assert is_safe is True
    assert "PT Andis Sentral Laboratorium" in safe_rfq
    assert "Franco Gudang Bogor" in safe_rfq
    assert "pemenuhan gudang kami di Bogor" in safe_rfq


def test_vendor_scoring_and_anomaly_filtering():
    # Test Landed Cost with PPN 11%
    landed_excl_ppn = calculate_landed_cost(10_000_000, ppn_included=False, shipping_est=100_000)
    assert landed_excl_ppn == 11_200_000.0  # 10M * 1.11 + 100k

    landed_incl_ppn = calculate_landed_cost(10_000_000, ppn_included=True, shipping_est=100_000)
    assert landed_incl_ppn == 10_100_000.0

    # Test Anomaly Detection: 500k vs 15M median (500k is rotor/accessory, not the full centrifuge)
    mock_quotes = [
        {"vendor_name": "Vendor A", "landed_cost_idr": 15_000_000},
        {"vendor_name": "Vendor B", "landed_cost_idr": 16_000_000},
        {"vendor_name": "Vendor C", "landed_cost_idr": 14_500_000},
        {"vendor_name": "Toko Murah", "landed_cost_idr": 500_000},  # Anomaly!
    ]
    screened = detect_price_anomalies(mock_quotes)
    anomalies = [q for q in screened if q.get("is_anomaly")]
    assert len(anomalies) == 1
    assert anomalies[0]["vendor_name"] == "Toko Murah"

    # Test Dynamic Margin
    low_m = recommend_margins(2_000_000)
    assert low_m["margin_pct"] == 30.0

    mid_m = recommend_margins(20_000_000)
    assert mid_m["margin_pct"] == 20.0

    high_m = recommend_margins(80_000_000)
    assert high_m["margin_pct"] == 15.0


def test_rank_and_select_best_vendors():
    quotes = [
        {
            "vendor_name": "PT CMSI",
            "tier": "partner",
            "is_partner": True,
            "brand": "Iwaki",
            "model": "100ml",
            "landed_cost_idr": 150_000,
            "stock_status": "ready",
            "warranty_months": 12,
        },
        {
            "vendor_name": "Monotaro",
            "tier": "marketplace",
            "brand": "Iwaki",
            "model": "100ml",
            "landed_cost_idr": 165_000,
            "stock_status": "ready",
            "warranty_months": 12,
        },
        {
            "vendor_name": "Distributor X",
            "tier": "general_competitor",
            "brand": "Duran",
            "model": "100ml Premium",
            "landed_cost_idr": 210_000,
            "stock_status": "ready",
            "warranty_months": 12,
        },
    ]

    res = rank_and_select_best_vendors(quotes)
    assert res["valid_candidates"] == 3
    # PT CMSI has lowest price and partner bonus -> should rank 1st
    assert res["ranked_quotes"][0]["vendor_name"] == "PT CMSI"
    assert res["selected_hpp"] == 150_000
    assert res["pricing"]["sell_price"] > 150_000
    # Duran is different brand and higher price -> should be best_premium
    assert res["best_premium"] is not None
    assert res["best_premium"]["brand"] == "Duran"


@pytest.mark.asyncio
async def test_sourcing_engine_e2e_min_three_vendors_and_partner_cmsi(tmp_path):
    db_path = tmp_path / "test_sourcing.db"
    state = StateManager(str(db_path))
    await state.init_db()

    # Seed vendors
    await state.seed_default_vendors_if_empty()

    mock_brain = MagicMock()
    mock_brain.think_json = AsyncMock(return_value={
        "canonical_name": "Labu Ukur 100ml Borosilikat",
        "key_specs": "Kaca borosilikat 3.3, akurasi kelas A, volume 100 ml",
        "vendors": [
            {
                "vendor_name": "Monotaro Indonesia",
                "brand": "Iwaki",
                "model": "Volumetric Flask 100ml",
                "unit_price_idr": 140000,
                "ppn_included": True,
                "stock_status": "ready",
                "lead_time_days": "2-3 hari",
                "warranty_months": 12,
                "tier": "marketplace",
                "source_url": "https://monotaro.id/sample",
            },
            {
                "vendor_name": "PT Telagasakti Sakatautama",
                "brand": "Merck",
                "model": "Volumetric Flask 100ml",
                "unit_price_idr": 180000,
                "ppn_included": True,
                "stock_status": "ready",
                "lead_time_days": "3-5 hari",
                "warranty_months": 12,
                "tier": "principal",
                "source_url": "https://telagasakti.co.id/sample",
            },
        ]
    })
    mock_brain.think = AsyncMock(return_value="{}")

    env = SimpleNamespace(tavily_api_key="", firecrawl_api_key="")
    engine = SourcingEngine(brain=mock_brain, env=env, state=state)

    # Inquire about glassware -> PT CMSI must be auto-included as partner!
    res = await engine.source_item(
        raw_query="Pak Ahnaf tolong carikan labu ukur Iwaki 100ml ya dari RSUD Cibinong",
        client_name="Dr. Hendra",
        institution="RSUD Cibinong",
    )

    # 1. Clean query must not leak RSUD Cibinong or Hendra
    assert "RSUD Cibinong" not in res["clean_query"]
    assert "Hendra" not in res["clean_query"]

    # 2. Minimum 3 vendors guaranteed
    assert len(res["ranked_quotes"]) >= 3

    # 3. PT CMSI partner is present for glassware
    vendor_names = [q["vendor_name"] for q in res["ranked_quotes"]]
    assert any("CMSI" in vn for vn in vendor_names)

    # 4. WhatsApp operator summary is populated
    assert "MATRIKS VENDOR PEMBANDING" in res["whatsapp_operator_summary"]
    assert "100% AMAN" in res["whatsapp_operator_summary"]
    assert "Rekomendasi Harga Jual" in res["whatsapp_operator_summary"]

    # 5. DB records saved in Price Memory
    quotes_in_db = await state.get_vendor_quotes_for_sourcing(res["sourcing_id"])
    assert len(quotes_in_db) >= 3


@pytest.mark.asyncio
async def test_operator_command_margin_update(tmp_path):
    db_path = tmp_path / "test_cmd.db"
    state = StateManager(str(db_path))
    await state.init_db()

    # Insert a sourcing request
    sid = await state.create_sourcing_request({
        "clean_query": "Autoclave 50L",
        "selected_hpp": 20_000_000,
        "margin_pct": 20.0,
        "quoted_price": 24_000_000,
    })

    eng = WhatsAppEngine.__new__(WhatsAppEngine)
    eng.state = state
    eng.wa_cfg = SimpleNamespace(report_phone="087764021131")
    eng._send_text_auto = AsyncMock(return_value=True)

    # Operator texts "MARGIN 25"
    res = await eng.handle_operator_command("6287764021131", "MARGIN 25")
    assert res["status"] == "operator_command_margin_updated"
    assert res["margin_pct"] == 25.0

    # Verify DB was updated
    req = await state.get_sourcing_request(sid)
    assert req["margin_pct"] == 25.0
    assert req["quoted_price"] == 25_000_000.0  # 20M * 1.25
