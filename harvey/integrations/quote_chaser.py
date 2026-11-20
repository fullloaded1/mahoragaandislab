"""Quote Chaser & Sales Cycle Follow-Up Engine for Harvey (PT Andis Sentral Laboratorium).

Automates the post-quotation follow-up cadence (H+2, H+6, H+12):
- Stage 1 (H+2/H+3): Receipt confirmation & technical review check.
- Stage 2 (H+6/H+7): Ready stock reservation & year-end promo incentive.
- Stage 3 (H+12/H+14): Budget / procurement obstacle check & flexible terms offer (tempo 30 hari).
"""

import asyncio
import csv
import logging
import re
from datetime import date, datetime
import aiosqlite
import pytz

from harvey.paths import PROJECT_ROOT
from harvey.integrations.whatsapp import (
    normalize_id_phone,
    is_internal_operator_number,
    build_followup_sph_message,
    build_followup_message,
    _jakarta_greeting,
)

logger = logging.getLogger("harvey.quote_chaser")
DB_PATH = PROJECT_ROOT / "data" / "harvey.db"


async def init_quotes_table():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS quotes (
                id TEXT PRIMARY KEY,
                prospect_id TEXT,
                company TEXT NOT NULL,
                contact_name TEXT,
                phone TEXT,
                email TEXT,
                items_summary TEXT,
                total_amount REAL DEFAULT 0,
                sent_date TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                stage INTEGER DEFAULT 0,
                last_followup_at TEXT,
                notes TEXT
            )
        """)
        await db.commit()


def build_quote_followup_message(
    stage: int,
    contact_name: str,
    company: str,
    items_summary: str,
) -> str:
    """Generate authentic, courteous Ahnaf-style post-quote WhatsApp follow-up."""
    name = (contact_name or "Pak/Bu").strip()
    comp = (company or "").strip()
    salutation = f"Pak/Bu *{name}*" if not name.startswith(("Pak", "Bu", "dr", "Dokter", "Ustadz")) else f"*{name}*"
    if comp:
        salutation = f"{salutation} ({comp})"

    items = items_summary or "alat laboratorium"
    from harvey.integrations.whatsapp import _jakarta_greeting
    greet = _jakarta_greeting()

    if stage == 1:
        # Touch 1: H+2 / H+3 - Receipt confirmation
        return (
            f"{greet} {salutation}, salam dari Ahnaf (PT Andis Sentral Laboratorium). 🙏\n\n"
            f"Sekadar memastikan apakah file penawaran resmi (SPH) untuk *{items}* yang kami kirimkan kemarin sudah diterima dengan baik oleh tim pengadaan/lab?\n\n"
            "Barangkali ada spesifikasi teknis tambahan, kelengkapan surat dukungan, atau dokumen legalitas yang perlu kami lengkapi, kami siap bantu ya Pak/Bu. Terima kasih banyak. 🙏"
        )
    elif stage == 2:
        # Touch 2: H+6 / H+7 - Ready stock & promo allocation
        return (
            f"Halo {greet.lower()} {salutation}, semoga aktivitas hari ini lancar. 🙏\n\n"
            f"Izin menyapa terkait penawaran *{items}* tempo hari. "
            "Kalau tim sedang menyusun draf pengadaan atau butuh dokumen pendukung (spesifikasi, surat dukungan, draft SPJ), "
            "kami siap bantu siapkan supaya prosesnya lebih ringan ya Pak/Bu. Terima kasih. 🙏"
        )
    else:
        # Touch 3: H+12 / H+14 - Budget / procurement obstacle check
        return (
            f"{greet} {salutation}, izin menyapa kembali. 🙏\n\n"
            f"Apakah untuk rencana pengadaan *{items}* masih berjalan sesuai timeline, "
            "atau ada kendala di penyesuaian anggaran yang bisa kami bantu carikan solusinya (seperti opsi pembayaran Tempo 30 Hari atau alternatif tipe produk)?\n\n"
            "Kalau memang belum prioritas, tidak apa-apa Pak/Bu, kami tetap siap bantu kapan pun dibutuhkan. Sukses selalu untuk Bapak/Ibu dan tim! 🙏"
        )


class QuoteChaser:
    def __init__(self, wa_engine=None):
        self.wa = wa_engine

    async def add_quote(
        self,
        quote_id: str,
        company: str,
        contact_name: str = "",
        phone: str = "",
        email: str = "",
        items_summary: str = "",
        total_amount: float = 0.0,
        sent_date_str: str = "",
        notes: str = "",
    ) -> bool:
        await init_quotes_table()
        s_date = sent_date_str or date.today().isoformat()
        norm_phone = normalize_id_phone(phone)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR REPLACE INTO quotes (
                    id, company, contact_name, phone, email,
                    items_summary, total_amount, sent_date, status, stage, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)
            """, (quote_id, company, contact_name, norm_phone, email, items_summary, total_amount, s_date, notes))
            await db.commit()
        logger.info(f"QuoteChaser: Recorded quote {quote_id} for {company} [{items_summary}]")
        return True

    async def list_quotes(self, status: str | None = None) -> list[dict]:
        await init_quotes_table()
        async with aiosqlite.connect(DB_PATH) as db:
            if status:
                cur = await db.execute("SELECT * FROM quotes WHERE status = ? ORDER BY sent_date DESC", (status,))
            else:
                cur = await db.execute("SELECT * FROM quotes ORDER BY sent_date DESC")
            cols = [d[0] for d in cur.description]
            rows = await cur.fetchall()
            return [dict(zip(cols, r)) for r in rows]

    async def process_due_followups(self, dry_run: bool = True) -> int:
        """Scan active pending quotes and dispatch stage-appropriate follow-ups (dry_run=True by default for safety)."""
        await init_quotes_table()
        quotes = await self.list_quotes(status="pending")
        if not quotes:
            return 0

        today = date.today()
        sent_count = 0

        for q in quotes:
            if _is_manual_operator_quote(q):
                continue
            try:
                s_date = date.fromisoformat(q["sent_date"])
            except ValueError:
                continue

            days_elapsed = (today - s_date).days
            stage = q.get("stage") or 0
            due_stage = 0

            if days_elapsed >= 12 and stage < 3:
                due_stage = 3
            elif days_elapsed >= 6 and stage < 2:
                due_stage = 2
            elif days_elapsed >= 2 and stage < 1:
                due_stage = 1

            if not due_stage:
                continue

            phone = q.get("phone")
            if not phone:
                continue

            if is_internal_operator_number(phone):
                logger.warning(f"QuoteChaser: SKIPPED operator phone number {phone} for quote {q.get('id')}")
                continue

            msg = build_quote_followup_message(
                stage=due_stage,
                contact_name=q.get("contact_name") or "",
                company=q.get("company") or "",
                items_summary=q.get("items_summary") or "",
            )

            if dry_run:
                logger.info(f"QuoteChaser [DRY-RUN]: Would send Stage {due_stage} follow-up to {phone} ({q['company']})")
                sent_count += 1
                continue

            if self.wa:
                auto_mode = getattr(getattr(self.wa, "wa_cfg", None), "auto_reply_mode", "draft")
                if auto_mode == "draft" and hasattr(self.wa, "state") and self.wa.state:
                    norm_ph = normalize_id_phone(phone)
                    if norm_ph:
                        today_str = today.isoformat()
                        outbox_id = await self.wa.state.add_outbox_item(
                            prospect_id=q.get("prospect_id") or "",
                            conversation_id="",
                            kind="reply",
                            to_email=norm_ph,
                            subject=f"Follow-Up SPH {q.get('id')} ({q.get('company')})",
                            body=msg,
                            send_at=datetime.now().isoformat(),
                            status="pending_review",
                            provider="whatsapp",
                        )
                        sent_count += 1
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("""
                                UPDATE quotes SET stage = ?, last_followup_at = ? WHERE id = ?
                            """, (due_stage, today_str, q["id"]))
                            await db.commit()
                        if hasattr(self.wa, "send_system_alert"):
                            await self.wa.send_system_alert(
                                f"📋 *[DRAF FOLLOW-UP SPH SIAP REVIEW]*\n"
                                f"SPH: *{q.get('id')}* — *{q.get('company')}* ({q.get('contact_name') or '-'})\n"
                                f"Kontak: `{norm_ph}`\n\n"
                                f"📝 *Draf:*\n\"{msg}\"\n\n"
                                f"👉 Ketik *OK* / *REVISI: ...* / *BATAL* di WA atau buka Dashboard."
                            )
                else:
                    ok = await self.wa._send_text_auto(phone, msg)
                    if ok:
                        sent_count += 1
                        today_str = today.isoformat()
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("""
                                UPDATE quotes SET stage = ?, last_followup_at = ? WHERE id = ?
                            """, (due_stage, today_str, q["id"]))
                            await db.commit()
                    logger.info(f"QuoteChaser: Stage {due_stage} follow-up sent to {phone} ({q['company']})")

        return sent_count


