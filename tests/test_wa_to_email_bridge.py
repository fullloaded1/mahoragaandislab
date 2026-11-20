import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from harvey.state import StateManager
from harvey.integrations.whatsapp import WhatsAppEngine


def make_engine(state):
    eng = WhatsAppEngine.__new__(WhatsAppEngine)
    eng.state = state
    eng.wa_cfg = SimpleNamespace(
        enabled=True,
        report_phone="087764021131",
        auto_reply_mode="draft",
        max_daily_messages=50,
        warm_only=False,
        human_takeover_enabled=False,
    )
    eng.config = SimpleNamespace(
        channels=SimpleNamespace(
            whatsapp=eng.wa_cfg,
            email=SimpleNamespace(provider="smtp")
        ),
        persona=SimpleNamespace(name="Ahnaf Ibrahim", email="ahnafibrahim@andislabs.com")
    )
    eng.env = SimpleNamespace(
        openwa_base_url="http://localhost:2785",
        openwa_api_key="",
        openwa_session_id="default"
    )
    eng.openwa = None
    eng.obsidian = None
    return eng


@pytest.mark.asyncio
async def test_classify_and_build_inbound_reply_with_email(tmp_path):
    db_path = tmp_path / "test.db"
    state = StateManager(str(db_path))
    await state.init_db()

    engine = make_engine(state)

    msg = "Boleh kirimkan penawaran dan katalognya ke email procurement@labsejahtera.co.id ya"
    intent, reply, is_hot = engine.classify_and_build_inbound_reply("Bambang", msg)

    assert intent == "MINTA VIA EMAIL (EMAIL DIBERIKAN)"
    assert "procurement@labsejahtera.co.id" in reply
    assert is_hot is True
    assert "1.140+ item" in reply or "katalog" in reply.lower()


@pytest.mark.asyncio
async def test_handle_single_inbound_omnichannel_email_capture(tmp_path):
    db_path = tmp_path / "test.db"
    state = StateManager(str(db_path))
    await state.init_db()

    engine = make_engine(state)

    engine._send_text_auto = AsyncMock(return_value=True)
    engine.send_system_alert = AsyncMock(return_value=True)

    # Create dummy prospect
    pid = "p_test_wa_email"
    async with state._connect() as db:
        await db.execute(
            """INSERT INTO prospects (id, first_name, company, phone, status, created_at, updated_at)
               VALUES (?, 'Budi', 'PT Bio Lab Solusi', '6281234567890', 'contacted', datetime('now'), datetime('now'))""",
            (pid,)
        )
        await db.commit()

    inbound_text = "Tolong kirimkan pricelist ke budi.santoso@biolab.id ya Pak"
    res = await engine.handle_single_inbound(
        phone="081234567890",
        message=inbound_text,
    )

    assert res["status"] == "email_captured_and_staged"
    assert res["email"] == "budi.santoso@biolab.id"

    # Verify prospect was updated with email
    async with state._connect() as db:
        cur = await db.execute("SELECT email, email_status, status FROM prospects WHERE id = ?", (pid,))
        p_row = await cur.fetchone()
        assert p_row[0] == "budi.santoso@biolab.id"
        assert p_row[1] == "verified"
        assert p_row[2] == "replied"

        # Verify staged email in outbox
        cur = await db.execute(
            "SELECT to_email, subject, body, status, provider, approved_by FROM outbox WHERE prospect_id = ?",
            (pid,)
        )
        o_row = await cur.fetchone()
        assert o_row is not None
        assert o_row[0] == "budi.santoso@biolab.id"
        assert "Katalog" in o_row[1]
        assert "1.140+ item" in o_row[2]
        assert "https://www.andislab.com/promo-akhir-tahun" in o_row[2]
        assert o_row[3] == "approved"
        assert o_row[5] == "omnichannel_auto_wa"


@pytest.mark.asyncio
async def test_operator_command_email_approval(tmp_path):
    db_path = tmp_path / "test.db"
    state = StateManager(str(db_path))
    await state.init_db()

    engine = make_engine(state)
    engine._send_text_auto = AsyncMock(return_value=True)

    # Insert pending review email
    async with state._connect() as db:
        await db.execute(
            """INSERT INTO outbox (id, prospect_id, to_email, subject, body, status, provider, created_at, updated_at)
               VALUES ('outbox_em_1', 'p1', 'client@test.id', 'Penawaran Alat', 'Isi email', 'pending_review', 'smtp', datetime('now'), datetime('now'))"""
        )
        await db.commit()

    res = await engine.handle_operator_command("6287764021131", "OK EMAIL")
    assert res["status"] == "operator_command_email_approved"

    async with state._connect() as db:
        cur = await db.execute("SELECT status, approved_by FROM outbox WHERE id = 'outbox_em_1'")
        row = await cur.fetchone()
        assert row[0] == "approved"
        assert row[1] == "operator_wa"
