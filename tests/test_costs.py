"""Cost engine tests.

Scraping is allowed to be flaky. These numbers are not: every one of them is
a franc that either exists or does not exist in the real world.
"""

import pytest

from nippon_margin.costs import capital_cost, compute_both, compute_landed
from nippon_margin.models import PriceTerms, ShippingMode

# The known-good calibration case: a G 63 quoted at $132,567 FOB should land
# at roughly CHF 125k with USD/CHF at 0.80.
G63_PRICE_USD = 132_567.0
G63_FX = 0.80


class TestKnownCase:
    def test_g63_lands_near_125k_roro(self, cfg):
        b = compute_landed(
            cfg,
            price_usd=G63_PRICE_USD,
            fx_usd_chf=G63_FX,
            mode=ShippingMode.RORO,
            watchlist_key="mercedes_g_class",
        )
        assert 120_000 <= b.landed_chf <= 130_000
        assert b.landed_chf == pytest.approx(125_964.54, abs=1.0)

    def test_g63_line_items(self, cfg):
        b = compute_landed(
            cfg, price_usd=G63_PRICE_USD, fx_usd_chf=G63_FX, mode=ShippingMode.RORO
        )
        assert b.fob_chf == pytest.approx(106_053.60, abs=0.01)
        assert b.freight_chf == pytest.approx(3_500.0)
        assert b.cif_chf == pytest.approx(109_553.60, abs=0.01)
        assert b.customs_duty_chf == 0.0
        assert b.automobilsteuer_chf == pytest.approx(109_553.60 * 0.04, abs=0.01)
        assert b.vat_chf == pytest.approx(109_553.60 * 1.04 * 0.081, abs=0.01)

    def test_container_is_cheaper_by_the_freight_delta_plus_its_tax(self, cfg):
        roro, cont = compute_both(cfg, price_usd=G63_PRICE_USD, fx_usd_chf=G63_FX)
        freight_delta = cfg.costs.shipping.roro_chf - cfg.costs.shipping.container_chf_per_car
        # Freight sits inside the customs value, so saving on it also saves the
        # 4% Automobilsteuer and the 8.1% VAT levied on top of it.
        expected = freight_delta * (1 + 0.04) * (1 + 0.081)
        assert (roro.landed_chf - cont.landed_chf) == pytest.approx(expected, abs=0.05)


class TestTaxChain:
    def test_vat_is_levied_on_cif_plus_automobilsteuer(self, cfg):
        b = compute_landed(cfg, price_usd=50_000, fx_usd_chf=0.9, mode=ShippingMode.RORO)
        assert b.vat_chf == pytest.approx(
            (b.cif_chf + b.automobilsteuer_chf) * cfg.costs.vat_pct, abs=0.01
        )
        # VAT must NOT be computed on the bare CIF.
        assert b.vat_chf > b.cif_chf * cfg.costs.vat_pct

    def test_automobilsteuer_is_levied_on_cif_only(self, cfg):
        b = compute_landed(cfg, price_usd=50_000, fx_usd_chf=0.9, mode=ShippingMode.RORO)
        assert b.automobilsteuer_chf == pytest.approx(
            b.cif_chf * cfg.costs.automobilsteuer_pct, abs=0.01
        )

    def test_landed_is_the_sum_of_its_parts(self, cfg):
        b = compute_landed(cfg, price_usd=77_000, fx_usd_chf=0.86, mode=ShippingMode.CONTAINER)
        total = (
            b.cif_chf
            + b.customs_duty_chf
            + b.automobilsteuer_chf
            + b.vat_chf
            + b.customs_clearance_chf
            + b.homologation_mfk_chf
            + b.agent_recon_buffer_chf
        )
        assert b.landed_chf == pytest.approx(total, abs=0.01)

    def test_cif_is_fob_plus_freight_plus_insurance(self, cfg):
        b = compute_landed(cfg, price_usd=30_000, fx_usd_chf=0.88, mode=ShippingMode.RORO)
        assert b.cif_chf == pytest.approx(b.fob_chf + b.freight_chf + b.insurance_chf, abs=0.01)

    def test_zero_duty_carries_a_note_telling_you_to_confirm_it(self, cfg):
        b = compute_landed(cfg, price_usd=30_000, fx_usd_chf=0.88, mode=ShippingMode.RORO)
        assert any("confirm the basis" in n.lower() for n in b.notes)

    def test_the_duty_note_names_the_actual_origin(self, cfg):
        b = compute_landed(cfg, price_usd=30_000, fx_usd_chf=0.88,
                           mode=ShippingMode.RORO, origin="SOUTH KOREA")
        assert any("SOUTH KOREA" in n for n in b.notes)

    def test_duty_applies_when_configured(self, cfg):
        dutied = cfg.model_copy(deep=True)
        dutied.costs.customs_duty_pct = 0.12
        b = compute_landed(dutied, price_usd=40_000, fx_usd_chf=0.9, mode=ShippingMode.RORO)
        assert b.customs_duty_chf == pytest.approx(b.cif_chf * 0.12, abs=0.01)


