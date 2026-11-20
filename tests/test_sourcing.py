"""Unit tests for SourcingEngine (By Request procurement)."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from harvey.config import EnvConfig, HarveyConfig
from harvey.integrations.sourcing import SourcingEngine


@pytest.fixture
def fake_env():
    return EnvConfig(
        firecrawl_api_key="fc-test-key",
        tavily_api_key="tvly-test-key",
    )


@pytest.fixture
def mock_brain():
    brain = MagicMock()
    brain.think_json = AsyncMock()
    brain.think = AsyncMock()
    return brain


@pytest.mark.asyncio
async def test_search_suppliers_mocked(fake_env, mock_brain):
    engine = SourcingEngine(brain=mock_brain, env=fake_env)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "results": [
            {"title": "Vendor Lab Indonesia", "url": "https://vendorlab.co.id/item-1", "content": "Spek alat lab"},
        ]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        results = await engine.search_suppliers("Micropipette Eppendorf")
        assert len(results) == 1
        assert results[0]["title"] == "Vendor Lab Indonesia"
        assert results[0]["url"] == "https://vendorlab.co.id/item-1"


@pytest.mark.asyncio
async def test_scrape_page_mocked(fake_env, mock_brain):
    engine = SourcingEngine(brain=mock_brain, env=fake_env)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "success": True,
        "data": {
            "markdown": "# Eppendorf Research Plus\n\nKapasitas 100-1000 uL ready stock.",
        }
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        content = await engine.scrape_page("https://vendorlab.co.id/item-1")
        assert "Eppendorf Research Plus" in content
        assert "100-1000 uL" in content


@pytest.mark.asyncio
async def test_source_item_end_to_end(fake_env, mock_brain):
    engine = SourcingEngine(brain=mock_brain, env=fake_env)

    # Mock search & scrape
    engine.search_suppliers = AsyncMock(return_value=[
        {"title": "Distributor Resmi", "url": "https://distributor.co.id/alat-a", "content": "Harga Rp 50.000.000"}
    ])
    engine.scrape_page = AsyncMock(return_value="Spesifikasi teknis lengkap alat A...")

    # Mock brain think_json
    mock_brain.think_json.return_value = {
        "official_name": "Alat Uji Spektro",
        "brand": "BrandX",
        "model_or_cat_no": "BX-100",
        "key_specs": "Spek lengkap",
        "estimated_cost_idr": 50000000,
        "lead_time_days": "7-14 hari kerja",
        "supplier_references": [{"name": "Distributor Resmi", "url": "https://distributor.co.id/alat-a"}],
        "brochure_urls": [],
        "consultative_response": "Halo Bapak, barang ini berstatus Special Order..."
    }

    res = await engine.source_item(
        item_query="Alat Uji Spektro",
        client_name="Dr. Ahmad",
        institution="Lab Farmasi",
        margin_pct=0.20
    )

    assert res["official_name"] == "Alat Uji Spektro"
    assert res["brand"] == "BrandX"
    pricing = res["pricing"]
    assert pricing["margin_pct"] == 20.0
    assert pricing["estimated_hpp"] == 50000000.0
    assert pricing["estimated_quote_price"] == 60000000.0
    assert "Rp 50.000.000" in pricing["formatted_hpp"]
    assert "Rp 60.000.000" in pricing["formatted_quote_price"]
    assert "Special Order" in res["consultative_response"]

