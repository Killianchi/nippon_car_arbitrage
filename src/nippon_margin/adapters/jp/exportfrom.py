"""exportfrom.jp -- static HTML, LHD-only stock list.

Server-rendered Vuetify: each card is an `a.post-item` carrying the title,
the USD price chip and the mileage. The detail page adds a clean
label/value spec table (manufacturer, model, year, displacement,
transmission, steering, mileage), which we pull for anything on the
watchlist.
"""

from __future__ import annotations

import re
from typing import Iterable

from ...models import JpListing, PriceTerms, Steering
from ...parse import (
    parse_engine_cc,
    parse_mileage,
    parse_price,
    parse_transmission,
    parse_year,
    split_make_model,
    text_of,
)
from ..base import JpAdapter


class ExportFromAdapter(JpAdapter):
    name = "exportfrom"
    base_url = "https://exportfrom.jp"

    def search_urls(self) -> Iterable[str]:
        yield f"{self.base_url}/stock-list/left-hand-cars"
        for page in range(2, self.source_cfg.max_pages + 1):
            yield f"{self.base_url}/stock-list/left-hand-cars?page={page}"

    def parse_page(self, html: str, url: str) -> list[JpListing]:
        out: list[JpListing] = []
        for card in self.dom(html).css("a.post-item"):
            listing = self._parse_card(card)
            if listing:
                out.append(self.finish(listing))
        return out

    def _parse_card(self, card) -> JpListing | None:
        href = card.attributes.get("href")
        if not href or "/stock-list/" not in href:
            return None

        title = card.attributes.get("title") or ""
        heading = card.css_first("h2")
        if heading:
            title = text_of(heading.text()) or title
        if not title:
            return None

        make, model = split_make_model(title)

        # `/stock-list/left-hand-cars/12375-maserati-quattroporte-sport-gt-s`
        slug = href.rstrip("/").split("/")[-1]
        ref = slug.split("-", 1)[0] if slug[:1].isdigit() else slug

        # The price chip renders as `$ \n 36000`; the "New" chip has no digits.
        price_usd = None
        for chip in card.css(".v-chip__content"):
            chip_text = text_of(chip.text())
            if "$" in chip_text or re.fullmatch(r"[\d,.\s]{4,}", chip_text):
                price_usd = parse_price(chip_text)
                if price_usd:
                    break

        mileage = None
        for cap in card.css(".text-caption"):
            cap_text = text_of(cap.text())
            if "mileage" in cap_text.lower():
                mileage = parse_mileage(cap_text)
                break

        # Year is not on the card, but it is in the image alt text
        # ("... 2013 for sale from Japan") and the image filename.
        year = None
        img = card.css_first("img")
        if img:
            year = parse_year(img.attributes.get("alt") or "") or parse_year(
                img.attributes.get("src") or ""
            )
        images = [img.attributes["src"]] if img and img.attributes.get("src") else []

        return JpListing(
            source=self.name,
            source_ref=ref,
            make=make,
            model=model,
            year=year,
            mileage_km=mileage,
            steering=Steering.LHD,  # this stock list is LHD-only by construction
            price_usd=price_usd,
            price_terms=PriceTerms.FOB,
            description=title,
            image_urls=images,
            url=self.absolute(href),
        )

    # -- detail enrichment --------------------------------------------------
    async def enrich(self, listing: JpListing) -> JpListing:
        """Pull the spec table from the detail page.

        Only called for listings that resolved onto the watchlist, so we do not
        spend 2 seconds per car on stock we will never buy.
        """
        html = await self.fetch_page(listing.url)
        if not html:
            return listing

        specs = _spec_table(html)
        listing.make = specs.get("manufacturer") or listing.make
        listing.model = specs.get("model") or listing.model
        listing.year = parse_year(specs.get("year")) or listing.year
        listing.mileage_km = parse_mileage(specs.get("mileage")) or listing.mileage_km
        listing.engine_cc = parse_engine_cc(specs.get("displacement")) or listing.engine_cc
        listing.transmission = parse_transmission(specs.get("transmission")) or listing.transmission
        listing.color = specs.get("colour") or specs.get("color") or listing.color
        listing.fuel = specs.get("fuel") or listing.fuel

        body = text_of(html)
        if "no accident" in body.lower():
            listing.repair_history = False
        listing.description = f"{listing.description} | {' '.join(f'{k}: {v}' for k, v in specs.items())}"[:2000]
        return listing


_ROW = re.compile(
    r"<t[hd][^>]*>\s*([^<]{2,40}?)\s*:?\s*</t[hd]>\s*<t[hd][^>]*>\s*([^<]{1,80}?)\s*</t[hd]>",
    re.I | re.S,
)


def _spec_table(html: str) -> dict[str, str]:
    """Label/value pairs from any two-column `<tr><th>..</th><td>..</td></tr>` table."""
    specs: dict[str, str] = {}
    for label, value in _ROW.findall(html):
        key = text_of(label).rstrip(":").strip().lower()
        val = text_of(value)
        if key and val and key not in specs:
            specs[key] = val
    return specs
