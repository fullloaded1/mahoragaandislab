"""Telephony & Voice AI Engine for Harvey.

Supports Dual-Mode:
- Mode A: PSTN Calling Desk Co-Pilot (For landline 021-38740154, script & battlecards)
- Mode B: Autonomous AI Voice Agent (Free Edge-TTS + Groq Whisper + Obsidian RAG)
"""

import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import edge_tts
from groq import Groq

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.telephony")

VOICE_DIR = PROJECT_ROOT / "data" / "voice_cache"
VOICE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_VOICE = "id-ID-GadisNeural"  # Professional Indonesian Female Voice (Nadia)
ALT_VOICE = "id-ID-ArdiNeural"       # Professional Indonesian Male Voice (Fallback)

COLD_CALL_SYSTEM_PROMPT = """Kamu adalah Nadia Safitri, staf pengadaan & sales B2B resmi dari PT Andis Sentral Laboratorium (AndisLab).
AndisLab adalah distributor & supplier resmi peralatan dan instrumen laboratorium (1.140+ item ready stock Jabodetabek, garansi resmi 12 bulan, legalitas LKPP lengkap, kantor: 021-38740154, web: www.andislab.com).

Kamu sedang berbicara melalui TELEPON KANTOR RESMI (PSTN) dengan resepsionis, staf laboratorium, atau bagian pengadaan instansi / pabrik di Indonesia.

ATURAN BICARA (GAYA PERCAKAPAN TELEPON WANITA SALES B2B ASLI):
1. Karakter suara: ramah, santun, artikulatif, luwes, dan percaya diri (bukan robot kaku, bukan gaul santai).
2. Jawaban HARUS SINGKAT (maksimal 2 kalimat per giliran bicara, tidak lebih dari 35 kata), karena di telepon lawan bicara cepat bosan jika panjang lebar.
3. JANGAN PERNAH gunakan kata "Halo kak" atau sejenisnya. Gunakan "Bapak", "Ibu", atau "Pak/Bu".
4. Taktik Tembus Resepsionis: Minta disambungkan dengan sopan ke "bagian laboratorium", "tim QC/QA", atau "bagian pengadaan alat lab" untuk konfirmasi surat penawaran katalog resmi dan ready stock alat lab.
5. Menghadapi Penolakan:
   - "Sudah ada vendor tetap": Terima dengan santun: "Baik Bapak/Ibu, tidak apa-apa. Izin AndisLab menjadi vendor pembanding resmi ber-KOP ya Pak/Bu, bilamana vendor lama sedang kosong stoknya."
   - "Belum ada anggaran / belum butuh": "Siap Bapak/Ibu, sangat dipahami. Izin kami bagikan Company Profile dan ringkasan katalog via WhatsApp atau email ya, agar tersimpan sewaktu pengajuan anggaran nanti."
   - "Minta email saja": "Siap Bapak/Ibu, segera kami kirimkan ke email. Boleh kami konfirmasi alamat emailnya dan nomor WhatsApp aktif PIC agar kami beri notifikasi saat email sudah terkirim?"
6. Kembalikan format JSON:
{
  "reply_text": "kalimat lisan santun yang akan diucapkan oleh Nadia",
  "intent": "resepsionis / minat / keberatan_vendor / keberatan_anggaran / minta_wa / salah_sambung / selesai",
  "pic_detected": "nama atau jabatan jika lawan bicara menyebutkan",
  "phone_detected": "nomor WA/HP jika lawan bicara menyebutkan",
  "action": "none / send_wa_comprof / close_call"
}"""


def is_office_landline(phone: str) -> bool:
    """Check if a phone number is an Indonesian office/fixed-line landline (PSTN), NOT mobile/WA."""
    if not phone:
        return False
    digits = re.sub(r"[^\d]", "", str(phone))
    if digits.startswith("62"):
        digits = "0" + digits[2:]
    # Indonesian mobile numbers strictly start with 08 (e.g. 0812, 0857, 0877, 0896)
    if digits.startswith("08"):
        return False
    # Indonesian landline area codes: 02 (Jabodetabek 021, Jabar 022/0251/026, Jateng 024/027),
    # 03 (Jatim/Bali/Nusa 031/034/036), 04 (Sulawesi), 05 (Kalimantan), 06 (Sumut/Aceh 061),
    # 07 (Sumbar/Riau/Sumsel), 09 (Papua/Maluku), or 1500 (corporate call center)
    if digits.startswith(("02", "03", "04", "05", "06", "07", "09")) and len(digits) >= 7:
        return True
    if digits.startswith("1500") and len(digits) >= 6:
        return True
    return False


def format_landline_dial(phone: str) -> str:
    """Format an Indonesian office landline for dialing (e.g. 0213152382)."""
    digits = re.sub(r"[^\d]", "", str(phone))
    if digits.startswith("62"):
        digits = "0" + digits[2:]
    return digits


