"""Adapter parse tests against frozen copies of the real pages.

The fixtures in `tests/fixtures/` are gzipped captures of live listing pages.
When a site redesigns, these tests fail loudly in CI instead of the daily run
quietly returning zero listings for a week.

Re-capture a fixture with:
    python scripts/capture_fixture.py exportfrom
"""

import gzip
import re
import statistics
from pathlib import Path

import pytest

from nippon_margin.adapters.ch.autouncle import AutoUncleAdapter
from nippon_margin.adapters.jp.carused import CarusedAdapter, extract_records
from nippon_margin.adapters.jp.exportfrom import ExportFromAdapter, _spec_table
from nippon_margin.adapters.jp.goonet import GooNetAdapter, _pretty, _variant
from nippon_margin.config import SourceConfig
from nippon_margin.models import PriceTerms, Steering

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    path = FIXTURES / f"{name}.html.gz"
    if not path.exists():
        pytest.skip(f"fixture {name} not captured")
    return gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")


def adapter(cls, cfg, **kw):
    return cls(cfg, SourceConfig(enabled=True, max_pages=1, **kw), fetcher=None)


class TestExportFrom:
    @pytest.fixture(scope="class")
    def listings(self, cfg):
        return adapter(ExportFromAdapter, cfg).parse_page(fixture("exportfrom"), "x")

    def test_finds_the_whole_grid(self, listings):
        assert len(listings) >= 10

    def test_every_listing_has_the_essentials(self, listings):
        for lst in listings:
            assert lst.source == "exportfrom"
            assert lst.source_ref
            assert lst.make and lst.model
            assert lst.url.startswith("https://exportfrom.jp/")

    def test_prices_are_plausible_usd(self, listings):
        prices = [x.price_usd for x in listings if x.price_usd]
        assert len(prices) >= 10
        assert all(1_000 < p < 2_000_000 for p in prices)

    def test_years_and_mileage_are_parsed(self, listings):
        assert sum(1 for x in listings if x.year) >= 10
        assert sum(1 for x in listings if x.mileage_km) >= 10

    def test_lhd_stock_list_is_marked_lhd(self, listings):
        assert all(x.steering is Steering.LHD for x in listings)

    def test_watchlist_models_are_recognised(self, listings):
        keys = {x.watchlist_key for x in listings if x.watchlist_key}
        # This capture contains an Abarth 695, an E31 850i and a W204 C63.
        assert {"abarth_695", "bmw_850i_e31", "mercedes_c63_w204"} <= keys

    def test_a_quattroporte_is_not_filed_as_a_granturismo(self, listings):
        for lst in listings:
            if "quattroporte" in lst.model.lower():
                assert lst.watchlist_key != "maserati_granturismo"

    def test_spec_table_parses_the_detail_page(self, cfg):
        html = (
            "<table><tr><th>Manufacturer:</th><td>Maserati</td></tr>"
            "<tr><th>Year:</th><td>2013</td></tr>"
            "<tr><th>Steering:</th><td>Left</td></tr></table>"
        )
        specs = _spec_table(html)
        assert specs["manufacturer"] == "Maserati"
        assert specs["year"] == "2013"
        assert specs["steering"] == "Left"


class TestCarused:
    @pytest.fixture(scope="class")
    def listings(self, cfg):
        return adapter(CarusedAdapter, cfg, renderer="http").parse_page(fixture("carused"), "x")

    def test_rsc_payload_yields_records(self):
        records = extract_records(fixture("carused"))
        assert len(records) >= 20
        assert all("refno" in r for r in records)

    def test_listings_carry_the_structured_fields(self, listings):
        assert len(listings) >= 20
        assert sum(1 for x in listings if x.model_code) >= 20
        assert sum(1 for x in listings if x.mileage_km is not None) >= 20
        assert sum(1 for x in listings if x.year) >= 20

    def test_prices_are_plausible_fob_usd(self, listings):
        prices = [x.price_usd for x in listings if x.price_usd]
        assert len(prices) >= 20
        assert all(500 < p < 1_000_000 for p in prices)

    def test_steering_filter_is_honoured(self, listings):
        assert all(x.steering is Steering.LHD for x in listings)

    def test_refs_are_unique(self, listings):
        refs = [x.source_ref for x in listings]
        assert len(refs) == len(set(refs))

    def test_sold_cars_are_dropped(self, cfg):
        records = extract_records(fixture("carused"))
        assert not any(r.get("is_sold") for r in records if r.get("refno") in {
            x.source_ref for x in adapter(CarusedAdapter, cfg, renderer="http")
            .parse_page(fixture("carused"), "x")
        })


