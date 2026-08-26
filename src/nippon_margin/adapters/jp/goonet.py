"""goo-net-exchange.com -- the export arm of Japan's largest used-car portal.

Two things make this the strongest Japanese source we have:

* **It names the trim.** Every card's `h3.title` carries the grade as the
  dealer registered it -- `911 CARRERA 4S`, `911 TURBO S`, `911 CARRERA 4 GTS`.
  Everywhere else the title is a bare `PORSCHE 911` and the trim has to be
  inferred from the katashiki, which only covers the cars that publish one.
  A Carrera and a GT3 are a CHF 70k different car, so this is not cosmetic.
* **It prices in FOB, explicitly, in both currencies.** The card carries
  `Car Price (FOB)` next to a hidden input holding
  `{"USD": "$66,875", "JPY": "¥10,575,000"}`, so there is no repeat of
  the SBT mistake of reading a bundled "total price" as a shipping quote.

Also on every card: prefecture ("Aichi Japan"), registration year and month,
mileage, displacement, steering side and transmission. Roughly half the stock
is LHD -- Japan imports a lot of left-hand-drive European metal -- so the
listings survive the RHD filter far better than a domestic portal's would.

Paging is a path, not a query: `index.html`, `index-2.html`, `index-3.html`.
The query-string filters the site's own JS builds are POST-only and return
500 to a GET, so steering is filtered downstream rather than at the source.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

from selectolax.parser import Node

from ...models import JpListing, PriceTerms
from ...parse import (
    parse_engine_cc,
    parse_mileage,
    parse_month,
    parse_price,
    parse_steering,
    parse_transmission,
    parse_year,
    text_of,
)
from ..base import JpAdapter

#: `/usedcars/PORSCHE/911/700020009220260801001/`
_DETAIL_HREF = re.compile(r"^/usedcars/[A-Z_0-9]+/[^/]+/(\d{6,})/$")


class GooNetAdapter(JpAdapter):
    name = "goonet"
    base_url = "https://www.goo-net-exchange.com"

    def search_urls(self) -> Iterable[str]:
        # The 911 tiers all share PORSCHE/911, so the same path arrives once
        # per watch entry. Fetching it five times would be five times the
        # requests for one page of cars.
        seen: set[str] = set()
        for item in self.cfg.watchlist:
            path = item.goonet_path
            if not path:
                continue
            for page in range(1, self.source_cfg.max_pages + 1):
                leaf = "index.html" if page == 1 else f"index-{page}.html"
                url = f"{self.base_url}/usedcars/{path.strip('/')}/{leaf}"
                if url not in seen:
                    seen.add(url)
                    yield url

    def parse_page(self, html: str, url: str) -> list[JpListing]:
        out: list[JpListing] = []
        seen: set[str] = set()
        for anchor in self.dom(html).css("a[href]"):
            m = _DETAIL_HREF.match(anchor.attributes.get("href") or "")
            if not m:
                continue
            ref = m.group(1)
            if ref in seen:
                continue
            listing = self._parse_card(anchor, ref)
            if listing:
                seen.add(ref)
                out.append(self.finish(listing))
        return out

    def _parse_card(self, card: Node, ref: str) -> JpListing | None:
        heading = card.css_first("h3.title")
        if not heading:
            return None
        # "PORSCHE 911\n911 CARRERA 4S" -> make, model, grade on separate lines.
        lines = [ln.strip() for ln in heading.text().splitlines() if ln.strip()]
        if not lines:
            return None
        make, model = _split_make_model(lines[0])
        if not make or not model:
            return None
        # The grade line repeats the model ("911 CARRERA 4S" for a 911), so
        # strip it back to the part that is actually the trim.
        variant = _variant(model, lines[1]) if len(lines) > 1 else None

        price_usd = _usd(card, "currency-price")
        details = [text_of(li.text()) for li in card.css("ul.details li")]
        detail_blob = " ".join(details)
        # `2019.01` -- registration year and month, always the first cell.
        stamp = details[0] if details else ""

        location = card.css_first("p.location")

        listing = JpListing(
            source=self.name,
            source_ref=ref,
            make=make,
            model=model,
            variant=variant,
            year=parse_year(stamp),
            reg_month=parse_month(stamp.replace(".", "/")),
            mileage_km=_first(parse_mileage, details, "km"),
            engine_cc=_first(parse_engine_cc, details, "cc"),
            transmission=parse_transmission(detail_blob),
            steering=parse_steering(detail_blob),
            price_usd=price_usd,
            # The caption on the figure we read is literally "Car Price (FOB)".
            price_terms=PriceTerms.FOB if price_usd else PriceTerms.UNKNOWN,
            location=text_of(location.text()) if location else None,
            description=" | ".join([*lines, detail_blob]),
            image_urls=_images(card),
            url=self.absolute(card.attributes.get("href")),
        )
        return listing


def _usd(card: Node, field: str) -> float | None:
    """The USD side of `{"USD": "$66,875", "JPY": "¥10,575,000"}`.

    Yen is the site's native currency and the USD figure is its own
    conversion, but the rest of the engine speaks USD FOB, so we take theirs
    rather than re-converting at a different rate than the buyer will see.
    """
    node = card.css_first(f'input[name="{field}"]')
    if not node:
        return None
    try:
        payload = json.loads(node.attributes.get("value") or "")
    except (ValueError, TypeError):
        return None
    return parse_price(payload.get("USD")) if isinstance(payload, dict) else None


def _first(fn, values: list[str], marker: str) -> int | None:
    """Run `fn` over the first detail cell mentioning `marker`.

    The cells are unlabelled and their order is not contractual, so they are
    recognised by shape ("47,000 km", "3000cc") the way the SBT strip is.
    """
    for value in values:
        if marker in value.lower():
            got = fn(value)
            if got is not None:
                return got
    return None


def _images(card: Node) -> list[str]:
    out = []
    for img in card.css("img"):
        src = img.attributes.get("data-src") or img.attributes.get("src") or ""
        if src.startswith("http") and "nophoto" not in src and "/common/" not in src:
            out.append(src)
    return out[:3]


_MAKE_WORDS = {
    "MERCEDES": "Mercedes-Benz",
    "MERCEDES_BENZ": "Mercedes-Benz",
    "ALFA": "Alfa Romeo",
    "ALFA_ROMEO": "Alfa Romeo",
}


def _pretty(shouted: str) -> str:
    """`911 CARRERA 4 GTS` -> `911 Carrera 4 GTS`.

    Goo-net shouts every title. Plain `.title()` would render `GTS` as `Gts`
    and `RS` as `Rs`, which is how a trim ends up looking invented in the
    digest. Short words on a Porsche badge are acronyms -- GTS, GT3, RS, 4S,
    S, T -- so only words long enough to be real words get title-cased.
    """
    return " ".join(w.title() if len(w) > 3 else w.upper() for w in shouted.split())


def _split_make_model(line: str) -> tuple[str, str]:
    """`PORSCHE 911` -> `("Porsche", "911")`, `ALFA ROMEO GIULIA` -> two words."""
    words = line.split()
    if not words:
        return "", ""
    for span in (2, 1):
        head = "_".join(w.upper() for w in words[:span])
        if head in _MAKE_WORDS and len(words) > span:
            return _MAKE_WORDS[head], _pretty(" ".join(words[span:]))
    return words[0].title(), _pretty(" ".join(words[1:]))


def _variant(model: str, grade_line: str) -> str | None:
    """`911 CARRERA 4S` against model `911` -> `Carrera 4S`.

    Goo-net repeats the model at the head of the grade, and a variant that
    still contains the model name confuses the trim matcher (`911 Carrera`
    is not a trim; `Carrera` is).
    """
    words = grade_line.split()
    model_words = model.upper().split()
    while words and model_words and words[0].upper() == model_words[0]:
        words.pop(0)
        model_words.pop(0)
    return _pretty(" ".join(words)) or None
