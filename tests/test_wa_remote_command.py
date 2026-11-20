import pytest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from harvey.integrations.whatsapp import WhatsAppEngine
from harvey.state import StateManager


def make_engine(state):
    eng = WhatsAppEngine.__new__(WhatsAppEngine)
    eng.state = state
    eng.wa_cfg = SimpleNamespace(
        enabled=True,
        report_phone="087764021131",
        auto_reply_mode="draft",
        max_daily_messages=30,
        inbound_scan_enabled=True,
        quote_followup_enabled=True,
        provider="web",
    )
    eng.env = SimpleNamespace(fonnte_api_key="")
    eng.openwa = None
    eng.obsidian = None
    return eng


@pytest.mark.asyncio
async def test_operator_command_ok_sends_pending_outbox(tmp_path):
    db_path = tmp_path / "test.db"
    state = StateManager(db_path=db_path)
    await state.init_db()

    # Create dummy prospect and conversation
    pid = "p_lead_1"
    cid = "c_lead_1"
    async with state._connect() as db:
        await db.execute(
            """INSERT INTO prospects (id, first_name, last_name, company, phone, status, created_at, updated_at)
               VALUES (?, 'dr. Agus', '', 'RSUD Cibinong', '081233334444', 'replied', '2026-11-09T00:00:00', '2026-11-09T00:00:00')""",
            (pid,)
        )
        await db.execute(
            """INSERT INTO conversations (id, prospect_id, channel, stage, status, created_at, updated_at)
               VALUES (?, ?, 'whatsapp', 'inbound', 'open', '2026-11-09T00:00:00', '2026-11-09T00:00:00')""",
            (cid, pid)
        )
        await db.commit()

    # Add pending outbox item
    oid = await state.add_outbox_item(
        prospect_id=pid,
        conversation_id=cid,
        kind="reply",
        to_email="081233334444",
        subject="Balasan WhatsApp",
        body="Halo Dokter, berikut kami siapkan spesifikasi lengkap autoclave 50L.",
        send_at="2026-11-09T00:00:00",
        status="pending_review",
        provider="whatsapp",
    )

    engine = make_engine(state)

    with patch.object(engine, "_send_text_auto", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True

        res = await engine.handle_single_inbound(
            phone="087764021131",
            message="OK",
        )

        assert res["status"] == "operator_command_sent"
        assert res["outbox_id"] == oid

        # Verify outbox item was updated to sent
        item = await state.get_outbox_item(oid)
        assert item["status"] == "sent"
        assert item["approved_by"] == "operator_wa"

        # Verify messages sent: 1 to client, 1 confirmation to operator
        assert mock_send.call_count == 2
        sent_destinations = [call[0][0] for call in mock_send.call_args_list]
        assert any("081233334444" in d or "6281233334444" in d for d in sent_destinations)
        assert any("6287764021131" in d for d in sent_destinations)


@pytest.mark.asyncio
async def test_operator_command_revisi_edits_and_sends(tmp_path):
    db_path = tmp_path / "test.db"
    state = StateManager(db_path=db_path)
    await state.init_db()

    pid = "p_lead_2"
    cid = "c_lead_2"
    async with state._connect() as db:
        await db.execute(
            """INSERT INTO prospects (id, first_name, last_name, company, phone, status, created_at, updated_at)
               VALUES (?, 'Pak Joko', '', 'PT Biofarmaka', '081566667777', 'replied', '2026-11-09T00:00:00', '2026-11-09T00:00:00')""",
            (pid,)
        )
        await db.execute(
            """INSERT INTO conversations (id, prospect_id, channel, stage, status, created_at, updated_at)
               VALUES (?, ?, 'whatsapp', 'inbound', 'open', '2026-11-09T00:00:00', '2026-11-09T00:00:00')""",
            (cid, pid)
        )
        await db.commit()

    oid = await state.add_outbox_item(
        prospect_id=pid,
        conversation_id=cid,
        kind="reply",
        to_email="081566667777",
        subject="Balasan WhatsApp",
        body="Draf awal yang akan direvisi",
        send_at="2026-11-09T00:00:00",
        status="pending_review",
        provider="whatsapp",
    )

    engine = make_engine(state)

    with patch.object(engine, "_send_text_auto", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True

        revised_text = "Halo Pak Joko, untuk centrifuge kapasitas 15ml ready di Bogor ya Pak."
        res = await engine.handle_single_inbound(
            phone="087764021131",
            message=f"REVISI: {revised_text}",
        )

        assert res["status"] == "operator_command_revised_sent"
        assert res["outbox_id"] == oid

        # Verify outbox item has revised body and is marked sent
        item = await state.get_outbox_item(oid)
        assert item["status"] == "sent"
        assert item["body"] == revised_text
        assert item["approved_by"] == "operator_wa_revised"


@pytest.mark.asyncio
async def test_operator_command_batal_cancels_outbox(tmp_path):
    db_path = tmp_path / "test.db"
    state = StateManager(db_path=db_path)
    await state.init_db()

    pid = "p_lead_3"
    async with state._connect() as db:
        await db.execute(
            """INSERT INTO prospects (id, first_name, last_name, company, phone, status, created_at, updated_at)
               VALUES (?, 'Bu Rina', '', 'Lab Lingkungan', '081988889999', 'replied', '2026-11-09T00:00:00', '2026-11-09T00:00:00')""",
            (pid,)
        )
        await db.commit()

    oid = await state.add_outbox_item(
        prospect_id=pid,
        kind="reply",
        to_email="081988889999",
        subject="Balasan WhatsApp",
        body="Pesan yang akan dibatalkan",
        send_at="2026-11-09T00:00:00",
        status="pending_review",
        provider="whatsapp",
    )

    engine = make_engine(state)

    with patch.object(engine, "_send_text_auto", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True

        res = await engine.handle_single_inbound(
            phone="087764021131",
            message="BATAL",
        )

        assert res["status"] == "operator_command_cancelled"
        assert res["outbox_id"] == oid

        item = await state.get_outbox_item(oid)
        assert item["status"] == "cancelled"
        assert item["approved_by"] == "operator_wa_cancelled"

        # Verify prospect tagged WA_SKIP
        prospect = await state.get_prospect(pid)
        assert "WA_SKIP" in (prospect.personalization_notes or "")

