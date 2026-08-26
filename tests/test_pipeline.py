"""Analyze and alert stage behaviour.

These cover the decisions that are easy to get subtly wrong and hard to
notice: which alert fires, whether a duplicate can outrank the car you would
actually buy, and whether the daily stats say what the charts claim.
"""

from datetime import UTC, datetime, timedelta

import pytest

from nippon_margin.models import ChListing, FxRate, JpListing, ModelStats
from nippon_margin.pipeline.alert import select_alerts
from nippon_margin.pipeline.analyze import analyze, daily_stats, jp_price_moves, spread_moves
from nippon_margin.pipeline.report import build_digest, render_markdown, weekly_portfolio
from nippon_margin.pipeline.scrape import filter_by_origin
from nippon_margin.store.sqlite_store import SqliteStore

NOW = datetime(2025, 3, 15, tzinfo=UTC)
TODAY = NOW.date().isoformat()


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "p.db")
    s.save_fx(FxRate(day=TODAY, usd_chf=0.80, jpy_chf=0.0053))
    yield s
    s.close()


def jp(ref="a", price=20_000.0, **kw):
    base = dict(source="sbtjapan", source_ref=ref, make="Porsche", model="Cayenne",
                year=2015, mileage_km=80_000, price_usd=price,
                watchlist_key="porsche_cayenne", first_seen=NOW, last_seen=NOW)
    base.update(kw)
    return JpListing(**base)


def ch(ref="c", price=60_000.0, **kw):
    base = dict(source="autouncle", source_ref=ref, make="Porsche", model="Cayenne",
                year=2015, mileage_km=80_000, price_chf=price, days_listed=30,
                watchlist_key="porsche_cayenne", first_seen=NOW, last_seen=NOW)
    base.update(kw)
    return ChListing(**base)


class TestAnalyze:
    def test_end_to_end_produces_scored_opportunities(self, cfg, store):
        store.upsert_jp([jp()])
        store.upsert_ch([ch(f"c{i}") for i in range(6)])
        opportunities = analyze(cfg, store, now=NOW)
        assert len(opportunities) == 1
        opp = opportunities[0]
        assert opp.opportunity_score > 0
        assert opp.landed_roro and opp.landed_container
        assert opp.comps.comp_count == 6
        assert store.load_opportunities()[0].id == opp.id

    def test_it_refuses_to_run_without_an_fx_rate(self, cfg, tmp_path):
        empty = SqliteStore(tmp_path / "nofx.db")
        empty.upsert_jp([jp()])
        with pytest.raises(RuntimeError, match="no FX rate"):
            analyze(cfg, empty, now=NOW)
        empty.close()

    def test_daily_stats_are_written_for_every_watched_model(self, cfg, store):
        store.upsert_jp([jp()])
        store.upsert_ch([ch()])
        analyze(cfg, store, now=NOW)
        rows = store.load_model_stats()
        assert len(rows) == len(cfg.watchlist)
        cayenne = next(r for r in rows if r.watchlist_key == "porsche_cayenne")
        assert cayenne.jp_count == 1 and cayenne.ch_count == 1

    def test_a_dearer_duplicate_cannot_outrank_the_cheap_one(self, cfg, store):
        chassis = "WP1AB2A29FLA12345"
        store.upsert_jp([
            jp("cheap", price=18_000, chassis_no=chassis),
            jp("dear", price=26_000, chassis_no=chassis, source="beforward"),
        ])
        store.upsert_ch([ch(f"c{i}") for i in range(6)])
        opportunities = analyze(cfg, store, now=NOW)
        by_ref = {o.jp_doc_id: o for o in opportunities}
        dear = by_ref["beforward_dear"]
        cheap = by_ref["sbtjapan_cheap"]
        assert cheap.is_cheapest_duplicate and cheap.opportunity_score > 0
        assert not dear.is_cheapest_duplicate
        assert dear.opportunity_score == 0.0
        assert any("listed cheaper" in f for f in dear.risk_flags)

    def test_spread_is_the_swiss_ask_minus_our_landed_cost(self, cfg):
        opportunities = []
        stats = daily_stats(cfg, [jp()], [ch()], opportunities,
                            fx_usd_chf=0.80, day=NOW.date())
        row = next(r for r in stats if r.watchlist_key == "porsche_cayenne")
        # No opportunities were passed, so there is no landed median and
        # therefore no spread -- better than inventing one.
        assert row.ch_median_price_chf == 60_000
        assert row.spread_chf is None


