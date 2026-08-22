"""The analyze stage: catalog -> ranked opportunities + daily market stats.

Reads what the scrape stage stored, prices every Japanese listing against the
Swiss pool, and writes back a ranked opportunity set plus one daily stats
snapshot per watched model (which is what makes the dashboard's charts cheap
-- it reads ~11 small docs, not thousands of listings).
"""

from __future__ import annotations

import logging
import statistics
from datetime import date, datetime, timedelta, timezone

from ..config import Config
from ..matching import build_opportunity, mark_duplicates
from ..models import ChListing, JpListing, ModelStats, Opportunity
from ..store.base import Store

log = logging.getLogger(__name__)


def analyze(cfg: Config, store: Store, *, now: datetime | None = None) -> list[Opportunity]:
    now = now or datetime.now(timezone.utc)

    fx = store.latest_fx()
    if not fx:
        raise RuntimeError(
            "no FX rate stored -- run `nippon-margin scrape` first, or "
            "`nippon-margin backfill --fx` to seed rates."
        )

    jp_listings = store.active_jp()
    ch_listings = store.active_ch()
    log.info("analyzing %d JP listings against %d CH listings at USD/CHF %.4f",
             len(jp_listings), len(ch_listings), fx.usd_chf)

    marks = mark_duplicates(jp_listings)

    # Group the Swiss pool by watchlist key so comp matching is a small scan
    # per car rather than a full cross product.
    ch_by_key: dict[str | None, list[ChListing]] = {}
    for listing in ch_listings:
        ch_by_key.setdefault(listing.watchlist_key, []).append(listing)
    unkeyed = ch_by_key.get(None, [])

    opportunities: list[Opportunity] = []
    for jp in jp_listings:
        pool = ch_by_key.get(jp.watchlist_key, []) + unkeyed if jp.watchlist_key else ch_listings
        opp = build_opportunity(cfg, jp, pool, fx_usd_chf=fx.usd_chf, now=now)
        if not opp:
            continue
        duplicate_of, cheapest = marks.get(jp.doc_id, (None, True))
        opp.duplicate_of = duplicate_of
        opp.is_cheapest_duplicate = cheapest
        if not cheapest:
            # The same car is on offer cheaper elsewhere; keep it visible but
            # never let it outrank the listing you would actually buy.
            opp.opportunity_score = 0.0
            opp.risk_flags = [*opp.risk_flags, f"Same chassis listed cheaper as {duplicate_of}"]
        opportunities.append(opp)

    opportunities.sort(key=lambda o: o.opportunity_score, reverse=True)
    store.save_opportunities(opportunities)

    stats = daily_stats(cfg, jp_listings, ch_listings, opportunities, fx_usd_chf=fx.usd_chf,
                        day=now.date())
    store.save_model_stats(stats)

    scored = [o for o in opportunities if o.opportunity_score > 0]
    log.info("%d opportunities, %d with a positive score; best %.3f",
             len(opportunities), len(scored),
             opportunities[0].opportunity_score if opportunities else 0.0)
    return opportunities


def daily_stats(cfg: Config, jp_listings: list[JpListing], ch_listings: list[ChListing],
                opportunities: list[Opportunity], *, fx_usd_chf: float,
                day: date) -> list[ModelStats]:
    """One snapshot per watched model, for the spread-over-time charts."""
    out: list[ModelStats] = []
    day_str = day.isoformat()

    for item in cfg.watchlist:
        jp_prices = [
            l.price_usd * fx_usd_chf
            for l in jp_listings
            if l.watchlist_key == item.key and l.price_usd
        ]
        ch_prices = [
            l.price_chf for l in ch_listings if l.watchlist_key == item.key and l.price_chf
        ]
        model_opps = [o for o in opportunities if o.watchlist_key == item.key]
        landed = [
            o.landed_roro.landed_chf for o in model_opps if o.landed_roro
        ]
        days_listed = [
            float(l.days_listed)
            for l in ch_listings
            if l.watchlist_key == item.key and l.days_listed is not None
        ]

        jp_median = round(statistics.median(jp_prices), 2) if jp_prices else None
        ch_median = round(statistics.median(ch_prices), 2) if ch_prices else None
        landed_median = round(statistics.median(landed), 2) if landed else None

        out.append(ModelStats(
            day=day_str,
            watchlist_key=item.key,
            jp_count=len(jp_prices),
            ch_count=len(ch_prices),
            jp_median_price_chf=jp_median,
            ch_median_price_chf=ch_median,
            median_landed_chf=landed_median,
            # The spread the dashboard charts: what a Swiss buyer pays minus
            # what the car actually costs us to put on the road here.
            spread_chf=(
                round(ch_median - landed_median, 2)
                if ch_median is not None and landed_median is not None
                else None
            ),
            median_days_listed=round(statistics.median(days_listed), 1) if days_listed else None,
            best_opportunity_score=(
                max((o.opportunity_score for o in model_opps), default=None) or None
            ),
        ))
    return out


def spread_moves(store: Store, *, days: int = 7) -> dict[str, float]:
    """Per-model change in spread over the last `days`, for the digest notes."""
    history = store.load_model_stats(days=days + 7)
    by_key: dict[str, list[ModelStats]] = {}
    for row in history:
        by_key.setdefault(row.watchlist_key, []).append(row)

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    moves: dict[str, float] = {}
    for key, rows in by_key.items():
        ordered = sorted((r for r in rows if r.spread_chf is not None), key=lambda r: r.day)
        if len(ordered) < 2:
            continue
        earlier = [r for r in ordered if r.day <= cutoff]
        baseline = earlier[-1] if earlier else ordered[0]
        moves[key] = round(ordered[-1].spread_chf - baseline.spread_chf, 2)
    return moves


def jp_price_moves(store: Store, *, days: int = 7) -> dict[str, float]:
    """Per-model fractional change in the JP median price."""
    history = store.load_model_stats(days=days + 7)
    by_key: dict[str, list[ModelStats]] = {}
    for row in history:
        by_key.setdefault(row.watchlist_key, []).append(row)

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    moves: dict[str, float] = {}
    for key, rows in by_key.items():
        ordered = sorted(
            (r for r in rows if r.jp_median_price_chf), key=lambda r: r.day
        )
        if len(ordered) < 2:
            continue
        earlier = [r for r in ordered if r.day <= cutoff]
        baseline = earlier[-1] if earlier else ordered[0]
        if not baseline.jp_median_price_chf:
            continue
        moves[key] = round(
            (ordered[-1].jp_median_price_chf - baseline.jp_median_price_chf)
            / baseline.jp_median_price_chf,
            4,
        )
    return moves
