"""Tests for Give-First PDF Asset Offers in Harvey's WhatsApp Engine.

All names, companies, and phone numbers in this file are completely fictional.
"""

import asyncio
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytz

from harvey.integrations.whatsapp import (
    WhatsAppEngine,
    build_asset_caption,
    build_asset_fallback_text,
    build_followup_message,
    detect_lead_segment,
    find_pending_asset_offer,
    get_asset_by_id,
    is_affirmative_consent,
    load_asset_catalog,
    pick_asset_for,
)
from tests.test_whatsapp_humanize import _FakeState

JKT = pytz.timezone("Asia/Jakarta")


def _at(hour: int) -> datetime:
    return JKT.localize(datetime(2026, 11, 4, hour, 0))


def test_segment_mapping():
    """Verify company/industry keyword mapping to the 4 target segments."""
    # pemerintah
    assert detect_lead_segment("Pemerintah", "Dinas Kesehatan Daerah") == "pemerintah"
    assert detect_lead_segment("", "Pemkab Contoh Wilayah") == "pemerintah"
    assert detect_lead_segment("", "Balai Pengujian Standar") == "pemerintah"
    assert detect_lead_segment("Kesehatan", "Puskesmas Kecamatan Contoh") == "pemerintah"
    assert detect_lead_segment("", "Labkesda Contoh") == "pemerintah"

    # kampus
    assert detect_lead_segment("Pendidikan", "Universitas Contoh Indonesia") == "kampus"
    assert detect_lead_segment("", "Politeknik Negeri Contoh") == "kampus"
    assert detect_lead_segment("", "Institut Teknologi Contoh") == "kampus"
    assert detect_lead_segment("", "Sekolah Tinggi Farmasi Contoh") == "kampus"

    # rumah_sakit
    assert detect_lead_segment("", "RSUD Contoh Medika") == "rumah_sakit"
    assert detect_lead_segment("Medis", "Rumah Sakit Contoh Sehat") == "rumah_sakit"
    assert detect_lead_segment("", "Klinik Utama Contoh") == "rumah_sakit"
    assert detect_lead_segment("", "RS Contoh Sentosa") == "rumah_sakit"

    # pabrik_qc
    assert detect_lead_segment("Pharmaceutical", "PT Farmasi Contoh") == "pabrik_qc"
    assert detect_lead_segment("Food", "Pabrik Pengolahan Makanan") == "pabrik_qc"
    assert detect_lead_segment("Industri", "PT Manufaktur Contoh") == "pabrik_qc"
    assert detect_lead_segment("", "PT Kimia Analitika Contoh") == "pabrik_qc"


def test_pick_asset_for_segments():
    """Verify that pick_asset_for selects the appropriate asset based on customer segment."""
    catalog = load_asset_catalog()
    assert len(catalog) >= 3

    # Segment pemerintah -> checklist_spj_bast
    pemerintah_asset = pick_asset_for("Pemerintah", "Dinas Kesehatan Daerah", catalog=catalog)
    assert pemerintah_asset is not None
    assert "pemerintah" in pemerintah_asset["best_for"]
    assert pemerintah_asset["id"] == "checklist_spj_bast"

    # Segment kampus -> checklist_spj_bast or panduan_perawatan_kalibrasi
    kampus_asset = pick_asset_for("Pendidikan", "Universitas Contoh Riset", catalog=catalog)
    assert kampus_asset is not None
    assert "kampus" in kampus_asset["best_for"]

    # Segment pabrik_qc -> referensi_hps (or panduan_perawatan_kalibrasi)
    pabrik_asset = pick_asset_for("Manufaktur", "PT Industri Contoh", catalog=catalog)
    assert pabrik_asset is not None
    assert "pabrik_qc" in pabrik_asset["best_for"]
    assert pabrik_asset["id"] == "referensi_hps"


