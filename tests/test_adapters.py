"""Adapter parse tests against frozen copies of the real pages.

The fixtures in `tests/fixtures/` are gzipped captures of live listing pages.
When a site redesigns, these tests fail loudly in CI instead of the daily run
quietly returning zero listings for a week.

Re-capture a fixture with:
    python scripts/capture_fixture.py exportfrom
"""

import gzip
import statistics
from pathlib import Path

import pytest

from nippon_margin.adapters.ch.autouncle import AutoUncleAdapter
from nippon_margin.adapters.jp.carused import CarusedAdapter, extract_records
from nippon_margin.adapters.jp.exportfrom import ExportFromAdapter, _spec_table
from nippon_margin.config import SourceConfig
from nippon_margin.models import Steering

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
        """The page mixes 996-991 cars with 992s. The watch entry wants
        1998-2019, so the 992s must not become comps for a car we would buy."""
        item = cfg.watch_item("porsche_911")
        keyed = [x for x in listings if x.watchlist_key == "porsche_911"]
        assert keyed, "no 911 resolved at all"
        assert all(item.year_ok(x.year) for x in keyed)

        out_of_window = [x for x in listings if x.year and not item.year_ok(x.year)]
        assert out_of_window, "fixture no longer contains an out-of-generation car"
        assert all(x.watchlist_key is None for x in out_of_window)

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
