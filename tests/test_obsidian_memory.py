import pytest
from pathlib import Path

from harvey.integrations.obsidian_memory import ObsidianMemory


def test_obsidian_memory_initialization_and_playbooks(tmp_path):
    vault_dir = tmp_path / "MyObsidianVault"
    mem = ObsidianMemory(vault_path=vault_dir)

    assert (vault_dir / "Daily").exists()
    assert (vault_dir / "Profiling Calon client").exists()
    assert (vault_dir / "Adaptive Playbooks").exists()

    # Verify default playbooks seeded
    playbook_rsud = vault_dir / "Adaptive Playbooks" / "Archetype_RSUD_Dinkes.md"
    assert playbook_rsud.exists()
    content = playbook_rsud.read_text(encoding="utf-8")
    assert "RSUD & Dinas Kesehatan" in content
    assert "Ready Stock Gudang Bogor" in content


def test_obsidian_log_chat_event(tmp_path):
    vault_dir = tmp_path / "MyObsidianVault"
    mem = ObsidianMemory(vault_path=vault_dir)

    ok = mem.log_chat_event(
        phone="628123456789",
        name="dr. Andi",
        company="RSUD Cibinong",
        direction="inbound",
        message="Selamat pagi, apakah autoclave 50L ready stok?",
        intent="MINTA PENAWARAN (HOT LEAD)",
    )
    assert ok is True

    # Check daily file created
    daily_files = list((vault_dir / "Daily").glob("*.md"))
    assert len(daily_files) == 1
    content = daily_files[0].read_text(encoding="utf-8")
    assert "INBOUND" in content
    assert "dr. Andi" in content
    assert "autoclave 50L ready stok?" in content
    assert "MINTA PENAWARAN (HOT LEAD)" in content


def test_obsidian_update_customer_profile(tmp_path):
    vault_dir = tmp_path / "MyObsidianVault"
    mem = ObsidianMemory(vault_path=vault_dir)

    ok = mem.update_customer_profile(
        phone="628123456789",
        name="dr. Andi",
        company="RSUD Cibinong",
        segment="RSUD",
        status="replied",
        interested_products=["Autoclave 50L", "Centrifuge"],
        notes="Butuh pengiriman cepat untuk ruang sterilisasi",
        last_message="Mohon dikirimkan brosur dan penawaran resminya",
    )
    assert ok is True

    profile_file = vault_dir / "Profiling Calon client" / "RSUD Cibinong.md"
    assert profile_file.exists()
    content = profile_file.read_text(encoding="utf-8")
    assert "company: \"RSUD Cibinong\"" in content
    assert "[[Archetype_RSUD_Dinkes]]" in content
    assert "[[Autoclave 50L]]" in content
    assert "Mohon dikirimkan brosur" in content


def test_obsidian_get_playbook_context(tmp_path):
    vault_dir = tmp_path / "MyObsidianVault"
    mem = ObsidianMemory(vault_path=vault_dir)

    ctx_rs = mem.get_playbook_context(segment="RSUD", industry="Kesehatan")
    assert "RSUD & Dinas Kesehatan" in ctx_rs

    ctx_kampus = mem.get_playbook_context(segment="Universitas", industry="LPPM")
    assert "Kampus, Universitas & Balai Riset" in ctx_kampus

    ctx_pabrik = mem.get_playbook_context(segment="PT Industri Pangan", industry="Food")
    assert "Pabrik Industri, Pangan & Farmasi" in ctx_pabrik