class TestAutoUncle:
    @pytest.fixture(scope="class")
    def listings(self, cfg):
        return adapter(AutoUncleAdapter, cfg).parse_page(fixture("au911"), "x")

    def test_finds_the_result_page(self, listings):
        assert len(listings) >= 20

    def test_prices_are_real_asking_prices_not_savings_badges(self, listings):
        """Regression: the "Below market CHF 3'700" badge used to win."""
        prices = [x.price_chf for x in listings if x.price_chf]
        assert len(prices) >= 20
        assert all(20_000 < p < 1_000_000 for p in prices), sorted(prices)[:3]
        # A page of Swiss 911s does not have a median under CHF 40k.
        assert statistics.median(prices) > 50_000

    def test_days_listed_is_captured(self, listings):
        days = [x.days_listed for x in listings if x.days_listed is not None]
        assert len(days) >= 20
        assert all(0 <= d <= 2000 for d in days)

    def test_price_cut_history_is_captured(self, listings):
        cut = [x for x in listings if x.had_price_cut]
        assert cut, "AutoUncle exposes price changes; none were parsed"
        for lst in cut:
            assert lst.price_change_history[-1].price < lst.price_change_history[0].price

    def test_canton_is_captured(self, listings):
        cantons = {x.canton for x in listings if x.canton}
        assert len(cantons) >= 3
        assert "Zürich" in cantons or "Zug" in cantons

    def test_only_in_generation_911s_resolve_to_the_watch_entry(self, cfg, listings):
        """The page mixes 996-991 cars with 992s. The watch entries want
        1998-2019, so the 992s must not become comps for a car we would buy."""
        tiers = {"porsche_911", "porsche_911_carrera", "porsche_911_carrera_s",
                 "porsche_911_turbo", "porsche_911_gt"}
        keyed = [x for x in listings if x.watchlist_key in tiers]
        assert keyed, "no 911 resolved at all"
        assert all(cfg.watch_item(x.watchlist_key).year_ok(x.year) for x in keyed)

        out_of_window = [
            x for x in listings
            if x.year and not cfg.watch_item("porsche_911").year_ok(x.year)
        ]
        assert out_of_window, "fixture no longer contains an out-of-generation car"
        assert all(x.watchlist_key is None for x in out_of_window)

    def test_911_variants_land_in_their_own_price_tier(self, listings):
        """A GT3 and a base Carrera must not share a comp pool: on this very
        page they are CHF 148,900 and CHF 34,900."""
        by_tier: dict[str, set[str]] = {}
        for lst in listings:
            if lst.watchlist_key and lst.watchlist_key.startswith("porsche_911"):
                by_tier.setdefault(lst.watchlist_key, set()).add(lst.variant or "")

        assert "porsche_911_gt" in by_tier
        assert all("GT3" in v or "GT2" in v for v in by_tier["porsche_911_gt"])

        assert "porsche_911_turbo" in by_tier
        assert all("Turbo" in v for v in by_tier["porsche_911_turbo"])

        # The base tier is base cars only -- no S, no GTS, no Turbo, no GT.
        for variant in by_tier.get("porsche_911_carrera", set()):
            assert not re.search(r"\b(4S|S|GTS|Turbo|GT3|GT2)\b", variant), variant

    def test_year_and_mileage_are_parsed(self, listings):
        assert sum(1 for x in listings if x.year) >= 20
        assert sum(1 for x in listings if x.mileage_km) >= 20

    def test_urls_are_absolute(self, listings):
        assert all(x.url.startswith("https://www.autouncle.ch/") for x in listings)


