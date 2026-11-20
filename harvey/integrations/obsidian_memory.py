"""Obsidian Second Brain Integration for Harvey AI.
Syncs daily chat interactions, customer profiling, and adaptive playbooks into an Obsidian Vault.
"""

import json
import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytz

logger = logging.getLogger("harvey.obsidian")

# Default to the active Windows vault if present
DEFAULT_VAULT_PATH = Path("C:/Users/Ancimmm/Documents/Obsidian Vault")


def _sanitize_filename(name: str) -> str:
    """Clean company or contact name for safe Windows filesystem naming."""
    clean = re.sub(r'[\\/*?:"<>|]', "", str(name or "Unknown")).strip()
    return clean[:80] or "Unknown_Client"


class ObsidianMemory:
    def __init__(self, vault_path: str | Path | None = None):
        if vault_path:
            self.vault_path = Path(vault_path)
        else:
            env_path = os.getenv("OBSIDIAN_VAULT_PATH", "")
            if env_path:
                self.vault_path = Path(env_path)
            elif DEFAULT_VAULT_PATH.exists():
                self.vault_path = DEFAULT_VAULT_PATH
            else:
                from harvey.paths import PROJECT_ROOT
                self.vault_path = PROJECT_ROOT / "data" / "obsidian_vault"

        self.daily_dir = self.vault_path / "Daily"
        self.profiling_dir = self.vault_path / "Profiling Calon client"
        self.playbooks_dir = self.vault_path / "Adaptive Playbooks"
        self._ensure_dirs()
        self._ensure_default_playbooks()

    def _ensure_dirs(self):
        try:
            self.daily_dir.mkdir(parents=True, exist_ok=True)
            self.profiling_dir.mkdir(parents=True, exist_ok=True)
            self.playbooks_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.debug(f"Obsidian: mkdir error: {e}")

    def _ensure_default_playbooks(self):
        """Seed default adaptive playbooks if they do not exist yet."""
        defaults = {
            "Archetype_RSUD_Dinkes.md": (
                "# 🏛️ Playbook Segmen: RSUD & Dinas Kesehatan\n\n"
                "## Karakteristik Komunikasi\n"
                "- Fokus utama: Kepatuhan audit BAST, garansi resmi 12 bulan, instalasi teknisi onsite, dan etalase E-Katalog INAPROC.\n"
                "- Waktu respon terbaik: Pagi hari (08:30 - 11:30 WIB).\n"
                "- Kata kunci efektif: `Ready Stock Gudang Bogor`, `Tanpa Indent`, `SPH Ber-KOP Resmi 1x24 Jam`.\n\n"
                "## Alat Paling Sering Diminati\n"
                "- Autoclave Sterilisasi 50L / 100L\n"
                "- Centrifuge Kecepatan Tinggi / Mikro\n"
                "- Biosafety Cabinet & Laminar Air Flow\n"
            ),
            "Archetype_Kampus_LPPM.md": (
                "# 🎓 Playbook Segmen: Kampus, Universitas & Balai Riset\n\n"
                "## Karakteristik Komunikasi\n"
                "- Fokus utama: Fleksibilitas pencairan hibah LPPM/BOPTN, skema Tempo Instansi 30 hari (bayar setelah barang uji fungsi).\n"
                "- Waktu respon terbaik: Jam siang / sore santai (13:30 - 16:30 WIB).\n"
                "- Kata kunci efektif: `Pendampingan Spesifikasi HPS`, `e-Faktur Pajak 1x24 Jam`, `Diskon Tunai 8%`.\n\n"
                "## Alat Paling Sering Diminati\n"
                "- Timbangan Analitik 4 Desimal (0.1 mg)\n"
                "- Oven Pengering & Incubator Lab\n"
                "- Hotplate Magnetic Stirrer & pH Meter\n"
            ),
            "Archetype_Pabrik_F&B_Pharma.md": (
                "# 🏭 Playbook Segmen: Pabrik Industri, Pangan & Farmasi\n\n"
                "## Karakteristik Komunikasi\n"
                "- Fokus utama: Kecepatan pengiriman (anti-downtime line produksi), sertifikat kalibrasi ISO 17025, dan COA.\n"
                "- Waktu respon terbaik: Cepat dan to-the-point.\n"
                "- Kata kunci efektif: `100% Ready Stock Jabodetabek`, `Pengiriman 1-3 Hari Kerja`, `Garansi Principal`.\n\n"
                "## Alat Paling Sering Diminati\n"
                "- Moisture Analyzer / Penentu Kadar Air\n"
                "- Muffle Furnace & Water Bath\n"
                "- Spectrophotometer UV-Vis\n"
            ),
            "Archetype_Mineral_Tambang.md": (
                "# ⛏️ Playbook Segmen: Pertambangan, Mineral & Pengolahan Logam\n\n"
                "## Karakteristik Komunikasi\n"
                "- Fokus utama: Ketahanan alat (heavy-duty), akurasi sertifikasi kalibrasi, pengujian kadar air, dan ketersediaan reagen asam.\n"
                "- Waktu respon terbaik: To-the-point, profesional, langsung lampirkan spesifikasi/katalog.\n"
                "- Kata kunci efektif: `Ready Stock Jabodetabek`, `Garansi 12 Bulan Resmi`, `Draf SPH Siap 1x24 Jam`.\n\n"
                "## Alat Paling Sering Diminati\n"
                "- Moisture Analyzer (Kadar Air Ore / Batubara)\n"
                "- Muffle Furnace Suhu Tinggi (1200°C)\n"
                "- Timbangan Analitik Presisi (0.1 mg)\n"
                "- Drying Oven Kapasitas Besar\n"
                "- Reagen Kimia & Asam Titrasi\n"
            ),
        }
        for fname, content in defaults.items():
            fpath = self.playbooks_dir / fname
            if not fpath.exists():
                try:
                    fpath.write_text(content, encoding="utf-8")
                except Exception:
                    pass

    def log_chat_event(
        self,
        phone: str,
        name: str,
        company: str,
        direction: str,
        message: str,
        intent: str = "",
    ) -> bool:
        """Append one message event into Obsidian Daily Log (Daily/YYYY-MM-DD.md)."""
        tz = pytz.timezone("Asia/Jakarta")
        now_jkt = datetime.now(tz)
        today_str = now_jkt.strftime("%Y-%m-%d")
        time_str = now_jkt.strftime("%H:%M")

        daily_file = self.daily_dir / f"{today_str}.md"
        dir_badge = "📥 INBOUND" if direction == "inbound" else "📤 OUTBOUND"
        who = name or company or phone
        intent_tag = f" `[{intent}]`" if intent else ""

        entry = (
            f"- **{time_str} WIB** | **{dir_badge}** | **{who}** (`{phone}`){intent_tag}:\n"
            f"  > {message.strip().replace(chr(10), ' ')}\n\n"
        )

        try:
            if not daily_file.exists():
                header = (
                    f"# 📅 Daily Sales Log — {today_str}\n\n"
                    f"## 💬 Log Percakapan WhatsApp (Harvey AI)\n\n"
                )
                daily_file.write_text(header + entry, encoding="utf-8")
            else:
                content = daily_file.read_text(encoding="utf-8")
                if "## 💬 Log Percakapan WhatsApp" not in content:
                    content += f"\n\n## 💬 Log Percakapan WhatsApp (Harvey AI)\n\n"
                content += entry
                daily_file.write_text(content, encoding="utf-8")
            return True
        except Exception as e:
            logger.debug(f"Obsidian log_chat_event error: {e}")
            return False

    def update_customer_profile(
        self,
        phone: str,
        name: str,
        company: str,
        segment: str = "",
        status: str = "replied",
        interested_products: list[str] | None = None,
        notes: str = "",
        last_message: str = "",
    ) -> bool:
        """Create or update customer profile markdown in 'Profiling Calon client/{Company}.md'."""
        comp_clean = _sanitize_filename(company or name or f"Kontak_{phone[-4:]}")
        profile_file = self.profiling_dir / f"{comp_clean}.md"

        tz = pytz.timezone("Asia/Jakarta")
        now_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M")

        # Resolve archetype wikilink
        archetype = "[[Archetype_Laboratorium_Umum]]"
        seg_low = (segment or company or notes).lower()
        if any(k in seg_low for k in ["rsud", "rumah sakit", "hospital", "klinik", "dinkes", "medis"]):
            archetype = "[[Archetype_RSUD_Dinkes]]"
        elif any(k in seg_low for k in ["univ", "kampus", "universitas", "politeknik", "fakultas", "lppm", "sekolah"]):
            archetype = "[[Archetype_Kampus_LPPM]]"
        elif any(k in seg_low for k in ["pt", "pabrik", "food", "pharma", "kimia", "industri", "fnb"]):
            archetype = "[[Archetype_Pabrik_F&B_Pharma]]"

        products = interested_products or []
        prods_yaml = "\n".join(f"  - \"[[{p}]]\"" for p in products) if products else "  - \"[[Peralatan Lab Umum]]\""

        try:
            if not profile_file.exists():
                content = (
                    f"---\n"
                    f"company: \"{company or comp_clean}\"\n"
                    f"pic_name: \"{name or '-'}\"\n"
                    f"phone: \"{phone}\"\n"
                    f"segment: \"{archetype}\"\n"
                    f"status: \"{status}\"\n"
                    f"last_contact: \"{now_str}\"\n"
                    f"interested_products:\n{prods_yaml}\n"
                    f"tags:\n"
                    f"  - harvey-sales\n"
                    f"  - {status}\n"
                    f"---\n\n"
                    f"# 🏢 {company or comp_clean} — {name or 'PIC Lab'}\n\n"
                    f"## 🧠 Ringkasan Profil & Memori AI\n"
                    f"- **Segmen Acuan:** {archetype}\n"
                    f"- **Kontak:** `{phone}`\n"
                    f"- **Catatan Khusus:** {notes or 'Prospek aktif via WhatsApp'}\n\n"
                    f"## 💬 Riwayat Interaksi Terbaru\n"
                    f"- **{now_str}:** {last_message.strip()[:200] or 'Pesan awal perkenalan dikirim'}\n"
                )
                profile_file.write_text(content, encoding="utf-8")
            else:
                # Append interaction
                prev = profile_file.read_text(encoding="utf-8")
                if "## 💬 Riwayat Interaksi Terbaru" in prev:
                    new_item = f"- **{now_str}:** {last_message.strip()[:200]}\n"
                    prev += new_item
                else:
                    prev += f"\n\n## 💬 Riwayat Interaksi Terbaru\n- **{now_str}:** {last_message.strip()[:200]}\n"
                profile_file.write_text(prev, encoding="utf-8")
            return True
        except Exception as e:
            logger.debug(f"Obsidian update_customer_profile error: {e}")
            return False

    def get_playbook_context(self, segment: str = "", industry: str = "") -> str:
        """Fetch adaptive playbook content for AI context injection."""
        seg_low = f"{segment} {industry}".lower()
        target_file = "Archetype_Laboratorium_Umum.md"
        if any(k in seg_low for k in ["mineral", "tambang", "mining", "batubara", "nikel", "batu bara", "smelter"]):
            target_file = "Archetype_Mineral_Tambang.md"
        elif any(k in seg_low for k in ["rsud", "rumah sakit", "hospital", "klinik", "dinkes", "medis"]):
            target_file = "Archetype_RSUD_Dinkes.md"
        elif any(k in seg_low for k in ["univ", "kampus", "universitas", "politeknik", "fakultas", "lppm", "pendidikan", "i3l"]):
            target_file = "Archetype_Kampus_LPPM.md"
        elif any(k in seg_low for k in ["pt", "pabrik", "food", "pharma", "kimia", "industri", "fnb", "pangan"]):
            target_file = "Archetype_Pabrik_F&B_Pharma.md"

        fpath = self.playbooks_dir / target_file
        if fpath.exists():
            try:
                return fpath.read_text(encoding="utf-8")
            except Exception:
                pass
        return ""

    def search_client_intelligence(self, company: str = "", contact_name: str = "", phone: str = "") -> dict:
        """Search Obsidian Vault for client profile, research notes, and past interactions."""
        res = {
            "found": False,
            "profile_notes": "",
            "segment": "",
            "playbook_guidance": "",
        }
        if not company and not contact_name and not phone:
            return res

        comp_clean = _sanitize_filename(company or contact_name)
        profile_file = self.profiling_dir / f"{comp_clean}.md"

        # 1. Check exact profile markdown
        if profile_file.exists():
            try:
                text = profile_file.read_text(encoding="utf-8")
                res["found"] = True
                res["profile_notes"] = text[:800]
                seg_m = re.search(r"segment:\s*\"?\[\[(.*?)\]\]\"?", text)
                if seg_m:
                    res["segment"] = seg_m.group(1)
            except Exception as e:
                logger.debug(f"Obsidian search profile error: {e}")

        # 2. Search in comprehensive research file if exists
        comprehensive_file = self.profiling_dir / "LAPORAN RISET PENJUALAN KOMPREHENSIF.md"
        if comprehensive_file.exists() and company:
            try:
                comp_lower = company.lower()
                lines = comprehensive_file.read_text(encoding="utf-8").split("\n")
                matched_section = []
                capturing = False
                for line in lines:
                    if comp_lower in line.lower() and ("##" in line or "#" in line):
                        capturing = True
                    elif capturing and line.startswith("## ") and comp_lower not in line.lower():
                        break
                    if capturing:
                        matched_section.append(line)
                        if len(matched_section) >= 30:
                            break
                if matched_section:
                    res["found"] = True
                    res["profile_notes"] += "\n" + "\n".join(matched_section)
            except Exception as e:
                logger.debug(f"Obsidian comprehensive search error: {e}")

        # 3. Attach matching playbook guidance
        res["playbook_guidance"] = self.get_playbook_context(res.get("segment") or company, company)
        return res