class TestPriceTerms:
    def test_cf_quote_does_not_double_count_freight(self, cfg):
        fob = compute_landed(
            cfg, price_usd=60_000, fx_usd_chf=0.9,
            mode=ShippingMode.RORO, price_terms=PriceTerms.FOB,
        )
        cf = compute_landed(
            cfg, price_usd=60_000, fx_usd_chf=0.9,
            mode=ShippingMode.RORO, price_terms=PriceTerms.CF,
        )
        assert cf.freight_chf == 0.0
        assert fob.freight_chf == cfg.costs.shipping.roro_chf
        assert cf.landed_chf < fob.landed_chf
        assert any("already in the asking price" in n for n in cf.notes)

    def test_cif_quote_adds_neither_freight_nor_insurance(self, cfg):
        insured = cfg.model_copy(deep=True)
        insured.costs.marine_insurance_pct = 0.01
        b = compute_landed(
            insured, price_usd=60_000, fx_usd_chf=0.9,
            mode=ShippingMode.RORO, price_terms=PriceTerms.CIF,
        )
        assert b.freight_chf == 0.0
        assert b.insurance_chf == 0.0
        assert b.cif_chf == pytest.approx(b.fob_chf, abs=0.01)


class TestInsurance:
    def test_insurance_is_a_pct_of_fob_plus_freight(self, cfg):
        insured = cfg.model_copy(deep=True)
        insured.costs.marine_insurance_pct = 0.005
        b = compute_landed(insured, price_usd=100_000, fx_usd_chf=0.9, mode=ShippingMode.RORO)
        assert b.insurance_chf == pytest.approx((b.fob_chf + b.freight_chf) * 0.005, abs=0.01)

    def test_default_config_charges_no_insurance(self, cfg):
        b = compute_landed(cfg, price_usd=100_000, fx_usd_chf=0.9, mode=ShippingMode.RORO)
        assert b.insurance_chf == 0.0


class TestPerModelOverrides:
    def test_911_uses_its_own_homologation_budget(self, cfg):
        p911 = compute_landed(
            cfg, price_usd=50_000, fx_usd_chf=0.9,
            mode=ShippingMode.RORO, watchlist_key="porsche_911",
        )
        macan = compute_landed(
            cfg, price_usd=50_000, fx_usd_chf=0.9,
            mode=ShippingMode.RORO, watchlist_key="porsche_macan",
        )
        assert p911.homologation_mfk_chf == 2500.0
        assert macan.homologation_mfk_chf == cfg.costs.homologation_mfk_chf
        assert p911.landed_chf - macan.landed_chf == pytest.approx(500.0, abs=0.01)

    def test_unknown_key_falls_back_to_the_default(self, cfg):
        b = compute_landed(
            cfg, price_usd=50_000, fx_usd_chf=0.9,
            mode=ShippingMode.RORO, watchlist_key="no_such_model",
        )
        assert b.homologation_mfk_chf == cfg.costs.homologation_mfk_chf


