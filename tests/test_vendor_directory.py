import pytest
from harvey.integrations.vendor_directory import (
    load_vendors,
    find_vendor_for_item,
    build_whatsapp_rfq,
    open_inaproc_in_chrome,
)


def test_load_vendors():
    vendors = load_vendors()
    assert len(vendors) >= 5
    ids = [v["id"] for v in vendors]
    assert "barcov" in ids
    assert "dlab" in ids
    assert "memmert" in ids


def test_find_vendor_for_item():
    # Exact brand
    v1 = find_vendor_for_item("Rotary Evaporator BRQ-107", brand="Barcov")
    assert v1 is not None
    assert v1["id"] == "barcov"

    # Category match
    v2 = find_vendor_for_item("High Speed Centrifuge 15000 RPM")
    assert v2 is not None
    assert v2["id"] == "dlab"

    # Oven match
    v3 = find_vendor_for_item("Natural Convection Drying Oven 108L", brand="Memmert")
    assert v3 is not None
    assert v3["id"] == "memmert"


def test_build_whatsapp_rfq():
    v = {
        "id": "barcov",
        "name": "PT Barcov Teknologi Indonesia",
        "phone": "087777277755",
    }
    rfq = build_whatsapp_rfq(
        vendor=v,
        package_name="Pengadaan Rotary Evaporator Set Jawa Timur",
        item_name="Rotary Evaporator Set",
        brand="Barcov",
        model="BRQ-107",
        qty=1,
    )
    assert "PT Barcov Teknologi Indonesia" in rfq
    assert "PT Andis Sentral Laboratorium" in rfq
    assert "BRQ-107" in rfq
    assert "Surat Dukungan" in rfq
    assert "Harga Net Rekanan" in rfq

