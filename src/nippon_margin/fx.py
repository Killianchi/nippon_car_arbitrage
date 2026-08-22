"""FX rates from the ECB via frankfurter.app (free, no key).

FX is a first-class margin driver, not plumbing: a 3% move in USD/CHF is
worth roughly CHF 3-4k on a G63, which is the difference between a trade and
a hobby. So rates are stored daily and surfaced on the dashboard rather than
fetched and forgotten.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from .http import Fetcher
from .models import FxRate
from .store.base import Store

log = logging.getLogger(__name__)

API = "https://api.frankfurter.app"


async def fetch_fx(fetcher: Fetcher, *, day: date | None = None) -> FxRate | None:
    """USD/CHF and JPY/CHF for `day` (default: latest published)."""
    path = day.isoformat() if day else "latest"
    data = await fetcher.get_json(f"{API}/{path}", params={"from": "CHF", "to": "USD,JPY"})
    if not isinstance(data, dict) or "rates" not in data:
        log.error("unexpected FX response for %s: %r", path, data)
        return None

    rates = data["rates"]
    # The API quotes CHF -> X; we want X -> CHF, which is the reciprocal.
    usd_per_chf = rates.get("USD")
    jpy_per_chf = rates.get("JPY")
    if not usd_per_chf or not jpy_per_chf:
        log.error("FX response missing USD or JPY: %r", rates)
        return None

    return FxRate(
        day=str(data.get("date") or (day or date.today()).isoformat()),
        usd_chf=round(1.0 / float(usd_per_chf), 6),
        jpy_chf=round(1.0 / float(jpy_per_chf), 8),
        fetched_at=datetime.now(timezone.utc),
    )


async def refresh_fx(fetcher: Fetcher, store: Store) -> FxRate | None:
    """Fetch today's rate and persist it. Falls back to the last stored rate."""
    rate = await fetch_fx(fetcher)
    if rate:
        store.save_fx(rate)
        log.info("FX %s: USD/CHF %.4f, JPY/CHF %.6f", rate.day, rate.usd_chf, rate.jpy_chf)
        return rate

    stored = store.latest_fx()
    if stored:
        log.warning("FX fetch failed; falling back to stored rate from %s", stored.day)
    else:
        log.error("FX fetch failed and no stored rate exists")
    return stored


async def backfill_fx(fetcher: Fetcher, store: Store, *, days: int = 90) -> int:
    """Historical rates, so the FX chart has a curve on day one."""
    end = date.today()
    start = end - timedelta(days=days)
    data = await fetcher.get_json(
        f"{API}/{start.isoformat()}..{end.isoformat()}",
        params={"from": "CHF", "to": "USD,JPY"},
    )
    if not isinstance(data, dict) or "rates" not in data:
        log.error("FX backfill failed")
        return 0

    saved = 0
    for day_str, rates in sorted(data["rates"].items()):
        usd, jpy = rates.get("USD"), rates.get("JPY")
        if not usd or not jpy:
            continue
        store.save_fx(
            FxRate(day=day_str, usd_chf=round(1.0 / float(usd), 6),
                   jpy_chf=round(1.0 / float(jpy), 8))
        )
        saved += 1
    log.info("backfilled %d FX days", saved)
    return saved


def pct_move(rates: list[FxRate], *, field: str = "usd_chf", days: int = 7) -> float | None:
    """Fractional change in a rate over the last `days` of stored history.

    Positive means CHF buys fewer USD/JPY -- i.e. imports got *more*
    expensive, and the spread narrowed.
    """
    if len(rates) < 2:
        return None
    ordered = sorted(rates, key=lambda r: r.day)
    latest = ordered[-1]
    cutoff = (date.fromisoformat(latest.day) - timedelta(days=days)).isoformat()
    earlier = [r for r in ordered if r.day <= cutoff]
    baseline = earlier[-1] if earlier else ordered[0]

    old = getattr(baseline, field)
    new = getattr(latest, field)
    if not old:
        return None
    return round((new - old) / old, 5)


def margin_impact_chf(price_usd: float, fx_move: float, fx_usd_chf: float) -> float:
    """CHF the landed cost moves for a given fractional FX move.

    This is what the dashboard annotation means by "a 3% yen weakening adds
    ~CHF Xk to the G63 spread": the tax chain amplifies the FOB move, because
    Automobilsteuer and VAT are levied on top of it.
    """
    fob_delta = price_usd * fx_usd_chf * fx_move
    return round(-fob_delta * 1.04 * 1.081, 2)
