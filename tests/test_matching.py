"""Comp matcher and scoring tests."""

from datetime import datetime, timedelta, timezone

import pytest

from nippon_margin.matching import (
    build_opportunity,
    comp_stats,
    find_comps,
    liquidity_score,
    mark_duplicates,
    percentile,
    resolve_watchlist_key,
    risk_flags,
    seasonality_multiplier,
)
from nippon_margin.models import (
    ChListing,
    CompStats,
    JpListing,
    PricePoint,
    Steering,
)

NOW = datetime(2025, 3, 15, tzinfo=timezone.utc)


def jp(**kw) -> JpListing:
    base = dict(
        source="exportfrom",
        source_ref="1",
        make="Mercedes-Benz",
        model="G-Class",
        model_code="463276",
        year=2019,
        mileage_km=40_000,
        steering=Steering.LHD,
        price_usd=132_567,
        watchlist_key="mercedes_g_class",
        last_seen=NOW,
        first_seen=NOW,
    )
    base.update(kw)
    return JpListing(**base)


def ch(ref="c1", price=180_000, year=2019, km=40_000, **kw) -> ChListing:
    base = dict(
        source="autouncle",
        source_ref=ref,
        make="Mercedes-Benz",
        model="G-Class",
        variant="G 63 AMG",
        year=year,
        mileage_km=km,
        price_chf=price,
        watchlist_key="mercedes_g_class",
        last_seen=NOW,
        first_seen=NOW,
    )
    base.update(kw)
    return ChListing(**base)


class TestPercentile:
    def test_interpolates_between_points(self):
        assert percentile([10, 20, 30, 40], 0.25) == 17.5
        assert percentile([10, 20, 30, 40], 0.5) == 25.0
        assert percentile([10, 20, 30, 40], 0.75) == 32.5

    def test_single_value_and_empty(self):
        assert percentile([42.0], 0.25) == 42.0
        assert percentile([], 0.5) is None

    def test_p25_is_below_the_median(self):
        data = [100.0, 110.0, 130.0, 200.0, 400.0]
        assert percentile(data, 0.25) < percentile(data, 0.5) < percentile(data, 0.75)


class TestWatchlistResolution:
    def test_model_code_wins(self, cfg):
        assert resolve_watchlist_key(cfg, make="", model="", model_code="463276") == "mercedes_g_class"

    def test_alias_in_title(self, cfg):
        assert resolve_watchlist_key(cfg, make="Porsche", model="911 Carrera S") == "porsche_911"

    def test_german_alias(self, cfg):
        assert resolve_watchlist_key(cfg, make="Mercedes-Benz", model="G-Klasse") == "mercedes_g_class"

    def test_off_watchlist_returns_none(self, cfg):
        assert resolve_watchlist_key(cfg, make="Toyota", model="Corolla") is None

    def test_description_only_match_is_accepted(self, cfg):
        key = resolve_watchlist_key(
            cfg, make="Porsche", model="Coupe", description="Rare 997 Carrera S, full history"
        )
        assert key == "porsche_911"