def test_stage_2_offer_text_has_title_question_no_http():
    """Stage 2 follow-up with an asset must contain asset title, a question, and no http links."""
    catalog = load_asset_catalog()
    kampus_asset = pick_asset_for("", "Universitas Contoh", catalog=catalog)
    pabrik_asset = pick_asset_for("", "PT Contoh Industri", catalog=catalog)

    # Kampus prospect offer
    msg_kampus = build_followup_message(
        stage=2,
        first_name="Budi",
        last_name="",
        company="Universitas Contoh",
        now=_at(9),
        asset=kampus_asset,
        seed="kampus_seed_1",
    )
    assert kampus_asset["title"] in msg_kampus
    assert "Pak Budi" in msg_kampus
    assert "boleh" in msg_kampus.lower() and "?" in msg_kampus
    assert "http" not in msg_kampus
    assert "terakhir" not in msg_kampus  # not the fallback text

    # Pabrik prospect offer
    msg_pabrik = build_followup_message(
        stage=2,
        first_name="Siti",
        last_name="",
        company="PT Contoh Industri",
        now=_at(14),
        asset=pabrik_asset,
        seed="pabrik_seed_2",
    )
    assert pabrik_asset["title"] in msg_pabrik
    assert "Bu Siti" in msg_pabrik
    assert "?" in msg_pabrik
    assert "http" not in msg_pabrik
    assert "terakhir" not in msg_pabrik


def test_fallback_when_index_missing(tmp_path, monkeypatch):
    """When catalog is missing or empty, pick_asset_for returns None and stage 2 falls back to polite close."""
    non_existent = tmp_path / "empty_catalog.yaml"
    loaded = load_asset_catalog(index_path=non_existent)
    assert loaded == []

    # pick_asset_for returns None
    asset = pick_asset_for("Pendidikan", "Universitas Contoh", catalog=[])
    assert asset is None

    # Stage 2 without asset falls back to the existing polite closing text
    fallback_msg = build_followup_message(
        stage=2,
        first_name="Budi",
        last_name="",
        company="PT Contoh Lab",
        asset=None,
        now=_at(9),
    )
    assert "terakhir" in fallback_msg
    assert "rencana pengadaan" in fallback_msg


def test_affirmative_vs_negative_detection():
    """Verify affirmative consent detection and negation filtering."""
    # Affirmative examples
    assert is_affirmative_consent("boleh pak") is True
    assert is_affirmative_consent("iya silakan kirim") is True
    assert is_affirmative_consent("ok") is True
    assert is_affirmative_consent("oke boleh") is True
    assert is_affirmative_consent("mau pak, tolong kirim") is True
    assert is_affirmative_consent("monggo dikirim mas") is True
    assert is_affirmative_consent("siap silakan") is True
    assert is_affirmative_consent("minta tolong kirim ya") is True
    assert is_affirmative_consent("ya boleh") is True

    # Negation examples (must return False)
    assert is_affirmative_consent("belum perlu") is False
    assert is_affirmative_consent("ga usah") is False
    assert is_affirmative_consent("tidak tertarik") is False
    assert is_affirmative_consent("jangan dulu") is False
    assert is_affirmative_consent("belum butuh pak") is False
    assert is_affirmative_consent("nggak perlu terima kasih") is False
    assert is_affirmative_consent("tidak usah kirim") is False

    # Neutral / non-consent
    assert is_affirmative_consent("") is False
    assert is_affirmative_consent("terima kasih infonya") is False
    assert is_affirmative_consent("saya sedang cuti") is False


def test_find_pending_asset_offer():
    """Verify parsing of notes tags to find pending asset offers."""
    # Pending offer
    notes1 = "[WA_SENT:2026-11-01] | [WA_FU1:2026-11-04] | [WA_FU2:2026-11-08] | [WA_ASSET_OFFER:checklist_spj_bast:2026-11-08]"
    pending = find_pending_asset_offer(notes1)
    assert pending == ("checklist_spj_bast", "2026-11-08")

    # Already fulfilled offer
    notes2 = f"{notes1} | [WA_ASSET_SENT:checklist_spj_bast:2026-11-09]"
    assert find_pending_asset_offer(notes2) is None

    # No asset offer in notes
    notes3 = "[WA_SENT:2026-11-01] | [WA_FU1:2026-11-04]"
    assert find_pending_asset_offer(notes3) is None

    # Multiple offers, one sent, one pending
    notes4 = (
        "[WA_ASSET_OFFER:checklist_spj_bast:2026-11-01] | "
        "[WA_ASSET_SENT:checklist_spj_bast:2026-11-02] | "
        "[WA_ASSET_OFFER:referensi_hps:2026-11-08]"
    )
    assert find_pending_asset_offer(notes4) == ("referensi_hps", "2026-11-08")