class TelephonyEngine:
    def __init__(self, state=None, config=None, env=None):
        self.state = state
        self.config = config
        self.env = env
        self.groq_api_key = (
            os.getenv("GROQ_API_KEY")
            or getattr(env, "groq_api_key", None)
            or ""
        )

    async def synthesize_speech(
        self,
        text: str,
        voice: str = DEFAULT_VOICE,
        output_path: Path | str | None = None,
    ) -> tuple[bytes, str]:
        """Synthesize text into natural Indonesian neural speech using edge-tts.
        Returns: (audio_bytes, audio_path)
        100% Free, Zero API Key.
        """
        clean_text = re.sub(r"[\*\_\[\]\(\)]", "", text).strip()
        if not clean_text:
            clean_text = "Halo, selamat siang."

        file_hash = f"voice_{abs(hash(clean_text + voice)) % 10000000:07d}.mp3"
        out_file = Path(output_path) if output_path else VOICE_DIR / file_hash

        if out_file.exists() and out_file.stat().st_size > 1000:
            return out_file.read_bytes(), str(out_file)

        last_err = None
        for attempt in range(3):
            try:
                communicate = edge_tts.Communicate(clean_text, voice=voice)
                await communicate.save(str(out_file))
                if out_file.exists() and out_file.stat().st_size > 0:
                    break
            except Exception as e:
                last_err = e
                await asyncio.sleep(0.6 * (attempt + 1))

        if not out_file.exists() or out_file.stat().st_size == 0:
            if last_err:
                raise last_err

        audio_bytes = out_file.read_bytes()
        return audio_bytes, str(out_file)

    def transcribe_audio(
        self,
        audio_bytes: bytes,
        filename: str = "speech.mp3",
    ) -> str:
        """Transcribe Indonesian speech using Groq Whisper (ultra low latency ~150ms)."""
        if not self.groq_api_key:
            logger.warning("Groq API key not configured for STT.")
            return ""

        try:
            client = Groq(api_key=self.groq_api_key)
            transcription = client.audio.transcriptions.create(
                file=(filename, audio_bytes),
                model="whisper-large-v3-turbo",
                language="id",
                temperature=0.0,
            )
            return transcription.text.strip()
        except Exception as e:
            logger.warning(f"Telephony: Groq Whisper transcription error: {e}")
            return ""

    async def conversational_turn(
        self,
        user_speech: str,
        lead_context: dict[str, Any] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Process one conversational turn for Ahnaf Voice Agent."""
        lead = lead_context or {}
        company = lead.get("company", "Instansi / Laboratorium")
        contact_name = lead.get("contact_name") or lead.get("first_name", "Bapak/Ibu")
        phone = lead.get("phone", "")

        # Format history
        formatted_history = []
        if history:
            for h in history[-4:]:
                r = h.get("role", "user")
                c = h.get("content", "")
                formatted_history.append(f"{'Ahnaf' if r == 'assistant' else 'Lawan Bicara'}: {c}")

        hist_str = "\n".join(formatted_history) if formatted_history else "(Awal percakapan)"

        user_prompt = f"""KONTEKS TELEPON:
- Lawan bicara: {contact_name} ({company})
- No Telp: {phone}
- Riwayat Dialog Terakhir:
{hist_str}

Lawan bicara baru saja mengatakan:
"{user_speech}"

Berikan respon lisan terbaik Ahnaf sekarang dalam format JSON."""

        # Call LLM via Groq (Fastest for voice turn ~200ms) or Gemini
        result = None
        if self.groq_api_key:
            try:
                client = Groq(api_key=self.groq_api_key)
                resp = client.chat.completions.create(
                    model="openai/gpt-oss-120b",
                    messages=[
                        {"role": "system", "content": COLD_CALL_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.3,
                    max_tokens=250,
                )
                raw = resp.choices[0].message.content
                result = json.loads(raw)
            except Exception as e:
                logger.debug(f"Telephony: Groq LLM failed: {e}")

        # Fallback to Gemini if Groq unavailable or failed
        if not result and self.state:
            try:
                from harvey.brain import Brain
                brain_cfg = getattr(self.config, "brain", None) if getattr(self, "config", None) else None
                brain = Brain(self.state, brain_cfg)
                full_prompt = f"{COLD_CALL_SYSTEM_PROMPT}\n\n{user_prompt}"
                result = await brain.think_json(full_prompt, session_id="voice-call", agent="handler", task="voice_turn")
            except Exception as e:
                logger.debug(f"Telephony: Gemini Brain fallback failed: {e}")

        # Final rule-based fallback
        if not result or not isinstance(result, dict) or not result.get("reply_text"):
            result = {
                "reply_text": "Baik Bapak/Ibu, terima kasih atas informasinya. Jika berkenan kami izin kirimkan ringkasan profil AndisLab dan katalog via WhatsApp ya Pak/Bu.",
                "intent": "minta_wa",
                "action": "send_wa_comprof",
            }

        return result

    def get_calling_script(self, lead: dict[str, Any]) -> dict[str, Any]:
        """Mode A: PSTN Calling Desk Co-Pilot Script & Battlecards."""
        comp = lead.get("company", "Instansi / Pabrik")
        who = lead.get("first_name") or lead.get("contact_name") or "Bapak/Ibu"
        city = lead.get("city", "Bogor")

        return {
            "receptionist_gatekeeper": (
                f"\"Selamat siang Bu/Pak, perkenalkan saya Nadia dari PT Andis Sentral Laboratorium, penyedia instrumen lab resmi. "
                f"Mohon izin dibantu sambungkan ke bagian Laboratorium / QC Pengadaan alat, terkait pengiriman katalog dan pembanding penawaran harga resmi. "
                f"Terima kasih banyak Bu.\""
            ),
            "pitch_to_lab_head": (
                f"\"Selamat siang Bapak/Ibu {who}, saya Nadia dari AndisLab. Kami distributor resmi instrumen laboratorium dengan 1.140+ unit ready stock di Jabodetabek. "
                f"Izin bertanya, apakah di lab {comp} saat ini sedang ada kebutuhan peremajaan alat uji, glassware, atau reagen rutin yang membutuhkan draf SPH resmi pembanding? "
                f"Kami siap bantu skema pengadaan resmi dan garansi 12 bulan.\""
            ),
            "battlecards": {
                "vendor_lama": "Katakan: 'Siap Pak/Bu, tidak masalah sama sekali. Izin AndisLab menjadi vendor pendamping atau pembanding HPS resmi ber-KOP ya Pak/Bu, bilamana sewaktu-waktu vendor lama stoknya kosong atau butuh unit mendesak.'",
                "belum_ada_anggaran": "Katakan: 'Siap Pak/Bu, kami sangat memahami siklus anggaran. Izin kami bagikan Company Profile dan daftar harga katalog ke WhatsApp Bapak/Ibu ya, agar tersimpan sewaktu pengajuan anggaran nanti.'",
                "minta_email": "Katakan: 'Siap Bapak/Ibu, segera kami kirimkan ke email. Boleh kami konfirmasi alamat emailnya dan nomor WhatsApp aktif PIC agar kami beri notifikasi saat email sudah terkirim?'",
            },
        }

    async def get_calling_queue(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get list of leads to call from database: strictly Indonesian office landlines (PSTN), never mobile/WA."""
        if not self.state:
            return []

        from harvey.config import is_partner_or_excluded

        leads = []
        async with self.state._connect() as db:
            # 1. Primary source: Companies table with verified office/landline phone numbers
            cur = await db.execute(
                """SELECT id, name, phone, website, location, industry, notes
                   FROM companies
                   WHERE phone IS NOT NULL AND phone != ''
                   ORDER BY updated_at DESC"""
            )
            comp_rows = await cur.fetchall()

            for cid, cname, cphone, cweb, cloc, cind, cnotes in comp_rows:
                if not is_office_landline(cphone):
                    continue
                if is_partner_or_excluded(company=cname, phone=cphone, config=self.config):
                    continue

                dial_phone = format_landline_dial(cphone)
                leads.append({
                    "id": cid,
                    "first_name": "Resepsionis / Bagian Lab",
                    "last_name": "",
                    "contact_name": "Resepsionis / Bagian Lab",
                    "company": cname or "Instansi",
                    "phone": dial_phone,
                    "display_phone": cphone,
                    "city": cloc or "Jabodetabek",
                    "email": "",
                    "status": "ready_to_call",
                    "notes": cnotes or "",
                    "is_landline": True,
                })
                if len(leads) >= limit:
                    break

            # 2. Secondary source: Prospects with genuine landline numbers (if any)
            if len(leads) < limit:
                cur = await db.execute(
                    """SELECT id, first_name, last_name, company, phone, email, status, personalization_notes
                       FROM prospects
                       WHERE phone IS NOT NULL AND phone != '' AND status NOT IN ('opted_out', 'closed_won', 'closed_lost')
                       ORDER BY updated_at DESC"""
                )
                p_rows = await cur.fetchall()
                for pid, fn, ln, comp, ph, em, st, pnotes in p_rows:
                    if not is_office_landline(ph):
                        continue
                    if is_partner_or_excluded(name=f"{fn} {ln}", company=comp, phone=ph, config=self.config):
                        continue
                    dial_phone = format_landline_dial(ph)
                    leads.append({
                        "id": pid,
                        "first_name": fn or "",
                        "last_name": ln or "",
                        "contact_name": f"{fn or ''} {ln or ''}".strip() or "PIC Lab",
                        "company": comp or "",
                        "phone": dial_phone,
                        "display_phone": ph,
                        "city": "Jabodetabek",
                        "email": em or "",
                        "status": st or "ready_to_call",
                        "notes": pnotes or "",
                        "is_landline": True,
                    })
                    if len(leads) >= limit:
                        break

        return leads

