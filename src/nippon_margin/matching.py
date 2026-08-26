"""Comparable matching, scoring and cross-source dedupe.

Given a Japanese listing and the current pool of Swiss listings, decide:
  * which Swiss cars are genuinely the same car (comps),
  * what one of them realistically sells for (p25, haircut for negotiation),
  * how fast it sells (liquidity),
  * what could go wrong (risk flags),
  * and finally the headline `opportunity_score`:

      margin_pct  x  liquidity_score  /  capital_weight

  i.e. margin per franc of capital, adjusted for how long that franc is
  expected to stay tied up.
"""

from __future__ import annotations

import re
import statistics
from datetime import UTC, datetime, timedelta

from .config import Config, WatchItem
from .costs import capital_cost, compute_both
from .models import (
    ChListing,
    CompStats,
    JpListing,
    Opportunity,
    PriceTerms,
    Steering,
    make_doc_id,
)

__all__ = [
    "normalise",
    "contains_phrase",
    "make_compatible",
    "extract_trim",
    "resolve_watchlist_key",
    "find_comps",
    "comp_stats",
    "liquidity_score",
    "risk_flags",
    "seasonality_multiplier",
    "build_opportunity",
    "mark_duplicates",
    "percentile",
]

_NOISE = re.compile(r"[^a-z0-9]+")


def normalise(text: str | None) -> str:
    """Lowercase, strip punctuation, collapse spaces. `G-Klasse` -> `g klasse`."""
    if not text:
        return ""
    return _NOISE.sub(" ", text.lower()).strip()


def _tokens(text: str | None) -> set[str]:
    return {t for t in normalise(text).split() if t}


def contains_phrase(haystack: str, phrase: str) -> bool:
    """Does `haystack` contain `phrase` as a whole-token run?

    Substring matching is wrong here and quietly poisons the comp set:
    `SL` matches `SLK 200`, `911` matches `1911`, and a Quattroporte
    `Sport GT S` matches a GranTurismo alias. Token-run matching does not.
    """
    hay = normalise(haystack).split()
    needle = normalise(phrase).split()
    if not needle or len(needle) > len(hay):
        return False
    return any(
        hay[i : i + len(needle)] == needle for i in range(len(hay) - len(needle) + 1)
    )


# --------------------------------------------------------------------------
# Watchlist resolution
# --------------------------------------------------------------------------
def resolve_watchlist_key(cfg: Config, *, make: str, model: str,
                          model_code: str | None = None,
                          variant: str | None = None,
                          description: str = "",
                          year: int | None = None) -> str | None:
    """Decide which watchlist entry a listing belongs to, or None.

    Model codes win when present -- `463276` is unambiguous where the free-text
    `G 63` on a Japanese page might be `G63 look-alike body kit`.
    """
    if model_code:
        code = model_code.strip().upper()
        for item in cfg.watchlist:
            if any(code == c.strip().upper() for c in item.model_codes) and item.year_ok(year):
                return item.key

    haystack = normalise(" ".join(filter(None, [make, model, variant])))
    desc = normalise(description)

    best: tuple[tuple[int, int, int], str] | None = None
    for item in cfg.watchlist:
        # The make has to agree. Without this a BMW 3 Series "320i Gran
        # Turismo" matches the Maserati GranTurismo alias and gets priced
        # against Maserati comps -- which is exactly the kind of nonsense
        # that reads as a 400% margin at the top of the digest.
        if not make_compatible(make, item.make):
            continue
        if not item.year_ok(year):
            continue
        for term, is_alias in item.scored_terms():
            t = normalise(term)
            if not t:
                continue
            if contains_phrase(haystack, t):
                in_title = 1
            elif len(t) >= 4 and contains_phrase(desc, t):
                in_title = 0
            else:
                continue
            # Ranked most-decisive first: a title beats a description, a trim
            # alias beats a bare model name, and only then does a longer term
            # beat a shorter one. Length alone would rank "Porsche 911" over
            # "Carrera 4S" -- two more characters and no idea which 911.
            score = (in_title, int(is_alias), len(t))
            if best is None or score > best[0]:
                best = (score, item.key)
    return best[1] if best else None


