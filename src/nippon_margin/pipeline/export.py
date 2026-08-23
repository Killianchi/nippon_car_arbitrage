"""Build the static data file the dashboard reads.

The dashboard is a plain static site: the daily run serialises everything it
needs into one JSON file that ships alongside the bundle. No database client,
no per-read billing, and the page is exactly as fresh as the last run -- which
is the honest answer, since the data only changes daily.

The file is trimmed deliberately: photos and comp links are capped, and only
scored opportunities are included. A snapshot that grows without bound would
turn every page load into a multi-megabyte download on a phone.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Config
from ..fx import margin_impact_chf, pct_move
from ..models import Opportunity
from ..store.base import Store

log = logging.getLogger(__name__)

MAX_OPPORTUNITIES = 150
MAX_COMP_LINKS = 10
MAX_IMAGES = 3
MAX_FX_DAYS = 180
MAX_STATS_DAYS = 400
MAX_RUNS = 20


def _opportunity(opp: Opportunity) -> dict[str, Any]:
    data = json.loads(opp.model_dump_json())
    comps = data.get("comps") or {}
    comps["comp_urls"] = (comps.get("comp_urls") or [])[:MAX_COMP_LINKS]
    # comp_refs are internal join keys; the dashboard never renders them.
    comps.pop("comp_refs", None)
    data["comps"] = comps
    data["image_urls"] = (data.get("image_urls") or [])[:MAX_IMAGES]
    return data


def build_snapshot(cfg: Config, store: Store) -> dict[str, Any]:
    opportunities = [
        o for o in store.load_opportunities(limit=500)
        if o.opportunity_score > 0 and o.is_cheapest_duplicate
    ][:MAX_OPPORTUNITIES]

    fx_rates = store.load_fx(days=MAX_FX_DAYS)
    fx_rates_asc = sorted(fx_rates, key=lambda r: r.day)
    latest_fx = fx_rates_asc[-1] if fx_rates_asc else None
    usd_move = pct_move(fx_rates, field="usd_chf", days=7)
    jpy_move = pct_move(fx_rates, field="jpy_chf", days=7)

    # Precompute the FX annotation here rather than in the browser: it needs
    # the tax chain, which is a business rule and belongs on this side.
    impacts = []
    if latest_fx and usd_move is not None:
        seen: set[str] = set()
        for opp in opportunities:
            key = opp.watchlist_key
            if not key or key in seen or not opp.price_usd:
                continue
            seen.add(key)
            impacts.append({
                "watchlist_key": key,
                "name": " ".join(
                    filter(None, [str(opp.year or ""), opp.make, opp.variant or opp.model])
                ),
                "recent_chf": margin_impact_chf(opp.price_usd, usd_move, latest_fx.usd_chf),
                "per_3pct_chf": margin_impact_chf(opp.price_usd, -0.03, latest_fx.usd_chf),
            })
            if len(impacts) >= 5:
                break

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "opportunities": [_opportunity(o) for o in opportunities],
        "model_stats": [
            json.loads(s.model_dump_json())
            for s in sorted(store.load_model_stats(days=MAX_STATS_DAYS), key=lambda s: s.day)
        ],
        "fx": [json.loads(r.model_dump_json()) for r in fx_rates_asc],
        "fx_moves": {"usd_chf_7d": usd_move, "jpy_chf_7d": jpy_move},
        "fx_impacts": impacts,
        "runs": [json.loads(r.model_dump_json()) for r in store.recent_runs(limit=MAX_RUNS)],
        "watchlist": [
            json.loads(w.model_dump_json()) for w in cfg.watchlist
        ],
        "capital_tiers": [
            {"name": t.name, "max_chf": t.max_chf} for t in cfg.capital.tiers
        ],
    }


def export(cfg: Config, store: Store, out: Path) -> Path:
    snapshot = build_snapshot(cfg, store)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, separators=(",", ":")), encoding="utf-8")
    log.info(
        "exported %d opportunities, %d fx days, %d stat rows -> %s (%.0f KB)",
        len(snapshot["opportunities"]), len(snapshot["fx"]),
        len(snapshot["model_stats"]), out, out.stat().st_size / 1024,
    )
    return out
