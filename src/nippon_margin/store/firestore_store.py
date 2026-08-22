"""Firestore backend -- the production datastore.

Actions runners are ephemeral, so Firestore is where state actually lives.
Firestore also bills per document read/write, so the design here is shaped by
the free tier (50k reads / 20k writes per day):

  * writes are batched (500 ops per commit, the API maximum);
  * a listing is only written when something actually changed -- an unchanged
    car costs one read of a summary doc, not a write;
  * the dashboard never scans `listings_*`. Everything it needs is
    precomputed into small summary documents by the daily run, so opening the
    app costs a handful of reads rather than thousands.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

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

COL_JP = "listings_jp"
COL_CH = "listings_ch"
COL_OPP = "opportunities"
COL_HISTORY = "price_history"
COL_STATS = "model_stats_daily"
COL_FX = "fx_rates"
COL_RUNS = "runs"
COL_ALERTS = "alerts_sent"
COL_CONFIG = "config"
COL_SUMMARY = "summaries"

BATCH_LIMIT = 500


def _credentials():
    """Service-account credentials from the env, however they were supplied.

    In Actions the workflow puts the whole JSON in `FIREBASE_SERVICE_ACCOUNT`;
    locally you are more likely to have a file path in the standard
    `GOOGLE_APPLICATION_CREDENTIALS`.
    """
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if raw:
        from google.oauth2 import service_account

        info = json.loads(raw)
        return service_account.Credentials.from_service_account_info(info), info.get("project_id")
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if path and os.path.exists(path):
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(path)
        with open(path, encoding="utf-8") as fh:
            return creds, json.load(fh).get("project_id")
    return None, None


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _payload(model) -> dict[str, Any]:
    return json.loads(model.model_dump_json())


def _fingerprint(data: dict[str, Any], ignore: tuple[str, ...]) -> str:
    """Hash of everything except the fields that change on every run.

    `last_seen` always moves; if we wrote on that alone, every listing would
    cost a write every day for nothing.
    """
    trimmed = {k: v for k, v in data.items() if k not in ignore}
    return hashlib.sha256(
        json.dumps(trimmed, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:32]


class FirestoreStore(Store):
    def __init__(self, project_id: str | None = None):
        try:
            from google.cloud import firestore
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "google-cloud-firestore is not installed. "
                "Install the extra: pip install -e '.[firestore]'"
            ) from exc

        creds, creds_project = _credentials()
        project = project_id or os.environ.get("FIREBASE_PROJECT_ID") or creds_project
        if not project:
            raise RuntimeError(
                "No Firebase project id. Set FIREBASE_PROJECT_ID, or "
                "meta.firebase_project_id in config.yaml, or supply a "
                "service-account JSON that names one."
            )
        self.db = (
            firestore.Client(project=project, credentials=creds)
            if creds
            else firestore.Client(project=project)
        )
        self.project = project
        self.writes = 0

    # -- batching -----------------------------------------------------------
    def _commit(self, ops: list[tuple[Any, dict, bool]]) -> None:
        """`ops` is a list of (doc_ref, payload, merge)."""
        for start in range(0, len(ops), BATCH_LIMIT):
            batch = self.db.batch()
            for ref, data, merge in ops[start : start + BATCH_LIMIT]:
                batch.set(ref, data, merge=merge)
            batch.commit()
            self.writes += len(ops[start : start + BATCH_LIMIT])

    # -- listings -----------------------------------------------------------
    def _upsert(self, collection: str, listings: list, price_attr: str) -> tuple[int, int]:
        col = self.db.collection(collection)
        ops: list[tuple[Any, dict, bool]] = []
        history_ops: list[tuple[Any, dict, bool]] = []
        new = updated = 0

        for lst in listings:
            ref = col.document(lst.doc_id)
            snapshot = ref.get()
            data = _payload(lst)
            price = getattr(lst, price_attr)
            fingerprint = _fingerprint(data, ignore=("last_seen", "first_seen", "status"))

            if not snapshot.exists:
                data["first_seen"] = _iso(lst.first_seen)
                data["last_seen"] = _iso(lst.last_seen)
                data["status"] = ListingStatus.ACTIVE.value
                data["_fp"] = fingerprint
                ops.append((ref, data, False))
                new += 1
                if price:
                    history_ops.append(self._history_op(lst.doc_id, collection, price, lst.last_seen))
                continue

            existing = snapshot.to_dict() or {}
            lst.first_seen = _parse_dt(existing.get("first_seen")) or lst.first_seen
            old_price = existing.get(price_attr)

            if existing.get("_fp") == fingerprint and existing.get("status") == "active":
                # Nothing but the clock moved: touch only last_seen.
                ops.append((ref, {"last_seen": _iso(lst.last_seen)}, True))
            else:
                data["first_seen"] = _iso(lst.first_seen)
                data["last_seen"] = _iso(lst.last_seen)
                data["status"] = ListingStatus.ACTIVE.value
                data["_fp"] = fingerprint
                ops.append((ref, data, True))
            updated += 1

            if price and old_price != price:
                history_ops.append(self._history_op(lst.doc_id, collection, price, lst.last_seen))

        self._commit(ops + history_ops)
        return new, updated

    def _history_op(self, doc_id: str, collection: str, price: float, at: datetime):
        side = "jp" if collection == COL_JP else "ch"
        ref = (
            self.db.collection(COL_HISTORY)
            .document(doc_id)
            .collection("points")
            .document(_iso(at)[:19])
        )
        return (ref, {"at": _iso(at), "price": price, "side": side}, False)

    def upsert_jp(self, listings: list[JpListing]) -> tuple[int, int]:
        return self._upsert(COL_JP, listings, "price_usd")

    def upsert_ch(self, listings: list[ChListing]) -> tuple[int, int]:
        return self._upsert(COL_CH, listings, "price_chf")

    def _active(self, collection: str, model) -> list:
        out = []
        query = self.db.collection(collection).where("status", "==", "active")
        for doc in query.stream():
            try:
                out.append(model.model_validate(doc.to_dict()))
            except Exception as exc:  # noqa: BLE001
                log.warning("skipping unreadable %s/%s: %s", collection, doc.id, exc)
        return out

    def active_jp(self) -> list[JpListing]:
        return self._active(COL_JP, JpListing)

    def active_ch(self) -> list[ChListing]:
        return self._active(COL_CH, ChListing)

    def mark_delisted(self, *, before: datetime) -> int:
        cutoff = _iso(before)
        ops: list[tuple[Any, dict, bool]] = []
        for collection in (COL_JP, COL_CH):
            query = (
                self.db.collection(collection)
                .where("status", "==", "active")
                .where("last_seen", "<", cutoff)
            )
            for doc in query.stream():
                ops.append((doc.reference, {"status": ListingStatus.DELISTED.value,
                                            "delisted_at": _iso(datetime.now(UTC))}, True))
        self._commit(ops)
        return len(ops)

    # -- derived ------------------------------------------------------------
    def save_opportunities(self, opportunities: list[Opportunity]) -> int:
        col = self.db.collection(COL_OPP)
        ops = [(col.document(o.id), _payload(o), False) for o in opportunities]

        # Anything that dropped out of today's ranking is stale; clear it so
        # the dashboard never shows a car that is already sold.
        keep = {o.id for o in opportunities}
        for doc in col.select([]).stream():
            if doc.id not in keep:
                doc.reference.delete()
                self.writes += 1

        self._commit(ops)
        self._write_summary(opportunities)
        return len(opportunities)

    def _write_summary(self, opportunities: list[Opportunity]) -> None:
        """One small doc the dashboard can open for a cheap first paint."""
        top = sorted(opportunities, key=lambda o: o.opportunity_score, reverse=True)[:20]
        summary = {
            "generated_at": _iso(datetime.now(UTC)),
            "total": len(opportunities),
            "by_tier": {
                tier: sum(1 for o in opportunities if o.capital_tier == tier)
                for tier in {o.capital_tier for o in opportunities}
            },
            "top": [
                {
                    "id": o.id,
                    "make": o.make,
                    "model": o.model,
                    "variant": o.variant,
                    "year": o.year,
                    "score": o.opportunity_score,
                    "margin_pct": o.margin_pct,
                    "gross_margin_chf": o.gross_margin_chf,
                    "landed_chf": o.landed_roro.landed_chf if o.landed_roro else None,
                    "capital_tier": o.capital_tier,
                    "url": o.url,
                }
                for o in top
            ],
        }
        self.db.collection(COL_SUMMARY).document("opportunities").set(summary)
        self.writes += 1

    def load_opportunities(self, *, limit: int = 200) -> list[Opportunity]:
        from google.cloud.firestore import Query

        query = (
            self.db.collection(COL_OPP)
            .order_by("opportunity_score", direction=Query.DESCENDING)
            .limit(limit)
        )
        out = []
        for doc in query.stream():
            try:
                out.append(Opportunity.model_validate(doc.to_dict()))
            except Exception as exc:  # noqa: BLE001
                log.warning("skipping unreadable opportunity %s: %s", doc.id, exc)
        return out

    def save_model_stats(self, stats: list[ModelStats]) -> int:
        col = self.db.collection(COL_STATS)
        ops = [(col.document(f"{s.day}_{s.watchlist_key}"), _payload(s), False) for s in stats]
        self._commit(ops)
        return len(stats)

    def load_model_stats(self, *, watchlist_key: str | None = None,
                         days: int = 90) -> list[ModelStats]:
        from google.cloud.firestore import Query

        query = self.db.collection(COL_STATS)
        if watchlist_key:
            query = query.where("watchlist_key", "==", watchlist_key)
        query = query.order_by("day", direction=Query.DESCENDING).limit(days * 40)
        return [ModelStats.model_validate(d.to_dict()) for d in query.stream()]

    # -- fx -----------------------------------------------------------------
    def save_fx(self, rate: FxRate) -> None:
        self.db.collection(COL_FX).document(rate.day).set(_payload(rate))
        self.writes += 1

    def load_fx(self, *, days: int = 30) -> list[FxRate]:
        from google.cloud.firestore import Query

        query = (
            self.db.collection(COL_FX)
            .order_by("day", direction=Query.DESCENDING)
            .limit(days)
        )
        return [FxRate.model_validate(d.to_dict()) for d in query.stream()]

    def latest_fx(self) -> FxRate | None:
        rates = self.load_fx(days=1)
        return rates[0] if rates else None

    # -- price history ------------------------------------------------------
    def record_price_change(self, *, doc_id: str, side: str, price: float,
                            at: datetime) -> None:
        ref = (
            self.db.collection(COL_HISTORY)
            .document(doc_id)
            .collection("points")
            .document(_iso(at)[:19])
        )
        ref.set({"at": _iso(at), "price": price, "side": side})
        self.writes += 1

    def price_history(self, doc_id: str) -> list[tuple[datetime, float]]:
        docs = (
            self.db.collection(COL_HISTORY)
            .document(doc_id)
            .collection("points")
            .order_by("at")
            .stream()
        )
        out = []
        for doc in docs:
            data = doc.to_dict() or {}
            when = _parse_dt(data.get("at"))
            if when and data.get("price") is not None:
                out.append((when, float(data["price"])))
        return out

    # -- runs + alerts ------------------------------------------------------
    def save_run(self, run: RunRecord) -> None:
        self.db.collection(COL_RUNS).document(run.id).set(_payload(run))
        # A pointer doc so the dashboard's health page is a single read.
        self.db.collection(COL_SUMMARY).document("last_run").set(_payload(run))
        self.writes += 2

    def recent_runs(self, *, limit: int = 20) -> list[RunRecord]:
        from google.cloud.firestore import Query

        query = (
            self.db.collection(COL_RUNS)
            .order_by("started_at", direction=Query.DESCENDING)
            .limit(limit)
        )
        return [RunRecord.model_validate(d.to_dict()) for d in query.stream()]

    def alert_sent_at(self, key: str) -> datetime | None:
        doc = self.db.collection(COL_ALERTS).document(_safe_id(key)).get()
        if not doc.exists:
            return None
        return _parse_dt((doc.to_dict() or {}).get("at"))

    def mark_alert_sent(self, key: str, at: datetime) -> None:
        self.db.collection(COL_ALERTS).document(_safe_id(key)).set(
            {"key": key, "at": _iso(at)}
        )
        self.writes += 1

    # -- live config --------------------------------------------------------
    def watchlist_override(self) -> list[dict] | None:
        doc = self.db.collection(COL_CONFIG).document("watchlist").get()
        if not doc.exists:
            return None
        rows = (doc.to_dict() or {}).get("items")
        return rows if isinstance(rows, list) else None


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _safe_id(key: str) -> str:
    import re

    cleaned = re.sub(r"[^A-Za-z0-9_.\-]", "-", key)
    return cleaned if 0 < len(cleaned) <= 120 else hashlib.sha1(key.encode()).hexdigest()