class TestFindComps:
    def test_exact_match_is_a_comp(self, cfg):
        assert len(find_comps(cfg, jp(), [ch()], now=NOW)) == 1

    def test_year_outside_tolerance_is_rejected(self, cfg):
        pool = [ch("a", year=2018), ch("b", year=2020), ch("c", year=2016)]
        refs = {c.source_ref for c in find_comps(cfg, jp(), pool, now=NOW)}
        assert refs == {"a", "b"}

    def test_mileage_outside_30pct_is_rejected(self, cfg):
        pool = [
            ch("in_low", km=28_500),   # -29%
            ch("in_high", km=51_500),  # +29%
            ch("out", km=80_000),      # +100%
        ]
        refs = {c.source_ref for c in find_comps(cfg, jp(), pool, now=NOW)}
        assert refs == {"in_low", "in_high"}

    def test_different_model_is_rejected(self, cfg):
        other = ch("x", make="Porsche", model="Cayenne", variant="Cayenne S",
                   watchlist_key="porsche_cayenne")
        assert find_comps(cfg, jp(), [other], now=NOW) == []

    def test_comp_without_a_price_is_rejected(self, cfg):
        assert find_comps(cfg, jp(), [ch("np", price=None)], now=NOW) == []

    def test_stale_comp_is_rejected(self, cfg):
        old = ch("old", last_seen=NOW - timedelta(days=cfg.matching.max_comp_age_days + 5))
        assert find_comps(cfg, jp(), [old], now=NOW) == []

    def test_comp_with_unknown_mileage_is_rejected(self, cfg):
        assert find_comps(cfg, jp(), [ch("nk", km=None)], now=NOW) == []

    def test_unknown_jp_mileage_does_not_disqualify(self, cfg):
        comps = find_comps(cfg, jp(mileage_km=None), [ch("a", km=150_000)], now=NOW)
        assert len(comps) == 1

    def test_model_code_bridges_to_the_swiss_variant_name(self, cfg):
        """`463276` on the JP side, `G 63 AMG` on the CH side, no shared key."""
        unkeyed_jp = jp(watchlist_key=None, make="MERCEDES BENZ", model="G CLASS")
        unkeyed_ch = ch("v", watchlist_key=None, make="Mercedes-Benz",
                        model="G 63 AMG", variant=None)
        assert len(find_comps(cfg, unkeyed_jp, [unkeyed_ch], now=NOW)) == 1


class TestCompStats:
    def test_percentiles_and_count(self, cfg):
        comps = [ch(f"c{i}", price=p) for i, p in enumerate([150_000, 170_000, 190_000, 210_000])]
        s = comp_stats(comps)
        assert s.comp_count == 4
        assert s.swiss_median_ask == 180_000
        assert s.swiss_p25 == 165_000
        assert s.swiss_p75 == 195_000

    def test_days_listed_median(self, cfg):
        comps = [ch("a", days_listed=10), ch("b", days_listed=50), ch("c", days_listed=90)]
        assert comp_stats(comps).median_days_listed == 50.0

    def test_price_cut_fraction_uses_only_comps_with_history(self, cfg):
        cut = ch("cut", price_change_history=[
            PricePoint(at=NOW - timedelta(days=20), price=200_000),
            PricePoint(at=NOW, price=180_000),
        ])
        firm = ch("firm", price_change_history=[
            PricePoint(at=NOW - timedelta(days=20), price=180_000),
            PricePoint(at=NOW, price=180_000),
        ])
        no_history = ch("none")
        s = comp_stats([cut, firm, no_history])
        assert s.comp_count == 3
        assert s.pct_with_price_cut == 0.5

    def test_empty_pool(self, cfg):
        s = comp_stats([])
        assert s.comp_count == 0 and s.swiss_p25 is None


class TestLiquidityScore:
    def test_bounded_zero_to_one(self, cfg):
        for s in [
            CompStats(comp_count=0),
            CompStats(comp_count=100, median_days_listed=1, pct_with_price_cut=0.0),
            CompStats(comp_count=1, median_days_listed=900, pct_with_price_cut=1.0),
        ]:
            assert 0.0 <= liquidity_score(cfg, s) <= 1.0

    def test_fast_deep_market_beats_slow_thin_one(self, cfg):
        good = CompStats(comp_count=15, median_days_listed=15, pct_with_price_cut=0.05)
        bad = CompStats(comp_count=2, median_days_listed=200, pct_with_price_cut=0.9)
        assert liquidity_score(cfg, good) > liquidity_score(cfg, bad)

    def test_days_reference_maps_to_a_neutral_half(self, cfg):
        s = CompStats(comp_count=0, median_days_listed=cfg.scoring.liquidity.days_listed_reference,
                      pct_with_price_cut=0.5)
        # comp_count 0 contributes 0, the other two contribute 0.5 each.
        w = cfg.scoring.liquidity.weights
        expected = (w.days_listed * 0.5 + w.price_cuts * 0.5) / (
            w.comp_count + w.days_listed + w.price_cuts
        )
        assert liquidity_score(cfg, s) == pytest.approx(expected, abs=1e-4)

    def test_missing_signals_fall_back_to_neutral(self, cfg):
        s = CompStats(comp_count=6)
        assert 0.2 < liquidity_score(cfg, s) < 0.8

    def test_more_price_cuts_lower_the_score(self, cfg):
        a = CompStats(comp_count=6, median_days_listed=40, pct_with_price_cut=0.1)
        b = CompStats(comp_count=6, median_days_listed=40, pct_with_price_cut=0.8)
        assert liquidity_score(cfg, a) > liquidity_score(cfg, b)


