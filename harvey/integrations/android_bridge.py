"""Android Tablet GSM Gateway Bridge for Harvey Telephony.

Connects to an Android tablet/phone via ADB (USB or Wi-Fi Wireless Debugging)
to execute cellular GSM outbound calls using the SIM card inside the tablet.
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.android_bridge")

ADB_PORTABLE = PROJECT_ROOT / "tools" / "platform-tools" / "adb.exe"


def get_adb_bin() -> str:
    """Resolve adb binary path: portable local build or system PATH."""
    if ADB_PORTABLE.exists():
        return str(ADB_PORTABLE)
    system_adb = shutil.which("adb")
    if system_adb:
        return system_adb
    return "adb"


class AndroidBridge:
    def __init__(self, adb_bin: str | None = None):
        self.adb_bin = adb_bin or get_adb_bin()

    def run_adb(self, *args: str, timeout: float = 10.0) -> tuple[int, str, str]:
        """Execute an ADB command synchronously."""
        cmd = [self.adb_bin, *args]
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return res.returncode, res.stdout.strip(), res.stderr.strip()
        except subprocess.TimeoutExpired:
            return -1, "", "Timeout expired"
        except Exception as e:
            return -1, "", str(e)

    async def run_adb_async(self, *args: str, timeout: float = 10.0) -> tuple[int, str, str]:
        """Execute an ADB command asynchronously."""
        return await asyncio.to_thread(self.run_adb, *args, timeout=timeout)

    async def get_devices(self) -> list[dict[str, str]]:
        """List attached Android devices and their status."""
        code, out, _ = await self.run_adb_async("devices")
        if code != 0 or not out:
            return []

        devices = []
        for line in out.splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) >= 2:
                serial, state = parts[0], parts[1]
                # Query device model if online
                model = serial
                if state == "device":
                    _, prop_out, _ = await self.run_adb_async("-s", serial, "shell", "getprop", "ro.product.model")
                    if prop_out:
                        model = prop_out.strip()
                devices.append({
                    "serial": serial,
                    "state": state,
                    "model": model,
                })
        return devices

    async def get_primary_device(self) -> dict[str, str] | None:
        """Get the first active online Android device (ignoring offline emulators)."""
        devices = await self.get_devices()
        for d in devices:
            if d.get("state") == "device" and not d.get("serial", "").startswith("emulator-"):
                return d
        # Fallback to any online device
        for d in devices:
            if d.get("state") == "device":
                return d
        return None

    async def resolve_serial(self, serial: str | None = None) -> str | None:
        """Resolve target device serial: user-specified or the primary active device."""
        if serial and serial != "default":
            return serial
        primary = await self.get_primary_device()
        if primary:
            return primary["serial"]
        return None

    async def dial_number(self, phone_number: str, serial: str | None = None) -> dict[str, Any]:
        """Place a real GSM phone call on the Android tablet using the inserted SIM card."""
        from harvey.integrations.telephony import is_office_landline, format_landline_dial

        if not is_office_landline(phone_number):
            return {
                "success": False,
                "phone": phone_number,
                "error": "Panggilan telepon hanya diperbolehkan untuk nomor kantor resmi (PSTN seperti 021/022/dsb), bukan nomor HP/WhatsApp seluler (08xx).",
            }

        clean_num = format_landline_dial(phone_number)
        if not clean_num:
            return {"success": False, "error": "Nomor telepon tidak valid"}

        target_serial = await self.resolve_serial(serial)
        if not target_serial:
            return {
                "success": False,
                "phone": phone_number,
                "error": "Tidak ada tablet Android yang terhubung online (state: device). Pastikan USB Debugging diaktifkan dan popup izin di layar tablet sudah disetujui.",
            }

        target_args = ["-s", target_serial]
        cmd = [*target_args, "shell", "am", "start", "-a", "android.intent.action.CALL", "-d", f"tel:{clean_num}"]

        code, out, err = await self.run_adb_async(*cmd)
        if code == 0:
            logger.info(f"AndroidBridge: Berhasil memulai panggilan ke {clean_num} via tablet ({target_serial})")
            return {
                "success": True,
                "phone": clean_num,
                "serial": target_serial,
                "message": f"Panggilan GSM ke {clean_num} dimulai di tablet ({target_serial}).",
            }
        else:
            logger.warning(f"AndroidBridge: Gagal dial {clean_num}: {err}")
            return {
                "success": False,
                "phone": clean_num,
                "error": err or out,
            }

    async def end_call(self, serial: str | None = None) -> dict[str, Any]:
        """End the ongoing phone call on the tablet."""
        target_serial = await self.resolve_serial(serial)
        target_args = ["-s", target_serial] if target_serial else []
        # KEYCODE_ENDCALL = 6
        code, out, err = await self.run_adb_async(*target_args, "shell", "input", "keyevent", "6")
        if code == 0:
            logger.info(f"AndroidBridge: Panggilan telepon diakhiri pada {target_serial or 'device'}.")
            return {"success": True, "message": "Panggilan berhasil diakhiri."}
        return {"success": False, "error": err or out}

    async def get_call_state(self, serial: str | None = None) -> dict[str, Any]:
        """Get the current call state on the Android tablet:
        - IDLE: tidak ada panggilan
        - RINGING: telepon sedang berdering
        - OFFHOOK: panggilan sedang berlangsung (sedang tersambung bicara)
        """
        target_serial = await self.resolve_serial(serial)
        target_args = ["-s", target_serial] if target_serial else []
        code, out, _ = await self.run_adb_async(
            *target_args, "shell", "dumpsys", "telephony.registry", timeout=5.0
        )
        if code != 0 or not out:
            # Fallback to telecom dump
            code, out, _ = await self.run_adb_async(
                *target_args, "shell", "dumpsys", "telecom", timeout=5.0
            )

        state = "UNKNOWN"
        raw_state_num = None
        match = re.search(r"mCallState\s*=\s*(\d+)", out)
        if match:
            raw_state_num = int(match.group(1))
            if raw_state_num == 0:
                state = "IDLE"
            elif raw_state_num == 1:
                state = "RINGING"
            elif raw_state_num == 2:
                state = "OFFHOOK"  # Call active / talking

        return {
            "state": state,
            "raw_code": raw_state_num,
            "connected": code == 0,
        }

