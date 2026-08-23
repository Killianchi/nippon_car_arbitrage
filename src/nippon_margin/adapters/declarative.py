"""Declarative adapters: a new source in ~20 lines of data, not code.

Most listing sites are the same shape -- a repeating card with a link, a
title, a price and a handful of spec values. `SourceSpec` describes that
shape; `DeclarativeJpAdapter` / `DeclarativeChAdapter` do the rest.

Adding a source:

    SourceSpec(
        name="tokyocarz",
        base_url="https://www.tokyocarz.com",
        url_template="{base}/stock?q={query}&page={page}",
        card="div.car-item",
        link="a[href*='/car/']",
        title="h3",
        price=".price",
        specs="li",
    )

...plus one line in `SPECS` and one in config.yaml. See README, "Adding a
new source adapter".

Selectors are best-effort by nature: when a site redesigns, the adapter
returns zero rows, the run record notes it, and the dashboard's Runs page
shows the source flatlining. That is the intended failure mode -- visible and
non-fatal -- rather than a crash that takes the other sources down with it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..models import ChListing, JpListing, PriceTerms, SellerType, Steering
from ..parse import (
    parse_engine_cc,
    parse_grade,
    parse_mileage,
    parse_month,
    parse_price,
    parse_price_terms,
    parse_repair_history,
    parse_steering,
    parse_transmission,
    parse_year,
    split_make_model,
    text_of,
)
from .base import ChAdapter, JpAdapter


@dataclass(frozen=True)
class SourceSpec:
    name: str
    base_url: str
    #: `{base}`, `{query}`, `{make}`, `{model}`, `{page}` are substituted.
    url_template: str
    card: str
    link: str = "a[href]"
    title: str = "h2, h3, .title"
    price: str = ".price"
    #: container whose text carries the unlabelled spec strip
    specs: str = "li, td, span"
    image: str = "img"
    #: extra CSS for a labelled spec table on the card, if the site has one
    spec_label: str | None = None
    spec_value: str | None = None
    #: regex extracting a stable listing ref from the detail href
    ref_pattern: str = r"([A-Za-z0-9\-_]{3,40})/?$"
    currency: str = "USD"
    default_steering: str | None = None
    default_terms: str = "UNKNOWN"
    notes: str = ""
    #: static extra paths appended to the generated search URLs
    extra_urls: tuple[str, ...] = field(default_factory=tuple)


class _DeclarativeMixin:
    spec: SourceSpec

    def search_urls(self) -> Iterable[str]:
        template = self.spec.url_template
        for item in self.cfg.watchlist:
            query = item.search_terms()[0]
            for page in range(1, self.source_cfg.max_pages + 1):
                yield template.format(
                    base=self.spec.base_url,
                    query=query.replace(" ", "+"),
                    make=_slug(item.make),
                    model=_slug(item.model),
                    page=page,
                )
        for extra in self.spec.extra_urls:
            yield f"{self.spec.base_url}{extra}"

    # -- shared card reading ------------------------------------------------
    def _href_and_ref(self, card) -> tuple[str, str] | None:
        link = card.css_first(self.spec.link) or card.css_first("a[href]")
        href = link.attributes.get("href") if link else None
        if not href:
            return None
        m = re.search(self.spec.ref_pattern, href)
        ref = m.group(1) if m else None
        return (href, ref) if ref else None

    def _title(self, card) -> str:
        node = card.css_first(self.spec.title)
        return text_of(node.text()) if node else ""

    def _price(self, card) -> float | None:
        node = card.css_first(self.spec.price)
        return parse_price(text_of(node.text())) if node else None

    def _values(self, card) -> list[str]:
        return [text_of(n.text()) for n in card.css(self.spec.specs) if text_of(n.text())]

    def _labelled(self, card) -> dict[str, str]:
        if not (self.spec.spec_label and self.spec.spec_value):
            return {}
        labels = [text_of(n.text()) for n in card.css(self.spec.spec_label)]
        values = [text_of(n.text()) for n in card.css(self.spec.spec_value)]
        return {
            label.rstrip(":").lower(): value
            for label, value in zip(labels, values, strict=False)
            if label and value
        }

    def _images(self, card) -> list[str]:
        return [
            img.attributes["src"]
            for img in card.css(self.spec.image)
            if img.attributes.get("src", "").startswith(("http", "//"))
        ][:3]


class DeclarativeJpAdapter(_DeclarativeMixin, JpAdapter):
    spec: SourceSpec

    def __init__(self, cfg, source_cfg, fetcher, spec: SourceSpec):
        super().__init__(cfg, source_cfg, fetcher)
        self.spec = spec
        self.name = spec.name
        self.base_url = spec.base_url

    def parse_page(self, html: str, url: str) -> list[JpListing]:
        out: list[JpListing] = []
        seen: set[str] = set()
        for card in self.dom(html).css(self.spec.card):
            found = self._href_and_ref(card)
            if not found:
                continue
            href, ref = found
            if ref in seen:
                continue
            title = self._title(card) or text_of(card.text())[:120]
            if not title:
                continue
            seen.add(ref)

            blob = " ".join(self._values(card)) or text_of(card.text())
            labelled = self._labelled(card)
            make, model = split_make_model(re.sub(r"\b(19|20)\d{2}\b", "", title).strip())
            terms, port = parse_price_terms(blob)

            out.append(self.finish(JpListing(
                source=self.spec.name,
                source_ref=ref,
                make=make,
                model=model,
                year=parse_year(labelled.get("year") or title) or parse_year(blob),
                reg_month=parse_month(labelled.get("year") or blob),
                mileage_km=parse_mileage(labelled.get("mileage") or blob),
                transmission=parse_transmission(labelled.get("transmission") or blob),
                steering=(
                    parse_steering(labelled.get("steering") or blob)
                    or Steering(self.spec.default_steering or "UNKNOWN")
                ),
                fuel=labelled.get("fuel"),
                engine_cc=parse_engine_cc(labelled.get("engine") or blob),
                color=labelled.get("color") or labelled.get("colour"),
                price_usd=self._price(card),
                price_terms=terms if terms is not PriceTerms.UNKNOWN
                            else PriceTerms(self.spec.default_terms),
                price_port=port,
                auction_grade=parse_grade(labelled.get("grade", "")) if labelled.get("grade") else None,
                repair_history=parse_repair_history(blob),
                description=f"{title} | {blob}"[:2000],
                image_urls=self._images(card),
                url=self.absolute(href),
            )))
        return out


class DeclarativeChAdapter(_DeclarativeMixin, ChAdapter):
    spec: SourceSpec

    def __init__(self, cfg, source_cfg, fetcher, spec: SourceSpec):
        super().__init__(cfg, source_cfg, fetcher)
        self.spec = spec
        self.name = spec.name
        self.base_url = spec.base_url

    def parse_page(self, html: str, url: str) -> list[ChListing]:
        out: list[ChListing] = []
        seen: set[str] = set()
        for card in self.dom(html).css(self.spec.card):
            found = self._href_and_ref(card)
            if not found:
                continue
            href, ref = found
            if ref in seen:
                continue
            title = self._title(card)
            if not title:
                continue
            seen.add(ref)

            blob = " ".join(self._values(card)) or text_of(card.text())
            labelled = self._labelled(card)
            make, rest = split_make_model(re.sub(r"\b(19|20)\d{2}\b", "", title).strip())
            parts = rest.split(None, 1)

            seller = SellerType.UNKNOWN
            if re.search(r"\bprivat|private\b", blob, re.I):
                seller = SellerType.PRIVATE
            elif re.search(r"\bh(ä|ae)ndler|dealer|garage|AG\b", blob, re.I):
                seller = SellerType.DEALER

            out.append(self.finish(ChListing(
                source=self.spec.name,
                source_ref=ref,
                make=make,
                model=parts[0] if parts else rest,
                variant=parts[1] if len(parts) > 1 else None,
                year=parse_year(labelled.get("year") or title) or parse_year(blob),
                mileage_km=parse_mileage(labelled.get("mileage") or blob),
                price_chf=self._price(card),
                seller_type=seller,
                canton=labelled.get("canton") or labelled.get("kanton"),
                ch_fahrzeug=True if re.search(r"CH[- ]Fahrzeug|Schweizer Auslieferung", blob, re.I) else None,
                url=self.absolute(href),
            )))
        return out


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
