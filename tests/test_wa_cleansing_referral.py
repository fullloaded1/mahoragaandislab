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
async def test_salah_sambung_auto_cleansing(tmp_path):
    db_path = tmp_path / "test.db"
    state = StateManager(db_path=db_path)
    await state.init_db()

    # Create dummy prospect
    pid = "p_dummy_cleansing"
    phone = "081299991111"
    async with state._connect() as db:
        await db.execute(
            """INSERT INTO prospects (id, first_name, last_name, company, phone, status, created_at, updated_at)
               VALUES (?, 'Budi', 'Utomo', 'Lab Sehat', ?, 'sent', '2026-11-09T00:00:00', '2026-11-09T00:00:00')""",
            (pid, phone)
        )
        await db.commit()

    engine = make_engine(state)

    with patch.object(engine, "_send_text_auto", new_callable=AsyncMock) as mock_send, \
         patch.object(engine, "send_system_alert", new_callable=AsyncMock) as mock_alert:
        mock_send.return_value = True
        mock_alert.return_value = True

        res = await engine.handle_single_inbound(
            phone=phone,
            message="Maaf mas ini salah sambung, bukan nomor lab atau bagian pengadaan",
        )

        assert res["status"] == "cleansed"
        assert res["intent"] == "SALAH SAMBUNG"

        # Verify prospect status is opted_out
        prospect = await state.get_prospect(pid)
        assert prospect.status == "opted_out"
        assert "CLEANSING:SALAH_SAMBUNG" in (prospect.personalization_notes or "")

        # Verify polite apology was sent
        assert mock_send.call_count >= 1
        sent_phone, sent_text = mock_send.call_args_list[0][0][:2]
        assert "6281299991111" in sent_phone
        assert "kekeliruan" in sent_text.lower() or "dihapus" in sent_text.lower()

        # Verify operator alert was triggered
        assert mock_alert.call_count == 1
        assert "Auto-Cleansing" in mock_alert.call_args[0][0]


@pytest.mark.asyncio
async def test_sudah_resign_with_referral(tmp_path):
    db_path = tmp_path / "test.db"
    state = StateManager(db_path=db_path)
    await state.init_db()

    pid = "p_old_pic"
    phone = "081255554444"
    async with state._connect() as db:
        await db.execute(
            """INSERT INTO prospects (id, first_name, last_name, company, phone, status, created_at, updated_at)
               VALUES (?, 'Siti', 'Aminah', 'RS Sehat Medika', ?, 'sent', '2026-11-09T00:00:00', '2026-11-09T00:00:00')""",
            (pid, phone)
        )
        await db.commit()

    engine = make_engine(state)

    with patch.object(engine, "_send_text_auto", new_callable=AsyncMock) as mock_send, \
         patch.object(engine, "send_system_alert", new_callable=AsyncMock) as mock_alert:
        mock_send.return_value = True
        mock_alert.return_value = True

        res = await engine.handle_single_inbound(
            phone=phone,
            message="Saya sudah resign dari RS Sehat Medika, untuk urusan lab silakan hubungi Pak Hendra di 085288887777 ya",
        )

        assert res["status"] == "referral_created"
        assert res["intent"] == "REFERRAL BARU"

        # Verify old prospect is opted_out
        old_prospect = await state.get_prospect(pid)
        assert old_prospect.status == "opted_out"
        assert "RESIGNED_REFERRAL" in (old_prospect.personalization_notes or "")

        # Verify new prospect was created
        new_pid = res["new_prospect_id"]
        new_prospect = await state.get_prospect(new_pid)
        assert new_prospect is not None
        assert "Hendra" in new_prospect.first_name
        assert new_prospect.company == "RS Sehat Medika"
        assert "085288887777" in new_prospect.phone or "6285288887777" in new_prospect.phone
        assert new_prospect.source == "referral_inbound"

        # Verify thank you message sent to old PIC
        assert mock_send.call_count >= 1
        assert "terima kasih" in mock_send.call_args_list[0][0][1].lower()

        # Verify operator alert
        assert mock_alert.call_count == 1
        assert "Referral Baru Ditemukan" in mock_alert.call_args[0][0]


@pytest.mark.asyncio
async def test_sudah_resign_without_referral(tmp_path):
    db_path = tmp_path / "test.db"
    state = StateManager(db_path=db_path)
    await state.init_db()

    pid = "p_resigned_nophone"
    phone = "081377776666"
    async with state._connect() as db:
        await db.execute(
            """INSERT INTO prospects (id, first_name, last_name, company, phone, status, created_at, updated_at)
               VALUES (?, 'Dewi', 'Lestari', 'Balai Riset', ?, 'sent', '2026-11-09T00:00:00', '2026-11-09T00:00:00')""",
            (pid, phone)
        )
        await db.commit()

    engine = make_engine(state)

    with patch.object(engine, "_send_text_auto", new_callable=AsyncMock) as mock_send, \
         patch.object(engine, "send_system_alert", new_callable=AsyncMock) as mock_alert:
        mock_send.return_value = True
        mock_alert.return_value = True

        res = await engine.handle_single_inbound(
            phone=phone,
            message="Maaf saya sudah tidak kerja di balai riset lagi mas",
        )

        assert res["status"] == "resigned_asked_referral"
        assert res["intent"] == "SUDAH RESIGN (TANYA PENGGANTI)"

        # Verify old prospect marked opted_out
        old_prospect = await state.get_prospect(pid)
        assert old_prospect.status == "opted_out"
        assert "RESIGNED_NO_REFERRAL" in (old_prospect.personalization_notes or "")

        # Verify polite ask sent to prospect
        assert mock_send.call_count >= 1
        assert "menggantikan" in mock_send.call_args_list[0][0][1].lower() or "pic" in mock_send.call_args_list[0][0][1].lower()

