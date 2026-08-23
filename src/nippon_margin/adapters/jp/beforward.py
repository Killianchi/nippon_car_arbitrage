"""beforward.jp -- stock list per make/model.

Two quirks worth knowing:

  * the stock list only answers on paths with a trailing slash and uses
    *numeric* make ids (`/stocklist/make=106/sortkey=n/`), so we scrape the
    make -> id table off the index page once per run rather than hardcoding
    ids that will drift;
  * prices are shown discounted, with the pre-discount figure alongside. The
    discounted figure is the one you can actually pay.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from ...models import JpListing, PriceTerms, Steering
from ...parse import (
    parse_engine_cc,
    parse_mileage,
    parse_month,
    parse_price,
    parse_transmission,
    parse_year,
    text_of,
)
from ..base import JpAdapter

log = logging.getLogger(__name__)

#: BE FORWARD's own filter links spell it exactly this way -- `steering=LHD`
#: and `steering=1` both 404. It is the one JP source that does not filter to
#: left-hand drive by default, so without this the catalog silently takes in
#: RHD stock that is legal to import but hard to resell in Switzerland.
STEERING_FILTER = "steering=Left"

_DETAIL = re.compile(r"^/[a-z0-9\-]+/[a-z0-9\-]+/([a-z0-9]+)/id/(\d+)/?$", re.I)
_MAKE_LINK = re.compile(r'href="/stocklist/make=(\d+)/sortkey=n"[^>]*>\s*([A-Z][A-Z\- ]{1,24})')


class BeForwardAdapter(JpAdapter):
    name = "beforward"
    base_url = "https://www.beforward.jp"

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._make_ids: dict[str, str] = {}

    async def run(self) -> list[JpListing]:
        # Resolve make ids before the base class walks search_urls().
        index = await self.fetch_page(f"{self.base_url}/stocklist/")
        if index:
            self._make_ids = {
                name.strip().upper(): make_id for make_id, name in _MAKE_LINK.findall(index)
            }
            log.info("beforward: resolved %d make ids", len(self._make_ids))
        return await super().run()

    def search_urls(self) -> Iterable[str]:
        wanted = {w.make.upper() for w in self.cfg.watchlist}
        ids = {
            make_id
            for name, make_id in self._make_ids.items()
            if any(_same_make(name, w) for w in wanted)
        }
        if not ids:
            # Make ids unavailable (first run, or the index changed shape):
            # fall back to the make-less list rather than scraping nothing.
            yield f"{self.base_url}/stocklist/{STEERING_FILTER}/sortkey=n/"
            return
        for make_id in sorted(ids):
            for page in range(1, self.source_cfg.max_pages + 1):
                suffix = "" if page == 1 else f"page={page}/"
                yield (
                    f"{self.base_url}/stocklist/make={make_id}"
                    f"/{STEERING_FILTER}/sortkey=n/{suffix}"
                )

    def parse_page(self, html: str, url: str) -> list[JpListing]:
        out: list[JpListing] = []
        seen: set[str] = set()
        for row in self.dom(html).css("tr.stocklist-row, div.stocklist-row, .car-detail"):
            listing = self._parse_row(row)
            if listing and listing.source_ref not in seen:
                seen.add(listing.source_ref)
                out.append(self.finish(listing))
        return out

    def _parse_row(self, row) -> JpListing | None:
        href = ref = None
        for link in row.css("a[href]"):
            candidate = link.attributes.get("href") or ""
            m = _DETAIL.match(candidate)
            if m:
                href, ref = candidate, m.group(2)
                break
        if not href or not ref:
            return None

        title_node = row.css_first(".make-model") or row.css_first(".title")
        title = text_of(title_node.text()) if title_node else ""
        if not title:
            return None

        # "2009 HINO DUTRO"
        year = parse_year(title)
        name = re.sub(r"\b(19|20)\d{2}\b", "", title).strip()
        parts = name.split(None, 1)
        make = _titlecase(parts[0]) if parts else ""
        model = _titlecase(parts[1]) if len(parts) > 1 else ""

        # The discounted price is what you pay; `original-vehicle-price` is the
        # struck-through figure and must not win.
        price = None
        price_node = row.css_first(".price-area .price") or row.css_first(".price")
        if price_node:
            price = parse_price(text_of(price_node.text()))

        specs = _basic_spec(row)
        stock_code = _DETAIL.match(href).group(1).upper()

        return JpListing(
            source=self.name,
            source_ref=ref,
            make=make,
            model=model,
            year=parse_year(specs.get("year")) or year,
            reg_month=parse_month(specs.get("year")),
            mileage_km=parse_mileage(specs.get("mileage")),
            transmission=parse_transmission(specs.get("trans.") or specs.get("trans")),
            engine_cc=parse_engine_cc(specs.get("engine")),
            price_usd=price,
            price_terms=PriceTerms.FOB,
            # The stock list is filtered to left-hand drive by the URL above.
            steering=Steering.LHD,
            chassis_no=stock_code,
            description=f"{title} | ref {stock_code} | "
                        + " ".join(f"{k}: {v}" for k, v in specs.items()),
            image_urls=[
                img.attributes["src"]
                for img in row.css("img")
                if img.attributes.get("src", "").startswith(("http", "//"))
            ][:3],
            url=self.absolute(href),
        )


def _basic_spec(row) -> dict[str, str]:
    """`.basic-spec-col` cells render as a `Mileage / 93,000 km` label+value pair."""
    specs: dict[str, str] = {}
    for cell in row.css(".basic-spec-col"):
        label_node = cell.css_first(".title")
        value_node = cell.css_first(".val")
        if not (label_node and value_node):
            continue
        key = text_of(label_node.text()).rstrip(":").lower()
        value = text_of(value_node.text())
        if key and value:
            specs[key] = value
    return specs


def _same_make(site_name: str, wanted: str) -> bool:
    norm = lambda s: re.sub(r"[^a-z]", "", s.lower())  # noqa: E731
    a, b = norm(site_name), norm(wanted)
    return bool(a) and bool(b) and (a == b or a.startswith(b) or b.startswith(a))


def _titlecase(text: str) -> str:
    return " ".join(w.capitalize() if w.isupper() and len(w) > 2 else w for w in text.split())
