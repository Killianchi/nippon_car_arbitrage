"""SQLite store round-trips.

The Firestore backend implements the same contract; these tests pin the
behaviour both must have -- above all that a re-run is idempotent and that
`first_seen` survives, because how long a car has been sitting is itself a
signal.
"""

from datetime import UTC, datetime, timedelta

import pytest

from nippon_margin.models import (
    AdapterResult,
    ChListing,
    FxRate,
    JpListing,
    ModelStats,
    RunRecord,
)
from nippon_margin.store.sqlite_store import SqliteStore

NOW = datetime(2025, 3, 15, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "test.db")
    yield s
    s.close()


def jp(ref="a", price=50_000.0, seen=NOW, **kw):
    return JpListing(source="exportfrom", source_ref=ref, make="Porsche", model="911",
                     year=2015, price_usd=price, watchlist_key="porsche_911",
                     first_seen=seen, last_seen=seen, **kw)


def ch(ref="c", price=90_000.0, seen=NOW, **kw):
    return ChListing(source="autouncle", source_ref=ref, make="Porsche", model="911",
                     year=2015, price_chf=price, watchlist_key="porsche_911",
                     first_seen=seen, last_seen=seen, **kw)


class TestUpsert:
    def test_insert_then_update(self, store):
        assert store.upsert_jp([jp()]) == (1, 0)
        assert store.upsert_jp([jp()]) == (0, 1)
        assert len(store.active_jp()) == 1

    def test_first_seen_survives_an_update(self, store):
        store.upsert_jp([jp(seen=NOW)])
        later = NOW + timedelta(days=10)
        store.upsert_jp([jp(seen=later)])
        stored = store.active_jp()[0]
        assert stored.first_seen.date() == NOW.date()
        assert stored.last_seen.date() == later.date()

    def test_both_sides_are_stored_separately(self, store):
        store.upsert_jp([jp()])
        store.upsert_ch([ch()])
        assert len(store.active_jp()) == 1
        assert len(store.active_ch()) == 1

    def test_full_payload_round_trips(self, store):
        original = jp(mileage_km=42_000, auction_grade=4.5, chassis_no="WDB1234567890",
                      image_urls=["https://x/1.jpg"], model_code="997M9701")
        store.upsert_jp([original])
        restored = store.active_jp()[0]
        assert restored.mileage_km == 42_000
        assert restored.auction_grade == 4.5
        assert restored.model_code == "997M9701"
        assert restored.image_urls == ["https://x/1.jpg"]


class TestDelisting:
    def test_stale_listings_are_marked_delisted(self, store):
        store.upsert_jp([jp("old", seen=NOW - timedelta(days=10)), jp("fresh", seen=NOW)])
        marked = store.mark_delisted(before=NOW - timedelta(days=3))
        assert marked == 1
        assert {x.source_ref for x in store.active_jp()} == {"fresh"}

    def test_a_returning_listing_is_reactivated(self, store):
        store.upsert_jp([jp("x", seen=NOW - timedelta(days=10))])
        store.mark_delisted(before=NOW - timedelta(days=3))
        assert store.active_jp() == []
        store.upsert_jp([jp("x", seen=NOW)])
        assert len(store.active_jp()) == 1


class TestPriceHistory:
    def test_a_price_change_is_recorded(self, store):
        store.upsert_jp([jp(price=50_000, seen=NOW)])
        store.upsert_jp([jp(price=45_000, seen=NOW + timedelta(days=1))])
        history = store.price_history(jp().doc_id)
        assert [p for _, p in history] == [50_000, 45_000]

    def test_an_unchanged_price_adds_no_point(self, store):
        store.upsert_jp([jp(price=50_000, seen=NOW)])
        store.upsert_jp([jp(price=50_000, seen=NOW + timedelta(days=1))])
        assert len(store.price_history(jp().doc_id)) == 1


