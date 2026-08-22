"""sbtjapan.com -- LHD filter, and both FOB and total (C&F) prices.

SBT is the one Japanese source that publishes the *total* price next to the
vehicle price, which is the number that actually matters: it tells you what
the exporter's freight assumption is, so we can stop guessing for this
listing and use their C&F figure instead of our RoRo estimate.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from urllib.parse import quote_plus

from ...models import JpListing, PriceTerms, Steering
from ...parse import (
    parse_engine_cc,
    parse_grade,
    parse_mileage,
    parse_month,
    parse_price,
    parse_steering,
    parse_transmission,
    parse_year,
    split_make_model,
    text_of,
)
from ..base import JpAdapter

# The spec strip renders as bare `<li>`/`<span>` values with no labels:
#   92AM5502 | 82,000km | 3,600cc | AT | 4WD | RHD | PETROL | WHITE
log = logging.getLogger(__name__)

# <option value="52" > PORSCHE (1266) </option>
_MAKE_OPTION = re.compile(r'<option\s+value="(\d+)"[^>]*>\s*([A-Z][A-Z\-. ]{1,28}?)\s*\(')

_MODEL_CODE = re.compile(r"^[A-Z0-9]{4,14}$")
_FUELS = {"PETROL", "DIESEL", "HYBRID", "ELECTRIC", "LPG", "GASOLINE"}
_DRIVE = {"2WD", "4WD", "AWD", "FF", "FR", "RWD"}


class SbtJapanAdapter(JpAdapter):
    name = "sbtjapan"
    base_url = "https://www.sbtjapan.com"

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._make_ids: dict[str, str] = {}

    async def run(self) -> list[JpListing]:
        # SBT's search ignores a free-text `keyword`; it filters on numeric
        # `make_id` taken from the maker <select>. Scrape that map once per
        # run rather than hardcoding ids that will drift.
        index = await self.fetch_page(f"{self.base_url}/used-cars/")
        if index:
            self._make_ids = {
                name.strip().upper(): make_id
                for make_id, name in _MAKE_OPTION.findall(index)
            }
            log.info("sbtjapan: resolved %d make ids", len(self._make_ids))
        return await super().run()

    def search_urls(self) -> Iterable[str]:
        seen: set[str] = set()
        for item in self.cfg.watchlist:
            make_id = self._make_id_for(item.make)
            if not make_id:
                continue
            model = quote_plus(item.model.upper())
            for page in range(1, self.source_cfg.max_pages + 1):
                # `isModel=1` narrows to the model; without it SBT returns the
                # whole make, which for Mercedes is thousands of vans.
                url = (
                    f"{self.base_url}/used-cars/search"
                    f"?make_id={make_id}&model%5B%5D={model}&isModel=1"
                    f"&steering=LHD&page={page}"
                )
                if url not in seen:
                    seen.add(url)
                    yield url
            # Model names differ between SBT and the watchlist often enough
            # that a make-level page is worth one request as a fallback.
            fallback = f"{self.base_url}/used-cars/search?make_id={make_id}&steering=LHD&page=1"
            if fallback not in seen:
                seen.add(fallback)
                yield fallback

    def _make_id_for(self, make: str) -> str | None:
        wanted = re.sub(r"[^a-z]", "", make.lower())
        for name, make_id in self._make_ids.items():
            normalised = re.sub(r"[^a-z]", "", name.lower())
            if normalised == wanted or normalised.startswith(wanted) or wanted.startswith(normalised):
                return make_id
        return None

    def parse_page(self, html: str, url: str) -> list[JpListing]:
        out: list[JpListing] = []
        for card in self.dom(html).css(".card-product"):
            listing = self._parse_card(card)
            if listing:
                out.append(self.finish(listing))
        return out

    def _parse_card(self, card) -> JpListing | None:
        link = card.css_first("a.card-product__wrap") or card.css_first("a[href*='/used-cars/']")
        href = link.attributes.get("href") if link else None
        if not href:
            return None
        ref = href.rstrip("/").split("/")[-1]
        if not ref:
            return None

        heading = card.css_first("h2") or card.css_first(".card-product__product")
        title = text_of(heading.text()) if heading else ""
        if not title:
            return None

        # "2012/3 PORSCHE CAYENNE BASE GRADE"
        year = parse_year(title)
        month = parse_month(title)
        name = re.sub(r"^\s*(19|20)\d{2}\s*/\s*\d{1,2}\s*", "", title).strip()
        make, model = split_make_model(_titlecase(name))

        vehicle_price = _price_in(card, "card-product__vehicle-price")
        total_price = _price_in(card, "card-product__total-price")
        # Prefer the exporter's own C&F number over our freight assumption.
        if total_price:
            price, terms = total_price, PriceTerms.CF
        else:
            price, terms = vehicle_price, PriceTerms.FOB

        specs = _spec_values(card)
        return JpListing(
            source=self.name,
            source_ref=ref,
            make=make,
            model=model,
            model_code=specs.get("model_code"),
            year=year,
            reg_month=month,
            mileage_km=specs.get("mileage_km"),
            transmission=specs.get("transmission"),
            steering=specs.get("steering", Steering.UNKNOWN),
            fuel=specs.get("fuel"),
            engine_cc=specs.get("engine_cc"),
            color=specs.get("color"),
            price_usd=price,
            price_terms=terms,
            auction_grade=specs.get("grade"),
            description=title + " | " + " ".join(specs.get("raw", [])),
            image_urls=_images(card),
            url=self.absolute(href),
        )


def _price_in(card, class_name: str) -> float | None:
    node = card.css_first(f".{class_name}")
    return parse_price(text_of(node.text())) if node else None


def _images(card) -> list[str]:
    return [
        img.attributes["src"]
        for img in card.css("img")
        if img.attributes.get("src", "").startswith("http")
    ][:3]


def _spec_values(card) -> dict:
    """Read the unlabelled spec strip by recognising the value shapes."""
    values = [
        text_of(node.text())
        for node in card.css("li, span, td")
        if text_of(node.text())
    ]
    out: dict = {"raw": values[:20]}
    for value in values:
        upper = value.upper()
        if "KM" in upper and out.get("mileage_km") is None:
            out["mileage_km"] = parse_mileage(value)
        elif "CC" in upper and out.get("engine_cc") is None:
            out["engine_cc"] = parse_engine_cc(value)
        elif upper in {"AT", "MT", "AMT", "CVT"} and out.get("transmission") is None:
            out["transmission"] = parse_transmission(value)
        elif upper in {"LHD", "RHD"} and out.get("steering") is None:
            out["steering"] = parse_steering(value)
        elif upper in _FUELS and out.get("fuel") is None:
            out["fuel"] = upper.title()
        elif upper in _DRIVE:
            continue
        elif upper.startswith("GRADE") and out.get("grade") is None:
            out["grade"] = parse_grade(value)
        elif (
            out.get("model_code") is None
            and _MODEL_CODE.match(upper)
            and any(ch.isdigit() for ch in upper)
            and any(ch.isalpha() for ch in upper)
        ):
            out["model_code"] = upper
    return out


def _titlecase(text: str) -> str:
    """`PORSCHE CAYENNE BASE GRADE` -> `Porsche Cayenne Base Grade`."""
    return " ".join(w.capitalize() if w.isupper() and len(w) > 3 else w for w in text.split())