class TestRiskFlags:
    def test_rhd_is_flagged(self, cfg):
        flags = risk_flags(cfg, jp(steering=Steering.RHD), CompStats(comp_count=10))
        assert any("RHD" in f for f in flags)

    def test_lhd_is_not_flagged_for_steering(self, cfg):
        flags = risk_flags(cfg, jp(steering=Steering.LHD), CompStats(comp_count=10))
        assert not any("RHD" in f or "Steering side" in f for f in flags)

    def test_unknown_steering_is_flagged(self, cfg):
        flags = risk_flags(cfg, jp(steering=Steering.UNKNOWN), CompStats(comp_count=10))
        assert any("Steering side" in f for f in flags)

    def test_low_auction_grade_is_flagged(self, cfg):
        flags = risk_flags(cfg, jp(auction_grade=3.0), CompStats(comp_count=10))
        assert any("grade" in f.lower() for f in flags)

    def test_repair_history_is_flagged(self, cfg):
        flags = risk_flags(cfg, jp(repair_history=True), CompStats(comp_count=10))
        assert any("Repair history" in f for f in flags)

    def test_thin_comp_set_is_flagged(self, cfg):
        flags = risk_flags(cfg, jp(), CompStats(comp_count=1))
        assert any("Thin comp set" in f for f in flags)

    def test_model_specific_flag_from_the_config_table(self, cfg):
        listing = jp(make="Porsche", model="911", variant="997 Carrera S",
                     model_code="997M9701", watchlist_key="porsche_911")
        flags = risk_flags(cfg, listing, CompStats(comp_count=10))
        assert any("bore scoring" in f.lower() for f in flags)

    def test_e92_m3_gets_the_rod_bearing_flag(self, cfg):
        listing = jp(make="BMW", model="M3", model_code="E92", watchlist_key="bmw_m3_e92")
        flags = risk_flags(cfg, listing, CompStats(comp_count=10))
        assert any("rod bearing" in f.lower() for f in flags)

    def test_over_mileage_limit_is_flagged(self, cfg):
        flags = risk_flags(cfg, jp(mileage_km=300_000), CompStats(comp_count=10))
        assert any("over the" in f for f in flags)

    def test_flags_are_deduplicated(self, cfg):
        flags = risk_flags(cfg, jp(), CompStats(comp_count=10))
        assert len(flags) == len(set(flags))


class TestSeasonality:
    def test_convertible_scores_higher_in_spring(self, cfg):
        sl = jp(make="Mercedes-Benz", model="SL", model_code="R230", watchlist_key="mercedes_sl")
        assert seasonality_multiplier(cfg, sl, month=4) > 1.0
        assert seasonality_multiplier(cfg, sl, month=11) == 1.0

    def test_g_class_scores_higher_before_the_snow(self, cfg):
        assert seasonality_multiplier(cfg, jp(), month=10) > 1.0
        assert seasonality_multiplier(cfg, jp(), month=4) == 1.0

    def test_bodyless_model_is_neutral(self, cfg):
        assert seasonality_multiplier(cfg, jp(watchlist_key=None), month=4) == 1.0

    def test_disabling_seasonality_neutralises_it(self, cfg):
        off = cfg.model_copy(deep=True)
        off.scoring.seasonality.enabled = False
        assert seasonality_multiplier(off, jp(), month=10) == 1.0


