import pytest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from harvey.integrations.whatsapp import (
    strip_quoted_echo,
    is_closing_acknowledgment,
    split_message_into_bubbles,
    WhatsAppEngine,
)


def test_strip_quoted_echo():
    # 1. WhatsApp Web timestamp style
    sample_wa = "[14.04, 10/11/2026] andislab: Selamat siang Pak/Bu...\n\nuntuk saat ini kami bisa dihandl oleh tim ya pak"
    assert strip_quoted_echo(sample_wa) == "untuk saat ini kami bisa dihandl oleh tim ya pak"

    # 2. Markdown quote style
    sample_md = "> Boleh minta katalog alat lab?\nAda autoclave 50 liter?"
    assert strip_quoted_echo(sample_md) == "Ada autoclave 50 liter?"

    # 3. Sender prefix style
    sample_prefix = "Ahnaf: Halo pak ada promo\nKatalognya boleh dikirimkan ke email?"
    assert strip_quoted_echo(sample_prefix) == "Katalognya boleh dikirimkan ke email?"

    # 4. Clean message unchanged
    clean = "Berapa harga rotary evaporator set?"
    assert strip_quoted_echo(clean) == clean


def test_is_closing_acknowledgment():
    assert is_closing_acknowledgment("terima kasih") is True
    assert is_closing_acknowledgment("Terima kasih banyak pak 🙏") is True
    assert is_closing_acknowledgment("baik pak") is True
    assert is_closing_acknowledgment("siap pak terima kasih") is True
    assert is_closing_acknowledgment("noted pak.") is True
    assert is_closing_acknowledgment("oke mas") is True
    assert is_closing_acknowledgment("sama-sama pak") is True
    assert is_closing_acknowledgment("terima kasih atas kerjasamanya") is True

    # Real inquiries must NOT be classified as closing
    assert is_closing_acknowledgment("berapa harga autoclave?") is False
    assert is_closing_acknowledgment("bisa kirim katalog?") is False
    assert is_closing_acknowledgment("terima kasih, tolong kirimkan penawaran ya") is False


@pytest.mark.asyncio
async def test_adaptive_reply_closing_ack_zero_token():
    eng = WhatsAppEngine.__new__(WhatsAppEngine)
    eng.config = None
    eng.state = None
    eng.obsidian = None

    intent, reply, is_hot, send_pdf = await eng.generate_adaptive_reply(
        sender_name="Budi",
        company="PT Maju",
        incoming_message="Terima kasih banyak pak atas infonya 🙏",
    )

    assert intent == "CLOSING_ACK"
    assert reply == ""
    assert is_hot is False
    assert send_pdf is False


@pytest.mark.asyncio
async def test_handle_single_inbound_closing_ack_no_outbox(tmp_path):
    from harvey.state import StateManager
    db_path = tmp_path / "test.db"
    state = StateManager(db_path=db_path)
    await state.init_db()

    eng = WhatsAppEngine.__new__(WhatsAppEngine)
    eng.state = state
    eng.wa_cfg = SimpleNamespace(
        enabled=True,
        report_phone="087764021131",
        auto_reply_mode="safe",
        max_daily_messages=30,
        inbound_scan_enabled=True,
        quote_followup_enabled=True,
        provider="web",
    )
    eng.env = SimpleNamespace(fonnte_api_key="")
    eng.openwa = None
    eng.obsidian = None
    eng.config = None

    with patch.object(eng, "_send_text_auto", new_callable=AsyncMock) as mock_send:
        res = await eng.handle_single_inbound(
            phone="081234567890",
            message="[14.04, 10/11/2026] andislab: Halo pak\n\nBaik pak terima kasih banyak 🙏",
            title="Dr. Anton",
        )

        assert res["status"] == "closing_ack_acknowledged"
        assert res["intent"] == "CLOSING_ACK"
        # No message should be sent
        mock_send.assert_not_called()

        # No pending outbox should be created
        async with state._connect() as db:
            cur = await db.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending_review'")
            count = (await cur.fetchone())[0]
            assert count == 0


def test_split_message_into_bubbles():
    # 1. Multi-paragraph message should split by paragraph
    msg = (
        "Halo selamat siang Bapak/Ibu.\n\n"
        "Perkenalkan saya dari PT Andis Sentral Laboratorium, distributor resmi alat lab dan reagen di Jabodetabek.\n\n"
        "Boleh kami izin kirimkan katalog lengkap dan daftar harga pembanding via PDF ke nomor ini?"
    )
    bubbles = split_message_into_bubbles(msg)
    assert len(bubbles) == 3
    assert bubbles[0] == "Halo selamat siang Bapak/Ibu."
    assert "distributor resmi" in bubbles[1]
    assert "katalog lengkap" in bubbles[2]

    # 2. Operational alert / card MUST NOT be split
    alert = (
        "*[HARVEY SYSTEM ALERT]*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔥 SPH QUO-038 Closing Won!\n"
        "Nilai: Rp 450.000.000"
    )
    alert_bubbles = split_message_into_bubbles(alert)
    assert len(alert_bubbles) == 1
    assert alert_bubbles[0] == alert.strip()

    # 3. Short single sentence remains 1 bubble
    short_msg = "Baik Bapak, siap kami siapkan penawarannya."
    assert split_message_into_bubbles(short_msg) == [short_msg]

