"""SQLite backend -- the only backend.

The whole catalog is one file. That is what makes the git-backed state sync
in `statesync.py` possible: a single blob to encrypt, commit and restore.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ..models import (
    ChListing,
    FxRate,
    JpListing,
    ListingStatus,
    ModelStats,
    Opportunity,
    RunRecord,
)
from .base import Store

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings_jp (
    doc_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    watchlist_key TEXT,
    price_usd REAL,
    status TEXT NOT NULL DEFAULT 'active',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jp_key ON listings_jp(watchlist_key, status);
CREATE INDEX IF NOT EXISTS idx_jp_seen ON listings_jp(last_seen);

CREATE TABLE IF NOT EXISTS listings_ch (
    doc_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    watchlist_key TEXT,
    price_chf REAL,
    status TEXT NOT NULL DEFAULT 'active',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ch_key ON listings_ch(watchlist_key, status);
CREATE INDEX IF NOT EXISTS idx_ch_seen ON listings_ch(last_seen);

CREATE TABLE IF NOT EXISTS opportunities (
    id TEXT PRIMARY KEY,
    watchlist_key TEXT,
    opportunity_score REAL NOT NULL DEFAULT 0,
    computed_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opp_score ON opportunities(opportunity_score DESC);

CREATE TABLE IF NOT EXISTS price_history (
    doc_id TEXT NOT NULL,
    side TEXT NOT NULL,
    at TEXT NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (doc_id, at)
);

CREATE TABLE IF NOT EXISTS model_stats_daily (
    day TEXT NOT NULL,
    watchlist_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (day, watchlist_key)
);

CREATE TABLE IF NOT EXISTS fx_rates (
    day TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts_sent (
    key TEXT PRIMARY KEY,
    at TEXT NOT NULL
);
"""


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _dump(model) -> str:
    return model.model_dump_json()


