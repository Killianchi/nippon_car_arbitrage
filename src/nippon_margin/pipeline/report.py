"""The report stage: a daily digest you can read on a phone in 30 seconds.

Renders both Markdown (for Telegram/email) and HTML (for the Actions
artifact), from the same data.
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, date, datetime

from ..config import Config
from ..fx import margin_impact_chf, pct_move
from ..models import Opportunity
from ..store.base import Store
from .analyze import jp_price_moves, spread_moves

log = logging.getLogger(__name__)


def _chf(value: float | None) -> str:
    return f"CHF {value:,.0f}" if value is not None else "-"


def _pct(value: float | None) -> str:
    return f"{value * 100:+.1f}%" if value is not None else "-"


def build_digest(cfg: Config, store: Store, *, top_n: int = 10) -> dict:
    """Everything both renderers need, computed once."""
    opportunities = [
        o for o in store.load_opportunities(limit=500)
        if o.opportunity_score > 0 and o.is_cheapest_duplicate
    ]
    top = opportunities[:top_n]

    fx_rates = store.load_fx(days=30)
    latest_fx = fx_rates[0] if fx_rates else None
    usd_move = pct_move(fx_rates, field="usd_chf", days=7)
    jpy_move = pct_move(fx_rates, field="jpy_chf", days=7)

    fx_note = None
    if latest_fx and usd_move is not None and top:
        headline = top[0]
        impact = margin_impact_chf(headline.price_usd or 0, usd_move, latest_fx.usd_chf)
        fx_note = (
            f"USD/CHF moved {_pct(usd_move)} over 7 days -- worth {_chf(abs(impact))} "
            f"{'off' if impact > 0 else 'onto'} the landed cost of the "
            f"{headline.year or ''} {headline.make} {headline.model}".strip()
        )

    return {
        "day": date.today().isoformat(),
        "generated_at": datetime.now(UTC),
        "top": top,
        "total": len(opportunities),
        "by_tier": {
            tier.name: [o for o in opportunities if o.capital_tier == tier.name][:5]
            for tier in cfg.capital.tiers
        },
        "fx": latest_fx,
        "fx_usd_move_7d": usd_move,
        "fx_jpy_move_7d": jpy_move,
        "fx_note": fx_note,
        "spread_moves": spread_moves(store, days=7),
        "jp_price_moves": jp_price_moves(store, days=7),
        "last_run": (store.recent_runs(limit=1) or [None])[0],
    }


# --------------------------------------------------------------------------
def render_markdown(cfg: Config, digest: dict) -> str:
    lines: list[str] = [f"*nippon-margin — {digest['day']}*", ""]

    fx = digest["fx"]
    if fx:
        lines.append(
            f"FX: USD/CHF {fx.usd_chf:.4f} ({_pct(digest['fx_usd_move_7d'])} 7d) · "
            f"JPY/CHF {fx.jpy_chf:.5f} ({_pct(digest['fx_jpy_move_7d'])} 7d)"
        )
    if digest["fx_note"]:
        lines.append(f"_{digest['fx_note']}_")
    lines.append("")

    if not digest["top"]:
        lines.append("No positive-margin opportunities today.")
    else:
        lines.append(f"*Top {len(digest['top'])} of {digest['total']} opportunities*")
        lines.append("")
        for i, o in enumerate(digest["top"], 1):
            lines.extend(_markdown_opportunity(i, o))

    moves = {k: v for k, v in digest["spread_moves"].items() if abs(v) >= 500}
    if moves:
        lines.append("*Market movement (7d spread)*")
        for key, delta in sorted(moves.items(), key=lambda kv: -abs(kv[1])):
            direction = "widening" if delta > 0 else "narrowing"
            lines.append(f"· {key}: {direction} by {_chf(abs(delta))}")
        lines.append("")

    jp_moves = {k: v for k, v in digest["jp_price_moves"].items() if abs(v) >= 0.03}
    if jp_moves:
        lines.append("*Japanese price moves (7d)*")
        for key, move in sorted(jp_moves.items(), key=lambda kv: kv[1]):
            lines.append(f"· {key}: {_pct(move)}")
        lines.append("")

    run = digest["last_run"]
    if run:
        ok = sum(1 for a in run.adapters if a.ok)
        lines.append(
            f"_Last run {run.id}: {ok}/{len(run.adapters)} sources ok, "
            f"{run.jp_count} JP / {run.ch_count} CH listings._"
        )
        for err in run.errors[:5]:
            lines.append(f"  ⚠️ {err}")

    return "\n".join(lines)


def _markdown_opportunity(index: int, o: Opportunity) -> list[str]:
    name = " ".join(filter(None, [str(o.year or ""), o.make, o.variant or o.model]))
    roro = o.landed_roro
    container = o.landed_container

    out = [
        f"*{index}. {name}* — score `{o.opportunity_score:.3f}`",
        f"   JP ask ${o.price_usd:,.0f} → landed {_chf(roro.landed_chf if roro else None)} "
        f"RoRo / {_chf(container.landed_chf if container else None)} container",
        f"   CH p25 {_chf(o.comps.swiss_p25)} (median {_chf(o.comps.swiss_median_ask)}, "
        f"{o.comps.comp_count} comps)",
        f"   Gross {_chf(o.gross_margin_chf)} ({_pct(o.margin_pct)}) · "
        f"net of capital {_chf(o.net_margin_chf)}",
        f"   Liquidity {o.liquidity_score:.2f} · {o.expected_holding_days}d expected · "
        f"tier {o.capital_tier}",
    ]
    if o.mileage_km:
        out.append(f"   {o.mileage_km:,} km")
    if o.risk_flags:
        out.append("   ⚠️ " + "; ".join(o.risk_flags[:3]))
    if o.url:
        out.append(f"   {o.url}")
    out.append("")
    return out


# --------------------------------------------------------------------------
def render_html(cfg: Config, digest: dict) -> str:
    """Standalone HTML digest -- uploaded as an Actions artifact."""
    esc = html.escape
    rows = []
    for i, o in enumerate(digest["top"], 1):
        name = esc(" ".join(filter(None, [str(o.year or ""), o.make, o.variant or o.model])))
        flags = "".join(f"<li>{esc(f)}</li>" for f in o.risk_flags)
        breakdown = "".join(
            f"<tr><td>{esc(label)}</td><td class='n'>{value:,.0f}</td></tr>"
            for label, value in (o.landed_roro.as_rows() if o.landed_roro else [])
        )
        comps = "".join(
            f"<li><a href='{esc(u)}'>{esc(u[:70])}</a></li>" for u in o.comps.comp_urls[:8]
        )
        image = (
            f"<img src='{esc(o.image_urls[0])}' alt='' loading='lazy'>" if o.image_urls else ""
        )
        rows.append(f"""
        <details class="opp">
          <summary>
            <span class="rank">{i}</span>
            <span class="name">{name}</span>
            <span class="score">{o.opportunity_score:.3f}</span>
            <span class="margin">{_pct(o.margin_pct)}</span>
            <span class="chf">{_chf(o.gross_margin_chf)}</span>
            <span class="tier">{esc(o.capital_tier)}</span>
          </summary>
          <div class="body">
            {image}
            <table class="breakdown"><caption>Landed cost (RoRo)</caption>{breakdown}</table>
            <p>Swiss p25 {_chf(o.comps.swiss_p25)} · median {_chf(o.comps.swiss_median_ask)}
               · {o.comps.comp_count} comps · median {o.comps.median_days_listed or '-'} days listed</p>
            <p>Net of capital {_chf(o.net_margin_chf)} over {o.expected_holding_days} days
               · liquidity {o.liquidity_score:.2f}</p>
            {f'<ul class="flags">{flags}</ul>' if flags else ''}
            <p><a href="{esc(o.url)}">Japanese listing</a></p>
            {f'<details><summary>Swiss comps</summary><ul>{comps}</ul></details>' if comps else ''}
          </div>
        </details>""")

    fx = digest["fx"]
    fx_line = (
        f"USD/CHF {fx.usd_chf:.4f} ({_pct(digest['fx_usd_move_7d'])} 7d) · "
        f"JPY/CHF {fx.jpy_chf:.5f} ({_pct(digest['fx_jpy_move_7d'])} 7d)"
        if fx else "no FX rate stored"
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>nippon-margin {digest['day']}</title>
<style>
 :root {{ color-scheme: dark; --bg:#0f1113; --fg:#e8e6e3; --dim:#9aa0a6; --line:#2a2e33; --pos:#4ade80; }}
 body {{ background:var(--bg); color:var(--fg); font:15px/1.5 ui-sans-serif,system-ui,sans-serif; margin:0; padding:1rem; }}
 h1 {{ font-size:1.25rem; margin:0 0 .25rem; }}
 .sub {{ color:var(--dim); font-size:.85rem; margin-bottom:1rem; }}
 .opp {{ border:1px solid var(--line); border-radius:8px; margin-bottom:.5rem; }}
 summary {{ display:grid; grid-template-columns:2ch 1fr auto; gap:.5rem; padding:.6rem .75rem; cursor:pointer; align-items:baseline; }}
 .rank {{ color:var(--dim); }}
 .name {{ font-weight:600; }}
 .score {{ color:var(--pos); font-variant-numeric:tabular-nums; }}
 .margin,.chf,.tier {{ color:var(--dim); font-size:.85rem; }}
 .body {{ padding:0 .75rem .75rem; border-top:1px solid var(--line); }}
 .body img {{ max-width:100%; border-radius:6px; margin:.5rem 0; }}
 table.breakdown {{ width:100%; border-collapse:collapse; font-size:.85rem; margin:.5rem 0; }}
 table.breakdown caption {{ text-align:left; color:var(--dim); padding-bottom:.25rem; }}
 table.breakdown td {{ padding:.15rem 0; border-bottom:1px solid var(--line); }}
 td.n {{ text-align:right; font-variant-numeric:tabular-nums; }}
 .flags li {{ color:#fbbf24; }}
 a {{ color:#60a5fa; }}
 @media (min-width:700px) {{ summary {{ grid-template-columns:2ch 1fr 6ch 6ch 8ch 5ch; }} }}
</style></head><body>
<h1>nippon-margin — {digest['day']}</h1>
<p class="sub">{esc(fx_line)}<br>{esc(digest['fx_note'] or '')}</p>
<p class="sub">{len(digest['top'])} shown of {digest['total']} scored opportunities</p>
{''.join(rows) or '<p>No positive-margin opportunities today.</p>'}
</body></html>"""


def weekly_portfolio(cfg: Config, digest: dict) -> str:
    """Best 5 candidates per capital tier -- the Sunday view."""
    lines = [f"*nippon-margin weekly — {digest['day']}*", ""]
    for tier in cfg.capital.tiers:
        picks = digest["by_tier"].get(tier.name, [])
        ceiling = f"< {_chf(tier.max_chf)}" if tier.max_chf else "no ceiling"
        lines.append(f"*{tier.name}* ({ceiling})")
        if not picks:
            lines.append("  nothing worth buying")
        for o in picks:
            name = " ".join(filter(None, [str(o.year or ""), o.make, o.variant or o.model]))
            lines.append(
                f"  · {name} — {_chf(o.gross_margin_chf)} ({_pct(o.margin_pct)}), "
                f"score {o.opportunity_score:.3f}"
            )
        lines.append("")
    return "\n".join(lines)