# ── Safe automation: grouped, rate-limited, reviewable ──

def _split_contact(contact_name: str) -> tuple[str, str]:
    parts = (contact_name or "").split()
    return (parts[0] if parts else ""), " ".join(parts[1:])


def preview_key(company: str, contact_name: str) -> str:
    return f"{(company or '').strip().lower()}|{(contact_name or '').strip().lower()}"


def _parse_date_safe(d_str: str) -> date | None:
    if not d_str:
        return None
    s = d_str.strip()
    try:
        return date.fromisoformat(s)
    except Exception:
        pass
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except Exception:
            pass
    return None


VIP_TARGET_PATTERNS = [
    "karacoco",
    "central proteina",
    "cpp",
    "achmad yani",
    "unjani",
    "tirta arta",
    "mandiri lampung",
    "uim",
]


def _is_vip_target(company: str) -> bool:
    low = (company or "").lower()
    return any(p in low for p in VIP_TARGET_PATTERNS)


def build_vip_closing_message(g: dict, now: datetime | None = None) -> str | None:
    """Specialized high-converting B2B closing messages for AndisLab's top 5 priority accounts:
    PT Karacoco, PT CPP, UNJANI, PT Tirta Arta Makmur, and UIM Lampung."""
    comp_low = (g.get("company") or "").lower()
    greet = _jakarta_greeting(now)

    # 1. PT Karacoco Nucifera Pratama
    if "karacoco" in comp_low:
        return (
            f"{greet} Pak Fadlan (PT Karacoco Nucifera Pratama), salam dari Ahnaf AndisLab. 🙏\n\n"
            "Izin follow-up untuk 3 penawaran pengadaan Glassware, General Lab, dan Chemical lab pengolahan kelapa tempo hari (total Rp 221 Juta).\n\n"
            "Mumpung unit fisik barang saat ini masih ready stock di gudang kami dan kuota Promo Akhir Tahun masih aktif, kami siap support skema closing terbaik untuk Karacoco:\n"
            "1. Diskon Tunai 8% (potongan langsung hemat ~Rp 17,7 Juta) untuk pelunasan sebelum kirim, ATAU\n"
            "2. Skema DP 50% PO (diskon 5%, pelunasan tempo 14 hari kerja).\n"
            "3. Free ongkir & packing kayu aman standar ekspedisi cairan/kaca tanpa biaya tambahan.\n\n"
            "Kira-kira dari kebutuhan di atas, apakah draf PO-nya sudah bisa kami bantu proseskan minggu ini agar alokasi stoknya bisa kami amankan Pak Fadlan? Terima kasih banyak. 🙏"
        )

    # 2. PT Central Proteina Prima Tbk (PT CPP)
    if "central proteina" in comp_low or "cpp" in comp_low:
        return (
            f"{greet} Tim Purchasing CP Prima / Ibu Dhea (PT Central Proteina Prima Tbk), salam dari Ahnaf AndisLab. 🙏\n\n"
            "Izin menyambung penawaran unit *Spectrophotometer UV-Vis* (Rp 146 Juta) untuk kebutuhan QC lab pakan tempo hari.\n\n"
            "Menjelang cut-off pengadaan akhir tahun, kami ingin pastikan unit ready stock ini dapat diamankan untuk CP Prima dengan skema fleksibel:\n"
            "1. Opsi Tempo Instansi 30 Hari (0%) setelah BAST & e-Faktur Pajak resmi terbit, ATAU\n"
            "2. Diskon 8% Cash (efisiensi anggaran QC hemat Rp 11.680.000,-).\n"
            "3. Sudah termasuk QC uji fungsi teknisi, garansi resmi 12 bulan, dan dokumen SPJ tuntas tahun anggaran 2026.\n\n"
            "Apakah ada kelengkapan administrasi atau surat dukungan yang perlu kami bantu siapkan untuk proses PO-nya Bu Dhea? Terima kasih. 🙏"
        )

    # 3. Universitas Jenderal Achmad Yani (UNJANI / UJANI)
    if "achmad yani" in comp_low or "unjani" in comp_low:
        return (
            f"{greet} Dokter Rhea (FK Universitas Jenderal Achmad Yani), salam dari Ahnaf AndisLab. 🙏\n\n"
            "Izin menanyakan terkait penawaran peralatan *General Laboratory* (Rp 54 Juta) untuk FK UNJANI tempo hari.\n\n"
            "Mengingat periode tutup buku anggaran kampus/LPPM 2026 semakin dekat, kami siap support kelancaran administrasi pengadaan lab:\n"
            "1. Dokumen SPJ, BAST resmi, dan e-Faktur Pajak siap terbit 1x24 jam lengkap siap audit anggaran 2026.\n"
            "2. Skema pembayaran fleksibel termin anggaran kampus (Tempo Instansi setelah BAST).\n"
            "3. Free ongkir area Cimahi/Bandung + uji fungsi teknisi & garansi 12 bulan.\n\n"
            "Apakah berkas penawarannya perlu kami bantu sesuaikan atau buatkan revisi SPH resmi baru Dok? Terima kasih banyak. 🙏"
        )

    # 4. PT Tirta Arta Makmur
    if "tirta arta" in comp_low:
        return (
            f"{greet} Pak Sadewo (PT Tirta Arta Makmur), salam dari Ahnaf AndisLab. 🙏\n\n"
            "Izin konfirmasi singkat terkait penawaran *Glassware & General Lab* (Rp 33 Juta) untuk laboratorium mutu air tempo hari.\n\n"
            "Mumpung stok unit di gudang Jabodetabek ready siap kirim:\n"
            "1. Opsi Diskon Tunai 8% (harga langsung dipotong hemat ~Rp 2,64 Juta) sebelum kirim, ATAU opsi DP 50% pelunasan 14 hari.\n"
            "2. Free ongkir Jabodetabek + jaminan garansi penggantian 100% jika ada barang pecah saat pengiriman.\n\n"
            "Kira-kira apakah proses pengadaannya sudah bisa kami siapkan jadwal pengirimannya minggu ini Pak Sadewo? Terima kasih. 🙏"
        )

    # 5. Universitas Indonesia Mandiri (UIM) Lampung
    if "mandiri lampung" in comp_low or "uim" in comp_low:
        return (
            f"{greet} Pak Jovin (Universitas Indonesia Mandiri Lampung), salam dari Ahnaf AndisLab. 🙏\n\n"
            "Izin follow-up mengenai penawaran paket lengkap sarana lab *Glassware and General Lab* (Rp 339 Juta) tempo hari.\n\n"
            "Untuk mendukung kelancaran kesiapan lab praktikum kampus UIM sebelum pergantian tahun, kami siapkan skema kemitraan khusus:\n"
            "1. Diskon Volume Pembayaran Tunai 8% (efisiensi yayasan hemat Rp 27.122.000,-!), ATAU opsi pencairan bertahap DP 50% PO / Tempo 30 Hari Instansi.\n"
            "2. Pengiriman kargo palet kayu aman ke Lampung dengan proteksi penuh + garansi resmi 12 bulan.\n"
            "3. Dokumen BAST & e-Faktur Pajak tuntas tahun anggaran 2026.\n\n"
            "Apakah saat ini proses evaluasi penawarannya ada yang bisa kami bantu koordinasikan Pak Jovin? Terima kasih banyak. 🙏"
        )

    return None


