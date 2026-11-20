"""Unit & integration tests for Official Auto-SPH PDF & Excel Generator (Template Penawaran ANDISLAB)."""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from harvey.integrations.sph_generator import (
    SPHGenerator,
    format_idr,
    parse_specifications,
    terbilang_rupiah,
)
from harvey.integrations.openwa import OpenWAClient


def test_terbilang_and_format_idr():
    assert format_idr(19650000) == "19.650.000"
    assert format_idr(21811500) == "21.811.500"
    assert terbilang_rupiah(21811500) == "Dua Puluh Satu Juta Delapan Ratus Sebelas Ribu Lima Ratus Rupiah"
    assert terbilang_rupiah(1500000) == "Satu Juta Lima Ratus Ribu Rupiah"


def test_parse_specifications():
    raw_pipe = "With VT1.1 Standard Top Cup | Continuous or touch vortexing | Variable Speed 0-2500 rpm"
    specs = parse_specifications(raw_pipe)
    assert len(specs) == 3
    assert any("Speed" in k for k, _ in specs)

    raw_dict = {"Maximum Capacity": "220 g", "Readability": "0.1 mg"}
    specs_dict = parse_specifications(raw_dict)
    assert specs_dict == [("Maximum Capacity", "220 g"), ("Readability", "0.1 mg")]


@pytest.mark.asyncio
async def test_sph_generator_end_to_end(tmp_path):
    db_file = tmp_path / "test_harvey.db"
    archive_dir = tmp_path / "sph_archives"
    gen = SPHGenerator(db_path=db_file, archive_dir=archive_dir)

    quo_no = await gen.get_next_quotation_number()
    assert quo_no.startswith("QUO/")
    assert ".ASL/" in quo_no

    meta = gen.render_pdf(
        quotation_no=quo_no,
        company_name="PT Contoh Laboratorium Nusantara",
        contact_name="Bapak Contoh",
        contact_title="Kepala Laboratorium",
        address="Jl. Contoh Industri No. 10, Jakarta",
        email="procurement@example.com",
        phone="081200000001",
        items=[
            {
                "name": "Analytical Balance",
                "brand": "Aczet",
                "model": "224C",
                "qty": 1,
                "unit_price": 19650000,
                "specifications": {
                    "Maximum Capacity": "220 g",
                    "Readability": "0.1 mg (0.0001 g)",
                },
            }
        ],
        include_ppn=True,
    )

    pdf_file = Path(meta["pdf_path"])
    assert pdf_file.exists()
    assert pdf_file.stat().st_size > 2000
    assert meta["subtotal"] == 19650000.0
    assert meta["ppn_amount"] == 2161500.0
    assert meta["grand_total"] == 21811500.0
    assert meta["formatted_grand_total"] == "Rp 21.811.500"


@pytest.mark.asyncio
async def test_openwa_send_document_base64(tmp_path):
    sample_pdf = tmp_path / "SPH_Sample.pdf"
    sample_pdf.write_bytes(b"%PDF-1.4 sample content")

    client = OpenWAClient(base_url="http://localhost:2785", api_key="test-key", session_id="andisbot")
    client._resolve_session_id = AsyncMock(return_value="session-uuid-123")

    mock_resp = MagicMock()
    mock_resp.status_code = 201

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        ok = await client.send_document(
            phone="6281200000001",
            file_path=str(sample_pdf),
            caption="SPH Resmi",
        )
        assert ok is True
        called_json = mock_post.call_args.kwargs["json"]
        assert called_json["chatId"] == "6281200000001@c.us"
        assert called_json["mimetype"] == "application/pdf"
        assert called_json["filename"] == "SPH_Sample.pdf"
        assert len(called_json["base64"]) > 10