_MAKE_ALIASES = {
    "mercedes": "mercedesbenz",
    "mercedesbenz": "mercedesbenz",
    "vw": "volkswagen",
    "alfa": "alfaromeo",
    "alfaromeo": "alfaromeo",
}


def make_compatible(listing_make: str | None, watch_make: str | None) -> bool:
    """Do two make strings refer to the same manufacturer?

    Permissive only where it is safe: an unnamed make on either side matches
    anything (some exporters put the make only in the title), and the known
    aliases below collapse `Mercedes` / `Mercedes-Benz` and `Alfa` /
    `Alfa Romeo`. Everything else must agree.
    """
    a = re.sub(r"[^a-z]", "", (listing_make or "").lower())
    b = re.sub(r"[^a-z]", "", (watch_make or "").lower())
    if not a or not b:
        return True
    a, b = _MAKE_ALIASES.get(a, a), _MAKE_ALIASES.get(b, b)
    return a == b


def extract_trim(cfg: Config, *parts: str | None) -> str | None:
    """The trim named in some free text, or None if none is.

    Japanese exporters put the trim in the model string when they mention it
    at all (`Cayenne S`), and leave it out entirely more often than not; Swiss
    listings put it in a variant field. So both sides are read as free text
    rather than from a designated column.

    Matched longest-first as whole tokens, so `Turbo S` wins over `Turbo` and
    a `GLS` is never read as an `S`.
    """
    haystack = normalise(" ".join(p for p in parts if p))
    if not haystack:
        return None
    for trim in sorted(cfg.matching.trims, key=len, reverse=True):
        if contains_phrase(haystack, trim):
            return normalise(trim)
    return None


def _trim_compatible(cfg: Config, jp: JpListing, ch: ChListing) -> bool:
    """Reject a comp only when both sides name a trim and the trims differ.

    An unnamed trim is not evidence of a base model -- most Japanese titles
    simply do not say -- so an unknown trim keeps every comp and is surfaced
    as a risk flag instead.
    """
    if not cfg.matching.match_trim:
        return True
    jp_trim = extract_trim(cfg, jp.model, jp.variant)
    if jp_trim is None:
        return True
    ch_trim = extract_trim(cfg, ch.model, ch.variant)
    if ch_trim is None:
        return True
    return jp_trim == ch_trim


def _comp_key(cfg: Config, key: str | None) -> str | None:
    """The pool a watchlist entry prices against -- itself, unless redirected.

    One hop only: `comps_from` names a real tier, never another redirect, so
    following it further would only invite a cycle.
    """
    item = cfg.watch_item(key) if key else None
    return (item.comps_from or key) if item else key


def _watch_terms(cfg: Config, key: str | None) -> tuple[WatchItem | None, set[str]]:
    item = cfg.watch_item(key) if key else None
    if not item:
        return None, set()
    terms = {normalise(t) for t in item.search_terms()}
    return item, {t for t in terms if t}


# --------------------------------------------------------------------------
# Comp matching
# --------------------------------------------------------------------------
def _same_model(cfg: Config, jp: JpListing, ch: ChListing) -> bool:
    """Is this Swiss listing the same model as the Japanese one?

    Three routes, cheapest first: both already resolved to the same watchlist
    entry; the JP model code maps onto the CH variant name; or plain
    make+model text overlap.
    """
    if jp.watchlist_key and ch.watchlist_key:
        return _comp_key(cfg, jp.watchlist_key) == _comp_key(cfg, ch.watchlist_key)

    mapped = cfg.resolve_model_code(jp.model_code)
    if mapped:
        ch_text = " ".join(filter(None, [ch.make, ch.model, ch.variant]))
        if contains_phrase(ch_text, mapped.model) or contains_phrase(ch_text, mapped.variant):
            return True

    if not make_compatible(jp.make, ch.make):
        return False
    jp_model, ch_model = normalise(jp.model), normalise(ch.model)
    if not jp_model or not ch_model:
        return False
    return (
        contains_phrase(ch_model, jp_model)
        or contains_phrase(jp_model, ch_model)
        or bool(_tokens(jp_model) & _tokens(ch_model))
    )


