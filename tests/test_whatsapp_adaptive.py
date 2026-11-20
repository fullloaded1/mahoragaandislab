import pytest
from datetime import datetime
from harvey.integrations.whatsapp import (
    build_intro_message,
    build_antiban_wa_message,
    build_followup_message,
    _industry_hook,
)


def test_adaptive_outreach_learns_from_email_status():
    """If prospect was already sent an email, WhatsApp outreach adapts to reference it."""
    # With email note
    msg_with_email = build_intro_message(
        first_name="Budi",
        last_name="Santoso",
        company="PT Kimia Medika",
        industry="pharma",
        notes="[EMAIL_SENT:2026-11-05] | [SCORE:85]",
        status="contacted",
    )
    assert "email" in msg_with_email.lower()
    assert "whatsapp" in msg_with_email.lower()
    assert "Budi" in msg_with_email or "Pak" in msg_with_email

    # Pure cold new prospect (no email)
    msg_cold = build_intro_message(
        first_name="Dewi",
        last_name="Lestari",
        company="Universitas Brawijaya",
        industry="pendidikan",
        notes="",
        status="new",
    )
    assert "email perkenalan" not in msg_cold.lower()
    assert "riset" in msg_cold.lower() or "praktikum" in msg_cold.lower() or "hps" in msg_cold.lower()


def test_adaptive_outreach_learns_from_inaproc_status():
    """If prospect has INAPROC tender notes, WhatsApp outreach leads with procurement compliance."""
    msg = build_intro_message(
        first_name="Agus",
        company="RSUD Cibinong",
        industry="hospital",
        notes="[INAPROC_MINIKOMPETISI:Paket-001]",
        status="queued",
    )
    assert "inaproc" in msg.lower() or "pengadaan" in msg.lower()


def test_industry_hooks_expanded():
    """Verify expanded industry hooks cover hospitals, universities, mining, and food."""
    assert "centrifuge" in _industry_hook("RSUD dr. Soetomo").lower() or "sterilisasi" in _industry_hook("RSUD dr. Soetomo").lower()
    assert "pangan" in _industry_hook("PT Indofood F&B").lower()
    assert "riset" in _industry_hook("Fakultas Biologi").lower() or "hps" in _industry_hook("Fakultas Biologi").lower()
    assert "minerba" in _industry_hook("PT Tambang Emas Mining").lower()


def test_followup_stages_adapt_gracefully():
    """Verify follow-up adapts from gentle spec assistance (stage 1) to graceful close (stage 3)."""
    # Stage 1: Soft check-in
    fu1 = build_followup_message(
        stage=1,
        first_name="Rina",
        company="PT Bio Farma",
        industry="farmasi",
    )
    assert "tertimbun" in fu1.lower() or "terlewat" in fu1.lower() or "tertumpuk" in fu1.lower() or "menyambung" in fu1.lower()

    # Stage 3+: Graceful polite close
    fu3 = build_followup_message(
        stage=3,
        first_name="Rina",
        company="PT Bio Farma",
        industry="farmasi",
    )
    assert "terakhir" in fu3.lower()
    assert "sukses selalu" in fu3.lower() or "santai saja" in fu3.lower()

