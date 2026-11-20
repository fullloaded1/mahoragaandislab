"""Unit tests for INAPROC / LPSE Watcher integration.

All data used in tests is fictional.
"""

from harvey.integrations.inaproc_watcher import (
    InaprocPackage,
    InaprocWatcher,
    format_inaproc_alert_message,
    match_package_to_andislab,
)


def test_match_package_timbangan_high_score():
    cat, models, score = match_package_to_andislab(
        "Pengadaan Timbangan Analitik 220g dan Moisture Analyzer Laboratorium",
        hps_budget=50_000_000,
    )
    assert cat == "timbangan"
    assert "Aczet CY224C" in models
    assert score >= 75


def test_match_package_autoclave():
    cat, models, score = match_package_to_andislab(
        "Belanja Alat Sterilisasi Autoclave Uap 60L",
        hps_budget=75_000_000,
    )
    assert cat == "autoclave"
    assert "Daihan Labtech LAC-50455P" in models
    assert score >= 60


def test_match_unrelated_package():
    cat, models, score = match_package_to_andislab(
        "Pengadaan Alat Tulis Kantor dan Komputer Desktop",
        hps_budget=30_000_000,
    )
    assert score < 40


def test_format_alert_contains_details():
    pkg = InaprocPackage(
        package_id="TND-SAMPLE-1",
        package_name="Pengadaan Oven Pengering Lab",
        institution="Instansi Contoh",
        hps_budget=45_000_000,
        category_matched="oven_inkubator",
        matched_models=["Daihan Labtech LDO-060F"],
        match_score=85,
        procurement_type="e-Purchasing",
        location="Bogor",
        source_url="https://katalog.inaproc.id/sample",
        deadline="2026-12-01",
    )
    msg = format_inaproc_alert_message(pkg)
    assert "Instansi Contoh" in msg
    assert "Rp 45.000.000" in msg
    assert "OVEN_INKUBATOR" in msg
    assert "Daihan Labtech LDO-060F" in msg


def test_watcher_records_and_avoids_duplicate(tmp_path):
    watcher = InaprocWatcher(data_dir=tmp_path)
    sample = [
        {
            "id": "PKG-01",
            "name": "Pengadaan Timbangan Analitik",
            "budget": 35000000,
            "institution": "Dinas Contoh",
        }
    ]
    res1 = watcher.filter_and_record_packages(sample)
    assert len(res1) == 1

    # Second run with same package should not produce duplicate alert
    res2 = watcher.filter_and_record_packages(sample)
    assert len(res2) == 0


def test_match_package_mini_kompetisi_boost():
    cat, models, score_normal = match_package_to_andislab(
        "Pengadaan Laboratorium Mikrobiologi",
        procurement_type="Tender",
    )
    cat2, models2, score_mini = match_package_to_andislab(
        "Pengadaan Laboratorium Mikrobiologi",
        procurement_type="Mini Kompetisi E-Katalog",
    )
    assert score_mini > score_normal
    assert score_mini - score_normal == 25
    assert score_mini >= 70


def test_match_package_pengadaan_langsung_boost():
    cat, models, score_pl = match_package_to_andislab(
        "Pengadaan Langsung Timbangan Analitik Lab Lingkungan",
        hps_budget=45_000_000,
        procurement_type="Pengadaan Langsung",
    )
    assert cat == "timbangan"
    assert score_pl >= 80


def test_format_alert_mini_kompetisi():
    pkg = InaprocPackage(
        package_id="MK-001",
        package_name="Mini Kompetisi Paket Autoclave dan Sterilisasi",
        institution="Dinas Kesehatan",
        hps_budget=95_000_000,
        category_matched="autoclave",
        matched_models=["Daihan Labtech LAC-50455P"],
        match_score=90,
        procurement_type="Mini Kompetisi E-Katalog",
        location="Bandung",
        source_url="https://spse.inaproc.id/sample",
        deadline="2026-11-20",
    )
    msg = format_inaproc_alert_message(pkg)
    assert "MINI KOMPETISI E-KATALOG DITEMUKAN" in msg
    assert "E-Katalog LKPP v5/v6" in msg


def test_format_alert_pengadaan_langsung():
    pkg = InaprocPackage(
        package_id="PL-002",
        package_name="Pengadaan Langsung Oven Pengering Laboratorium",
        institution="Politeknik Negeri",
        hps_budget=60_000_000,
        category_matched="oven_inkubator",
        matched_models=["Daihan Labtech LDO-060F"],
        match_score=85,
        procurement_type="Pengadaan Langsung",
        location="Jakarta",
        source_url="https://lpse.sample.go.id",
        deadline="2026-11-25",
    )
    msg = format_inaproc_alert_message(pkg)
    assert "PENGADAAN LANGSUNG (PL) DITEMUKAN" in msg
    assert "SPH resmi ber-KOP AndisLab" in msg


def test_heuristic_package_extraction(tmp_path):
    watcher = InaprocWatcher(data_dir=tmp_path)
    search_data = [
        {
            "title": "Pengumuman Mini Kompetisi Pengadaan Alat Laboratorium Mikrobiologi",
            "snippet": "Dinas Lingkungan Hidup. Pagu Rp. 90.000.000,00. 15 November 2026.",
            "link": "https://spse.inaproc.id/probolinggokota/pengumuman/10015458000",
        }
    ]
    parsed = watcher._heuristic_package_extraction(search_data)
    assert len(parsed) == 1
    assert parsed[0]["id"] == "10015458000"
    assert parsed[0]["budget"] == 90000000.0
    assert "Mini Kompetisi" in parsed[0]["procurement_type"]


