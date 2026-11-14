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

from selectolax.lexbor import LexborNode as Node

from ...models import ChListing, PricePoint, SellerType
from ...parse import parse_mileage, parse_price, parse_year, text_of, to_int
from ..base import ChAdapter

_DETAIL_HREF = re.compile(r"^/[a-z]{2}/d/\d+")
# `/en/d/7131239-used-2008-audi-r8-coupe-advanced-420-bhp` -- the slug carries
# the registration year and the power, per listing, and cannot be crossed with
# a neighbouring card the way a DOM walk can. It is the most reliable thing on
# the page, so it is read first and the card only fills the gaps.
_DETAIL_SLUG = re.compile(
    r"^/[a-z]{2}/d/(\d+)-(used|new)-((?:19|20)\d{2})-(.+?)(?:-(\d{2,4})-bhp)?/?$", re.I
)
_CHF = re.compile(r"CHF\s*([\d'’ .,]+)")
# "8807 Freienbach, Schwyz" -> canton "Schwyz". Anchored: see _location_map.
_CANTON = re.compile(r"^\d{4}\s+[^,]{2,30},\s*([A-Za-zÀ-ÿ.\- ]{2,30})$")


class AutoUncleAdapter(ChAdapter):
    name = "autouncle"
    base_url = "https://www.autouncle.ch"

    def search_urls(self) -> Iterable[str]:
        # Several watch entries can share one AutoUncle page: the 911 tiers all
        # live under /porsche/911 and are told apart by variant when parsed.
        # Yielding the page once per entry would fetch it five times.
        seen: set[str] = set()
        for item in self.cfg.watchlist:
            make = _slug(item.make)
            model = item.ch_model_slug or _slug(item.model)
            if not (make and model):
                continue
            for page in range(1, self.source_cfg.max_pages + 1):
                suffix = "" if page == 1 else f"?page={page}"
                url = f"{self.base_url}/en/used-cars/{make}/{model}{suffix}"
                if url not in seen:
                    seen.add(url)
                    yield url

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

        # "Porsche 911 Carrera 385 HP | Good price" -- the year used to sit in
        # the heading as "Used (2021)" and no longer does, so it comes from the
        # URL slug, which carries it per listing.
        make, model, variant, year = _split_title(title)
        slug = _DETAIL_SLUG.match(href)
        slug_year = int(slug.group(3)) if slug else None
        slug_hp = slug.group(5) if slug else None
        year = year or slug_year

        card_text = text_of(card.text())
        specs = _leaf_texts(card)
        subtitle = _subtitle(card)

        # The card carries several CHF figures: the asking price, the previous
        # price when it has been cut, and a "Below market CHF 3'700" savings
        # badge. The badge lives inside a <button>; taking the cheapest figure
        # would silently price a 911 at CHF 3,700, so read them positionally
        # from the non-button nodes instead.
        prices = _price_figures(card)
        price_chf = prices[0] if prices else None
        previous_chf = prices[1] if len(prices) > 1 and prices[1] > prices[0] else None

        mileage = _mileage_from(specs)
        if mileage is None:
            for li in card.css("li"):
                li_text = text_of(li.text())
                if re.search(r"\bkm\b", li_text, re.I):
                    mileage = parse_mileage(li_text)
                    if mileage:
                        break

        days_listed = _definition_number(card, "days listed")
        if days_listed is None:
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
            engine_cc=_engine_cc(subtitle),
            power_hp=_power_hp(title, slug_hp),
            transmission=_transmission(specs),
            days_listed=days_listed,
            price_change_history=history,
            seller_type=seller,
            canton=canton,
            ch_fahrzeug=True if re.search(r"CH[- ]Fahrzeug|Swiss delivery", card_text, re.I) else None,
            url=self.absolute(href),
        )


# --------------------------------------------------------------------------
# The spec strip. Each card is a small, well-formed document: a heading, an
# engine/gearbox sub-title, the prices, a row of spec spans, and a <dt>/<dd>
# list for the market metrics. Reading the nodes beats reading the card's
# concatenated text, which runs together with no separators -- "days listed"
# followed by "67" and then "13.6 L/100km" flattens to "days listed6713.6".
#
# Engine and gearbox are the only thing separating some cars: an Audi R8's
# variant is "Coupé Advanced" whether it is the 4.2 V8 or the 5.2 V10, a
# CHF 50k difference, and the gated manual is a further premium.
_SPEC_GEARBOX = {"automatic", "manual"}


def _leaf_texts(card: Node) -> list[str]:
    """Text of every leaf element in the card, in document order."""
    out: list[str] = []
    for node in card.css("*"):
        if node.child is not None and node.child.tag != "-text":
            continue
        text = text_of(node.text())
        if text:
            out.append(text)
    return out