class TestTrendHelpers:
    def test_spread_move_is_latest_minus_baseline(self, store):
        store.save_model_stats([
            ModelStats(day="2025-03-01", watchlist_key="porsche_911", spread_chf=5_000),
            ModelStats(day="2025-03-15", watchlist_key="porsche_911", spread_chf=9_000),
        ])
        assert spread_moves(store, days=7)["porsche_911"] == 4_000

    def test_jp_price_move_is_fractional(self, store):
        store.save_model_stats([
            ModelStats(day="2025-03-01", watchlist_key="porsche_911",
                       jp_median_price_chf=20_000),
            ModelStats(day="2025-03-15", watchlist_key="porsche_911",
                       jp_median_price_chf=18_000),
        ])
        assert jp_price_moves(store, days=7)["porsche_911"] == pytest.approx(-0.1)

    def test_a_single_snapshot_yields_no_move(self, store):
        store.save_model_stats([
            ModelStats(day="2025-03-15", watchlist_key="porsche_911", spread_chf=9_000),
        ])
        assert spread_moves(store, days=7) == {}


class TestAlerts:
    def _seed(self, cfg, store, *, price=12_000.0, comp_price=70_000.0):
        store.upsert_jp([jp(price=price)])
        store.upsert_ch([ch(f"c{i}", price=comp_price) for i in range(8)])
        return analyze(cfg, store, now=NOW)

    def test_a_strong_opportunity_alerts(self, cfg, store):
        self._seed(cfg, store)
        alerts = select_alerts(cfg, store, now=NOW)
        assert any(k.startswith("opp:") for k, _ in alerts)

    def test_a_weak_opportunity_does_not(self, cfg, store):
        # Swiss comps barely above landed cost.
        self._seed(cfg, store, price=20_000, comp_price=27_000)
        assert not any(k.startswith("opp:") for k, _ in select_alerts(cfg, store, now=NOW))

    def test_the_cooldown_suppresses_a_repeat(self, cfg, store):
        self._seed(cfg, store)
        first = select_alerts(cfg, store, now=NOW)
        key = next(k for k, _ in first if k.startswith("opp:"))
        store.mark_alert_sent(key, NOW)
        again = select_alerts(cfg, store, now=NOW + timedelta(days=1))
        assert key not in {k for k, _ in again}
        # ...but it fires again once the cooldown lapses.
        later = select_alerts(cfg, store, now=NOW + timedelta(days=30))
        assert key in {k for k, _ in later}

    def test_max_alerts_per_run_is_respected(self, cfg, store):
        store.upsert_jp([jp(f"a{i}", price=12_000) for i in range(30)])
        store.upsert_ch([ch(f"c{i}", price=70_000) for i in range(8)])
        analyze(cfg, store, now=NOW)
        opp_alerts = [k for k, _ in select_alerts(cfg, store, now=NOW) if k.startswith("opp:")]
        assert len(opp_alerts) <= cfg.alerts.max_alerts_per_run

    def test_a_japanese_price_drop_alerts(self, cfg, store):
        store.save_model_stats([
            ModelStats(day="2025-03-08", watchlist_key="porsche_911",
                       jp_median_price_chf=20_000),
            ModelStats(day=TODAY, watchlist_key="porsche_911",
                       jp_median_price_chf=17_000),
        ])
        keys = {k for k, _ in select_alerts(cfg, store, now=NOW)}
        assert any(k.startswith("jpdrop:porsche_911") for k in keys)

    def test_a_small_japanese_move_does_not_alert(self, cfg, store):
        store.save_model_stats([
            ModelStats(day="2025-03-08", watchlist_key="porsche_911",
                       jp_median_price_chf=20_000),
            ModelStats(day=TODAY, watchlist_key="porsche_911",
                       jp_median_price_chf=19_800),
        ])
        assert not any(k.startswith("jpdrop") for k, _ in select_alerts(cfg, store, now=NOW))

    def test_a_big_fx_move_alerts(self, cfg, store):
        store.save_fx(FxRate(day="2025-03-08", usd_chf=0.75, jpy_chf=0.0053))
        keys = {k for k, _ in select_alerts(cfg, store, now=NOW)}
        assert any(k.startswith("fx:usd_chf") for k in keys)

    def test_a_quiet_fx_week_does_not_alert(self, cfg, store):
        store.save_fx(FxRate(day="2025-03-08", usd_chf=0.7995, jpy_chf=0.0053))
        assert not any(k.startswith("fx:") for k, _ in select_alerts(cfg, store, now=NOW))


