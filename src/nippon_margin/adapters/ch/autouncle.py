"""AutoUncle.ch -- the Swiss sell-side workhorse.

AutoUncle aggregates AutoScout24, Autolina and comparis into one index and,
crucially, publishes two things the underlying portals hide: **days listed**
and **price-change history**. Those are the liquidity signal -- an asking
price nobody has met for 180 days is not a market price.

AutoScout24 itself is deliberately not scraped: their ToS forbids it. If you
obtain official partner/API access, add an adapter that uses it and this one
becomes a cross-check rather than the primary source.

Parsing strategy: AutoUncle ships hashed CSS class names that rotate on every
deploy, so selecting on them would break weekly. We anchor on the stable
`a[href^="/en/d/"]` detail link, walk up to the enclosing card, and read the
card by its *labels* ("Days listed", "CHF", "km") rather than its classes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from selectolax.parser import Node

from ...models import ChListing, PricePoint, SellerType
from ...parse import parse_mileage, parse_price, parse_year, text_of, to_int
from ..base import ChAdapter

_DETAIL_HREF = re.compile(r"^/[a-z]{2}/d/\d+")
_CHF = re.compile(r"CHF\s*([\d'’ .,]+)")
# "8807 Freienbach, Schwyz" -> canton "Schwyz". Anchored: see _location_map.
_CANTON = re.compile(r"^\d{4}\s+[^,]{2,30},\s*([A-Za-zÀ-ÿ.\- ]{2,30})$")


class AutoUncleAdapter(ChAdapter):
    name = "autouncle"
    base_url = "https://www.autouncle.ch"

    def search_urls(self) -> Iterable[str]:
        for item in self.cfg.watchlist:
            make = _slug(item.make)
            model = item.ch_model_slug or _slug(item.model)
            if not (make and model):
                continue
            for page in range(1, self.source_cfg.max_pages + 1):
                suffix = "" if page == 1 else f"?page={page}"
                yield f"{self.base_url}/en/used-cars/{make}/{model}{suffix}"

    def parse_page(self, html: str, url: str) -> list[ChListing]:
        out: list[ChListing] = []
        seen: set[str] = set()
        dom = self.dom(html)
        # Seller location lives in a separate strip from the price card; the
        # outgoing-link href is what ties the two together.
        locations = _location_map(dom)
        for anchor in dom.css("a[href]"):
            href = anchor.attributes.get("href") or ""
            if not _DETAIL_HREF.match(href):
                continue
            ref = _ref_from_href(href)
            if not ref or ref in seen:
                continue
            card = _card_for(anchor)
            listing = self._parse_card(card, anchor, href, ref)
            if listing:
                place = locations.get(ref)
                if place:
                    listing.canton = place.canton
                    listing.seller_type = place.seller_type
                seen.add(ref)
                out.append(self.finish(listing))
        return out

    def _parse_card(self, card: Node, anchor: Node, href: str, ref: str) -> ChListing | None:
        heading = anchor.css_first("h3") or card.css_first("h3")
        title = text_of(heading.text()) if heading else ""
        if not title:
            return None

        # "Used (2012) Porsche 911 Carrera S Cabriolet 400 HP | Good price"
        make, model, variant, year = _split_title(title)

        card_text = text_of(card.text())

        # The card carries several CHF figures: the asking price, the previous
        # price when it has been cut, and a "Below market CHF 3'700" savings
        # badge. The badge lives inside a <button>; taking the cheapest figure
        # would silently price a 911 at CHF 3,700, so read them positionally
        # from the non-button nodes instead.
        prices = _price_figures(card)
        price_chf = prices[0] if prices else None
        previous_chf = prices[1] if len(prices) > 1 and prices[1] > prices[0] else None

        mileage = None
        for li in card.css("li"):
            li_text = text_of(li.text())
            if re.search(r"\bkm\b", li_text, re.I):
                mileage = parse_mileage(li_text)
                if mileage:
                    break

        days_listed = _labelled_number(card_text, "Days listed")

        # AutoUncle only prints a second CHF figure when the price was cut, so
        # "no second figure" means "no cut", not "unknown". Recording a single
        # point for those cars keeps them in the denominator of the cut rate --
        # otherwise every model reads 100% cut and the liquidity score is
        # uniformly depressed by a measurement artefact.
        history: list[PricePoint] = []
        now = datetime.now(UTC)
        if price_chf:
            if previous_chf and previous_chf > price_chf:
                # AutoUncle shows the delta, not the date. Anchor the old price
                # to the start of the listing so the cut sits in the history.
                started = now - timedelta(days=days_listed or 30)
                history = [
                    PricePoint(at=started, price=previous_chf),
                    PricePoint(at=now, price=price_chf),
                ]
            else:
                history = [PricePoint(at=now, price=price_chf)]

        m = _CANTON.search(card_text)
        canton = m.group(1).strip() if m else None
        seller = SellerType.UNKNOWN

        return ChListing(
            source=self.name,
            source_ref=ref,
            make=make,
            model=model,
            variant=variant,
            year=year,
            mileage_km=mileage,
            price_chf=price_chf,
            days_listed=days_listed,
            price_change_history=history,
            seller_type=seller,
            canton=canton,
            ch_fahrzeug=True if re.search(r"CH[- ]Fahrzeug|Swiss delivery", card_text, re.I) else None,
            url=self.absolute(href),
        )


# --------------------------------------------------------------------------
def _card_for(anchor: Node, *, max_levels: int = 6, widen: int = 3) -> Node:
    """Climb to the smallest ancestor that holds the whole card.

    The price, days-listed and location live as *siblings* of the detail
    link, so the anchor alone is not enough. Once we have the price block we
    widen a little further to pick up the seller/location strip -- but only
    while the subtree still describes exactly one car.
    """
    node = anchor
    for _ in range(max_levels):
        parent = node.parent
        if parent is None:
            break
        node = parent
        text = node.text() or ""
        if "Days listed" in text and "CHF" in text:
            for _ in range(widen):
                nxt = node.parent
                if nxt is None or (nxt.text() or "").count("Days listed") != 1:
                    break
                node = nxt
            return node
    return node


_PRICE_ONLY = re.compile(r"^CHF\s*[\d'’ .,]+$")


def _price_figures(card: Node) -> list[float]:
    """Asking-price figures in DOM order, excluding badges inside buttons.

    First is the current ask; a larger second one is the pre-cut price.
    """
    out: list[float] = []
    for node in card.css("div,span,p"):
        text = text_of(node.text())
        if not text or not _PRICE_ONLY.match(text):
            continue
        if _inside_button(node, card):
            continue
        value = parse_price(text)
        if value and value > 500 and value not in out:
            out.append(value)
    return out


def _inside_button(node: Node, stop: Node) -> bool:
    parent = node.parent
    while parent is not None and parent is not stop:
        if parent.tag == "button":
            return True
        parent = parent.parent
    return False


def _labelled_number(text: str, label: str) -> int | None:
    """The first integer following `label` in the card's flattened text."""
    m = re.search(re.escape(label) + r"\D{0,20}?(\d{1,5})", text)
    return to_int(m.group(1)) if m else None