class TestFx:
    def test_latest_returns_the_newest_day(self, store):
        store.save_fx(FxRate(day="2025-03-10", usd_chf=0.88, jpy_chf=0.0059))
        store.save_fx(FxRate(day="2025-03-14", usd_chf=0.80, jpy_chf=0.0053))
        assert store.latest_fx().day == "2025-03-14"

    def test_saving_the_same_day_twice_overwrites(self, store):
        store.save_fx(FxRate(day="2025-03-14", usd_chf=0.88, jpy_chf=0.0059))
        store.save_fx(FxRate(day="2025-03-14", usd_chf=0.80, jpy_chf=0.0053))
        assert len(store.load_fx(days=10)) == 1
        assert store.latest_fx().usd_chf == 0.80

    def test_empty_store_has_no_rate(self, store):
        assert store.latest_fx() is None


class TestOpportunities:
    def test_saving_replaces_the_previous_set(self, cfg, store):
        from nippon_margin.matching import build_opportunity

        first = build_opportunity(cfg, jp("a"), [ch()], fx_usd_chf=0.8, now=NOW)
        store.save_opportunities([first])
        second = build_opportunity(cfg, jp("b"), [ch()], fx_usd_chf=0.8, now=NOW)
        store.save_opportunities([second])
        stored = store.load_opportunities()
        # A car that sold overnight must not linger in the dashboard.
        assert len(stored) == 1
        assert stored[0].jp_doc_id == jp("b").doc_id

    def test_loaded_in_score_order(self, cfg, store):
        from nippon_margin.matching import build_opportunity

        cheap = build_opportunity(cfg, jp("cheap", price=20_000),
                                  [ch(f"c{i}", price=90_000) for i in range(6)],
                                  fx_usd_chf=0.8, now=NOW)
        dear = build_opportunity(cfg, jp("dear", price=95_000),
                                 [ch(f"d{i}", price=110_000) for i in range(6)],
                                 fx_usd_chf=0.8, now=NOW)
        store.save_opportunities([dear, cheap])
        scores = [o.opportunity_score for o in store.load_opportunities()]
        assert scores == sorted(scores, reverse=True)


class TestRunsAndAlerts:
    def test_run_round_trips_with_adapter_detail(self, store):
        run = RunRecord(
            id="20250315T050000Z", started_at=NOW, jp_count=10, ch_count=20,
            adapters=[AdapterResult(source="exportfrom", ok=True, count=10),
                      AdapterResult(source="carused", ok=False, error="403")],
            errors=["carused: 403"],
        )
        store.save_run(run)
        restored = store.recent_runs(limit=5)[0]
        assert restored.jp_count == 10
        assert len(restored.adapters) == 2
        assert restored.adapters[1].error == "403"

    def test_alert_cooldown_bookkeeping(self, store):
        assert store.alert_sent_at("opp:x") is None
        store.mark_alert_sent("opp:x", NOW)
        assert store.alert_sent_at("opp:x").date() == NOW.date()

    def test_alert_keys_with_unsafe_characters(self, store):
        key = "opp:https://example.com/a/b?c=1"
        store.mark_alert_sent(key, NOW)
        assert store.alert_sent_at(key) is not None


class TestModelStats:
    def test_one_row_per_day_per_model(self, store):
        store.save_model_stats([
            ModelStats(day="2025-03-14", watchlist_key="porsche_911", jp_count=3),
            ModelStats(day="2025-03-15", watchlist_key="porsche_911", jp_count=5),
        ])
        # Re-saving the same day must overwrite, not duplicate.
        store.save_model_stats([
            ModelStats(day="2025-03-15", watchlist_key="porsche_911", jp_count=7),
        ])
        rows = store.load_model_stats(watchlist_key="porsche_911")
        assert len(rows) == 2
        assert max(rows, key=lambda r: r.day).jp_count == 7

    def test_filtering_by_model(self, store):
        store.save_model_stats([
            ModelStats(day="2025-03-15", watchlist_key="porsche_911"),
            ModelStats(day="2025-03-15", watchlist_key="mercedes_g_class"),
        ])
        assert len(store.load_model_stats(watchlist_key="porsche_911")) == 1
        assert len(store.load_model_stats()) == 2
