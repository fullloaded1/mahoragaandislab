"""Unit tests for Dedicated INAPROC E-Katalog v6 & Mini Kompetisi Agent.

All test data is fictional.
"""

import pytest
from pathlib import Path
from harvey.agents.inaproc_agent import InaprocAgent
from harvey.config import load_config, load_env
from harvey.state import StateManager


@pytest.mark.asyncio
async def test_agent_initialization(tmp_path):
    config = load_config()
    env = load_env()
    state = StateManager(db_path=str(tmp_path / "test.db"))
    await state.init_db()

    agent = InaprocAgent(state, config, env, min_margin=0.20)
    assert agent.min_margin == 0.20
    assert agent.portal_version == "v6"
    assert agent.is_profile_present() is False


@pytest.mark.asyncio
async def test_session_status_unauthenticated(tmp_path):
    config = load_config()
    env = load_env()
    state = StateManager(db_path=str(tmp_path / "test.db"))
    await state.init_db()

    agent = InaprocAgent(state, config, env)
    status = await agent.check_session_status()
    assert status["logged_in"] is False
    assert "Belum Login" in status["status_text"]


@pytest.mark.asyncio
async def test_match_product_and_pricing_with_20_margin(tmp_path):
    config = load_config()
    env = load_env()
    state = StateManager(db_path=str(tmp_path / "test.db"))
    await state.init_db()

    # Seed mock inventory
    async with state._connect() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id TEXT PRIMARY KEY,
                name TEXT,
                brand TEXT,
                model TEXT,
                catalog_no TEXT,
                category TEXT,
                specifications TEXT,
                unit TEXT,
                harga_beli REAL,
                price REAL,
                status_stok TEXT,
                available INTEGER,
                supplier TEXT,
                description TEXT,
                keywords TEXT
            )
        """)
        await db.execute("""
            INSERT INTO inventory (id, name, brand, model, harga_beli, price, status_stok, specifications)
            VALUES ('INV-01', 'Autoclave 50L Sterilizer', 'Daihan Labtech', 'LAC-50455P', 40000000.0, 52000000.0, 'Ready Stock', '50 Liter Steam')
        """)
        await db.commit()

    agent = InaprocAgent(state, config, env, min_margin=0.20)
    match = await agent.match_product_and_pricing("Belanja Modal Pengadaan Autoclave Sterilisasi Uap", hps_budget=60000000.0)

    assert match is not None
    assert match["eligible"] is True
    assert match["matched_id"] == "INV-01"
    assert match["cost_hpp"] == 40000000.0
    assert match["floor_price"] == 48000000.0  # HPP * 1.20
    assert match["target_price"] >= 48000000.0  # Above 20% margin
    assert match["margin_pct"] >= 20.0


@pytest.mark.asyncio
async def test_pricing_guardrail_rejects_below_20_margin(tmp_path):
    config = load_config()
    env = load_env()
    state = StateManager(db_path=str(tmp_path / "test.db"))
    await state.init_db()

    async with state._connect() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id TEXT PRIMARY KEY,
                name TEXT,
                brand TEXT,
                model TEXT,
                harga_beli REAL,
                price REAL,
                status_stok TEXT,
                specifications TEXT
            )
        """)
        await db.execute("""
            INSERT INTO inventory (id, name, brand, model, harga_beli, price, status_stok, specifications)
            VALUES ('INV-02', 'Timbangan Analitik 220g', 'Aczet', 'CY224C', 20000000.0, 26000000.0, 'Ready Stock', '0.1mg')
        """)
        await db.commit()

    agent = InaprocAgent(state, config, env, min_margin=0.20)
    # HPS 21.000.000 is lower than floor price 24.000.000 (HPP 20jt + 20%)
    match = await agent.match_product_and_pricing("Pengadaan Timbangan Analitik", hps_budget=21000000.0)

    assert match is not None
    assert match["eligible"] is False
    assert "melebihi HPS" in match["reason"]


@pytest.mark.asyncio
async def test_record_and_get_inaproc_bids(tmp_path):
    config = load_config()
    env = load_env()
    state = StateManager(db_path=str(tmp_path / "test.db"))
    await state.init_db()

    bid_data = {
        "id": "MK-TEST-001",
        "package_name": "Mini Kompetisi Pengadaan Oven Lab",
        "institution": "Universitas Indonesia",
        "hps_budget": 50000000.0,
        "item_requirements": [{"name": "Oven Pengering"}],
        "matched_inventory_id": "INV-03",
        "matched_product_name": "Daihan Oven LDO-060F",
        "cost_hpp": 25000000.0,
        "target_price": 32000000.0,
        "margin_pct": 21.8,
        "status": "ready_to_submit",
        "source_url": "https://katalog.inaproc.id/sample",
        "deadline": "2026-11-20",
    }
    await state.record_inaproc_bid(bid_data)

    bids = await state.get_inaproc_bids()
    assert len(bids) == 1
    assert bids[0]["id"] == "MK-TEST-001"
    assert bids[0]["status"] == "ready_to_submit"
    assert bids[0]["margin_pct"] == 21.8

    # Update status
    await state.update_inaproc_bid_status("MK-TEST-001", "submitted", notes="Submitted OK")
    updated = await state.get_inaproc_bids(status="submitted")
    assert len(updated) == 1
    assert updated[0]["notes"] == "Submitted OK"

