"""Tests for grouped, rate-limited SPH follow-ups. All data is fictional."""

from datetime import date, datetime

import pytz

from harvey.integrations.quote_chaser import build_group_message, group_quotes

TODAY = date(2026, 11, 3)


def _q(qid, phone, item, sent="2026-10-15", stage=0, last=None, status="pending",
       company="PT Contoh Lab", contact="Ibu Sari Dewi"):
    return {"id": qid, "phone": phone, "items_summary": item, "sent_date": sent,
            "stage": stage, "last_followup_at": last, "status": status,
            "company": company, "contact_name": contact}


def test_duplicates_for_same_phone_become_one_message():
    groups = group_quotes([
        _q("Q1", "628111000222", "Autoclave"),
        _q("Q2", "0811 1000 222", "Timbangan"),
    ], today=TODAY)
    assert len(groups) == 1
    assert groups[0]["items"] == ["Autoclave", "Timbangan"]
    assert groups[0]["ids"] == ["Q1", "Q2"]


def test_only_stage1_for_old_quotes_and_skips_replied_and_operator():
    groups = group_quotes([
        _q("Q1", "628111000222", "Autoclave"),
        _q("Q2", "628111000333", "Oven", status="replied"),
        _q("Q3", "087764021131", "Incubator"),
        _q("Q4", "", "Shaker"),
    ], today=TODAY)
    assert [g["due_stage"] for g in groups] == [1]
    assert groups[0]["phone"] == "628111000222"


def test_stage2_needs_gap_since_last_followup():
    too_soon = group_quotes([_q("Q1", "628111000222", "X", stage=1, last="2026-11-01")], today=TODAY)
    assert too_soon == []
    due = group_quotes([_q("Q1", "628111000222", "X", stage=1, last="2026-10-27")], today=TODAY)
    assert due and due[0]["due_stage"] == 2
    done = group_quotes([_q("Q1", "628111000222", "X", stage=2, last="2026-10-20")], today=TODAY)
    assert done == []


def test_group_message_mentions_item_and_person():
    g = group_quotes([_q("Q1", "628111000222", "Autoclave")], today=TODAY)[0]
    msg = build_group_message(g, now=pytz.timezone("Asia/Jakarta").localize(datetime(2026, 11, 3, 9)))
    assert "Bu Sari" in msg and "Autoclave" in msg and "tempo hari" in msg

