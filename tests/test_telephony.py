import pytest
from pathlib import Path
from harvey.integrations.telephony import TelephonyEngine, DEFAULT_VOICE


def test_telephony_script_generation():
    engine = TelephonyEngine()
    lead = {
        "company": "Laboratorium Sentral Farmasi",
        "first_name": "Rudi",
        "city": "Bandung",
    }
    script = engine.get_calling_script(lead)
    assert "receptionist_gatekeeper" in script
    assert "pitch_to_lab_head" in script
    assert "battlecards" in script
    assert "Nadia" in script["receptionist_gatekeeper"]
    assert "Laboratorium Sentral Farmasi" in script["pitch_to_lab_head"]
    assert "vendor_lama" in script["battlecards"]


@pytest.mark.asyncio
async def test_telephony_speech_synthesis(tmp_path):
    engine = TelephonyEngine()
    test_out = tmp_path / "voice_test.mp3"
    audio_bytes, path_str = await engine.synthesize_speech(
        text="Halo, ini adalah pengujian suara Ahnaf untuk sistem telepon.",
        voice=DEFAULT_VOICE,
        output_path=test_out,
    )
    assert len(audio_bytes) > 1000
    assert Path(path_str).exists()
    assert test_out.stat().st_size > 1000


@pytest.mark.asyncio
async def test_telephony_conversational_turn():
    engine = TelephonyEngine()
    lead = {
        "company": "PT Indo Farma",
        "first_name": "Pak Anton",
        "phone": "0812345678",
    }
    turn = await engine.conversational_turn(
        user_speech="Kami di sini sudah ada vendor tetap dari distributor Bandung mas.",
        lead_context=lead,
    )
    assert isinstance(turn, dict)
    assert "reply_text" in turn
    assert len(turn["reply_text"]) > 5


@pytest.mark.asyncio
async def test_android_bridge(monkeypatch):
    from harvey.integrations.android_bridge import AndroidBridge
    bridge = AndroidBridge()

    # Mock ADB devices
    def mock_devices(*args, **kwargs):
        if "devices" in args:
            return 0, "List of devices attached\nRF8R123456\tdevice\n", ""
        if "getprop" in args:
            return 0, "Galaxy Tab A8", ""
        if "am" in args:
            return 0, "Starting: Intent { act=android.intent.action.CALL dat=tel:08123456789 }", ""
        if "keyevent" in args:
            return 0, "", ""
        if "dumpsys" in args:
            return 0, "mCallState=2\n", ""
        return 0, "", ""

    monkeypatch.setattr(bridge, "run_adb", mock_devices)

    devices = await bridge.get_devices()
    assert len(devices) == 1
    assert devices[0]["serial"] == "RF8R123456"
    assert devices[0]["model"] == "Galaxy Tab A8"

    primary = await bridge.get_primary_device()
    assert primary is not None
    assert primary["serial"] == "RF8R123456"

    # Rejects mobile 08xx number
    mobile_res = await bridge.dial_number("08123456789")
    assert mobile_res["success"] is False
    assert "nomor kantor resmi" in mobile_res["error"]

    # Accepts office landline 021xx number
    dial_res = await bridge.dial_number("0213152382")
    assert dial_res["success"] is True

    hangup_res = await bridge.end_call()
    assert hangup_res["success"] is True

    call_state = await bridge.get_call_state()
    assert call_state["state"] == "OFFHOOK"


