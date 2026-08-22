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
from ..models import AdapterResult, ChListing, JpListing, RunRecord
from ..store.base import Store

log = logging.getLogger(__name__)

#: Nothing may take longer than this; the whole workflow has a ~15 min budget.
ADAPTER_TIMEOUT_S = 8 * 60


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