class SqliteStore(Store):
    def __init__(self, path: str | Path = "data/nippon.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- listings -----------------------------------------------------------
    def _upsert(self, table: str, listings: list, price_col: str,
                price_attr: str) -> tuple[int, int]:
        new = updated = 0
        cur = self.conn.cursor()
        for lst in listings:
            doc_id = lst.doc_id
            row = cur.execute(
                f"SELECT first_seen, {price_col} FROM {table} WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            price = getattr(lst, price_attr)
            if row is None:
                cur.execute(
                    f"""INSERT INTO {table}
                        (doc_id, source, watchlist_key, {price_col}, status,
                         first_seen, last_seen, payload)
                        VALUES (?,?,?,?,?,?,?,?)""",
                    (doc_id, lst.source, lst.watchlist_key, price,
                     ListingStatus.ACTIVE.value, _iso(lst.first_seen), _iso(lst.last_seen),
                     _dump(lst)),
                )
                new += 1
                if price:
                    self.record_price_change(
                        doc_id=doc_id, side=table[-2:], price=price, at=lst.last_seen
                    )
            else:
                # Preserve the original first_seen: it is how long the car has
                # been sitting, which is a demand signal in its own right.
                lst.first_seen = datetime.fromisoformat(row["first_seen"])
                cur.execute(
                    f"""UPDATE {table}
                        SET source=?, watchlist_key=?, {price_col}=?, status=?,
                            last_seen=?, payload=?
                        WHERE doc_id=?""",
                    (lst.source, lst.watchlist_key, price, ListingStatus.ACTIVE.value,
                     _iso(lst.last_seen), _dump(lst), doc_id),
                )
                updated += 1
                if price and row[price_col] != price:
                    self.record_price_change(
                        doc_id=doc_id, side=table[-2:], price=price, at=lst.last_seen
                    )
        self.conn.commit()
        return new, updated

    def upsert_jp(self, listings: list[JpListing]) -> tuple[int, int]:
        return self._upsert("listings_jp", listings, "price_usd", "price_usd")

    def upsert_ch(self, listings: list[ChListing]) -> tuple[int, int]:
        return self._upsert("listings_ch", listings, "price_chf", "price_chf")

    def _active(self, table: str, model):
        rows = self.conn.execute(
            f"SELECT payload FROM {table} WHERE status = 'active'"
        ).fetchall()
        out = []
        for row in rows:
            try:
                out.append(model.model_validate_json(row["payload"]))
            except Exception as exc:  # noqa: BLE001 - a stale row must not kill analyze
                log.warning("skipping unreadable %s row: %s", table, exc)
        return out

    def active_jp(self) -> list[JpListing]:
        return self._active("listings_jp", JpListing)

    def active_ch(self) -> list[ChListing]:
        return self._active("listings_ch", ChListing)

    def mark_delisted(self, *, before: datetime) -> int:
        total = 0
        for table in ("listings_jp", "listings_ch"):
            cur = self.conn.execute(
                f"UPDATE {table} SET status='delisted' "
                "WHERE status='active' AND last_seen < ?",
                (_iso(before),),
            )
            total += cur.rowcount
        self.conn.commit()
        return total

    # -- derived ------------------------------------------------------------
    def save_opportunities(self, opportunities: list[Opportunity]) -> int:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM opportunities")
        cur.executemany(
            "INSERT INTO opportunities (id, watchlist_key, opportunity_score, "
            "computed_at, payload) VALUES (?,?,?,?,?)",
            [(o.id, o.watchlist_key, o.opportunity_score, _iso(o.computed_at), _dump(o))
             for o in opportunities],
        )
        self.conn.commit()
        return len(opportunities)

    def load_opportunities(self, *, limit: int = 200) -> list[Opportunity]:
        rows = self.conn.execute(
            "SELECT payload FROM opportunities ORDER BY opportunity_score DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [Opportunity.model_validate_json(r["payload"]) for r in rows]

    def save_model_stats(self, stats: list[ModelStats]) -> int:
        self.conn.executemany(
            "INSERT OR REPLACE INTO model_stats_daily (day, watchlist_key, payload) "
            "VALUES (?,?,?)",
            [(s.day, s.watchlist_key, _dump(s)) for s in stats],
        )
        self.conn.commit()
        return len(stats)

    def load_model_stats(self, *, watchlist_key: str | None = None,
                         days: int = 90) -> list[ModelStats]:
        sql = "SELECT payload FROM model_stats_daily"
        params: list = []
        if watchlist_key:
            sql += " WHERE watchlist_key = ?"
            params.append(watchlist_key)
        sql += " ORDER BY day DESC LIMIT ?"
        params.append(days * 40)
        rows = self.conn.execute(sql, params).fetchall()
        return [ModelStats.model_validate_json(r["payload"]) for r in rows]

    # -- fx -----------------------------------------------------------------
    def save_fx(self, rate: FxRate) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO fx_rates (day, payload) VALUES (?,?)",
            (rate.day, _dump(rate)),
        )
        self.conn.commit()

    def load_fx(self, *, days: int = 30) -> list[FxRate]:
        rows = self.conn.execute(
            "SELECT payload FROM fx_rates ORDER BY day DESC LIMIT ?", (days,)
        ).fetchall()
        return [FxRate.model_validate_json(r["payload"]) for r in rows]

    def latest_fx(self) -> FxRate | None:
        rates = self.load_fx(days=1)
        return rates[0] if rates else None

    # -- price history ------------------------------------------------------
    def record_price_change(self, *, doc_id: str, side: str, price: float,
                            at: datetime) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO price_history (doc_id, side, at, price) VALUES (?,?,?,?)",
            (doc_id, side, _iso(at), price),
        )

    def price_history(self, doc_id: str) -> list[tuple[datetime, float]]:
        rows = self.conn.execute(
            "SELECT at, price FROM price_history WHERE doc_id = ? ORDER BY at", (doc_id,)
        ).fetchall()
        return [(datetime.fromisoformat(r["at"]), r["price"]) for r in rows]

    # -- runs + alerts ------------------------------------------------------
    def save_run(self, run: RunRecord) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (id, started_at, payload) VALUES (?,?,?)",
            (run.id, _iso(run.started_at), _dump(run)),
        )
        self.conn.commit()

    def recent_runs(self, *, limit: int = 20) -> list[RunRecord]:
        rows = self.conn.execute(
            "SELECT payload FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [RunRecord.model_validate_json(r["payload"]) for r in rows]

    def alert_sent_at(self, key: str) -> datetime | None:
        row = self.conn.execute("SELECT at FROM alerts_sent WHERE key = ?", (key,)).fetchone()
        return datetime.fromisoformat(row["at"]) if row else None

    def mark_alert_sent(self, key: str, at: datetime) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO alerts_sent (key, at) VALUES (?,?)", (key, _iso(at))
        )
        self.conn.commit()