def _year_ok(cfg: Config, jp: JpListing, ch: ChListing) -> bool:
    if jp.year is None or ch.year is None:
        return False
    return abs(jp.year - ch.year) <= cfg.matching.year_tolerance


def _mileage_ok(cfg: Config, jp: JpListing, ch: ChListing) -> bool:
    """Mileage within +/- tolerance. An unknown JP mileage does not disqualify.

    A missing Swiss mileage does: without it we cannot tell a 30k-km car from
    a 200k-km one, and that difference is the whole trade.
    """
    if jp.mileage_km is None:
        return True
    if ch.mileage_km is None:
        return False
    tol = cfg.matching.mileage_tolerance_pct
    lo, hi = jp.mileage_km * (1 - tol), jp.mileage_km * (1 + tol)
    return lo <= ch.mileage_km <= hi


def find_comps(cfg: Config, jp: JpListing, pool: list[ChListing],
               *, now: datetime | None = None) -> list[ChListing]:
    """Swiss comparables for one Japanese listing."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=cfg.matching.max_comp_age_days)
    out: list[ChListing] = []
    for ch in pool:
        if ch.price_chf is None or ch.price_chf <= 0:
            continue
        last_seen = ch.last_seen
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)
        if last_seen < cutoff:
            continue
        if not _same_model(cfg, jp, ch):
            continue
        if not _year_ok(cfg, jp, ch):
            continue
        if not _mileage_ok(cfg, jp, ch):
            continue
        if not _trim_compatible(cfg, jp, ch):
            continue
        out.append(ch)
    return out


def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolation percentile, `q` in [0, 1]. None for empty input."""
    if not values:
        return None
    data = sorted(values)
    if len(data) == 1:
        return float(data[0])
    pos = q * (len(data) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(data) - 1)
    frac = pos - lo
    return float(data[lo] + (data[hi] - data[lo]) * frac)


def comp_stats(comps: list[ChListing]) -> CompStats:
    prices = [c.price_chf for c in comps if c.price_chf]
    if not prices:
        return CompStats(comp_count=0)

    days = [float(c.days_listed) for c in comps if c.days_listed is not None]
    # A comp with a single recorded price point is evidence of "no cut", not
    # absence of evidence -- see the note in the AutoUncle adapter. Only comps
    # with no price history at all are excluded from the denominator.
    observed = [c for c in comps if c.price_change_history]
    pct_cut = (
        sum(1 for c in observed if c.had_price_cut) / len(observed) if observed else None
    )

    return CompStats(
        comp_count=len(prices),
        swiss_median_ask=round(statistics.median(prices), 2),
        swiss_p25=round(percentile(prices, 0.25), 2),
        swiss_p75=round(percentile(prices, 0.75), 2),
        median_days_listed=round(statistics.median(days), 1) if days else None,
        pct_with_price_cut=round(pct_cut, 3) if pct_cut is not None else None,
        comp_urls=[c.url for c in comps if c.url][:20],
        comp_refs=[c.doc_id for c in comps][:50],
    )


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def liquidity_score(cfg: Config, stats: CompStats) -> float:
    """0..1. How confident are we this car actually sells, and quickly?

    Three signals: how many comps exist at all (a thin market is a slow
    market), how long they have been sitting, and how many sellers have
    already cut their price.
    """
    lc = cfg.scoring.liquidity
    w = lc.weights

    count_component = min(stats.comp_count / max(lc.comp_count_saturation, 1), 1.0)

    if stats.median_days_listed is None:
        days_component = 0.5
    else:
        ref = max(lc.days_listed_reference, 1.0)
        days_component = ref / (ref + stats.median_days_listed)

    cut_component = 0.5 if stats.pct_with_price_cut is None else 1.0 - stats.pct_with_price_cut

    total_w = w.comp_count + w.days_listed + w.price_cuts
    if total_w <= 0:
        return 0.0
    score = (
        w.comp_count * count_component
        + w.days_listed * days_component
        + w.price_cuts * cut_component
    ) / total_w
    return round(max(0.0, min(score, 1.0)), 4)