class TestFx:
    def test_landed_scales_with_fx_on_the_car_but_not_on_fixed_costs(self, cfg):
        cheap = compute_landed(cfg, price_usd=100_000, fx_usd_chf=0.80, mode=ShippingMode.RORO)
        dear = compute_landed(cfg, price_usd=100_000, fx_usd_chf=0.88, mode=ShippingMode.RORO)
        assert dear.landed_chf > cheap.landed_chf
        # A 10% FX move moves landed cost by less than 10%, because homologation,
        # freight and the recon buffer are CHF-denominated.
        assert (dear.landed_chf / cheap.landed_chf - 1) < 0.10

    def test_a_3pct_weaker_yen_widens_the_g63_spread(self, cfg):
        """The FX sensitivity the dashboard annotates."""
        base = compute_landed(
            cfg, price_usd=G63_PRICE_USD, fx_usd_chf=G63_FX, mode=ShippingMode.RORO
        )
        weaker = compute_landed(
            cfg, price_usd=G63_PRICE_USD, fx_usd_chf=G63_FX * 0.97, mode=ShippingMode.RORO
        )
        saving = base.landed_chf - weaker.landed_chf
        assert 3_000 < saving < 5_000


class TestValidation:
    @pytest.mark.parametrize("price", [0, -1, None])
    def test_bad_price_is_rejected(self, cfg, price):
        with pytest.raises(ValueError):
            compute_landed(cfg, price_usd=price, fx_usd_chf=0.9, mode=ShippingMode.RORO)

    @pytest.mark.parametrize("fx", [0, -0.5, None])
    def test_bad_fx_is_rejected(self, cfg, fx):
        with pytest.raises(ValueError):
            compute_landed(cfg, price_usd=10_000, fx_usd_chf=fx, mode=ShippingMode.RORO)


class TestCapitalCost:
    def test_interest_accrues_pro_rata(self, cfg):
        c = capital_cost(cfg, 100_000, 365)
        assert c == pytest.approx(100_000 * cfg.capital.annual_interest_rate, abs=0.01)
        assert capital_cost(cfg, 100_000, 90) == pytest.approx(c * 90 / 365, abs=0.01)

    def test_zero_holding_is_free(self, cfg):
        assert capital_cost(cfg, 100_000, 0) == 0.0

    def test_negative_inputs_are_clamped(self, cfg):
        assert capital_cost(cfg, -5, 90) == 0.0
        assert capital_cost(cfg, 100_000, -5) == 0.0


class TestOrigin:
    """A Japanese exporter's stock is not necessarily in Japan: 48 of 50 of
    SBT's LHD Porsches sit in Incheon. Freight, paperwork and grading follow
    the car, not the website."""

    def test_an_unconfigured_origin_falls_back_and_says_so(self, cfg):
        b = compute_landed(cfg, price_usd=30_000, fx_usd_chf=0.80,
                           mode=ShippingMode.RORO, origin="SOUTH KOREA")
        assert b.freight_chf == cfg.costs.shipping.roro_chf
        assert any("freight figure is the one quoted for JAPAN" in n for n in b.notes)

    def test_a_configured_origin_uses_its_own_rate_without_a_note(self, cfg):
        from nippon_margin.config import OriginShipping

        tuned = cfg.model_copy(deep=True)
        tuned.costs.shipping.by_origin = {
            "SOUTH KOREA": OriginShipping(roro_chf=2900.0, container_chf_per_car=2000.0)
        }
        b = compute_landed(tuned, price_usd=30_000, fx_usd_chf=0.80,
                           mode=ShippingMode.RORO, origin="SOUTH KOREA")
        assert b.freight_chf == 2900.0
        assert not any("quoted for" in n for n in b.notes)

    def test_the_assumed_origin_gets_no_warning(self, cfg):
        b = compute_landed(cfg, price_usd=30_000, fx_usd_chf=0.80,
                           mode=ShippingMode.RORO, origin="JAPAN")
        assert not any("quoted for" in n for n in b.notes)

    def test_origin_is_case_insensitive(self, cfg):
        a = compute_landed(cfg, price_usd=30_000, fx_usd_chf=0.80,
                           mode=ShippingMode.RORO, origin="japan")
        assert not any("quoted for" in n for n in a.notes)
