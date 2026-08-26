"""The scrape stage.

Runs every enabled adapter concurrently, with per-source error isolation: a
source that 500s, changes its HTML or hangs produces an error row in the run
record and nothing worse. The run only fails if *every* source fails, which
is the signal that something is wrong with us rather than with them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime, timedelta

from ..adapters.base import Adapter
from ..adapters.registry import build_adapters
from ..config import Config
from ..fx import refresh_fx
from ..http import Fetcher
from ..matching import mark_duplicates
from ..models import AdapterResult, ChListing, JpListing, RunRecord, Steering
from ..store.base import Store

log = logging.getLogger(__name__)

#: Nothing may take longer than this; the whole workflow has a ~15 min budget.
ADAPTER_TIMEOUT_S = 8 * 60


def filter_by_origin(cfg: Config, jp: list[JpListing]) -> tuple[list[JpListing], dict[str, int]]:
    """Keep listings we are willing to buy from, and say what we saw.

    The rule is per *source*, not per listing: **if a site states countries,
    a listing from it with no country is missing data, not a Japanese car.**

    Some exporters (exportfrom.jp) never print a location at all and sell
    Japanese stock only -- for those, an unstated origin really does mean
    `assumed_origin`, and dropping them would discard good stock for nothing.
    But BE FORWARD and SBT print a location on every car, most of it not in
    Japan. On a site like that, a blank field is a gap in the scrape, and
    assuming Japan would quietly buy from Incheon.

    `allow_unknown_origin` is the master switch for that assumption; the
    source-level test decides where it is allowed to apply.
    """
    seen: dict[str, int] = {}
    for lst in jp:
        key = cfg.risk.resolve_origin(lst.location) or "not stated"
        seen[key] = seen.get(key, 0) + 1

    if not cfg.risk.allowed_origins:
        return jp, seen

    # Sources that publish a location for at least one car are held to it
    # for all of them.
    states_origin = {
        lst.source for lst in jp if cfg.risk.resolve_origin(lst.location) is not None
    }

    kept = []
    for lst in jp:
        origin = cfg.risk.resolve_origin(lst.location)
        if origin is not None:
            if cfg.risk.origin_allowed(origin):
                kept.append(lst)
        elif lst.source not in states_origin and cfg.risk.allow_unknown_origin:
            kept.append(lst)
    return kept, seen


async def run_adapter(adapter: Adapter) -> tuple[AdapterResult, list]:
    started = time.monotonic()
    try:
        listings = await asyncio.wait_for(adapter.run(), timeout=ADAPTER_TIMEOUT_S)
        return (
            AdapterResult(
                source=adapter.name, ok=True, count=len(listings),
                duration_s=round(time.monotonic() - started, 1),
            ),
            listings,
        )
    except TimeoutError:
        log.error("%s timed out after %ss", adapter.name, ADAPTER_TIMEOUT_S)
        return (
            AdapterResult(source=adapter.name, ok=False, error=f"timeout after {ADAPTER_TIMEOUT_S}s",
                          duration_s=round(time.monotonic() - started, 1)),
            [],
        )
    except Exception as exc:  # noqa: BLE001 - isolation is the entire point
        log.exception("%s failed", adapter.name)
        return (
            AdapterResult(source=adapter.name, ok=False, error=f"{type(exc).__name__}: {exc}",
                          duration_s=round(time.monotonic() - started, 1)),
            [],
        )


async def scrape(cfg: Config, store: Store, *, only: str | None = None,
                 dry_run: bool = False) -> RunRecord:
    started = datetime.now(UTC)
    run = RunRecord(
        id=started.strftime("%Y%m%dT%H%M%SZ"),
        started_at=started,
        command="scrape",
        git_sha=os.environ.get("GITHUB_SHA"),
    )

    async with Fetcher(cfg.sources.http) as fetcher:
        fx = await refresh_fx(fetcher, store) if not dry_run else None
        if fx:
            log.info("FX %s: USD/CHF %.4f", fx.day, fx.usd_chf)

        adapters = build_adapters(cfg, fetcher, only=only)
        if not adapters:
            run.errors.append("no adapters enabled")
            run.ok = False
            return run

        log.info("running %d adapters: %s", len(adapters), ", ".join(a.name for a in adapters))
        results = await asyncio.gather(*(run_adapter(a) for a in adapters))

    jp: list[JpListing] = []
    ch: list[ChListing] = []
    for result, listings in results:
        run.adapters.append(result)
        if not result.ok and result.error:
            run.errors.append(f"{result.source}: {result.error}")
        for listing in listings:
            (jp if isinstance(listing, JpListing) else ch).append(listing)

    if cfg.risk.allowed_origins:
        before = len(jp)
        jp, seen_origins = filter_by_origin(cfg, jp)
        log.info(
            "origin filter: kept %d/%d JP listings (allowed: %s); saw %s",
            len(jp), before, ", ".join(cfg.risk.allowed_origins),
            ", ".join(f"{k}={v}" for k, v in sorted(seen_origins.items(), key=lambda kv: -kv[1])),
        )

    if cfg.risk.exclude_rhd:
        before = len(jp)
        jp = [lst for lst in jp if lst.steering is not Steering.RHD]
        if before != len(jp):
            log.info("dropped %d right-hand-drive listings", before - len(jp))

    if cfg.sources.only_watchlist:
        before_jp, before_ch = len(jp), len(ch)
        jp = [lst for lst in jp if lst.watchlist_key]
        ch = [lst for lst in ch if lst.watchlist_key]
        log.info(
            "watchlist filter: kept %d/%d JP and %d/%d CH listings",
            len(jp), before_jp, len(ch), before_ch,
        )

    # The same physical car is routinely listed by several exporters at
    # several prices; flag the cheapest before anything is stored.
    marks = mark_duplicates(jp)
    for listing in jp:
        duplicate_of, cheapest = marks.get(listing.doc_id, (None, True))
        if not cheapest:
            listing.description = f"[duplicate of {duplicate_of}] {listing.description}"[:2000]

    run.jp_count = len(jp)
    run.ch_count = len(ch)
    run.ok = any(r.ok for r in run.adapters)

    if dry_run:
        log.info("dry run: parsed %d JP and %d CH listings, wrote nothing", len(jp), len(ch))
        run.finished_at = datetime.now(UTC)
        return run

    new_jp, upd_jp = store.upsert_jp(jp)
    new_ch, upd_ch = store.upsert_ch(ch)
    log.info("JP: %d new, %d updated | CH: %d new, %d updated", new_jp, upd_jp, new_ch, upd_ch)

    cutoff = datetime.now(UTC) - timedelta(days=cfg.catalog.delist_after_days)
    delisted = store.mark_delisted(before=cutoff)
    if delisted:
        log.info("marked %d listings delisted (unseen for %d days)",
                 delisted, cfg.catalog.delist_after_days)

    run.finished_at = datetime.now(UTC)
    store.save_run(run)
    return run
