"""Adapter contract.

Adding a source means subclassing `JpAdapter` or `ChAdapter` and implementing
`search_urls()` + `parse_page()`. Everything else -- throttling, robots,
caching, Playwright rendering, error isolation, watchlist resolution -- is
handled here, which is what keeps a new adapter to roughly 30 lines.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from ..config import Config, SourceConfig
from ..http import Fetcher, render
from ..matching import resolve_watchlist_key
from ..models import ChListing, JpListing

log = logging.getLogger(__name__)


class Adapter(ABC):
    #: stable short name; also the doc-id prefix and the `--source` value
    name: str = ""
    #: "jp" (buy side) or "ch" (sell side)
    side: str = "jp"
    #: used for absolute-ising relative hrefs
    base_url: str = ""

    def __init__(self, cfg: Config, source_cfg: SourceConfig, fetcher: Fetcher):
        self.cfg = cfg
        self.source_cfg = source_cfg
        self.fetcher = fetcher

    # -- to implement -------------------------------------------------------
    @abstractmethod
    def search_urls(self) -> Iterable[str]:
        """Every listing-index URL this adapter should walk this run."""

    @abstractmethod
    def parse_page(self, html: str, url: str) -> list:
        """Listings found on one index page."""

    # -- provided -----------------------------------------------------------
    def absolute(self, href: str | None) -> str:
        if not href:
            return ""
        return href if href.startswith("http") else urljoin(self.base_url, href)

    @staticmethod
    def dom(html: str) -> HTMLParser:
        return HTMLParser(html)

    async def fetch_page(self, url: str) -> str | None:
        """Fetch via plain HTTP or headless Chromium, per the source config."""
        if self.source_cfg.renderer == "playwright":
            cached = self.fetcher._read_cache(url)  # noqa: SLF001 - same package
            if cached is not None:
                return cached
            if not await self.fetcher.allowed(url):
                log.warning("robots.txt disallows %s -- skipping", url)
                return None
            html = await render(url, self.cfg.sources.http, wait_selector=self.wait_selector())
            if html:
                self.fetcher._write_cache(url, html)  # noqa: SLF001
            return html
        return await self.fetcher.get(url)

    def wait_selector(self) -> str | None:
        """CSS selector that signals a rendered page is ready. Override if useful."""
        return None

    @staticmethod
    def series_key(url: str) -> str:
        """Group a URL with the other pages of the same query.

        Used to stop paginating a query the moment it runs dry -- most of
        these searches have far fewer results than `max_pages`, and walking
        the empty tail costs 2 seconds a page for nothing.
        """
        return re.sub(r"([?&/])page(=|/)\d+", r"\1page\g<2>N", url)

    async def run(self) -> list:
        """Walk every search URL and return whatever parsed cleanly.

        A page that fails to fetch or parse is logged and skipped: one broken
        page must never cost us the rest of the source, and one broken source
        must never cost us the run.
        """
        out: list = []
        seen: set[str] = set()
        exhausted: set[str] = set()
        fetched = 0

        for url in self.search_urls():
            key = self.series_key(url)
            if key in exhausted:
                continue

            html = await self.fetch_page(url)
            fetched += 1
            if not html:
                exhausted.add(key)
                continue
            try:
                items = self.parse_page(html, url)
            except Exception as exc:  # noqa: BLE001 - a site redesign is not fatal
                log.exception("parse failed for %s (%s): %s", url, self.name, exc)
                continue

            added = 0
            for item in items:
                if item.doc_id in seen:
                    continue
                seen.add(item.doc_id)
                out.append(item)
                added += 1
            if added == 0:
                exhausted.add(key)

        log.info("%s: %d listings from %d fetched pages", self.name, len(out), fetched)
        return out


class JpAdapter(Adapter):
    side = "jp"

    def finish(self, listing: JpListing) -> JpListing:
        """Resolve the watchlist key and normalise before the listing escapes."""
        listing.source = self.name
        if not listing.watchlist_key:
            listing.watchlist_key = resolve_watchlist_key(
                self.cfg,
                make=listing.make,
                model=listing.model,
                model_code=listing.model_code,
                variant=listing.variant,
                description=listing.description,
            )
        return listing


class ChAdapter(Adapter):
    side = "ch"

    def finish(self, listing: ChListing) -> ChListing:
        listing.source = self.name
        if not listing.watchlist_key:
            listing.watchlist_key = resolve_watchlist_key(
                self.cfg,
                make=listing.make,
                model=listing.model,
                variant=listing.variant,
            )
        return listing
