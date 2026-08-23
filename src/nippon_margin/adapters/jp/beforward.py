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

#: `<a href="/stocklist/make=106/model=1108/...">MERCEDES-BENZ G-Class</a>`
_MODEL_LINK = re.compile(
    r'href="/stocklist/make=(\d+)/model=(\d+)[^"]*"[^>]*>\s*([A-Za-z0-9\-. ]{2,40})'
)


class BeForwardAdapter(JpAdapter):
    name = "beforward"
    base_url = "https://www.beforward.jp"

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._make_ids: dict[str, str] = {}
        self._targets: list[tuple[str, str]] = []

    async def run(self) -> list[JpListing]:
        # Two levels of ids have to be resolved before the base class walks
        # search_urls(). Filtering by make alone returns the whole make
        # newest-first, which for Mercedes means C-Classes and Actros trucks:
        # a watchlist model never surfaces on the first pages, so the source
        # contributed 50 listings and 0 matches.
        index = await self.fetch_page(f"{self.base_url}/stocklist/")
        if index:
            self._make_ids = {
                name.strip().upper(): make_id for make_id, name in _MAKE_LINK.findall(index)
            }
            log.info("beforward: resolved %d make ids", len(self._make_ids))

        await self._resolve_models()
        return await super().run()

    async def _resolve_models(self) -> None:
        """Map each watched make+model onto beforward's numeric model id."""
        # Keyed by make this was a dict, which silently kept only the last
        # entry per manufacturer: four Mercedes watch items collapsed to one,
        # so G-Class and SL were never looked up at all.
        for name, make_id in self._make_ids.items():
            items = [w for w in self.cfg.watchlist if _same_make(name, w.make)]
            if not items:
                continue
            page = await self.fetch_page(
                f"{self.base_url}/stocklist/make={make_id}/{STEERING_FILTER}/sortkey=n/"
            )
            if not page:
                continue
            for found_make, model_id, label in _MODEL_LINK.findall(page):
                if found_make != make_id:
                    continue
                # "MERCEDES-BENZ G-Class" -> "G-Class"
                model_name = re.sub(r"^[A-Z\-]+\s+", "", label.strip(), count=1)
                for item in items:
                    if _model_matches(model_name, item):
                        self._targets.append((make_id, model_id))
                        break
        log.info("beforward: resolved %d make/model targets", len(self._targets))

    def search_urls(self) -> Iterable[str]:
        if not self._targets:
            # Ids unresolved (first run, or the index changed shape): fall back
            # to the unfiltered list rather than scraping nothing.
            yield f"{self.base_url}/stocklist/{STEERING_FILTER}/sortkey=n/"
            return
        for make_id, model_id in sorted(set(self._targets)):
            for page in range(1, self.source_cfg.max_pages + 1):
                suffix = "" if page == 1 else f"page={page}/"
                yield (
                    f"{self.base_url}/stocklist/make={make_id}/model={model_id}"
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

        # BE FORWARD prints the country in the spec strip ("Location Korea").
        # Most of its stock for these models is NOT in Japan -- 29 of 31 cards
        # on the 8-Series page sit in Korea -- so leaving this uncaptured let
        # Korean cars through the origin filter as "location unknown".
        location = specs.get("location")

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
            location=location,
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


def _model_matches(site_model: str, item) -> bool:
    """Does beforward's model label name the watched model?

    Their labels are broad families ("G-Class", "911"), so this compares
    against the watchlist model and its aliases rather than a trim.
    """
    from ...matching import contains_phrase

    for candidate in (item.model, *item.aliases):
        if not candidate:
            continue
        if contains_phrase(site_model, candidate) or contains_phrase(candidate, site_model):
            return True
    return False


def _same_make(site_name: str, wanted: str) -> bool:
    norm = lambda s: re.sub(r"[^a-z]", "", s.lower())  # noqa: E731
    a, b = norm(site_name), norm(wanted)
    return bool(a) and bool(b) and (a == b or a.startswith(b) or b.startswith(a))


def _titlecase(text: str) -> str:
    return " ".join(w.capitalize() if w.isupper() and len(w) > 2 else w for w in text.split())