class TestReport:
    def test_digest_renders_without_data(self, cfg, store):
        digest = build_digest(cfg, store)
        text = render_markdown(cfg, digest)
        assert "No positive-margin opportunities" in text

    def test_digest_includes_the_cost_breakdown(self, cfg, store):
        store.upsert_jp([jp(price=12_000)])
        store.upsert_ch([ch(f"c{i}", price=70_000) for i in range(8)])
        analyze(cfg, store, now=NOW)
        text = render_markdown(cfg, build_digest(cfg, store))
        assert "landed" in text and "RoRo" in text and "container" in text
        assert "Porsche" in text

    def test_weekly_portfolio_covers_every_tier(self, cfg, store):
        store.upsert_jp([jp(price=12_000)])
        store.upsert_ch([ch(f"c{i}", price=70_000) for i in range(8)])
        analyze(cfg, store, now=NOW)
        text = weekly_portfolio(cfg, build_digest(cfg, store))
        for tier in cfg.capital.tiers:
            assert tier.name in text

    def test_html_digest_is_self_contained(self, cfg, store):
        from nippon_margin.pipeline.report import render_html

        store.upsert_jp([jp(price=12_000)])
        store.upsert_ch([ch(f"c{i}", price=70_000) for i in range(8)])
        analyze(cfg, store, now=NOW)
        html = render_html(cfg, build_digest(cfg, store))
        assert html.startswith("<!doctype html>")
        assert "<style>" in html
        # No external assets: this is read from an Actions artifact offline.
        assert "<link" not in html and "<script" not in html


class TestTrendWindowAnchoring:
    """The window anchors to the data's own latest day, never to the clock.

    Anchoring on `today` meant that after a few days of failed runs every model
    reported "no movement" -- stale data disguised as a calm market.
    """

    def test_an_old_snapshot_pair_still_reports_its_move(self, store):
        store.save_model_stats([
            ModelStats(day="2019-01-01", watchlist_key="porsche_911", spread_chf=5_000),
            ModelStats(day="2019-01-15", watchlist_key="porsche_911", spread_chf=9_000),
        ])
        assert spread_moves(store, days=7)["porsche_911"] == 4_000

    def test_a_model_with_a_null_field_is_skipped(self, store):
        store.save_model_stats([
            ModelStats(day="2025-03-01", watchlist_key="porsche_911", spread_chf=None),
            ModelStats(day="2025-03-15", watchlist_key="porsche_911", spread_chf=9_000),
        ])
        assert "porsche_911" not in spread_moves(store, days=7)

    def test_a_malformed_day_does_not_crash(self, store):
        store.save_model_stats([
            ModelStats(day="not-a-date", watchlist_key="porsche_911", spread_chf=5_000),
            ModelStats(day="also-bad", watchlist_key="porsche_911", spread_chf=9_000),
        ])
        assert spread_moves(store, days=7) == {}