class TestBeForwardSteering:
    """BE FORWARD is the one Japanese source that does not default to LHD."""

    def test_every_search_url_carries_the_lhd_filter(self, cfg):
        from nippon_margin.adapters.jp.beforward import STEERING_FILTER, BeForwardAdapter

        ad = BeForwardAdapter(cfg, SourceConfig(enabled=True, max_pages=2), fetcher=None)
        ad._make_ids = {"MERCEDES-BENZ": "106", "PORSCHE": "42"}
        urls = list(ad.search_urls())
        assert urls
        assert all(STEERING_FILTER in u for u in urls), urls

    def test_the_fallback_url_is_filtered_too(self, cfg):
        """With no make ids resolved we still must not scrape RHD stock."""
        from nippon_margin.adapters.jp.beforward import STEERING_FILTER, BeForwardAdapter

        ad = BeForwardAdapter(cfg, SourceConfig(enabled=True, max_pages=1), fetcher=None)
        ad._make_ids = {}
        urls = list(ad.search_urls())
        assert len(urls) == 1
        assert STEERING_FILTER in urls[0]

    def test_the_exact_spelling_is_pinned(self):
        """`steering=LHD` and `steering=1` both 404 on beforward.jp."""
        from nippon_margin.adapters.jp.beforward import STEERING_FILTER

        assert STEERING_FILTER == "steering=Left"


class TestSbtPriceTerms:
    """SBT names no incoterm. Its "Total Price" sits a flat ~USD 1,800 above
    the vehicle price whether the car is $25k or $336k -- a shipping charge to
    their own default market, not C&F to Europe. Treating it as C&F skipped
    our freight entirely and understated landed cost by ~CHF 2,000 a car."""

    def test_the_vehicle_price_is_used_as_fob(self, cfg):
        from selectolax.parser import HTMLParser

        from nippon_margin.adapters.jp.sbtjapan import SbtJapanAdapter
        from nippon_margin.models import PriceTerms

        html = """
        <div class="card-product">
          <a class="card-product__wrap" href="/used-cars/AB1234"></a>
          <h2 class="card-product__product">2015/3 PORSCHE 911 CARRERA</h2>
          <div class="card-product__vehicle-price">Vehicle Price USD 25,340</div>
          <div class="card-product__total-price">Total Price USD 27,053</div>
        </div>"""
        ad = SbtJapanAdapter(cfg, SourceConfig(enabled=True, max_pages=1), fetcher=None)
        listing = ad._parse_card(HTMLParser(html).css_first(".card-product"))
        assert listing.price_usd == 25_340
        assert listing.price_terms is PriceTerms.FOB

    def test_the_published_total_is_kept_for_reference(self, cfg):
        from selectolax.parser import HTMLParser

        from nippon_margin.adapters.jp.sbtjapan import SbtJapanAdapter

        html = """
        <div class="card-product">
          <a class="card-product__wrap" href="/used-cars/AB1234"></a>
          <h2 class="card-product__product">2015/3 PORSCHE 911</h2>
          <div class="card-product__vehicle-price">Vehicle Price USD 25,340</div>
          <div class="card-product__total-price">Total Price USD 27,053</div>
        </div>"""
        ad = SbtJapanAdapter(cfg, SourceConfig(enabled=True, max_pages=1), fetcher=None)
        listing = ad._parse_card(HTMLParser(html).css_first(".card-product"))
        assert "SBT total price $27,053" in listing.description

    def test_an_ask_total_does_not_break_the_price(self, cfg):
        from selectolax.parser import HTMLParser

        from nippon_margin.adapters.jp.sbtjapan import SbtJapanAdapter

        html = """
        <div class="card-product">
          <a class="card-product__wrap" href="/used-cars/AB1234"></a>
          <h2 class="card-product__product">2015/3 PORSCHE 911</h2>
          <div class="card-product__vehicle-price">Vehicle Price USD 25,340</div>
          <div class="card-product__total-price">Total Price Ask</div>
        </div>"""
        ad = SbtJapanAdapter(cfg, SourceConfig(enabled=True, max_pages=1), fetcher=None)
        listing = ad._parse_card(HTMLParser(html).css_first(".card-product"))
        assert listing.price_usd == 25_340