def risk_flags(cfg: Config, jp: JpListing, stats: CompStats) -> list[str]:
    flags: list[str] = []

    if cfg.risk.penalise_rhd and jp.steering is Steering.RHD:
        flags.append("RHD (Swiss buyers discount heavily; homologation is fine but resale is not)")
    elif jp.steering is Steering.UNKNOWN:
        flags.append("Steering side not stated -- confirm LHD before bidding")

    if jp.auction_grade is not None and jp.auction_grade < cfg.risk.min_auction_grade:
        flags.append(f"Auction grade {jp.auction_grade} below minimum {cfg.risk.min_auction_grade}")

    if jp.repair_history:
        flags.append("Repair history declared")

    origin = cfg.risk.resolve_origin(jp.location)
    if origin and origin != cfg.risk.assumed_origin:
        _, _, using_default = cfg.costs.shipping.for_origin(origin)
        detail = "freight, paperwork and export rules all differ"
        if using_default:
            detail += f"; freight is still the {cfg.risk.assumed_origin} figure"
        flags.append(f"Car is in {jp.location or origin}, not {cfg.risk.assumed_origin} -- {detail}")
        if jp.auction_grade is None:
            flags.append(
                f"No Japanese auction grade -- {origin} stock is not graded that way, "
                "so condition is unverified"
            )

    if stats.comp_count < cfg.matching.min_comps_for_confidence:
        flags.append(f"Thin comp set ({stats.comp_count}) -- price estimate is weak")

    # Only ~2% of Japanese listings name a trim, so flagging "trim unknown"
    # would fire on almost every row and mean nothing. What is worth saying is
    # when the comps themselves disagree: a wide p25-to-p75 spread means the
    # set is mixing trims or conditions, and the median is not a price.
    spread_limit = cfg.matching.comp_spread_warn_ratio
    if (
        spread_limit
        and stats.swiss_p25
        and stats.swiss_p75
        and stats.swiss_p75 / stats.swiss_p25 >= spread_limit
    ):
        flags.append(
            f"Comps disagree ({stats.swiss_p25:,.0f}-{stats.swiss_p75:,.0f} CHF, "
            f"{stats.swiss_p75 / stats.swiss_p25:.2f}x) -- likely mixed trims; "
            f"confirm which variant this car is"
        )

    item = cfg.watch_item(jp.watchlist_key) if jp.watchlist_key else None
    if item:
        if item.max_km and jp.mileage_km and jp.mileage_km > item.max_km:
            flags.append(f"Mileage {jp.mileage_km:,} km over the {item.max_km:,} km limit")
        if item.min_grade and jp.auction_grade is not None and jp.auction_grade < item.min_grade:
            flags.append(f"Below the {item.min_grade} grade floor set for {item.key}")
        flags.extend(item.risk_notes)

    haystack = normalise(
        " ".join(filter(None, [jp.make, jp.model, jp.variant, jp.model_code, jp.description]))
    )
    for rule in cfg.model_risk_flags:
        if normalise(rule.match) and contains_phrase(haystack, rule.match):
            flags.append(rule.flag)

    seen: set[str] = set()
    return [f for f in flags if not (f in seen or seen.add(f))]


def seasonality_multiplier(cfg: Config, jp: JpListing, *, month: int) -> float:
    """Convertibles sell in spring; G-Wagens sell before the snow."""
    season = cfg.scoring.seasonality
    if not season.enabled:
        return 1.0
    item = cfg.watch_item(jp.watchlist_key) if jp.watchlist_key else None
    if not item or not item.body:
        return 1.0
    rule = season.rules().get(item.body)
    if rule and month in rule.months:
        return float(rule.multiplier)
    return 1.0