class TestSteering:
    """Switzerland: RHD is legal to import and homologate, but resale is poor.

    A 0.92 score haircut understates that, so explicitly right-hand-drive
    stock is dropped before it ever reaches the catalog. Stock whose steering
    is merely *unstated* is kept and flagged -- most sources omit the field on
    pages that are left-hand drive anyway.
    """

    def test_rhd_listings_never_reach_the_catalog(self, cfg, store):
        from nippon_margin.models import Steering

        listings = [
            jp("lhd", steering=Steering.LHD),
            jp("rhd", steering=Steering.RHD),
            jp("unknown", steering=Steering.UNKNOWN),
        ]
        kept = [lst for lst in listings if lst.steering is not Steering.RHD] \
            if cfg.risk.exclude_rhd else listings
        refs = {lst.source_ref for lst in kept}
        assert "rhd" not in refs
        assert {"lhd", "unknown"} <= refs

    def test_unstated_steering_is_flagged_rather_than_dropped(self, cfg):
        from nippon_margin.matching import risk_flags
        from nippon_margin.models import CompStats, Steering

        flags = risk_flags(cfg, jp(steering=Steering.UNKNOWN), CompStats(comp_count=10))
        assert any("Steering side" in f for f in flags)

    def test_the_exclusion_can_be_turned_off(self, cfg):
        off = cfg.model_copy(deep=True)
        off.risk.exclude_rhd = False
        assert off.risk.exclude_rhd is False
        assert cfg.risk.exclude_rhd is True


class TestOriginFilter:
    """Japanese exporters sell plenty of stock that is not in Japan. Korea is
    a different trade: different freight, different paperwork, and no auction
    grading, so condition cannot be verified the same way."""

    def test_only_allowed_origins_survive(self, cfg):
        assert cfg.risk.origin_allowed("JAPAN") is True
        assert cfg.risk.origin_allowed("SOUTH KOREA") is False

    def test_case_and_spacing_do_not_matter(self, cfg):
        assert cfg.risk.origin_allowed(" japan ") is True

    def test_an_unpublished_location_is_kept_by_default(self, cfg):
        """exportfrom.jp prints no location and sells Japanese stock only."""
        assert cfg.risk.origin_allowed(None) is True

    def test_unknowns_can_be_excluded(self, cfg):
        strict = cfg.model_copy(deep=True)
        strict.risk.allow_unknown_origin = False
        assert strict.risk.origin_allowed(None) is False

    def test_an_empty_allow_list_permits_anywhere(self, cfg):
        anywhere = cfg.model_copy(deep=True)
        anywhere.risk.allowed_origins = []
        assert anywhere.risk.origin_allowed("SOUTH KOREA") is True


class TestOriginResolution:
    """Exporters print a city, a region or a country with no consistency.
    BE FORWARD writes "Yokohama" and "Korea" in the same column, so a raw
    last-token read makes "YOKOHAMA" look like a country."""

    @pytest.mark.parametrize("location,expected", [
        ("Yokohama", "JAPAN"),
        ("Kyushu", "JAPAN"),
        ("Tokyo, JAPAN", "JAPAN"),
        ("Korea", "SOUTH KOREA"),
        ("Incheon, SOUTH KOREA", "SOUTH KOREA"),
    ])
    def test_places_resolve_to_countries(self, cfg, location, expected):
        assert cfg.risk.resolve_origin(location) == expected

    def test_an_unmapped_place_fails_the_allow_list(self, cfg):
        """The safe direction: lose stock rather than buy from somewhere
        unintended. The scrape log names unmapped origins so they can be added."""
        resolved = cfg.risk.resolve_origin("Busan")
        assert resolved == "BUSAN"
        assert cfg.risk.origin_allowed(resolved) is False

    def test_no_location_resolves_to_none(self, cfg):
        assert cfg.risk.resolve_origin(None) is None
        assert cfg.risk.resolve_origin("") is None

    def test_korean_stock_is_excluded_end_to_end(self, cfg):
        from nippon_margin.models import JpListing

        korean = JpListing(source="beforward", source_ref="k", make="BMW",
                           model="8 Series", location="Korea")
        japanese = JpListing(source="beforward", source_ref="j", make="BMW",
                             model="8 Series", location="Yokohama")
        kept = [
            lst for lst in (korean, japanese)
            if cfg.risk.origin_allowed(cfg.risk.resolve_origin(lst.location))
        ]
        assert [lst.source_ref for lst in kept] == ["j"]


