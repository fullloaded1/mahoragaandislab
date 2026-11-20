import pytest
from starlette.testclient import TestClient
from harvey.dashboard import app, token_store


@pytest.fixture
def auth_client():
    tok = token_store.get()
    client = TestClient(app, base_url="http://127.0.0.1:5555")
    client.headers.update({"Authorization": f"Bearer {tok}"})
    return client


def test_api_vendors(auth_client):
    res = auth_client.get("/api/vendors")
    assert res.status_code == 200
    data = res.json()
    assert "vendors" in data
    assert len(data["vendors"]) >= 5
    v_ids = [v["id"] for v in data["vendors"]]
    assert "barcov" in v_ids
    assert "dlab" in v_ids


def test_api_inaproc_vendor_rfq(auth_client):
    payload = {
        "item_name": "Rotary Evaporator Set",
        "brand": "Barcov",
        "model": "BRQ-107",
        "package_name": "Pengadaan Rotary Evaporator Jawa Timur",
        "qty": 1,
    }
    res = auth_client.post("/api/inaproc/vendor-rfq", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert "Barcov" in data["rfq_text"]
    assert "Surat Dukungan" in data["rfq_text"]
    assert "wa_link" in data


def test_api_telephony_queue(auth_client):
    res = auth_client.get("/api/telephony/queue")
    assert res.status_code == 200
    data = res.json()
    assert "leads" in data


def test_api_telephony_turn(auth_client):
    payload = {
        "speech": "Selamat siang, kami sudah ada vendor tetap dari Jakarta pak.",
        "lead": {
            "company": "Laboratorium Uji Mutu",
            "contact_name": "Ibu Ratna",
            "phone": "0812345678",
        },
        "history": [],
    }
    res = auth_client.post("/api/telephony/turn", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert "reply_text" in data
    assert len(data["reply_text"]) > 5
    assert data["audio_base64"].startswith("data:audio/mp3;base64,")
