"""Landed cost engine.

Turns a Japanese FOB asking price into the all-in CHF cost of the same car
sitting on Swiss plates, ready to sell.

The chain, in the order the Swiss customs computation actually applies:

    FOB (USD)  x  USD/CHF               -> fob_chf
    + freight (RoRo or container share) -> and marine insurance
    = CIF                               <- the customs value
    + customs duty       (0% under the Japan-CH FTA, *with* a certificate
                          of origin -- otherwise you pay MFN)
    + Automobilsteuer    4%   of CIF
    + VAT                8.1% of (CIF + Automobilsteuer)
    + customs clearance / forwarder handling
    + homologation: COC, Form 13.20A, MFK, small fixes
    + agent + reconditioning buffer
    = landed_chf

Cost of capital is deliberately *not* part of landed cost: it is a function
of how long you hold the car, so it is subtracted from margin instead (see
`capital_cost`).
"""

from __future__ import annotations

from .config import Config
from .models import CostBreakdown, PriceTerms, ShippingMode

__all__ = ["compute_landed", "compute_both", "capital_cost", "freight_for"]


def freight_for(cfg: Config, mode: ShippingMode, origin: str | None = None) -> float:
    """Per-car freight in CHF for a shipping scenario, from a given country.

    Origin matters: a Japanese exporter's stock is not necessarily in Japan.
    SBT lists most of its LHD Porsches in Incheon, and Incheon is not
    Yokohama for either the freight rate or the paperwork.
    """
    roro, container, _ = cfg.costs.shipping.for_origin(origin)
    return float(roro if mode is ShippingMode.RORO else container)


def compute_landed(
    cfg: Config,
    *,
    price_usd: float,
    fx_usd_chf: float,
    mode: ShippingMode,
    price_terms: PriceTerms = PriceTerms.FOB,
    watchlist_key: str | None = None,
    origin: str | None = None,
) -> CostBreakdown:
    """Full cost breakdown for one car under one shipping scenario.

    `price_terms` matters: a C&F / CIF quote already contains ocean freight,
    so adding our freight assumption on top would double-count it.
    """
    if price_usd is None or price_usd <= 0:
        raise ValueError("price_usd must be positive")
    if fx_usd_chf is None or fx_usd_chf <= 0:
        raise ValueError("fx_usd_chf must be positive")

    c = cfg.costs
    notes: list[str] = []

    fob_chf = price_usd * fx_usd_chf

    freight_included = price_terms in (PriceTerms.CF, PriceTerms.CIF)
    if freight_included:
        freight_chf = 0.0
        notes.append(
            f"Quoted {price_terms.value}: ocean freight already in the asking price, "
            f"so the {mode.value} freight assumption is not added."
        )
    else:
        freight_chf = freight_for(cfg, mode, origin)
        _, _, using_default = cfg.costs.shipping.for_origin(origin)
        if origin and origin.upper() != cfg.risk.assumed_origin and using_default:
            notes.append(
                f"Car is in {origin}, but the freight figure is the one quoted for "
                f"{cfg.risk.assumed_origin}. Set costs.shipping.by_origin['{origin.upper()}'] "
                f"once you have a real rate."
            )
        if mode is ShippingMode.CONTAINER:
            notes.append(
                f"Container rate assumes {c.shipping.container_cars_per_load}-car consolidation; "
                "shipping alone costs the RoRo figure."
            )

    if price_terms is PriceTerms.CIF:
        insurance_chf = 0.0
    else:
        insurance_chf = (fob_chf + freight_chf) * c.marine_insurance_pct

    cif_chf = fob_chf + freight_chf + insurance_chf

    customs_duty_chf = cif_chf * c.customs_duty_pct
    if c.customs_duty_pct == 0 and c.customs_duty_requires_certificate_of_origin:
        notes.append(
            f"Duty modelled at {c.customs_duty_pct:.0%}. Confirm the basis with your "
            f"forwarder -- it is not the same question as which FTA applies, and the "
            f"answer may differ for a car shipping from {origin or cfg.risk.assumed_origin}."
        )

    automobilsteuer_chf = cif_chf * c.automobilsteuer_pct
    vat_chf = (cif_chf + automobilsteuer_chf) * c.vat_pct

    homologation_chf = cfg.homologation_for(watchlist_key)

    landed = (
        cif_chf
        + customs_duty_chf
        + automobilsteuer_chf
        + vat_chf
        + c.customs_clearance_chf
        + homologation_chf
        + c.agent_recon_buffer_chf
    )

    return CostBreakdown(
        mode=mode,
        price_usd=float(price_usd),
        fx_usd_chf=float(fx_usd_chf),
        fob_chf=round(fob_chf, 2),
        freight_chf=round(freight_chf, 2),
        insurance_chf=round(insurance_chf, 2),
        cif_chf=round(cif_chf, 2),
        customs_duty_chf=round(customs_duty_chf, 2),
        automobilsteuer_chf=round(automobilsteuer_chf, 2),
        vat_chf=round(vat_chf, 2),
        customs_clearance_chf=round(c.customs_clearance_chf, 2),
        homologation_mfk_chf=round(homologation_chf, 2),
        agent_recon_buffer_chf=round(c.agent_recon_buffer_chf, 2),
        landed_chf=round(landed, 2),
        notes=notes,
    )


def compute_both(
    cfg: Config,
    *,
    price_usd: float,
    fx_usd_chf: float,
    price_terms: PriceTerms = PriceTerms.FOB,
    watchlist_key: str | None = None,
    origin: str | None = None,
) -> tuple[CostBreakdown, CostBreakdown]:
    """(RoRo, container) breakdowns -- both scenarios are always visible."""
    kw = dict(
        price_usd=price_usd,
        fx_usd_chf=fx_usd_chf,
        price_terms=price_terms,
        watchlist_key=watchlist_key,
        origin=origin,
    )
    return (
        compute_landed(cfg, mode=ShippingMode.RORO, **kw),
        compute_landed(cfg, mode=ShippingMode.CONTAINER, **kw),
    )


def capital_cost(cfg: Config, landed_chf: float, holding_days: int) -> float:
    """Interest on the money tied up in the car for the expected holding period."""
    if landed_chf <= 0 or holding_days <= 0:
        return 0.0
    return round(landed_chf * cfg.capital.annual_interest_rate * (holding_days / 365.0), 2)