def _ref_from_href(href: str) -> str | None:
    m = re.search(r"/d/(\d+)", href)
    return m.group(1) if m else None


_TITLE = re.compile(r"^(?:Used|New|Neu|Occasion)?\s*\((\d{4})\)\s*(.+)$", re.I)


def _split_title(title: str) -> tuple[str, str, str | None, int | None]:
    """`Used (2012) Porsche 911 Carrera S Cabriolet 400 HP | Good price`."""
    core = title.split("|")[0].strip()
    year = None
    m = _TITLE.match(core)
    if m:
        year = int(m.group(1))
        core = m.group(2).strip()
    else:
        year = parse_year(core)
        core = re.sub(r"\(?\b(19|20)\d{2}\b\)?", "", core).strip()

    core = re.sub(r"\b\d{2,4}\s*(HP|PS|kW)\b.*$", "", core, flags=re.I).strip()

    from ...parse import split_make_model

    make, rest = split_make_model(core)
    parts = rest.split(None, 1)
    model = parts[0] if parts else rest
    variant = parts[1].strip() if len(parts) > 1 else None
    return make, model, variant, year


@dataclass(frozen=True)
class _Place:
    canton: str | None
    seller_type: SellerType


def _location_map(dom) -> dict[str, _Place]:
    """`{listing_ref: place}` from the seller strip under each card.

    Each strip holds the originating portal ("Autoscout24"), the seller's
    `8807 Freienbach, Schwyz` line, and an outgoing link whose path carries
    the AutoUncle listing id -- which is what lets us join it to the card.
    """
    out: dict[str, _Place] = {}
    for link in dom.css('a[href*="/outgoing_link/"]'):
        href = link.attributes.get("href") or ""
        m = re.search(r"/outgoing_link/([a-z0-9\-]+)/(\d+)", href)
        if not m:
            continue
        portal, ref = m.group(1), m.group(2)
        node = link
        canton = None
        for _ in range(4):
            node = node.parent
            if node is None:
                break
            # Match per element, not on the concatenated subtree text: the
            # portal label runs straight into the postcode ("Autoscout248807
            # Freienbach, Schwyz") and swallows the digit boundary.
            for candidate in node.css("div,p,span"):
                found = _CANTON.match(text_of(candidate.text()))
                if found:
                    canton = found.group(1).strip()
                    break
            if canton:
                break
        seller = SellerType.DEALER if portal else SellerType.UNKNOWN
        if "private" in portal:
            seller = SellerType.PRIVATE
        out[ref] = _Place(canton=canton, seller_type=seller)
    return out


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
