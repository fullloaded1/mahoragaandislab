"""
Mahoraga Big Data & LanceDB Vector Intelligence Engine (Tahap 1 & Tahap 3).

Indexes all historical AndisLab assets into local LanceDB (`data/lancedb/`):
  1. `sku_catalog`: All 1,142+ inventory items from `data/inventory.json`
  2. `chat_memory`: Real WhatsApp/email threads + operator REVISI lessons from `data/harvey.db`
  3. `price_market_memory`: Quotes (SPH), INAPROC bids, and vendor quotes

Uses deterministic local L2-normalized feature-hashing embeddings (word 1-2 grams +
char 3-4 grams) so indexing & cosine search run in milliseconds with zero API cost.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

import lancedb
import pyarrow as pa

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LANCEDB_DIR = PROJECT_ROOT / "data" / "lancedb"
SQLITE_PATH = PROJECT_ROOT / "data" / "harvey.db"
INVENTORY_PATH = PROJECT_ROOT / "data" / "inventory.json"

VECTOR_DIM = 512

PARTNER_EXCLUSIONS = (
    "batavia",
    "kuku",
    "andis kukuh",
    "cmsi",
    "cahayatiara",
    "indotrading",
    "partner_mitra",
)

SEGMENT_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "Kesehatan & Rumah Sakit",
        (
            "rsud",
            "rumah sakit",
            "hospital",
            "klinik",
            "clinic",
            "puskesmas",
            "prodia",
            "labkes",
            "patologi",
            "kesehatan",
            "medis",
            "medical",
            "farmakologi",
        ),
    ),
    (
        "Pendidikan & Riset",
        (
            "universitas",
            "university",
            "institut",
            "politeknik",
            "fakultas",
            "fmipa",
            "mipa",
            "smk",
            "sekolah",
            "kampus",
            "akademi",
            "pendidikan",
            "education",
            "riset",
            "brin",
            "lipi",
            "mahasiswa",
            "dosen",
        ),
    ),
    (
        "Pemerintah & Pengadaan",
        (
            "dinas",
            "kementerian",
            "pemkab",
            "pemkot",
            "pemprov",
            "bpom",
            "balai",
            "bappebti",
            "klhk",
            "pertanian",
            "kelautan",
            "perikanan",
            "pdam",
            "inaproc",
            "sirup",
            "lpse",
            "pemerintah",
            "government",
        ),
    ),
    (
        "Industri Swasta (QC/R&D)",
        (
            "pt ",
            "cv ",
            "tbk",
            "industri",
            "pabrik",
            "manufaktur",
            "manufacturing",
            "farmasi",
            "pharma",
            "food",
            "makanan",
            "minuman",
            "beverage",
            "sawit",
            "palm",
            "agro",
            "agribisnis",
            "kosmetik",
            "cosmetic",
            "maklon",
            "tambang",
            "mining",
            "energi",
            "energy",
            "kimia",
            "chemical",
            "pertamina",
            "quality control",
            "qc",
            "r&d",
            "oil",
            "gas",
            "semen",
            "tekstil",
            "kertas",
            "pulp",
        ),
    ),
]


def _normalize_text(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s\-/\.]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _embed_text(text: str, dim: int = VECTOR_DIM) -> list[float]:
    """Deterministic L2-normalized signed feature-hashing vector (word + char n-grams)."""
    clean = _normalize_text(text)
    if not clean:
        return [0.0] * dim

    vec = [0.0] * dim
    tokens = clean.split()

    features: list[tuple[str, float]] = []
    for i, tok in enumerate(tokens):
        if len(tok) >= 2:
            features.append((f"w:{tok}", 4.5))
        if i + 1 < len(tokens):
            features.append((f"b:{tok}_{tokens[i + 1]}", 3.0))

    for feat, weight in features:
        digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[idx] += sign * weight

    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        inv = 1.0 / norm
        vec = [round(v * inv, 6) for v in vec]
    return vec


def classify_segment(name: str = "", industry: str = "", notes: str = "") -> str:
    """Classify a company/prospect into one of the 4 primary AndisLab verticals."""
    blob = f" {name} {industry} {notes} ".lower()
    for segment, keywords in SEGMENT_RULES:
        if any(kw in blob for kw in keywords):
            return segment
    return "Lainnya / Belum Terkategori"


def _is_partner_record(*fields: str) -> bool:
    blob = " ".join(str(f or "") for f in fields).lower()
    return any(p in blob for p in PARTNER_EXCLUSIONS)


class MahoragaBigData:
    """Unified local LanceDB vector store + SQLite Big Data analytics engine."""

    def __init__(
        self,
        lancedb_dir: Path | str | None = None,
        sqlite_path: Path | str | None = None,
        inventory_path: Path | str | None = None,
    ) -> None:
        self.lancedb_dir = Path(lancedb_dir) if lancedb_dir else LANCEDB_DIR
        self.sqlite_path = Path(sqlite_path) if sqlite_path else SQLITE_PATH
        self.inventory_path = Path(inventory_path) if inventory_path else INVENTORY_PATH
        self.lancedb_dir.mkdir(parents=True, exist_ok=True)
        self._db = None

    @property
    def db(self):
        if self._db is None:
            self._db = lancedb.connect(str(self.lancedb_dir))
        return self._db

    def _connect_sqlite(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.sqlite_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Index Builders
    # ------------------------------------------------------------------

    def build_sku_index(self) -> int:
        if not self.inventory_path.exists():
            return 0
        with open(self.inventory_path, "r", encoding="utf-8") as f:
            items = json.load(f)

        rows: list[dict[str, Any]] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            brand = str(item.get("brand") or "")
            model = str(item.get("model") or "")
            catalog_no = str(item.get("catalog_no") or "")
            category = str(item.get("category") or "")
            specs = str(item.get("specifications") or "")
            keywords = " ".join(item.get("keywords") or []) if isinstance(item.get("keywords"), list) else str(item.get("keywords") or "")
            search_text = f"{name} {brand} {model} {catalog_no} {category} {keywords} {specs}".strip()
            rows.append(
                {
                    "id": str(item.get("id") or f"sku_{idx}"),
                    "name": name,
                    "brand": brand,
                    "model": model,
                    "catalog_no": catalog_no,
                    "category": category,
                    "price": float(item.get("price") or 0.0),
                    "harga_beli": float(item.get("harga_beli") or 0.0),
                    "status_stok": str(item.get("status_stok") or "unknown"),
                    "search_text": search_text[:1000],
                    "vector": _embed_text(search_text),
                }
            )

        schema = pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("name", pa.string()),
                pa.field("brand", pa.string()),
                pa.field("model", pa.string()),
                pa.field("catalog_no", pa.string()),
                pa.field("category", pa.string()),
                pa.field("price", pa.float64()),
                pa.field("harga_beli", pa.float64()),
                pa.field("status_stok", pa.string()),
                pa.field("search_text", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), VECTOR_DIM)),
            ]
        )
        self.db.create_table("sku_catalog", data=rows, schema=schema, mode="overwrite")
        return len(rows)

    def build_chat_memory_index(self) -> int:
        if not self.sqlite_path.exists():
            return 0
        conn = self._connect_sqlite()
        rows: list[dict[str, Any]] = []
        try:
            convs = conn.execute(
                """
                SELECT c.id, c.prospect_id, c.thread_json, c.intent, c.stage,
                       p.company, p.first_name, p.last_name, p.status AS p_status,
                       p.personalization_notes
                FROM conversations c
                LEFT JOIN prospects p ON p.id = c.prospect_id
                """
            ).fetchall()

            for conv in convs:
                if _is_partner_record(
                    conv["company"],
                    conv["first_name"],
                    conv["last_name"],
                    conv["personalization_notes"],
                ):
                    continue
                if (conv["intent"] or "").upper() == "OPT_OUT" or (conv["p_status"] or "") == "opted_out":
                    continue
                try:
                    turns = json.loads(conv["thread_json"] or "[]")
                except Exception:
                    turns = []
                if not isinstance(turns, list) or not turns:
                    continue

                # Pair inbound customer message with subsequent outbound reply
                for i, t in enumerate(turns):
                    if not isinstance(t, dict):
                        continue
                    direction = (t.get("direction") or "").lower()
                    body = str(t.get("body") or t.get("text") or "").strip()
                    if direction != "inbound" or len(body) < 8:
                        continue
                    reply_body = ""
                    for j in range(i + 1, min(i + 3, len(turns))):
                        nxt = turns[j]
                        if isinstance(nxt, dict) and (nxt.get("direction") or "").lower() == "outbound":
                            reply_body = str(nxt.get("body") or nxt.get("text") or "").strip()
                            if reply_body:
                                break
                    if not reply_body:
                        continue
                    search_text = f"{body} | {conv['intent'] or ''} | {conv['company'] or ''}"
                    rows.append(
                        {
                            "id": f"conv_{conv['id']}_{i}",
                            "source_type": "conversation",
                            "prospect_id": str(conv["prospect_id"] or ""),
                            "company": str(conv["company"] or "Customer"),
                            "intent": str(conv["intent"] or "general"),
                            "stage": str(conv["stage"] or ""),
                            "customer_msg": body[:500],
                            "agent_reply": reply_body[:800],
                            "search_text": search_text[:800],
                            "vector": _embed_text(search_text),
                        }
                    )

            # Also index operator feedback lessons (approved/edited replies)
            fb_rows = conn.execute(
                """
                SELECT f.id, f.prospect_id, f.ai_draft, f.operator_action, f.final_text,
                       p.company, p.personalization_notes
                FROM operator_feedback_log f
                LEFT JOIN prospects p ON p.id = f.prospect_id
                WHERE f.operator_action IN ('approved', 'edited')
                  AND f.final_text IS NOT NULL AND length(trim(f.final_text)) > 10
                """
            ).fetchall()

            for fb in fb_rows:
                if _is_partner_record(fb["company"], fb["personalization_notes"]):
                    continue
                final_text = str(fb["final_text"] or "").strip()
                ai_draft = str(fb["ai_draft"] or "").strip()
                search_text = f"{ai_draft[:300]} -> {final_text[:300]}"
                rows.append(
                    {
                        "id": f"fb_{fb['id']}",
                        "source_type": f"operator_{fb['operator_action']}",
                        "prospect_id": str(fb["prospect_id"] or ""),
                        "company": str(fb["company"] or "Customer"),
                        "intent": "operator_verified",
                        "stage": str(fb["operator_action"] or ""),
                        "customer_msg": ai_draft[:400],
                        "agent_reply": final_text[:800],
                        "search_text": search_text[:800],
                        "vector": _embed_text(search_text),
                    }
                )
        finally:
            conn.close()

        schema = pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("source_type", pa.string()),
                pa.field("prospect_id", pa.string()),
                pa.field("company", pa.string()),
                pa.field("intent", pa.string()),
                pa.field("stage", pa.string()),
                pa.field("customer_msg", pa.string()),
                pa.field("agent_reply", pa.string()),
                pa.field("search_text", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), VECTOR_DIM)),
            ]
        )
        self.db.create_table("chat_memory", data=rows, schema=schema, mode="overwrite")
        return len(rows)

    def build_price_market_index(self) -> int:
        if not self.sqlite_path.exists():
            return 0
        conn = self._connect_sqlite()
        rows: list[dict[str, Any]] = []
        try:
            for q in conn.execute("SELECT * FROM quotes").fetchall():
                if _is_partner_record(q["company"], q["contact_name"], q["notes"]):
                    continue
                title = str(q["items_summary"] or f"SPH {q['id']}")
                search_text = f"{title} {q['company'] or ''} {q['status'] or ''} {q['notes'] or ''}"
                rows.append(
                    {
                        "id": f"quote_{q['id']}",
                        "source_type": "quote",
                        "title": title[:300],
                        "institution_or_vendor": str(q["company"] or ""),
                        "amount_idr": float(q["total_amount"] or 0.0),
                        "hpp_idr": 0.0,
                        "status": str(q["status"] or "draft"),
                        "notes": str(q["notes"] or "")[:300],
                        "search_text": search_text[:800],
                        "vector": _embed_text(search_text),
                    }
                )

            for b in conn.execute("SELECT * FROM inaproc_bids").fetchall():
                title = str(b["package_name"] or b["matched_product_name"] or f"Bid {b['id']}")
                search_text = f"{title} {b['institution'] or ''} {b['matched_product_name'] or ''} {b['status'] or ''}"
                rows.append(
                    {
                        "id": f"inaproc_{b['id']}",
                        "source_type": "inaproc_bid",
                        "title": title[:300],
                        "institution_or_vendor": str(b["institution"] or ""),
                        "amount_idr": float(b["hps_budget"] or b["target_price"] or 0.0),
                        "hpp_idr": float(b["cost_hpp"] or 0.0),
                        "status": str(b["status"] or ""),
                        "notes": str(b["notes"] or "")[:300],
                        "search_text": search_text[:800],
                        "vector": _embed_text(search_text),
                    }
                )

            for v in conn.execute("SELECT * FROM vendor_quotes").fetchall():
                title = f"{v['brand'] or ''} {v['model'] or ''} {v['specs'] or ''}".strip()
                search_text = f"{title} {v['vendor_name'] or ''} {v['stock_status'] or ''}"
                rows.append(
                    {
                        "id": f"vendor_{v['id']}",
                        "source_type": "vendor_quote",
                        "title": title[:300],
                        "institution_or_vendor": str(v["vendor_name"] or ""),
                        "amount_idr": float(v["unit_price_idr"] or 0.0),
                        "hpp_idr": float(v["landed_cost_idr"] or v["unit_price_idr"] or 0.0),
                        "status": str(v["stock_status"] or ""),
                        "notes": str(v["notes"] or "")[:300],
                        "search_text": search_text[:800],
                        "vector": _embed_text(search_text),
                    }
                )
        finally:
            conn.close()

        schema = pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("source_type", pa.string()),
                pa.field("title", pa.string()),
                pa.field("institution_or_vendor", pa.string()),
                pa.field("amount_idr", pa.float64()),
                pa.field("hpp_idr", pa.float64()),
                pa.field("status", pa.string()),
                pa.field("notes", pa.string()),
                pa.field("search_text", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), VECTOR_DIM)),
            ]
        )
        self.db.create_table("price_market_memory", data=rows, schema=schema, mode="overwrite")
        return len(rows)

    def build_all_indexes(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        sku_count = self.build_sku_index()
        chat_count = self.build_chat_memory_index()
        price_count = self.build_price_market_index()
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "sku_catalog": sku_count,
            "chat_memory": chat_count,
            "price_market_memory": price_count,
            "total_indexed": sku_count + chat_count + price_count,
            "elapsed_ms": elapsed_ms,
            "lancedb_dir": str(self.lancedb_dir),
        }

    # ------------------------------------------------------------------
    # Semantic Retrieval
    # ------------------------------------------------------------------

    def _search_table(
        self, table_name: str, query: str, top_k: int, min_score: float
    ) -> list[dict[str, Any]]:
        if not query or not query.strip():
            return []
        try:
            names = self.db.list_tables()
            table_names = names.tables if hasattr(names, "tables") else list(names)
            if table_name not in table_names:
                return []
            tbl = self.db.open_table(table_name)
            q_vec = _embed_text(query)
            hits = tbl.search(q_vec).metric("cosine").limit(top_k).to_list()
        except Exception as exc:
            logger.debug(f"[BigData] Search failed on {table_name}: {exc}")
            return []

        results: list[dict[str, Any]] = []
        for h in hits:
            # LanceDB cosine distance = 1 - cosine_similarity
            dist = float(h.get("_distance", 1.0))
            sim = max(0.0, round(1.0 - dist, 4))
            if sim < min_score:
                continue
            item = {k: v for k, v in h.items() if k not in ("vector", "_distance")}
            item["similarity"] = sim
            results.append(item)
        return results

    def search_sku_vector(self, query: str, top_k: int = 5, min_score: float = 0.18) -> list[dict[str, Any]]:
        return self._search_table("sku_catalog", query, top_k=top_k, min_score=min_score)

    def search_similar_cases(self, query: str, top_k: int = 3, min_score: float = 0.18) -> list[dict[str, Any]]:
        return self._search_table("chat_memory", query, top_k=top_k, min_score=min_score)

    def search_market_prices(self, query: str, top_k: int = 5, min_score: float = 0.18) -> list[dict[str, Any]]:
        return self._search_table("price_market_memory", query, top_k=top_k, min_score=min_score)

    # ------------------------------------------------------------------
    # Tahap 3: Win/Loss & Big Data Analytics
    # ------------------------------------------------------------------

    def get_table_counts(self) -> dict[str, int]:
        counts = {"sku_catalog": 0, "chat_memory": 0, "price_market_memory": 0}
        try:
            names = self.db.list_tables()
            table_names = names.tables if hasattr(names, "tables") else list(names)
            for t in counts:
                if t in table_names:
                    counts[t] = self.db.open_table(t).count_rows()
        except Exception:
            pass
        return counts

    def get_analytics(self) -> dict[str, Any]:
        lancedb_counts = self.get_table_counts()
        if not self.sqlite_path.exists():
            return {"lancedb_counts": lancedb_counts}

        conn = self._connect_sqlite()
        try:
            # 1. Segment Breakdown across prospects + quotes
            seg_prospects: Counter[str] = Counter()
            for p in conn.execute("SELECT company, industry, personalization_notes, status FROM prospects").fetchall():
                if _is_partner_record(p["company"], p["personalization_notes"]):
                    continue
                if (p["status"] or "") == "opted_out":
                    continue
                seg = classify_segment(p["company"] or "", p["industry"] or "", p["personalization_notes"] or "")
                seg_prospects[seg] += 1

            seg_quotes: dict[str, dict[str, Any]] = {}
            quote_status_counts: Counter[str] = Counter()
            quote_status_value: Counter[str] = Counter()
            blocker_counts: Counter[str] = Counter()
            equip_counter: Counter[str] = Counter()

            equip_keywords = (
                "oven",
                "incubator",
                "autoclave",
                "centrifuge",
                "mikroskop",
                "microscope",
                "spektrofotometer",
                "spectrophotometer",
                "ph meter",
                "timbangan",
                "balance",
                "hplc",
                "gc",
                "water bath",
                "shaker",
                "hotplate",
                "stirrer",
                "viscometer",
                "refractometer",
                "turbidity",
                "laminar",
                "biosafety",
                "fume hood",
                "furnace",
                "distillation",
                "titration",
                "pipette",
                "viscometer",
                "rotary evaporator",
                "homogenizer",
            )

            total_quotes = 0
            won_count = 0
            lost_count = 0
            won_value = 0.0
            total_pipeline_value = 0.0

            for q in conn.execute("SELECT * FROM quotes").fetchall():
                if _is_partner_record(q["company"], q["contact_name"], q["notes"]):
                    continue
                total_quotes += 1
                amt = float(q["total_amount"] or 0.0)
                st = (q["status"] or "draft").lower()
                quote_status_counts[st] += 1
                quote_status_value[st] += amt
                total_pipeline_value += amt

                if st in ("won", "po", "closed_won", "paid"):
                    won_count += 1
                    won_value += amt
                elif st in ("lost", "closed_lost", "cancelled", "rejected"):
                    lost_count += 1

                seg = classify_segment(q["company"] or "", "", q["notes"] or "")
                if seg not in seg_quotes:
                    seg_quotes[seg] = {"quotes": 0, "pipeline_rp": 0.0, "won": 0, "won_rp": 0.0}
                seg_quotes[seg]["quotes"] += 1
                seg_quotes[seg]["pipeline_rp"] += amt
                if st in ("won", "po", "closed_won", "paid"):
                    seg_quotes[seg]["won"] += 1
                    seg_quotes[seg]["won_rp"] += amt

                blk = str(q["blocker"] or "").strip()
                if blk:
                    blocker_counts[blk[:60]] += 1

                summary_l = (q["items_summary"] or "").lower()
                for kw in equip_keywords:
                    if kw in summary_l:
                        equip_counter[kw.title()] += 1

            # Also count equipment mentions in INAPROC + Conversations
            for b in conn.execute("SELECT package_name, matched_product_name FROM inaproc_bids").fetchall():
                txt = f"{b['package_name'] or ''} {b['matched_product_name'] or ''}".lower()
                for kw in equip_keywords:
                    if kw in txt:
                        equip_counter[kw.title()] += 1

            inaproc_by_status: dict[str, dict[str, Any]] = {}
            for b in conn.execute("SELECT status, hps_budget FROM inaproc_bids").fetchall():
                st = (b["status"] or "unknown").lower()
                if st not in inaproc_by_status:
                    inaproc_by_status[st] = {"count": 0, "hps_rp": 0.0}
                inaproc_by_status[st]["count"] += 1
                inaproc_by_status[st]["hps_rp"] += float(b["hps_budget"] or 0.0)

            fb_counts: Counter[str] = Counter()
            for fb in conn.execute("SELECT operator_action FROM operator_feedback_log").fetchall():
                fb_counts[(fb["operator_action"] or "unknown").lower()] += 1

            decided = won_count + lost_count
            win_rate_pct = round((won_count / decided) * 100, 1) if decided > 0 else round((won_count / total_quotes) * 100, 1) if total_quotes > 0 else 0.0

            segments = []
            all_segs = set(seg_prospects.keys()) | set(seg_quotes.keys())
            for seg in sorted(all_segs, key=lambda s: seg_prospects[s], reverse=True):
                qinfo = seg_quotes.get(seg, {"quotes": 0, "pipeline_rp": 0.0, "won": 0, "won_rp": 0.0})
                segments.append(
                    {
                        "segment": seg,
                        "prospects": seg_prospects[seg],
                        "quotes": qinfo["quotes"],
                        "pipeline_rp": qinfo["pipeline_rp"],
                        "won": qinfo["won"],
                        "won_rp": qinfo["won_rp"],
                    }
                )

            return {
                "lancedb_counts": lancedb_counts,
                "total_prospects_clean": sum(seg_prospects.values()),
                "segments": segments,
                "win_loss": {
                    "total_quotes": total_quotes,
                    "won_count": won_count,
                    "lost_count": lost_count,
                    "won_value_rp": won_value,
                    "total_pipeline_rp": total_pipeline_value,
                    "win_rate_pct": win_rate_pct,
                    "by_status": {
                        st: {"count": quote_status_counts[st], "value_rp": quote_status_value[st]}
                        for st in quote_status_counts
                    },
                    "top_blockers": [{"blocker": k, "count": v} for k, v in blocker_counts.most_common(6)],
                },
                "inaproc_summary": inaproc_by_status,
                "top_requested_equipment": [{"equipment": k, "mentions": v} for k, v in equip_counter.most_common(10)],
                "operator_learning": dict(fb_counts),
            }
        finally:
            conn.close()