class TestGooNet:
    """The only Japanese source that names the trim and labels its price FOB."""

    @pytest.fixture(scope="class")
    def listings(self, cfg):
        return adapter(GooNetAdapter, cfg).parse_page(fixture("goonet"), "x")

    def test_finds_the_whole_grid(self, listings):
        assert len(listings) >= 15

    def test_every_listing_has_the_essentials(self, listings):
        for lst in listings:
            assert lst.source == "goonet"
            assert lst.source_ref.isdigit()
            assert lst.make and lst.model
            assert lst.url.startswith("https://www.goo-net-exchange.com/usedcars/")

    def test_the_trim_is_named_on_almost_every_card(self, listings):
        """This is the whole reason for the source: elsewhere ~2% of Japanese
        listings state a trim, and a Carrera gets priced against a GT3."""
        named = [x for x in listings if x.variant]
        assert len(named) >= int(0.8 * len(listings))
        assert {"Carrera", "Carrera S", "Carrera 4S"} <= {x.variant for x in named}

    def test_trims_route_to_their_price_tier(self, listings):
        by_variant = {x.variant: x.watchlist_key for x in listings if x.watchlist_key}
        assert by_variant.get("Carrera") == "porsche_911_carrera"
        assert by_variant.get("Carrera S") == "porsche_911_carrera_s"
        assert by_variant.get("Carrera 4S") == "porsche_911_carrera_s"
        assert by_variant.get("Turbo S") == "porsche_911_turbo"

    def test_prices_are_fob_usd(self, listings):
        priced = [x for x in listings if x.price_usd]
        assert len(priced) >= 15
        assert all(1_000 < x.price_usd < 2_000_000 for x in priced)
        # The card caption is literally "Car Price (FOB)" -- no guessing, and
        # no repeat of reading a bundled total as a shipping-inclusive quote.
        assert all(x.price_terms is PriceTerms.FOB for x in priced)

    def test_the_spec_strip_is_read(self, listings):
        assert sum(1 for x in listings if x.year) >= 15
        assert sum(1 for x in listings if x.reg_month) >= 15
        assert sum(1 for x in listings if x.mileage_km) >= 15
        assert sum(1 for x in listings if x.engine_cc) >= 15
        assert sum(1 for x in listings if x.steering is not Steering.UNKNOWN) >= 15

    def test_both_steering_sides_are_present_and_told_apart(self, listings):
        """Japan holds a lot of imported LHD European metal; roughly half of
        this page is LHD. Reading them all as RHD would discard the source."""
        sides = {x.steering for x in listings}
        assert Steering.LHD in sides
        assert Steering.RHD in sides

    def test_location_is_a_japanese_prefecture(self, listings):
        located = [x.location for x in listings if x.location]
        assert len(located) >= 15
        assert all(loc.endswith("Japan") for loc in located)

    def test_out_of_generation_cars_are_not_keyed(self, cfg, listings):
        for lst in listings:
            if lst.watchlist_key:
                assert cfg.watch_item(lst.watchlist_key).year_ok(lst.year)

    def test_search_urls_visit_each_model_page_once(self, cfg):
        urls = list(adapter(GooNetAdapter, cfg).search_urls())
        assert len(urls) == len(set(urls)), "the five 911 tiers share one page"
        assert "https://www.goo-net-exchange.com/usedcars/PORSCHE/911/index.html" in urls

    def test_paging_is_a_path_not_a_query(self, cfg):
        """`?page=2` returns HTTP 500; the site pages on `index-N.html`."""
        cls = GooNetAdapter
        urls = list(cls(cfg, SourceConfig(enabled=True, max_pages=3), fetcher=None).search_urls())
        assert any(u.endswith("/PORSCHE/911/index-2.html") for u in urls)
        assert not any("?" in u for u in urls)