def _subtitle(card: Node) -> str:
    """The engine/gearbox line under the heading: `4.2-V8 R-Tronic`, `Coupé 3.0 PDK`."""
    for node in card.css("p"):
        text = text_of(node.text())
        if text and "CHF" not in text and not re.search(r"L/100km|g CO2", text):
            return text
    return ""


def _engine_cc(subtitle: str) -> int | None:
    """`4.2-V8 R-Tronic` -> 4200, `Coupé 3.0 PDK` -> 3000.

    Read from the sub-title only. The card also carries a consumption figure
    ("13.6 L/100km"), and a litres-anywhere match picks that up instead --
    which is how a 4.2 V8 came out as a 13.6-litre engine.
    """
    m = re.search(r"(?<![\d.])(\d\.\d)(?![\d])", subtitle)
    if not m:
        return None
    cc = int(round(float(m.group(1)) * 1000))
    return cc if 500 <= cc <= 9000 else None


def _power_hp(title: str, slug_hp: str | None) -> int | None:
    """Power from the URL slug when it is there, else `420 HP` in the heading."""
    if slug_hp:
        hp = int(slug_hp)
        if 20 <= hp <= 2000:
            return hp
    m = re.search(r"(\d{2,4})\s*(?:HP|PS)\b", title, re.I)
    if not m:
        return None
    hp = int(m.group(1))
    return hp if 20 <= hp <= 2000 else None


def _transmission(specs: list[str]) -> str | None:
    for text in specs:
        if text.strip().lower() in _SPEC_GEARBOX:
            return text.strip().title()
    return None


def _mileage_from(specs: list[str]) -> int | None:
    for text in specs:
        if re.fullmatch(r"[\d'’.,\s]{1,12}km", text.replace(" ", ""), re.I):
            got = parse_mileage(text)
            if got is not None:
                return got
    return None


def _definition_number(card: Node, label: str) -> int | None:
    """The `<dd>` paired with the `<dt>` carrying `label`.

    The market metrics are a definition list -- below market / price change /
    days listed -- strictly alternating, so pairing by document order is
    enough and needs no sibling walk.
    """
    terms = [text_of(t.text()).strip().lower() for t in card.css("dt")]
    values = [text_of(d.text()) for d in card.css("dd")]
    wanted = label.strip().lower()
    for term, value in zip(terms, values, strict=False):
        if term == wanted:
            return to_int(value)
    return None


# --------------------------------------------------------------------------
def _card_for(anchor: Node, *, max_levels: int = 6, widen: int = 3) -> Node:
    """The smallest ancestor that describes exactly one car.

    AutoUncle now wraps each listing in its own `<article>`, which is a far
    better boundary than any walk: one detail link, one price, one spec strip.
    Anchor on it.

    The old heuristic climbed until it saw "Days listed" beside a CHF figure.
    The site relabelled that to lower-case "days listed", so the test stopped
    firing, the walk ran its full six levels into the results grid, and every
    anchor on the page resolved to the *same* node -- 25 listings sharing one
    car's price and mileage. The fallback below keeps the walk for a layout
    without articles, now matched case-insensitively.
    """
    node: Node | None = anchor
    while node is not None:
        if node.tag == "article":
            return node
        node = node.parent

    node = anchor
    for _ in range(max_levels):
        parent = node.parent
        if parent is None:
            break
        node = parent
        text = node.text() or ""
        if re.search(r"days listed", text, re.I) and "CHF" in text:
            for _ in range(widen):
                nxt = node.parent
                if nxt is None or len(re.findall(r"(?i)days listed", nxt.text() or "")) != 1:
                    break
                node = nxt
            return node
    return node
    return node


_PRICE_ONLY = re.compile(r"^CHF\s*[\d'’ .,]+$")


def _price_figures(card: Node) -> list[float]:
    """Asking-price figures in DOM order: current ask first, pre-cut second.

    A discounted card renders both into one paragraph with no separator --
    "CHF 56'850CHF 59'850" -- while a sibling span repeats only the struck-out
    old price. Matching whole-text-is-a-price nodes therefore found the *old*
    price and nothing else, which marked every cut car up to its pre-cut ask.
    Read the paragraph and take its figures in order.
    """
    for node in card.css("p"):
        text = text_of(node.text())
        if not text or "CHF" not in text or _inside_button(node, card):
            continue
        figures = [parse_price(f"CHF {m}") for m in _CHF.findall(text)]
        found = [f for f in figures if f and f > 500]
        if len(found) >= 2:
            return found
        if found:
            break

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
