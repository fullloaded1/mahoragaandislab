"""OpenWA integration (https://github.com/rmyndharis/OpenWA)
A self-hosted, open-source WhatsApp API gateway with webhook support.
"""

import logging
import os
import re
import httpx
from typing import Any

logger = logging.getLogger("harvey.openwa")


class OpenWAClient:
    def __init__(self, base_url: str = "", api_key: str = "", session_id: str = ""):
        self.base_url = (base_url or os.getenv("OPENWA_BASE_URL") or "http://localhost:2785").rstrip("/")
        self.api_key = api_key or os.getenv("OPENWA_API_KEY") or ""
        self.session_id = session_id or os.getenv("OPENWA_SESSION_ID") or "default"
        self._resolved_sid: str = ""

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    def is_configured(self) -> bool:
        return bool(self.base_url)

    async def check_health(self) -> bool:
        """Check if OpenWA gateway is reachable."""
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                res = await client.get(f"{self.base_url}/api/docs", headers=self._headers())
                return res.status_code in (200, 301, 302, 401, 404)
        except Exception:
            return False

    async def _resolve_session_id(self) -> str:
        """Resolve session UUID if session_id is a name like 'default'."""
        if self._resolved_sid:
            return self._resolved_sid
        if len(self.session_id) >= 30 and "-" in self.session_id:
            self._resolved_sid = self.session_id
            return self._resolved_sid
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(f"{self.base_url}/api/sessions", headers=self._headers())
                if res.status_code == 200:
                    sessions = res.json()
                    for s in sessions:
                        if s.get("name") == self.session_id:
                            self._resolved_sid = s.get("id") or self.session_id
                            return self._resolved_sid
                    if sessions:
                        self._resolved_sid = sessions[0].get("id") or self.session_id
                        return self._resolved_sid
        except Exception as e:
            logger.debug(f"OpenWA: _resolve_session_id warning: {type(e).__name__}: {e}")
        return self.session_id

    async def get_session_status(self) -> dict[str, Any]:
        """Fetch session status from OpenWA."""
        try:
            sid = await self._resolve_session_id()
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(
                    f"{self.base_url}/api/sessions/{sid}",
                    headers=self._headers(),
                )
                if res.status_code == 200:
                    return res.json()
        except Exception as e:
            logger.debug(f"OpenWA get_session_status error: {e}")
        return {"status": "unreachable"}

    async def ensure_webhook(self, webhook_url: str = "http://host.docker.internal:5555/api/whatsapp/webhook") -> bool:
        """Ensure that inbound webhook is registered with OpenWA session."""
        try:
            sid = await self._resolve_session_id()
            headers = {}
            try:
                from harvey.paths import PROJECT_ROOT
                token_file = PROJECT_ROOT / "data" / "dashboard-token"
                if token_file.is_file():
                    tok = token_file.read_text(encoding="utf-8").strip()
                    if tok:
                        headers["Authorization"] = f"Bearer {tok}"
            except Exception:
                pass

            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(
                    f"{self.base_url}/api/sessions/{sid}/webhooks",
                    headers=self._headers(),
                )
                if res.status_code == 200:
                    existing = res.json()
                    for wh in existing:
                        if wh.get("url") == webhook_url:
                            return True
                # Register webhook
                payload = {"url": webhook_url, "events": ["message.received"]}
                if headers:
                    payload["headers"] = headers
                reg_res = await client.post(
                    f"{self.base_url}/api/sessions/{sid}/webhooks",
                    headers=self._headers(),
                    json=payload,
                )
                return reg_res.status_code in (200, 201)
        except Exception as e:
            logger.debug(f"OpenWA ensure_webhook error: {e}")
            return False

    async def check_number_registered(self, phone: str) -> bool | None:
        """Check whether a phone number is registered on WhatsApp via OpenWA (None if unknown)."""
        digits = re.sub(r"[^\d]", "", str(phone or ""))
        if not digits or not self.is_configured():
            return None
        try:
            sid = await self._resolve_session_id()
            async with httpx.AsyncClient(timeout=8.0) as client:
                res = await client.get(
                    f"{self.base_url}/api/sessions/{sid}/contacts/check-exists",
                    params={"phone": digits},
                    headers=self._headers(),
                )
                if res.status_code == 200:
                    data = res.json()
                    if isinstance(data, dict):
                        for k in ("exists", "numberExists", "registered", "valid"):
                            if k in data:
                                return bool(data[k])
        except Exception:
            pass
        return None

    async def send_text(self, phone: str, text: str) -> bool:
        """Send a plain text message via OpenWA."""
        target = str(phone or "").strip()
        if not target:
            return False
        if "@" in target:
            chat_id = target
        else:
            digits = re.sub(r"[^\d]", "", target)
            if not digits:
                return False
            # Check if this is a WhatsApp LID (14+ digits not starting with Indonesian 628)
            if len(digits) >= 14 and not digits.startswith("628"):
                chat_id = f"{digits}@lid"
            else:
                chat_id = f"{digits}@c.us"

        payload = {
            "chatId": chat_id,
            "text": text,
        }
        for attempt in range(2):
            try:
                sid = await self._resolve_session_id()
                async with httpx.AsyncClient(timeout=45.0) as client:
                    res = await client.post(
                        f"{self.base_url}/api/sessions/{sid}/messages/send-text",
                        headers=self._headers(),
                        json=payload,
                    )
                    if res.status_code in (200, 201):
                        logger.info(f"OpenWA: Pesan berhasil terkirim ke {phone}")
                        return True
                    logger.warning(f"OpenWA: Gagal mengirim ke {phone} (HTTP {res.status_code}): {res.text}")
                    if res.status_code < 500:
                        return False
            except Exception as e:
                logger.warning(f"OpenWA: Exception saat mengirim ke {phone} (attempt {attempt + 1}): {type(e).__name__}: {e}")
        return False

    async def send_document(
        self,
        phone: str,
        file_path: str,
        filename: str = "",
        caption: str = "",
        mimetype: str = "application/pdf",
    ) -> bool:
        """Send a PDF or document file via OpenWA `/messages/send-document` (Base64)."""
        import base64
        from pathlib import Path

        target = str(phone or "").strip()
        if not target:
            return False
        p = Path(file_path)
        if not p.exists():
            logger.warning(f"OpenWA: File dokumen tidak ditemukan: {file_path}")
            return False

        if "@" in target:
            chat_id = target
        else:
            digits = re.sub(r"[^\d]", "", target)
            if not digits:
                return False
            if len(digits) >= 14 and not digits.startswith("628"):
                chat_id = f"{digits}@lid"
            else:
                chat_id = f"{digits}@c.us"
        b64_data = base64.b64encode(p.read_bytes()).decode("ascii")
        payload = {
            "chatId": chat_id,
            "base64": b64_data,
            "mimetype": mimetype,
            "filename": filename or p.name,
        }
        if caption:
            payload["caption"] = caption[:1024]

        try:
            sid = await self._resolve_session_id()
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.post(
                    f"{self.base_url}/api/sessions/{sid}/messages/send-document",
                    headers=self._headers(),
                    json=payload,
                )
                if res.status_code in (200, 201):
                    logger.info(f"OpenWA: Dokumen '{filename or p.name}' berhasil terkirim ke {phone}")
                    return True
                else:
                    logger.warning(f"OpenWA: Gagal kirim dokumen ke {phone} (HTTP {res.status_code}): {res.text}")
                    return False
        except Exception as e:
            logger.warning(f"OpenWA: Exception saat kirim dokumen ke {phone}: {e}")
            return False


