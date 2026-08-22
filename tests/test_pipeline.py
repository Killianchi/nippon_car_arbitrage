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