def capital_weight(cfg: Config, landed_chf: float) -> float:
    """How heavily to penalise tying up capital. 1.0 at the reference price."""
    ref = max(cfg.scoring.capital_weight_reference_chf, 1.0)
    weight = (max(landed_chf, 1.0) / ref) ** cfg.scoring.capital_weight_exponent
    return round(max(weight, 1e-6), 6)


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------
def build_opportunity(
    cfg: Config,
    jp: JpListing,
    pool: list[ChListing],
    *,
    fx_usd_chf: float,
    now: datetime | None = None,
) -> Opportunity | None:
    """Full opportunity record for one Japanese listing, or None if unpriceable."""
    if not jp.price_usd or jp.price_usd <= 0 or fx_usd_chf <= 0:
        return None

    now = now or datetime.now(UTC)
    terms = jp.price_terms if isinstance(jp.price_terms, PriceTerms) else PriceTerms.UNKNOWN

    roro, container = compute_both(
        cfg,
        price_usd=jp.price_usd,
        fx_usd_chf=fx_usd_chf,
        price_terms=terms,
        watchlist_key=jp.watchlist_key,
        origin=cfg.risk.resolve_origin(jp.location),
    )
    # The RoRo figure is the headline: it is what a single car actually costs
    # today, without waiting for two more cars to fill a container.
    landed = roro.landed_chf

    comps = find_comps(cfg, jp, pool, now=now)
    stats = comp_stats(comps)

    realizable = None
    gross = None
    margin_pct = None
    if stats.swiss_p25:
        realizable = round(stats.swiss_p25 * cfg.matching.realization_factor, 2)
        gross = round(realizable - landed, 2)
        margin_pct = round(gross / landed, 4) if landed > 0 else None

    holding_days = (
        int(round(stats.median_days_listed))
        if stats.median_days_listed
        else cfg.capital.default_holding_days
    )
    cap_cost = capital_cost(cfg, landed, holding_days)
    net = round(gross - cap_cost, 2) if gross is not None else None

    liq = liquidity_score(cfg, stats)
    flags = risk_flags(cfg, jp, stats)
    risk_mult = round(cfg.risk.flag_penalty ** len(flags), 4)
    season_mult = seasonality_multiplier(cfg, jp, month=now.month)
    cap_weight = capital_weight(cfg, landed)

    score = 0.0
    if margin_pct is not None and margin_pct > 0:
        score = round(margin_pct * liq / cap_weight * season_mult * risk_mult, 6)

    mapped = cfg.resolve_model_code(jp.model_code)

    return Opportunity(
        id=make_doc_id("opp", jp.doc_id),
        jp_doc_id=jp.doc_id,
        watchlist_key=jp.watchlist_key,
        make=jp.make,
        model=jp.model,
        variant=jp.variant or (mapped.variant if mapped else None),
        year=jp.year,
        mileage_km=jp.mileage_km,
        location=jp.location,
        url=jp.url,
        image_urls=jp.image_urls[:6],
        price_usd=jp.price_usd,
        fx_usd_chf=fx_usd_chf,
        landed_roro=roro,
        landed_container=container,
        comps=stats,
        realizable_chf=realizable,
        gross_margin_chf=gross,
        margin_pct=margin_pct,
        capital_cost_chf=cap_cost,
        net_margin_chf=net,
        liquidity_score=liq,
        capital_weight=cap_weight,
        seasonality_multiplier=season_mult,
        risk_multiplier=risk_mult,
        opportunity_score=score,
        expected_holding_days=holding_days,
        capital_tier=cfg.capital.tier_for(landed),
        risk_flags=flags,
        computed_at=now,
    )


# --------------------------------------------------------------------------
# Cross-exporter dedupe
# --------------------------------------------------------------------------
def mark_duplicates(listings: list[JpListing]) -> dict[str, tuple[str | None, bool]]:
    """Group JP listings by chassis prefix; flag the cheapest of each group.

    The same physical car is routinely listed by four exporters at four
    different prices. We want the cheapest one, and we want to know the others
    exist -- a car on many exporter sites is a car the Japanese trade is
    struggling to move.

    Returns {doc_id: (duplicate_of, is_cheapest)}.
    """
    groups: dict[str, list[JpListing]] = {}
    result: dict[str, tuple[str | None, bool]] = {}

    for lst in listings:
        prefix = lst.chassis_prefix
        if prefix:
            groups.setdefault(prefix, []).append(lst)
        else:
            result[lst.doc_id] = (None, True)

    for members in groups.values():
        priced = [m for m in members if m.price_usd]
        if not priced:
            for m in members:
                result[m.doc_id] = (None, True)
            continue
        cheapest = min(priced, key=lambda m: m.price_usd)
        for m in members:
            if m.doc_id == cheapest.doc_id:
                result[m.doc_id] = (None, True)
            else:
                result[m.doc_id] = (cheapest.doc_id, False)
    return result