def test_classify_and_build_inbound_reply_with_asset_consent():
    """Verify classify_and_build_inbound_reply identifies consent and generates warm PDF caption."""
    notes = "[WA_SENT:2026-11-01] | [WA_FU2:2026-11-08] | [WA_ASSET_OFFER:referensi_hps:2026-11-08]"
    eng = WhatsAppEngine.__new__(WhatsAppEngine)

    intent, reply, is_hot = eng.classify_and_build_inbound_reply("Pak Budi", "boleh pak, silakan kirim", notes=notes)
    assert intent == "MINTA ASET"
    assert is_hot is True
    assert "Berikut Referensi Kisaran Harga (HPS)" in reply
    assert "Pak Budi" in reply
    assert "Semoga membantu tim" in reply


def test_send_document_via_web_guards():
    """Verify _send_document_via_web guards against operator phone and nonexistent PDF."""
    eng = WhatsAppEngine.__new__(WhatsAppEngine)
    eng.wa_cfg = SimpleNamespace(report_phone="087764021131")

    # Internal operator number guard
    res1 = asyncio.run(eng._send_document_via_web("087764021131", "caption", Path("nonexistent.pdf")))
    assert res1 == "error"

    # Nonexistent PDF guard
    res2 = asyncio.run(eng._send_document_via_web("628111000111", "caption", Path("nonexistent.pdf")))
    assert res2 == "error"


def test_run_one_followup_stage_2_records_asset_tag():
    """Verify that stage 2 follow-up tags [WA_FU2:date] AND [WA_ASSET_OFFER:<id>:date]."""
    today = date.today().isoformat()
    # Prospect contacted 8 days ago, FU1 sent 5 days ago -> due for stage 2
    past_sent = "2026-10-25"
    past_fu1 = "2026-10-28"
    notes = f"[WA_SENT:{past_sent}] | [WA_FU1:{past_fu1}]"
    row = ("prospect_1", "Budi", "Setiawan", "Universitas Contoh Lab", "628111000111", notes, "contacted", "2026-10-25")

    fake_state = _FakeState([row])
    eng = WhatsAppEngine.__new__(WhatsAppEngine)
    eng.wa_cfg = SimpleNamespace(
        followup_since="2020-01-01",
        followup1_after_days=3,
        followup2_after_days=7,
        report_phone="087764021131",
        max_daily_messages=20,
        flyer_path="data/assets/give_first/checklist_spj_bast.pdf",
    )
    eng.state = fake_state

    # Mock _send_via_web to succeed without launching browser
    sent_messages = []
    async def mock_send_via_web(phone, text):
        sent_messages.append((phone, text))
        return True

    eng._send_via_web = mock_send_via_web

    # Mock log_action on state
    async def mock_log_action(action_type, agent, details):
        pass
    fake_state.log_action = mock_log_action

    act = {"sent": 0, "numbers": []}
    ok = asyncio.run(eng._run_one_followup(act, use_fonnte=False))

    assert ok is True
    assert act["sent"] == 1
    assert "628111000111" in act["numbers"]

    # Check that stage 2 offer was sent with asset title
    assert len(sent_messages) == 1
    _ph, text = sent_messages[0]
    assert "Checklist Dokumen SPJ & BAST" in text

    # Verify updated notes in DB contain both tags
    cur = fake_state.con.execute("SELECT personalization_notes FROM prospects WHERE id = 'prospect_1'")
    updated_notes = cur.fetchone()[0]
    assert f"[WA_FU2:{today}]" in updated_notes
    assert f"[WA_ASSET_OFFER:checklist_spj_bast:{today}]" in updated_notes

