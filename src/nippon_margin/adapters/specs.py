"""Declarative specs for the second-tier sources.

These are the sources behind the first four: useful for breadth, not worth a
hand-written module each. Every one ships **disabled** in config.yaml. Enable
one, run `nippon-margin scrape --source <name> --local --dry-run`, and adjust
the selectors here until the count is non-zero -- the CLI prints what it
parsed so this is a two-minute loop, not an archaeology project.

The Swiss ones in particular are deliberately conservative: the spec ranks
AutoUncle first precisely because it aggregates these portals already, and
scraping them directly is a politeness and ToS cost you only pay when
AutoUncle stops being enough.
"""

from __future__ import annotations

from .declarative import SourceSpec

# --------------------------------------------------------------------------
# Japan (buy side)
# --------------------------------------------------------------------------
JAPAN_PARTNER = SourceSpec(
    name="japanpartner",
    base_url="https://www.japan-partner.com",
    url_template="{base}/left-hand.html?keyword={query}&page={page}",
    card="table.car-list tr, div.car-item, li.stock-item",
    link="a[href*='detail'], a[href*='car']",
    title="h2, h3, .car-name, td.name",
    price=".price, td.price, .fob",
    default_steering="LHD",
    default_terms="FOB",
    extra_urls=("/left-hand.html",),
    notes="LHD landing page; selectors need one live confirmation run.",
)

TOKYO_CARZ = SourceSpec(
    name="tokyocarz",
    base_url="https://www.tokyocarz.com",
    url_template="{base}/stock-list?keyword={query}&page={page}",
    card="div.stock-item, li.car, .vehicle-card",
    link="a[href]",
    title="h2, h3, .car-title",
    price=".price, .fob-price",
    default_terms="FOB",
    extra_urls=("/stock-list",),
    notes="Selectors need one live confirmation run.",
)

JAPANESE_CAR_TRADE = SourceSpec(
    name="japanesecartrade",
    base_url="https://www.japanesecartrade.com",
    url_template="{base}/LHD/?keyword={query}&page={page}",
    card="div.car-box, .stock-item, tr.car-row",
    link="a[href*='used-car'], a[href*='detail']",
    title="h2, h3, .car-title, .make-model",
    price=".price, .fob",
    default_steering="LHD",
    default_terms="FOB",
    extra_urls=("/LHD/",),
    notes="Aggregator across many exporters -- expect heavy chassis-level "
          "duplication with the direct sources; the dedupe pass handles it.",
)

TS_EXPORT = SourceSpec(
    name="tsexport",
    base_url="https://www.ts-export.com",
    url_template="{base}/en/stock?keyword={query}&page={page}",
    card="div.car-item, .stocklist-item, li.vehicle",
    link="a[href]",
    title="h2, h3, .title",
    price=".price",
    default_terms="FOB",
    extra_urls=("/en/stock",),
    notes="Aggregates auction, Yahoo and Goonet samples -- auction rows carry "
          "grades, which is why it is worth having despite the noise.",
)

# --------------------------------------------------------------------------
# Switzerland (sell side)
# --------------------------------------------------------------------------
# AutoScout24 is deliberately absent: their ToS forbids automated scraping.
# Use official partner/API access if you obtain it.
AUTOLINA = SourceSpec(
    name="autolina",
    base_url="https://www.autolina.ch",
    url_template="{base}/de/auto/suche?marke={make}&modell={model}&seite={page}",
    card="div.vehicle-item, article.car, .search-result-item",
    link="a[href*='/auto/']",
    title="h2, h3, .vehicle-title",
    price=".price, .vehicle-price",
    currency="CHF",
    notes="Selectors need one live confirmation run.",
)

CARFORYOU = SourceSpec(
    name="carforyou",
    base_url="https://www.carforyou.ch",
    url_template="{base}/de/auto/suche?make={make}&model={model}&page={page}",
    card="article, li[data-testid*='listing'], div.listing-item",
    link="a[href*='/de/auto/']",
    title="h2, h3, [data-testid*='title']",
    price="[data-testid*='price'], .price",
    currency="CHF",
    notes="Selectors need one live confirmation run.",
)

TUTTI = SourceSpec(
    name="tutti",
    base_url="https://www.tutti.ch",
    url_template="{base}/de/li/ganze-schweiz/autos?query={query}&page={page}",
    card="div[class*='ListItem'], article, a[href*='/de/vi/']",
    link="a[href*='/de/vi/']",
    title="h2, h3, div[class*='title']",
    price="span[class*='price'], .price",
    currency="CHF",
    notes="Mostly private sellers -- useful as a floor on realizable price.",
)

COMPARIS = SourceSpec(
    name="comparis",
    base_url="https://www.comparis.ch",
    url_template="{base}/carfinder/marktplatz/occasion?make={make}&model={model}&page={page}",
    card="div.css-listing, article, li[class*='result']",
    link="a[href*='/carfinder/']",
    title="h2, h3",
    price="[class*='price']",
    currency="CHF",
    notes="JS-heavy: set renderer: playwright in config.yaml before enabling.",
)

RICARDO = SourceSpec(
    name="ricardo",
    base_url="https://www.ricardo.ch",
    url_template="{base}/de/s/{query}?sort=ending_soonest&page={page}",
    card="div[class*='article'], article, li[class*='item']",
    link="a[href*='/de/a/']",
    title="h2, h3, [class*='title']",
    price="[class*='price']",
    currency="CHF",
    notes="COMPLETED auctions only -- these are actual transaction prices, "
          "which is the one thing asking prices cannot tell you. Weekly "
          "cadence is enough; see `nippon-margin scrape --source ricardo`.",
)

JP_SPECS = {
    s.name: s for s in (JAPAN_PARTNER, TOKYO_CARZ, JAPANESE_CAR_TRADE, TS_EXPORT)
}
CH_SPECS = {s.name: s for s in (AUTOLINA, CARFORYOU, TUTTI, COMPARIS, RICARDO)}