class TestBuildOpportunity:
    def test_margin_follows_the_p25_formula(self, cfg):
        pool = [ch(f"c{i}", price=p) for i, p in enumerate([150_000, 170_000, 190_000, 210_000])]
        opp = build_opportunity(cfg, jp(), pool, fx_usd_chf=0.80, now=NOW)
        assert opp is not None
        assert opp.comps.swiss_p25 == 165_000
        assert opp.realizable_chf == pytest.approx(165_000 * 0.93, abs=0.01)
        assert opp.gross_margin_chf == pytest.approx(
            165_000 * 0.93 - opp.landed_roro.landed_chf, abs=0.01
        )
        assert opp.margin_pct == pytest.approx(
            opp.gross_margin_chf / opp.landed_roro.landed_chf, abs=1e-4
        )

    def test_opportunity_score_is_margin_x_liquidity_over_capital_weight(self, cfg):
        pool = [ch(f"c{i}", price=200_000, days_listed=30) for i in range(6)]
        opp = build_opportunity(cfg, jp(), pool, fx_usd_chf=0.80, now=NOW)
        expected = (
            opp.margin_pct
            * opp.liquidity_score
            / opp.capital_weight
            * opp.seasonality_multiplier
            * opp.risk_multiplier
        )
        assert opp.opportunity_score == pytest.approx(expected, abs=1e-6)

    def test_both_shipping_scenarios_are_present(self, cfg):
        opp = build_opportunity(cfg, jp(), [ch()], fx_usd_chf=0.80, now=NOW)
        assert opp.landed_roro.landed_chf > opp.landed_container.landed_chf

    def test_capital_cost_is_subtracted_for_net_margin(self, cfg):
        pool = [ch(f"c{i}", price=250_000, days_listed=60) for i in range(5)]
        opp = build_opportunity(cfg, jp(), pool, fx_usd_chf=0.80, now=NOW)
        assert opp.expected_holding_days == 60
        assert opp.capital_cost_chf > 0
        assert opp.net_margin_chf == pytest.approx(
            opp.gross_margin_chf - opp.capital_cost_chf, abs=0.01
        )

    def test_cheaper_car_at_equal_margin_pct_scores_higher(self, cfg):
        """The capital-weight divisor is the point of the headline metric."""
        cheap_jp = jp(source_ref="cheap", price_usd=25_000, watchlist_key="mercedes_g_class")
        dear_jp = jp(source_ref="dear", price_usd=125_000)

        cheap_pool = [ch(f"a{i}", price=45_000, days_listed=30) for i in range(6)]
        dear_pool = [ch(f"b{i}", price=225_000, days_listed=30) for i in range(6)]

        cheap = build_opportunity(cfg, cheap_jp, cheap_pool, fx_usd_chf=0.80, now=NOW)
        dear = build_opportunity(cfg, dear_jp, dear_pool, fx_usd_chf=0.80, now=NOW)

        assert cheap.margin_pct > 0 and dear.margin_pct > 0
        assert cheap.capital_weight < dear.capital_weight
        assert cheap.opportunity_score > dear.opportunity_score

    def test_no_comps_yields_no_score_but_still_a_landed_cost(self, cfg):
        opp = build_opportunity(cfg, jp(), [], fx_usd_chf=0.80, now=NOW)
        assert opp is not None
        assert opp.comps.comp_count == 0
        assert opp.gross_margin_chf is None
        assert opp.opportunity_score == 0.0
        assert opp.landed_roro.landed_chf > 0

    def test_negative_margin_scores_zero(self, cfg):
        pool = [ch(f"c{i}", price=90_000) for i in range(5)]
        opp = build_opportunity(cfg, jp(), pool, fx_usd_chf=0.80, now=NOW)
        assert opp.gross_margin_chf < 0
        assert opp.opportunity_score == 0.0

    def test_unpriced_listing_is_skipped(self, cfg):
        assert build_opportunity(cfg, jp(price_usd=None), [ch()], fx_usd_chf=0.8, now=NOW) is None

    def test_capital_tier_is_assigned(self, cfg):
        opp = build_opportunity(cfg, jp(), [ch()], fx_usd_chf=0.80, now=NOW)
        assert opp.capital_tier == "large"
        cheap = build_opportunity(cfg, jp(price_usd=15_000), [ch()], fx_usd_chf=0.80, now=NOW)
        assert cheap.capital_tier == "small"

    def test_risk_flags_haircut_the_score(self, cfg):
        pool = [ch(f"c{i}", price=220_000, days_listed=30) for i in range(6)]
        clean = build_opportunity(cfg, jp(), pool, fx_usd_chf=0.80, now=NOW)
        risky = build_opportunity(
            cfg, jp(source_ref="r", repair_history=True, steering=Steering.RHD),
            pool, fx_usd_chf=0.80, now=NOW,
        )
        assert risky.risk_multiplier < clean.risk_multiplier
        assert risky.opportunity_score < clean.opportunity_score