def _is_manual_operator_quote(q: dict) -> bool:
    """Return True for accounts handled personally by the operator (e.g. FIWA)."""
    blob = f"{q.get('company', '')} {q.get('phone', '')} {q.get('notes', '')} {q.get('next_action', '')}".lower()
    return any(tok in blob for tok in ("fiwa", "ashabul fitrah", "81314450085", "[manual_operator]"))


def group_quotes(quotes: list[dict], today: date | None = None,
                 stage1_days: int = 2, stage2_days: int = 6) -> list[dict]:
    """One entry per phone number. Items of all that phone's pending quotes are
    merged into a single message. Entries are returned only when a follow-up is
    due (stage 1 after ``stage1_days`` since SPH, stage 2 after ``stage2_days``
    since the last follow-up). At most 2 follow-ups per contact.
    Prioritizes VIP accounts and highest deal value (excluding manual-operator accounts like FIWA)."""
    today = today or date.today()
    by_phone: dict[str, dict] = {}
    for qd in quotes:
        if qd.get("status") != "pending":
            continue
        if _is_manual_operator_quote(qd):
            continue
        phone = normalize_id_phone(qd.get("phone") or "")
        if not phone or is_internal_operator_number(phone):
            continue
        g = by_phone.setdefault(phone, {
            "phone": phone, "company": qd.get("company") or "",
            "contact_name": qd.get("contact_name") or "",
            "items": [], "ids": [], "stage": 99, "sent": None, "last": None,
            "total_amount": 0.0,
        })
        item = (qd.get("items_summary") or "").strip()
        if item and item not in g["items"]:
            g["items"].append(item)
        g["ids"].append(qd["id"])
        g["stage"] = min(g["stage"], int(qd.get("stage") or 0))
        try:
            g["total_amount"] += float(qd.get("total_amount") or 0.0)
        except Exception:
            pass

        sd = _parse_date_safe(qd.get("sent_date", ""))
        if sd:
            g["sent"] = sd if g["sent"] is None else min(g["sent"], sd)

        if qd.get("last_followup_at"):
            ld = _parse_date_safe(qd.get("last_followup_at", ""))
            if ld:
                g["last"] = ld if g["last"] is None else max(g["last"], ld)

    due = []
    for g in by_phone.values():
        if g["sent"] is None:
            continue
        if g["stage"] == 0 and (today - g["sent"]).days >= stage1_days:
            g["due_stage"] = 1
        elif g["stage"] == 1 and g["last"] and (today - g["last"]).days >= stage2_days:
            g["due_stage"] = 2
        else:
            continue
        # Set VIP priority: 0 for VIP targets (sent first!), 1 for standard
        g["vip_priority"] = 0 if _is_vip_target(g["company"]) else 1
        due.append(g)

    # Sort VIP targets & largest Rp deals to the top
    due.sort(key=lambda g: (g["vip_priority"], -g["total_amount"], g["sent"], g["company"]))
    return due


