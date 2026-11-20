"""Automated regression tests for Fase 0 send-gate, idempotency, and DB phone uniqueness."""

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from harvey.config import EnvConfig, HarveyConfig, ICPConfig, PersonaConfig, ProductConfig
from harvey.integrations.whatsapp import WhatsAppEngine
from harvey.state import StateManager


def _make_config() -> HarveyConfig:
    return HarveyConfig(
        persona=PersonaConfig(
            name="Alex Example",
            company="Example Lab Co",
            role="Account Specialist",
            email="alex@example.com",
            linkedin="linkedin.com/in/alex-example",
            tone="professional",
        ),
        product=ProductConfig(
            name="Example Lab Instrument",
            description="Laboratory equipment",
            pricing="Contact for quote",
            key_benefits=["Ready stock"],
            objection_responses={"price": "Flexible tiers"},
        ),
        icp=ICPConfig(
            industries=["Laboratory"],
            company_size="10-500",
            titles=["Lab Manager"],
            geography=["Jakarta"],
        ),
    )


def _setup_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test_harvey.db"
    db = StateManager(db_path)
    asyncio.run(db.init_db())
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_prospects_phone ON prospects(phone) WHERE phone IS NOT NULL AND phone != ''"
    )
    conn.commit()
    conn.close()
    return db_path


def test_referred_to_pic_blocks_main_contact(tmp_path: Path):
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(db_path)
    now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    conn.execute(
        """INSERT INTO prospects (id, first_name, company, phone, status, personalization_notes, created_at, updated_at)
           VALUES ('p_main', 'Alice', 'Example Lab', '6281200000001', 'opted_out', '[REFERRED_TO_PIC:6281200000099]', ?, ?)""",
        (now_iso, now_iso),
    )
    conn.commit()
    conn.close()

    wa = WhatsAppEngine(StateManager(db_path), _make_config(), EnvConfig())
    wa._send_single_text = AsyncMock(return_value=True)

    ok_gate, reason = asyncio.run(wa.can_contact_wa("6281200000001", is_reply=False))
    assert ok_gate is False
    assert reason == "opted_out_or_referred_to_pic"

    sent = asyncio.run(wa._send_text_auto("6281200000001", "Halo Example Lab", is_reply=True))
    assert sent is False
    wa._send_single_text.assert_not_called()


def test_idempotency_blocks_duplicate_message(tmp_path: Path):
    db_path = _setup_db(tmp_path)
    wa = WhatsAppEngine(StateManager(db_path), _make_config(), EnvConfig())
    wa._send_single_text = AsyncMock(return_value=True)

    first = asyncio.run(wa._send_text_auto("6281200000002", "Halo Example Corp, katalog siap."))
    second = asyncio.run(wa._send_text_auto("6281200000002", "Halo Example Corp, katalog siap."))
    assert first is True
    assert second is False
    assert wa._send_single_text.call_count == 1


def test_recent_outbound_20h_blocks_cold_but_allows_reply(tmp_path: Path):
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(db_path)
    now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    conn.execute(
        """INSERT INTO prospects (id, first_name, company, phone, status, created_at, updated_at)
           VALUES ('p_cold', 'Bob', 'Example Bio', '6281200000003', 'contacted', ?, ?)""",
        (now_iso, now_iso),
    )
    conn.execute(
        """INSERT INTO outbox (id, prospect_id, campaign_id, step, kind, to_email, subject, body, send_at, sent_at, status, provider, created_at)
           VALUES ('ob1', 'p_cold', 'c1', 1, 'sequence', '6281200000003', 'Intro', 'Halo', ?, ?, 'sent', 'whatsapp', ?)""",
        (now_iso, now_iso, now_iso),
    )
    conn.commit()
    conn.close()

    wa = WhatsAppEngine(StateManager(db_path), _make_config(), EnvConfig())
    ok_cold, reason_cold = asyncio.run(wa.can_contact_wa("6281200000003", is_reply=False))
    ok_reply, reason_reply = asyncio.run(wa.can_contact_wa("6281200000003", is_reply=True))
    assert ok_cold is False and reason_cold == "recent_outbound_20h"
    assert ok_reply is True and reason_reply == "ok"


def test_partner_exclusion_blocks_configured_partner(tmp_path: Path):
    db_path = _setup_db(tmp_path)
    cfg = _make_config()
    cfg.compliance.partner_exclusions = ["6281200000088"]
    wa = WhatsAppEngine(StateManager(db_path), cfg, EnvConfig())
    ok_gate, reason = asyncio.run(wa.can_contact_wa("6281200000088"))
    assert ok_gate is False
    assert reason == "partner_or_excluded"


def test_unique_phone_index_prevents_duplicates(tmp_path: Path):
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(db_path)
    now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    conn.execute(
        "INSERT INTO prospects (id, first_name, company, phone, status, created_at, updated_at) VALUES ('p1', 'A', 'Ex1', '6281200000004', 'new', ?, ?)",
        (now_iso, now_iso),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO prospects (id, first_name, company, phone, status, created_at, updated_at) VALUES ('p2', 'B', 'Ex2', '6281200000004', 'new', ?, ?)",
            (now_iso, now_iso),
        )
    conn.close()


def test_global_12h_cooldown_blocks_second_non_reply_across_paths(tmp_path: Path):
    db_path = _setup_db(tmp_path)
    wa = WhatsAppEngine(StateManager(db_path), _make_config(), EnvConfig())
    wa._send_single_text = AsyncMock(return_value=True)

    first = asyncio.run(wa._send_text_auto("6281200000005", "Pesan pertama ke klien", is_reply=False))
    second_diff_text = asyncio.run(wa._send_text_auto("6281200000005", "Pesan kedua dengan isi berbeda", is_reply=False))
    reply_allowed = asyncio.run(wa._send_text_auto("6281200000005", "Balasan langsung atas chat masuk", is_reply=True))
    assert first is True
    assert second_diff_text is False
    assert reply_allowed is True
    assert wa._send_single_text.call_count == 2
