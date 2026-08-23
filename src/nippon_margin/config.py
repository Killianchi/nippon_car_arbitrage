"""Typed configuration loaded from config.yaml.

Business parameters are never hardcoded: if you find a number in the engine
that is not on a `*Config` object, that is a bug.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_CONFIG_PATH = Path(os.environ.get("NIPPON_CONFIG", "config.yaml"))


class Base(BaseModel):
    model_config = ConfigDict(extra="allow")


class OriginShipping(Base):
    roro_chf: float | None = None
    container_chf_per_car: float | None = None


class ShippingConfig(Base):
    roro_chf: float = 3500.0
    container_chf_per_car: float = 2400.0
    container_cars_per_load: int = 3
    #: Per-origin freight overrides, keyed by upper-case country. Japanese
    #: exporters sell a lot of stock that is not in Japan, and the rates above
    #: were quoted for Japan.
    by_origin: dict[str, OriginShipping] = Field(default_factory=dict)

    def for_origin(self, origin: str | None) -> tuple[float, float, bool]:
        """(roro, container_per_car, is_default) for a country."""
        if origin:
            override = self.by_origin.get(origin.strip().upper())
            if override:
                return (
                    override.roro_chf if override.roro_chf is not None else self.roro_chf,
                    override.container_chf_per_car
                    if override.container_chf_per_car is not None
                    else self.container_chf_per_car,
                    False,
                )
        return self.roro_chf, self.container_chf_per_car, True


class CostsConfig(Base):
    shipping: ShippingConfig = Field(default_factory=ShippingConfig)
    marine_insurance_pct: float = 0.0
    customs_duty_pct: float = 0.0
    customs_duty_requires_certificate_of_origin: bool = True
    automobilsteuer_pct: float = 0.04
    vat_pct: float = 0.081
    homologation_mfk_chf: float = 2000.0
    agent_recon_buffer_chf: float = 800.0
    customs_clearance_chf: float = 0.0


class CapitalTier(Base):
    name: str
    max_chf: float | None = None


class CapitalConfig(Base):
    annual_interest_rate: float = 0.06
    default_holding_days: int = 90
    tiers: list[CapitalTier] = Field(
        default_factory=lambda: [
            CapitalTier(name="small", max_chf=30000),
            CapitalTier(name="mid", max_chf=80000),
            CapitalTier(name="large", max_chf=None),
        ]
    )

    def tier_for(self, landed_chf: float) -> str:
        for tier in self.tiers:
            if tier.max_chf is None or landed_chf < tier.max_chf:
                return tier.name
        return self.tiers[-1].name if self.tiers else "unknown"


class MatchingConfig(Base):
    year_tolerance: int = 1
    mileage_tolerance_pct: float = 0.30
    min_comps_for_confidence: int = 3
    realization_factor: float = 0.93
    max_comp_age_days: int = 120
    match_trim: bool = True
    trims: list[str] = Field(default_factory=list)
    #: Flag a comp set whose p75/p25 reaches this ratio. Set 0 to disable.
    comp_spread_warn_ratio: float = 1.30


class LiquidityWeights(Base):
    comp_count: float = 0.35
    days_listed: float = 0.40
    price_cuts: float = 0.25


class LiquidityConfig(Base):
    comp_count_saturation: int = 12
    days_listed_reference: float = 60.0
    weights: LiquidityWeights = Field(default_factory=LiquidityWeights)


class SeasonalityRule(Base):
    months: list[int] = Field(default_factory=list)
    multiplier: float = 1.0


class SeasonalityConfig(Base):
    enabled: bool = True
    convertible: SeasonalityRule = Field(default_factory=SeasonalityRule)
    offroad_4x4: SeasonalityRule = Field(default_factory=SeasonalityRule)

    def rules(self) -> dict[str, SeasonalityRule]:
        return {"convertible": self.convertible, "offroad_4x4": self.offroad_4x4}


class ScoringConfig(Base):
    liquidity: LiquidityConfig = Field(default_factory=LiquidityConfig)
    capital_weight_reference_chf: float = 50000.0
    capital_weight_exponent: float = 1.0
    seasonality: SeasonalityConfig = Field(default_factory=SeasonalityConfig)


class RiskConfig(Base):
    #: Country the cost assumptions were quoted for. Anything else is flagged.
    assumed_origin: str = "JAPAN"
    min_auction_grade: float = 4.0
    penalise_rhd: bool = True
    #: Drop right-hand-drive listings outright rather than merely flagging
    #: them. A Swiss buyer discounts RHD far harder than a 0.92 score haircut
    #: implies. Listings whose steering is simply *unstated* are kept and
    #: flagged -- most sources omit the field on pages that are LHD anyway.
    exclude_rhd: bool = True
    flag_penalty: float = 0.92


class EmailConfig(Base):
    smtp_host: str = ""
    smtp_port: int = 587
    from_addr: str = ""
    to_addr: str = ""


class AlertChannels(Base):
    telegram: bool = True
    email: bool = False


class AlertsConfig(Base):
    opportunity_score_threshold: float = 0.35
    min_margin_pct: float = 0.15
    min_gross_margin_chf: float = 8000
    jp_price_drop_pct: float = 0.05
    fx_move_pct: float = 0.02
    max_alerts_per_run: int = 10
    cooldown_days: int = 7
    channels: AlertChannels = Field(default_factory=AlertChannels)
    email: EmailConfig = Field(default_factory=EmailConfig)


class HttpConfig(Base):
    user_agent: str = "nippon-margin/0.1 personal-research"
    per_domain_delay_seconds: float = 2.0
    timeout_seconds: float = 30.0
    max_retries: int = 3
    respect_robots_txt: bool = True
    cache_dir: str = ".cache/html"
    cache_ttl_hours: float = 20.0
    #: Extra request headers. Empty by default -- see Fetcher.__aenter__ for
    #: why adding an `Accept` header breaks beforward.jp.
    extra_headers: dict[str, str] = Field(default_factory=dict)


class SourceConfig(Base):
    enabled: bool = False
    max_pages: int = 3
    renderer: str = "http"
    note: str | None = None


class SourcesConfig(Base):
    http: HttpConfig = Field(default_factory=HttpConfig)
    #: Discard scraped listings that do not resolve to a watchlist entry.
    #: A make-level page on an exporter returns hundreds of vans we will never
    #: buy; storing them bloats the catalog and the dashboard snapshot.
    only_watchlist: bool = True
    japan: dict[str, SourceConfig] = Field(default_factory=dict)
    switzerland: dict[str, SourceConfig] = Field(default_factory=dict)

    def all_sources(self) -> dict[str, SourceConfig]:
        return {**self.japan, **self.switzerland}


class WatchItem(Base):
    key: str
    make: str
    model: str
    aliases: list[str] = Field(default_factory=list)
    model_codes: list[str] = Field(default_factory=list)
    body: str | None = None
    #: AutoUncle/Autolina URL slug when it differs from `model`
    #: (their SL lives at `mercedes-benz/sl-class`, not `.../sl`).
    ch_model_slug: str | None = None
    max_km: int | None = None
    min_grade: float | None = None
    homologation_mfk_chf: float | None = None
    risk_notes: list[str] = Field(default_factory=list)

    def search_terms(self) -> list[str]:
        """Everything a JP or CH search page might call this car."""
        terms = [f"{self.make} {self.model}", self.model, *self.aliases]
        seen: set[str] = set()
        out: list[str] = []
        for t in terms:
            k = t.lower().strip()
            if k and k not in seen:
                seen.add(k)
                out.append(t.strip())
        return out


class ModelCodeEntry(Base):
    make: str
    model: str
    variant: str


class ModelRiskFlag(Base):
    match: str
    flag: str


class CatalogConfig(Base):
    delist_after_days: int = 3
    price_history_min_delta_chf: float = 50.0
    archive_evidence_for_alerts: bool = True


class MetaConfig(Base):
    timezone: str = "Europe/Zurich"
    dashboard_project: str = "nippon-margin"


class Config(Base):
    meta: MetaConfig = Field(default_factory=MetaConfig)
    costs: CostsConfig = Field(default_factory=CostsConfig)
    capital: CapitalConfig = Field(default_factory=CapitalConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    watchlist: list[WatchItem] = Field(default_factory=list)
    model_code_map: dict[str, ModelCodeEntry] = Field(default_factory=dict)
    model_risk_flags: list[ModelRiskFlag] = Field(default_factory=list)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)

    # ---- lookups ----
    def watch_item(self, key: str) -> WatchItem | None:
        for item in self.watchlist:
            if item.key == key:
                return item
        return None

    def homologation_for(self, key: str | None) -> float:
        """Per-model homologation override, falling back to the global default."""
        if key:
            item = self.watch_item(key)
            if item and item.homologation_mfk_chf is not None:
                return item.homologation_mfk_chf
        return self.costs.homologation_mfk_chf

    def resolve_model_code(self, code: str | None) -> ModelCodeEntry | None:
        if not code:
            return None
        key = code.strip().upper()
        for k, v in self.model_code_map.items():
            if k.upper() == key:
                return v
        return None

    def enabled_watchlist(self) -> list[WatchItem]:
        return list(self.watchlist)


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p} (run from the repo root or set NIPPON_CONFIG)")
    raw: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Config.model_validate(raw)