class TestGooNetTitleParsing:
    def test_acronyms_survive_the_shouting(self):
        assert _pretty("911 CARRERA 4 GTS") == "911 Carrera 4 GTS"
        assert _pretty("911 GT3 RS") == "911 GT3 RS"
        assert _pretty("911 CARRERA 4S CABRIOLET") == "911 Carrera 4S Cabriolet"

    def test_the_model_is_stripped_off_the_grade(self):
        """`911 Carrera` is not a trim; `Carrera` is."""
        assert _variant("911", "911 CARRERA 4S") == "Carrera 4S"
        assert _variant("911", "911 TURBO S") == "Turbo S"

    def test_a_grade_that_is_only_the_model_yields_no_trim(self):
        assert _variant("911", "911") is None


class TestAudiR8Tiers:
    """The R8 is two cars in one name, and the name never says which.

    Every Swiss R8 is a "Coupé", optionally with an equipment line, whether it
    is the 4.2 V8 or the 5.2 V10 -- a CHF 50k difference. So these tier on
    displacement, which both sides publish, and the gated manual on gearbox.
    """

    @pytest.fixture(scope="class")
    def ch(self, cfg):
        return adapter(AutoUncleAdapter, cfg).parse_page(fixture("aur8"), "x")

    @pytest.fixture(scope="class")
    def jp(self, cfg):
        return adapter(GooNetAdapter, cfg).parse_page(fixture("goonetr8"), "x")

    def test_the_swiss_card_yields_engine_gearbox_and_power(self, ch):
        assert len(ch) >= 20
        assert sum(1 for x in ch if x.engine_cc) >= 20
        assert sum(1 for x in ch if x.transmission) >= 20
        assert sum(1 for x in ch if x.power_hp) >= 20
        assert {x.engine_cc for x in ch if x.engine_cc} <= {4200, 5200}

    def test_the_swiss_variant_alone_would_not_separate_them(self, ch):
        """Both engines are sold under the same variant text -- which is why
        the tier cannot be read off the trim the way a 911's can."""
        by_variant = {}
        for x in ch:
            by_variant.setdefault(x.variant, set()).add(x.engine_cc)
        assert any(len(v) > 1 for v in by_variant.values()), \
            "fixture no longer has one variant name covering both engines"

    def test_displacement_routes_each_car_to_its_tier(self, ch):
        for x in ch:
            if x.engine_cc == 4200:
                assert x.watchlist_key == "audi_r8_v8"
            elif x.engine_cc == 5200:
                assert x.watchlist_key in {"audi_r8_v10", "audi_r8_v10_manual"}

    def test_an_automatic_v10_never_lands_in_the_manual_tier(self, ch):
        """The manual tier is rare enough that a page may hold none, so this
        asserts the direction that must always hold. The manual and
        unstated-engine cases are pinned in TestEngineAndGearboxTiers, which
        does not depend on which page a rare car happens to fall on."""
        for x in ch:
            if x.transmission == "Automatic":
                assert x.watchlist_key != "audi_r8_v10_manual"

    def test_both_gearboxes_are_told_apart(self, ch):
        boxes = {x.transmission for x in ch if x.transmission}
        assert boxes <= {"Manual", "Automatic"}
        assert "Automatic" in boxes

    def test_the_japanese_side_tiers_on_the_same_field(self, jp):
        assert len(jp) >= 10
        assert {x.engine_cc for x in jp if x.engine_cc} <= {4200, 5200}
        for x in jp:
            if x.engine_cc == 4200 and x.watchlist_key:
                assert x.watchlist_key == "audi_r8_v8"
            if x.engine_cc == 5200 and x.watchlist_key:
                assert x.watchlist_key in {"audi_r8_v10", "audi_r8_v10_manual"}

    def test_both_tiers_are_actually_present_on_both_sides(self, ch, jp):
        for side in (ch, jp):
            keys = {x.watchlist_key for x in side}
            assert "audi_r8_v8" in keys
            assert "audi_r8_v10" in keys