class TestDedupe:
    def test_cheapest_of_a_chassis_group_wins(self):
        a = jp(source="exportfrom", source_ref="a", price_usd=100_000, chassis_no="WDB4632761X123456")
        b = jp(source="beforward", source_ref="b", price_usd=95_000, chassis_no="WDB463276-1X12****")
        c = jp(source="sbtjapan", source_ref="c", price_usd=110_000, chassis_no="WDB4632761X123456")

        marks = mark_duplicates([a, b, c])
        assert marks[b.doc_id] == (None, True)
        assert marks[a.doc_id] == (b.doc_id, False)
        assert marks[c.doc_id] == (b.doc_id, False)

    def test_listings_without_a_chassis_are_all_kept(self):
        a = jp(source_ref="a", chassis_no=None)
        b = jp(source="beforward", source_ref="b", chassis_no=None)
        marks = mark_duplicates([a, b])
        assert all(v == (None, True) for v in marks.values())

    def test_different_chassis_are_not_merged(self):
        a = jp(source_ref="a", chassis_no="WDB4632761X111111", price_usd=100_000)
        b = jp(source="beforward", source_ref="b", chassis_no="WDB4633491X222222", price_usd=90_000)
        marks = mark_duplicates([a, b])
        assert marks[a.doc_id] == (None, True)
        assert marks[b.doc_id] == (None, True)

    def test_short_or_junk_chassis_is_ignored(self):
        a = jp(source_ref="a", chassis_no="---")
        assert mark_duplicates([a])[a.doc_id] == (None, True)


class TestTokenBoundaryMatching:
    """Substring matching poisons the comp set; these are the cases that bite."""

    def test_sl_does_not_match_slk(self, cfg):
        assert resolve_watchlist_key(cfg, make="Mercedes-Benz", model="SLK 200") != "mercedes_sl"
        assert resolve_watchlist_key(cfg, make="Mercedes-Benz", model="SL 500") == "mercedes_sl"

    def test_quattroporte_is_not_a_granturismo(self, cfg):
        """A `Sport GT S` Quattroporte used to match the GranTurismo watch entry."""
        key = resolve_watchlist_key(
            cfg, make="Maserati", model="Quattroporte Sport GT S"
        )
        assert key != "maserati_granturismo"

    def test_a_real_granturismo_still_matches(self, cfg):
        assert resolve_watchlist_key(
            cfg, make="Maserati", model="GranTurismo Sport"
        ) == "maserati_granturismo"

    def test_comp_matching_rejects_a_token_lookalike(self, cfg):
        sl = jp(make="Mercedes-Benz", model="SL", model_code="R230",
                watchlist_key=None, year=2006, mileage_km=80_000)
        slk = ch("slk", make="Mercedes-Benz", model="SLK 200", variant=None,
                 watchlist_key=None, year=2006, km=80_000)
        assert find_comps(cfg, sl, [slk], now=NOW) == []