def build_group_message(g: dict, now: datetime | None = None) -> str:
    first, last = _split_contact(g["contact_name"])
    if g["due_stage"] == 2:
        return build_followup_message(2, first, last, g["company"], now=now)

    # Check if this is one of our 5 VIP closing targets
    vip_msg = build_vip_closing_message(g, now=now)
    if vip_msg:
        return vip_msg

    items = ", ".join(g["items"][:3]) or "alat laboratorium"
    return build_followup_sph_message(g["contact_name"], g["company"], items, now=now)


class QuoteFollowupPlanner:
    """Reviewable CSV: edit column ``kirim`` to ``tidak`` to skip a row."""

    def __init__(self, chaser: "QuoteChaser", cfg):
        self.chaser = chaser
        self.cfg = cfg
        self.path = PROJECT_ROOT / cfg.quote_preview_path

    async def due_groups(self) -> list[dict]:
        pending_q = await self.chaser.list_quotes(status="pending")
        replied_q = await self.chaser.list_quotes(status="replied")
        # Include replied quotes that have gone quiet (>= 4 days since last follow-up or never followed up)
        today = date.today()
        stale_replied = []
        for q in replied_q:
            lf = _parse_date_safe(q.get("last_followup_at") or "")
            if lf is None or (today - lf).days >= 4:
                stale_replied.append(q)
        return group_quotes(
            pending_q + stale_replied,
            stage1_days=self.cfg.quote_stage1_after_days,
            stage2_days=self.cfg.quote_stage2_after_days,
        )

    async def write_preview(self) -> int:
        groups = await self.due_groups()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["kirim", "tahap", "perusahaan", "kontak", "alat", "tanggal_sph", "pesan"])
            for g in groups:
                w.writerow(["ya", g["due_stage"], g["company"], g["contact_name"],
                            "; ".join(g["items"]), g["sent"].isoformat() if g["sent"] else "",
                            build_group_message(g)])
        return len(groups)

    def approved_keys(self) -> set[str] | None:
        """Keys marked 'ya' in the CSV, or None if the file does not exist."""
        if not self.path.exists():
            return None
        keys = set()
        with open(self.path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if (row.get("kirim") or "").strip().lower() in ("ya", "y", "yes", "kirim"):
                    keys.add(preview_key(row.get("perusahaan", ""), row.get("kontak", "")))
        return keys

    async def next_due(self) -> dict | None:
        auto_mode = str(getattr(self.cfg, "auto_reply_mode", "draft") or "draft").lower().strip()
        groups = await self.due_groups()
        if auto_mode in ("full_auto", "autopilot"):
            return groups[0] if groups else None
        approved = self.approved_keys()
        if approved is None:
            n = await self.write_preview()
            logger.info(f"QuoteChaser: Pratinjau dibuat ({n} kontak) di {self.path}. Pengiriman menunggu siklus berikutnya.")
            approved = self.approved_keys() or set()
        for g in groups:
            if preview_key(g["company"], g["contact_name"]) in approved:
                return g
        return None

    async def mark_done(self, g: dict) -> None:
        today_str = date.today().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            for qid in g["ids"]:
                await db.execute(
                    "UPDATE quotes SET stage = ?, last_followup_at = ? WHERE id = ?",
                    (g["due_stage"], today_str, qid),
                )
            await db.commit()
