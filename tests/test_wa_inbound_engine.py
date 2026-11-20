import asyncio
import json
import pytest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    return eng


@pytest.mark.asyncio
async def test_handle_single_inbound_creates_convo_and_outbox(tmp_path):
    db_path = tmp_path / "test.db"
    state = StateManager(db_path=db_path)
    await state.init_db()

    engine = make_engine(state)

    with patch.object(engine, "_send_text_auto", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True

        res = await engine.handle_single_inbound(
            phone="081299998888",
            message="Halo Pak Ahnaf, boleh minta katalog timbangan analitik dan daftar harganya?",
            title="Dr. Budi Utomo",
        )

        assert res["status"] == "ok"
        assert res["intent"] == "MINTA PENAWARAN (HOT LEAD)"
        assert res["human_takeover"] is False
        assert res["conversation_id"] is not None
        assert res["outbox_id"] is not None

        # Verify conversation in DB
        convo = await state.get_conversation(res["conversation_id"])
        assert convo is not None
        assert convo.channel == "whatsapp"
        assert convo.intent == "MINTA PENAWARAN (HOT LEAD)"
        assert len(convo.thread) == 1
        assert convo.thread[0].content == "Halo Pak Ahnaf, boleh minta katalog timbangan analitik dan daftar harganya?"

        # Verify outbox item in DB
        outbox_item = await state.get_outbox_item(res["outbox_id"])
        assert outbox_item is not None
        assert outbox_item["provider"] == "whatsapp"
        assert outbox_item["status"] == "pending_review"
        assert "sph" in outbox_item["body"].lower()

        # Verify review notification was sent to operator
        assert mock_send.call_count >= 1
        call_args = mock_send.call_args[0]
        assert "6287764021131" in call_args[0]
        assert "INBOUND WHATSAPP" in call_args[1]


@pytest.mark.asyncio
async def test_handle_single_inbound_operator_ignored(tmp_path):
    db_path = tmp_path / "test.db"
    state = StateManager(db_path=db_path)
    await state.init_db()

    engine = make_engine(state)

    res = await engine.handle_single_inbound(
        phone="087764021131",
        message="Test message from operator",
    )
    assert res["status"] == "ignored_operator"


@pytest.mark.asyncio
async def test_handle_single_inbound_human_takeover(tmp_path):
    db_path = tmp_path / "test.db"
    state = StateManager(db_path=db_path)
    await state.init_db()

    engine = make_engine(state)
    engine.wa_cfg.auto_reply_mode = "safe"
    engine.wa_cfg.human_takeover_enabled = True

    # Pre-populate conversation where operator replied 10 minutes ago
    pid = "p_test_takeover"
    past_10m = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()

    async with state._connect() as db:
        await db.execute(
            "INSERT INTO prospects (id, first_name, phone, status, personalization_notes) VALUES (?, ?, ?, 'replied', '')",
            (pid, "Siti", "6281311112222")
        )
        thread = [
            {"direction": "inbound", "text": "halo", "sender": "prospect", "timestamp": past_10m},
            {"direction": "outbound", "text": "Halo Bu Siti, saya Ahnaf langsung.", "sender": "operator", "timestamp": past_10m},
        ]
        await db.execute(
            "INSERT INTO conversations (id, prospect_id, channel, stage, status, thread_json, created_at, updated_at) VALUES (?, ?, 'whatsapp', 'inbound', 'open', ?, ?, ?)",
            ("c_takeover", pid, json.dumps(thread), past_10m, past_10m)
        )
        await db.commit()

    with patch.object(engine, "_send_text_auto", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        res = await engine.handle_single_inbound(
            phone="6281311112222",
            message="Katalognya yang mana ya mas?",
        )

        assert res["status"] == "ok"
        assert res["human_takeover"] is True
        assert res["auto_replied"] is False  # Bot auto-reply must be silenced due to takeover!