class TestOriginFilterIsPerSource:
    """If a site states countries, a blank one is missing data -- not Japan.

    The listing-level rule alone would let a Korean car through on any source
    that happened to drop the field on one row. The assumption is only safe
    for a source that never publishes a location at all.
    """

    @staticmethod
    def _jp(source: str, ref: str, location: str | None) -> JpListing:
        return JpListing(
            source=source, source_ref=ref, make="Porsche", model="911",
            year=2013, mileage_km=60_000, price_usd=60_000,
            location=location, url=f"https://example.test/{source}/{ref}",
        )

    def test_a_source_that_never_states_a_location_is_assumed_japanese(self, cfg):
        """exportfrom.jp prints no location and sells Japanese stock only."""
        jp = [self._jp("exportfrom", "a", None), self._jp("exportfrom", "b", None)]
        kept, seen = filter_by_origin(cfg, jp)
        assert len(kept) == 2
        assert seen == {"not stated": 2}

    def test_a_blank_location_on_a_source_that_states_them_is_dropped(self, cfg):
        """The one that used to slip through: BE FORWARD publishes a location
        on every car, most of it Korean. A missing one is a scrape gap."""
        jp = [
            self._jp("beforward", "a", "Yokohama"),
            self._jp("beforward", "b", None),
            self._jp("beforward", "c", "Korea"),
        ]
        kept, _ = filter_by_origin(cfg, jp)
        assert [x.source_ref for x in kept] == ["a"]

    def test_sources_are_judged_independently(self, cfg):
        """One source stating locations must not disqualify another's blanks."""
        jp = [
            self._jp("beforward", "a", "Korea"),
            self._jp("beforward", "b", None),
            self._jp("exportfrom", "c", None),
        ]
        kept, _ = filter_by_origin(cfg, jp)
        assert [x.source_ref for x in kept] == ["c"]

    def test_a_stated_japanese_location_always_survives(self, cfg):
        jp = [
            self._jp("goonet", "a", "Aichi Japan"),
            self._jp("sbtjapan", "b", "Tokyo, JAPAN"),
        ]
        kept, _ = filter_by_origin(cfg, jp)
        assert len(kept) == 2

    def test_the_master_switch_still_drops_every_unknown(self, cfg):
        strict = cfg.model_copy(deep=True)
        strict.risk.allow_unknown_origin = False
        jp = [self._jp("exportfrom", "a", None), self._jp("goonet", "b", "Chiba Japan")]
        kept, _ = filter_by_origin(strict, jp)
        assert [x.source_ref for x in kept] == ["b"]

    def test_an_empty_allow_list_filters_nothing(self, cfg):
        anywhere = cfg.model_copy(deep=True)
        anywhere.risk.allowed_origins = []
        jp = [
            self._jp("beforward", "a", "Korea"),
            self._jp("beforward", "b", None),
        ]
        kept, seen = filter_by_origin(anywhere, jp)
        assert len(kept) == 2
        assert seen == {"SOUTH KOREA": 1, "not stated": 1}

    def test_what_it_saw_is_reported_before_filtering(self, cfg):
        """The log line is how an unmapped port gets noticed, so the tally has
        to count everything, including what is about to be dropped."""
        jp = [
            self._jp("beforward", "a", "Yokohama"),
            self._jp("beforward", "b", "Korea"),
            self._jp("beforward", "c", "Busan"),
        ]
        _, seen = filter_by_origin(cfg, jp)
        assert seen == {"JAPAN": 1, "SOUTH KOREA": 1, "BUSAN": 1}


class TestOriginResolutionShapes:
    """Every source writes a location its own way, and goo-net's has no comma."""

    @pytest.mark.parametrize("location,expected", [
        ("Aichi Japan", "JAPAN"),        # goo-net-exchange
        ("Chiba Japan", "JAPAN"),
        ("Tokyo, JAPAN", "JAPAN"),       # sbtjapan
        ("Yokohama", "JAPAN"),           # beforward
        ("Korea", "SOUTH KOREA"),
    ])
    def test_each_source_format_resolves(self, cfg, location, expected):
        assert cfg.risk.resolve_origin(location) == expected

    def test_the_last_word_rule_does_not_invent_countries(self, cfg):
        """`New Zealand` must not resolve on `Zealand`; only aliases match on
        the last word, and the fallback still reads the comma tail."""
        assert cfg.risk.resolve_origin("New Zealand") == "NEW ZEALAND"
        assert cfg.risk.origin_allowed(cfg.risk.resolve_origin("New Zealand")) is False
