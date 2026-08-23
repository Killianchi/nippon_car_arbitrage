"""Polite async fetching.

Every request goes through here so that the rate limit, the User-Agent, the
robots.txt check and the on-disk cache are impossible to forget in an adapter.

Rules enforced:
  * at most one request per `per_domain_delay_seconds` per domain, regardless
    of how many adapters are running concurrently;
  * robots.txt is fetched once per domain and honoured (configurable, but on
    by default);
  * every response body is cached to disk, so a parse failure can be
    re-debugged offline from the Actions artifact instead of by re-scraping.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import urllib.robotparser
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import HttpConfig

log = logging.getLogger(__name__)


@dataclass
class _DomainGate:
    """One lock + last-request timestamp per domain."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_request: float = 0.0


class Fetcher:
    """Throttled, cached, robots-aware HTTP client."""

    def __init__(self, cfg: HttpConfig, *, offline: bool = False):
        self.cfg = cfg
        self.offline = offline
        self.cache_dir = Path(cfg.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._gates: dict[str, _DomainGate] = defaultdict(_DomainGate)
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._robots_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self.stats = {"hits": 0, "misses": 0, "blocked": 0, "errors": 0}

    # -- lifecycle ----------------------------------------------------------
    async def __aenter__(self) -> Fetcher:
        # User-Agent only, deliberately. Several of these sites run a bot
        # challenge that fires on the *combination* of headers rather than the
        # UA: beforward.jp answers a 2 KB stub with HTTP 202 the moment an
        # explicit `Accept` header is present, and serves the real 795 KB
        # stock list when it is absent. Anything extra goes in `extra_headers`
        # per deployment rather than being baked in here.
        headers = {"User-Agent": self.cfg.user_agent}
        headers.update(getattr(self.cfg, "extra_headers", None) or {})
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=self.cfg.timeout_seconds,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # -- cache --------------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        host = urlparse(url).netloc or "unknown"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / host / f"{digest}.html"

    def _read_cache(self, url: str) -> str | None:
        path = self._cache_path(url)
        if not path.exists():
            return None
        age_h = (time.time() - path.stat().st_mtime) / 3600
        if not self.offline and age_h > self.cfg.cache_ttl_hours:
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def _write_cache(self, url: str, body: str) -> None:
        path = self._cache_path(url)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        except OSError as exc:  # a full disk must not fail the scrape
            log.warning("cache write failed for %s: %s", url, exc)

    # -- robots -------------------------------------------------------------
    async def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        async with self._robots_lock:
            if origin in self._robots:
                return self._robots[origin]
            parser: urllib.robotparser.RobotFileParser | None = None
            try:
                assert self._client is not None
                resp = await self._client.get(f"{origin}/robots.txt", timeout=15)
                if resp.status_code == 200:
                    parser = urllib.robotparser.RobotFileParser()
                    parser.parse(resp.text.splitlines())
            except Exception as exc:  # noqa: BLE001 - no robots.txt means no rules
                log.debug("robots.txt unavailable for %s: %s", origin, exc)
            self._robots[origin] = parser
            return parser

    async def allowed(self, url: str) -> bool:
        if not self.cfg.respect_robots_txt:
            return True
        parser = await self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self.cfg.user_agent, url)

    # -- fetching -----------------------------------------------------------
    async def _throttle(self, url: str) -> None:
        domain = urlparse(url).netloc
        gate = self._gates[domain]
        async with gate.lock:
            wait = self.cfg.per_domain_delay_seconds - (time.monotonic() - gate.last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            gate.last_request = time.monotonic()

    async def get(self, url: str, *, params: dict | None = None,
                  use_cache: bool = True) -> str | None:
        """Fetch a URL as text, or None if it is disallowed or unreachable."""
        full = str(httpx.URL(url, params=params)) if params else url

        if use_cache:
            cached = self._read_cache(full)
            if cached is not None:
                self.stats["hits"] += 1
                return cached

        if self.offline:
            log.info("offline: no cache entry for %s", full)
            return None

        if not await self.allowed(full):
            self.stats["blocked"] += 1
            log.warning("robots.txt disallows %s -- skipping", full)
            return None

        assert self._client is not None, "use Fetcher as an async context manager"

        last_exc: Exception | None = None
        for attempt in range(1, self.cfg.max_retries + 1):
            await self._throttle(full)
            try:
                resp = await self._client.get(full)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp
                    )
                if resp.status_code >= 400:
                    log.warning("HTTP %s for %s", resp.status_code, full)
                    self.stats["errors"] += 1
                    return None
                body = resp.text
                self.stats["misses"] += 1
                if use_cache:
                    self._write_cache(full, body)
                return body
            except Exception as exc:  # noqa: BLE001 - retried below
                last_exc = exc
                backoff = min(2 ** attempt, 30)
                log.warning("fetch %s failed (attempt %s/%s): %s",
                            full, attempt, self.cfg.max_retries, exc)
                if attempt < self.cfg.max_retries:
                    await asyncio.sleep(backoff)

        self.stats["errors"] += 1
        log.error("giving up on %s: %s", full, last_exc)
        return None

    async def get_json(self, url: str, *, params: dict | None = None) -> dict | list | None:
        import json

        body = await self.get(url, params=params, use_cache=False)
        if body is None:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            log.warning("bad JSON from %s: %s", url, exc)
            return None


async def render(url: str, cfg: HttpConfig, *, wait_selector: str | None = None,
                 wait_ms: int = 2500) -> str | None:
    """Fetch a JS-rendered page with headless Chromium.

    Playwright is an optional dependency: adapters that need it degrade to
    "source unavailable" rather than crashing the run when it is not
    installed (which is the normal state on a laptop).
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("playwright not installed -- cannot render %s", url)
        return None

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"]
            )
            try:
                page = await browser.new_page(
                    user_agent=cfg.user_agent,
                    viewport={"width": 1400, "height": 1000},
                    locale="en-US",
                )
                await page.goto(url, timeout=int(cfg.timeout_seconds * 1000),
                                wait_until="domcontentloaded")
                if wait_selector:
                    try:
                        await page.wait_for_selector(wait_selector, timeout=15_000)
                    except Exception:  # noqa: BLE001 - fall through to the plain wait
                        log.debug("selector %s never appeared on %s", wait_selector, url)
                await page.wait_for_timeout(wait_ms)
                return await page.content()
            finally:
                await browser.close()
    except Exception as exc:  # noqa: BLE001 - one dead renderer must not kill the run
        log.error("render failed for %s: %s", url, exc)
        return None


async def screenshot(url: str, out_path: Path, cfg: HttpConfig) -> bool:
    """Archive a full-page screenshot -- evidence for later negotiation."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return False
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"]
            )
            try:
                page = await browser.new_page(user_agent=cfg.user_agent,
                                              viewport={"width": 1400, "height": 1000})
                await page.goto(url, timeout=int(cfg.timeout_seconds * 1000),
                                wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                await page.screenshot(path=str(out_path), full_page=True)
                return True
            finally:
                await browser.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("screenshot failed for %s: %s", url, exc)
        return False
