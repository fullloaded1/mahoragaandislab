"""Tests for WhatsApp human-style messages and follow-up scheduling.

All names, companies and numbers here are fictional.
"""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytz

from harvey.integrations.whatsapp import (
    WhatsAppEngine,
    build_followup_message,
    build_full_promo_message,
    build_intro_message,
)

JKT = pytz.timezone("Asia/Jakarta")


def _at(hour):
    return JKT.localize(datetime(2026, 11, 3, hour, 0))


def test_intro_is_short_personal_and_link_free():
    msg = build_intro_message("Budi", "PIC", "PT Contoh Lab", "Pharmaceutical", seed="628111", now=_at(9))
    assert msg.startswith(("Selamat pagi Pak Budi", "Halo Pak Budi"))
    assert "PT Contoh Lab" in msg
    assert "http" not in msg and "Diskon 8%" not in msg
    assert len(msg) < 450


def test_intro_greeting_follows_time_and_generic_name_falls_back():
    msg = build_intro_message("Admin", "", "PT Contoh Lab", "", seed="x", now=_at(14))
    assert "Pak/Bu" in msg and "Admin" not in msg
    assert "siang" in msg.lower()


def test_intro_is_stable_per_seed():
    a = build_intro_message("Sari", "", "PT Contoh", "", seed="628222", now=_at(9))
    b = build_intro_message("Sari", "", "PT Contoh", "", seed="628222", now=_at(9))
    assert a == b


def test_full_promo_text_kept_for_replied_prospects():
    msg = build_full_promo_message("Budi", "PT Contoh Lab")
    assert "Diskon 8%" in msg and "andislab.com/promo-akhir-tahun" in msg


def test_followup_messages():
    one = build_followup_message(1, "Budi", "", "PT Contoh Lab", with_link=False, now=_at(9), seed="test1")
    assert "flyer" not in one.lower()
    assert "http" not in one
    assert "AndisLab" in one or "PT Andis Sentral Laboratorium" in one
    assert "http" in build_followup_message(1, "Budi", "", "PT Contoh Lab", with_link=True, now=_at(9), seed="test1")
    two = build_followup_message(2, "Budi", "", "PT Contoh Lab", now=_at(9))
    assert "terakhir" in two


class _FakeState:
    def __init__(self, rows):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(
            "CREATE TABLE prospects (id, first_name, last_name, company, phone, "
            "personalization_notes, status, created_at)"
        )
        self.con.executemany("INSERT INTO prospects VALUES (?,?,?,?,?,?,?,?)", rows)

    @asynccontextmanager
    async def _connect(self):
        con = self.con

        class _DB:
            async def execute(self, sql, params=()):
                cur = con.execute(sql, params)

                class _Cur:
                    async def fetchall(_s):
                        return cur.fetchall()

                return _Cur()

            async def commit(self):
                con.commit()

        yield _DB()


def _engine(rows):
    wa = SimpleNamespace(
        followup_since="2026-11-03",
        followup1_after_days=3,
        followup2_after_days=7,
        report_phone="087764021131",
    )
    eng = WhatsAppEngine.__new__(WhatsAppEngine)
    eng.wa_cfg = wa
    eng.state = _FakeState(rows)
    return eng


def _row(pid, notes, status="contacted"):
    return (pid, "Budi", "", "PT Contoh", "628111000111", notes, status, "2026-11-01")


def test_followup_due_day3_then_day7_and_respects_replied_and_since():
    today = date.today()
    d = lambda n: (today - timedelta(days=n)).isoformat()
    eng = _engine([
        _row("old", f"[WA_SENT:2026-10-30]"),                     # before followup_since
        _row("fresh", f"[WA_SENT:{today.isoformat()}]"),            # too early
        _row("replied", f"[WA_SENT:{d(4)}] | [WA_REPLIED:{d(1)}]"),
    ])
    assert asyncio.run(eng._followup_due()) is None

    eng = _engine([_row("a", f"[WA_SENT:{d(4)}]")])
    eng.wa_cfg.followup_since = "2000-01-01"
    due = asyncio.run(eng._followup_due())
    assert due and due["id"] == "a" and due["stage"] == 1

    eng = _engine([_row("b", f"[WA_SENT:{d(8)}] | [WA_FU1:{d(5)}]")])
    eng.wa_cfg.followup_since = "2000-01-01"
    due = asyncio.run(eng._followup_due())
    assert due and due["stage"] == 2

    eng = _engine([_row("c", f"[WA_SENT:{d(9)}] | [WA_FU1:{d(6)}] | [WA_FU2:{d(1)}]")])
    eng.wa_cfg.followup_since = "2000-01-01"
    assert asyncio.run(eng._followup_due()) is None

