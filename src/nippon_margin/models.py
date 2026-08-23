"""Core domain models.

Everything that crosses a module boundary (adapter -> store -> engine ->
report) is one of these. They are pydantic models so that a scraped dict with
a surprise type fails loudly at the adapter boundary rather than three stages
later in the cost engine.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class Steering(StrEnum):
    LHD = "LHD"
    RHD = "RHD"
    UNKNOWN = "UNKNOWN"


class PriceTerms(StrEnum):
    FOB = "FOB"
    CF = "C&F"
    CIF = "CIF"
    UNKNOWN = "UNKNOWN"


class SellerType(StrEnum):
    PRIVATE = "private"
    DEALER = "dealer"
    UNKNOWN = "unknown"


class ListingStatus(StrEnum):
    ACTIVE = "active"
    DELISTED = "delisted"


class ShippingMode(StrEnum):
    RORO = "roro"
    CONTAINER = "container"


class Base(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# --------------------------------------------------------------------------
# Japanese (buy side)
# --------------------------------------------------------------------------
class JpListing(Base):
    source: str
    source_ref: str

    make: str = ""
    model: str = ""
    model_code: str | None = None
    variant: str | None = None

    year: int | None = None
    reg_month: int | None = None
    mileage_km: int | None = None
    transmission: str | None = None
    steering: Steering = Steering.UNKNOWN
    fuel: str | None = None
    engine_cc: int | None = None
    color: str | None = None

    price_usd: float | None = None
    price_terms: PriceTerms = PriceTerms.UNKNOWN
    price_port: str | None = None

    #: Where the car physically sits, as printed by the exporter
    #: ("Incheon, SOUTH KOREA"). Japanese exporters sell plenty of stock that
    #: is not in Japan, and shipping, paperwork and grading all follow the
    #: car rather than the website.
    location: str | None = None

    auction_grade: float | None = None
    repair_history: bool | None = None

    description: str = ""
    options: list[str] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)
    url: str = ""

    chassis_no: str | None = None
    vin: str | None = None

    watchlist_key: str | None = None

    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)
    status: ListingStatus = ListingStatus.ACTIVE

    @field_validator("make", "model", mode="before")
    @classmethod
    def _clean_text(cls, v: Any) -> str:
        return normalise_ws(str(v or ""))

    @field_validator("year", mode="before")
    @classmethod
    def _sane_year(cls, v: Any) -> int | None:
        if v in (None, ""):
            return None
        try:
            y = int(v)
        except (TypeError, ValueError):
            return None
        return y if 1950 <= y <= date.today().year + 1 else None

    @property
    def doc_id(self) -> str:
        return make_doc_id(self.source, self.source_ref)

    @property
    def origin_country(self) -> str | None:
        """Country from `location`, upper-cased, or None if not stated."""
        if not self.location:
            return None
        tail = self.location.split(",")[-1].strip().upper()
        return tail or None

    @property
    def chassis_prefix(self) -> str | None:
        """Chassis prefix used for cross-exporter dedupe.

        Japanese chassis numbers look like `WDB4632761X123456`. Exporters
        frequently redact the serial (`WDB463276-1X12****`), so we key on the
        stable leading portion only.
        """
        raw = self.vin or self.chassis_no
        if not raw:
            return None
        cleaned = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
        cleaned = cleaned.replace("X", "X")
        if len(cleaned) < 8:
            return None
        return cleaned[:11]


# --------------------------------------------------------------------------
# Swiss (sell side)
# --------------------------------------------------------------------------
class PricePoint(Base):
    at: datetime
    price: float


class ChListing(Base):
    source: str
    source_ref: str

    make: str = ""
    model: str = ""
    variant: str | None = None

    year: int | None = None
    mileage_km: int | None = None
    price_chf: float | None = None

    days_listed: int | None = None
    price_change_history: list[PricePoint] = Field(default_factory=list)
    seller_type: SellerType = SellerType.UNKNOWN
    canton: str | None = None
    ch_fahrzeug: bool | None = None

    url: str = ""
    watchlist_key: str | None = None

    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)
    status: ListingStatus = ListingStatus.ACTIVE

    @property
    def doc_id(self) -> str:
        return make_doc_id(self.source, self.source_ref)

    @property
    def had_price_cut(self) -> bool:
        prices = [p.price for p in self.price_change_history]
        return len(prices) >= 2 and prices[-1] < prices[0]


# --------------------------------------------------------------------------
# Cost + opportunity
# --------------------------------------------------------------------------
class CostBreakdown(Base):
    """Every franc between the Japanese asking price and a car on Swiss plates."""

    mode: ShippingMode

    price_usd: float
    fx_usd_chf: float
    fob_chf: float

    freight_chf: float
    insurance_chf: float
    cif_chf: float

    customs_duty_chf: float
    automobilsteuer_chf: float
    vat_chf: float
    customs_clearance_chf: float
    homologation_mfk_chf: float
    agent_recon_buffer_chf: float

    landed_chf: float

    notes: list[str] = Field(default_factory=list)

    def as_rows(self) -> list[tuple[str, float]]:
        """Ordered line items, for the digest and the dashboard drill-down."""
        return [
            ("FOB (CHF)", self.fob_chf),
            ("Freight", self.freight_chf),
            ("Marine insurance", self.insurance_chf),
            ("= CIF", self.cif_chf),
            ("Customs duty", self.customs_duty_chf),
            ("Automobilsteuer (4%)", self.automobilsteuer_chf),
            ("VAT (8.1%)", self.vat_chf),
            ("Customs clearance", self.customs_clearance_chf),
            ("Homologation / MFK", self.homologation_mfk_chf),
            ("Agent + recon buffer", self.agent_recon_buffer_chf),
            ("= Landed", self.landed_chf),
        ]


class CompStats(Base):
    comp_count: int = 0
    swiss_median_ask: float | None = None
    swiss_p25: float | None = None
    swiss_p75: float | None = None
    median_days_listed: float | None = None
    pct_with_price_cut: float | None = None
    comp_urls: list[str] = Field(default_factory=list)
    comp_refs: list[str] = Field(default_factory=list)


class Opportunity(Base):
    id: str
    jp_doc_id: str
    watchlist_key: str | None = None

    make: str
    model: str
    variant: str | None = None
    year: int | None = None
    mileage_km: int | None = None
    location: str | None = None
    url: str = ""
    image_urls: list[str] = Field(default_factory=list)

    price_usd: float | None = None
    fx_usd_chf: float | None = None

    landed_roro: CostBreakdown | None = None
    landed_container: CostBreakdown | None = None

    comps: CompStats = Field(default_factory=CompStats)

    realizable_chf: float | None = None
    gross_margin_chf: float | None = None
    margin_pct: float | None = None
    capital_cost_chf: float | None = None
    net_margin_chf: float | None = None

    liquidity_score: float = 0.0
    capital_weight: float = 1.0
    seasonality_multiplier: float = 1.0
    risk_multiplier: float = 1.0
    opportunity_score: float = 0.0

    expected_holding_days: int = 90
    capital_tier: str = "mid"
    risk_flags: list[str] = Field(default_factory=list)

    duplicate_of: str | None = None
    is_cheapest_duplicate: bool = True

    computed_at: datetime = Field(default_factory=utcnow)


class FxRate(Base):
    day: str  # ISO date
    usd_chf: float
    jpy_chf: float
    fetched_at: datetime = Field(default_factory=utcnow)


class ModelStats(Base):
    day: str
    watchlist_key: str
    jp_count: int = 0
    ch_count: int = 0
    jp_median_price_chf: float | None = None
    ch_median_price_chf: float | None = None
    median_landed_chf: float | None = None
    spread_chf: float | None = None
    median_days_listed: float | None = None
    delist_rate_7d: float | None = None
    best_opportunity_score: float | None = None


class AdapterResult(Base):
    source: str
    ok: bool
    count: int = 0
    error: str | None = None
    duration_s: float = 0.0


class RunRecord(Base):
    id: str
    started_at: datetime
    finished_at: datetime | None = None
    command: str = "scrape"
    ok: bool = True
    jp_count: int = 0
    ch_count: int = 0
    opportunity_count: int = 0
    adapters: list[AdapterResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    git_sha: str | None = None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
_WS = re.compile(r"\s+")


def normalise_ws(text: str) -> str:
    return _WS.sub(" ", text).strip()


def make_doc_id(source: str, source_ref: str) -> str:
    """Deterministic, filesystem- and URL-safe document id.

    Some exporters use a full URL as their only stable reference, so anything
    containing separators or running long collapses to a hash suffix. Keeping
    ids opaque-but-stable is what makes upserts idempotent across runs.
    """
    ref = re.sub(r"[^A-Za-z0-9_.\-]", "-", source_ref).strip("-")
    if not ref or len(ref) > 120:
        ref = hashlib.sha1(source_ref.encode("utf-8")).hexdigest()[:20]
    return f"{source}_{ref}"
