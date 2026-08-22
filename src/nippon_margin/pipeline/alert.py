"""The alert stage: decide what is worth interrupting you for.

Four triggers, per the spec:
  1. an opportunity above the score threshold;
  2. a new listing that beats the best score ever stored for its model;
  3. a tracked model's Japanese median price dropping more than 5% in a week;
  4. FX moving more than 2% in a week -- FX is a core margin driver, so it
     gets surfaced rather than buried in the daily digest.

Everything is rate-limited by a per-key cooldown so a car that sits on the
market for a month does not alert you thirty times.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import Config
from ..fx import pct_move
from ..models import Opportunity
from ..store.base import Store
from ..alerting import email as email_channel
from ..alerting import telegram
from .analyze import jp_price_moves
from .report import _chf, _pct, build_digest, render_markdown, weekly_portfolio

log = logging.getLogger(__name__)


def _cooled_down(store: Store, key: str, *, days: int, now: datetime) -> bool:
    last = store.alert_sent_at(key)
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) >= timedelta(days=days)


def select_alerts(cfg: Config, store: Store, *, now: datetime | None = None) -> list[tuple[str, str]]:
    """`[(cooldown_key, message)]` for everything worth sending right now."""
    now = now or datetime.now(timezone.utc)
    alerts: list[tuple[str, str]] = []
    thresholds = cfg.alerts

    opportunities = [
        o for o in store.load_opportunities(limit=500)
        if o.opportunity_score > 0 and o.is_cheapest_duplicate
    ]

    # Best score previously recorded per model, from the daily snapshots.
    previous_best: dict[str, float] = {}
    for row in store.load_model_stats(days=60):
        if row.best_opportunity_score is None:
            continue
        if row.day == now.date().isoformat():
            continue  # today's own snapshot is not a baseline
        key = row.watchlist_key
        previous_best[key] = max(previous_best.get(key, 0.0), row.best_opportunity_score)

    for opp in opportunities:
        reasons: list[str] = []
        if (
            opp.opportunity_score >= thresholds.opportunity_score_threshold
            and (opp.margin_pct or 0) >= thresholds.min_margin_pct
            and (opp.gross_margin_chf or 0) >= thresholds.min_gross_margin_chf
        ):
            reasons.append(f"score {opp.opportunity_score:.3f} over threshold")

        best = previous_best.get(opp.watchlist_key or "")
        if best and opp.opportunity_score > best:
            reasons.append(f"new best for {opp.watchlist_key} (previous {best:.3f})")

        if not reasons:
            continue
        key = f"opp:{opp.jp_doc_id}"
        if not _cooled_down(store, key, days=thresholds.cooldown_days, now=now):
            continue
        alerts.append((key, _format_opportunity(opp, reasons)))
        if len(alerts) >= thresholds.max_alerts_per_run:
            log.info("hit max_alerts_per_run (%d); the rest are in the digest",
                     thresholds.max_alerts_per_run)
            break

    # -- Japanese price drops ------------------------------------------------
    for model_key, move in jp_price_moves(store, days=7).items():
        if move > -thresholds.jp_price_drop_pct:
            continue
        key = f"jpdrop:{model_key}:{now.date().isoformat()}"
        if not _cooled_down(store, key, days=thresholds.cooldown_days, now=now):
            continue
        alerts.append((
            key,
            f"*JP price drop* — {model_key} median is {_pct(move)} over 7 days. "
            "Cheaper buy side, same Swiss ask: the spread just widened.",
        ))

    # -- FX ------------------------------------------------------------------
    rates = store.load_fx(days=30)
    for field, label in (("usd_chf", "USD/CHF"), ("jpy_chf", "JPY/CHF")):
        move = pct_move(rates, field=field, days=7)
        if move is None or abs(move) < thresholds.fx_move_pct:
            continue
        key = f"fx:{field}:{now.date().isoformat()}"
        if not _cooled_down(store, key, days=3, now=now):
            continue
        direction = "weaker CHF (imports dearer)" if move > 0 else "stronger CHF (imports cheaper)"
        alerts.append((
            key,
            f"*FX* — {label} moved {_pct(move)} in 7 days: {direction}. "
            "Every landed cost in the catalog just moved with it.",
        ))

    return alerts


def _format_opportunity(opp: Opportunity, reasons: list[str]) -> str:
    name = " ".join(filter(None, [str(opp.year or ""), opp.make, opp.variant or opp.model]))
    lines = [
        f"*{name}*",
        "  " + "; ".join(reasons),
        f"  JP ${opp.price_usd:,.0f} → landed "
        f"{_chf(opp.landed_roro.landed_chf if opp.landed_roro else None)} (RoRo)",
        f"  CH p25 {_chf(opp.comps.swiss_p25)} across {opp.comps.comp_count} comps",
        f"  Gross {_chf(opp.gross_margin_chf)} ({_pct(opp.margin_pct)}), "
        f"net {_chf(opp.net_margin_chf)} after {opp.expected_holding_days}d of capital",
        f"  Liquidity {opp.liquidity_score:.2f} · tier {opp.capital_tier}",
    ]
    if opp.risk_flags:
        lines.append("  ⚠️ " + "; ".join(opp.risk_flags[:3]))
    if opp.url:
        lines.append(f"  {opp.url}")
    return "\n".join(lines)


async def archive_evidence(cfg: Config, opportunities: list[Opportunity],
                           out_dir: Path) -> int:
    """Screenshot + HTML of every alerted listing.

    Japanese stock turns over fast and exporters edit listings. When you are
    negotiating in three weeks, "the ad said 48,000 km on 22 August" is worth
    having in a file.
    """
    from ..http import Fetcher, screenshot

    if not cfg.catalog.archive_evidence_for_alerts:
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    async with Fetcher(cfg.sources.http) as fetcher:
        for opp in opportunities:
            if not opp.url:
                continue
            stem = out_dir / opp.jp_doc_id
            html = await fetcher.get(opp.url, use_cache=False)
            if html:
                stem.with_suffix(".html").write_text(html, encoding="utf-8")
                saved += 1
            await screenshot(opp.url, stem.with_suffix(".png"), cfg.sources.http)
    log.info("archived evidence for %d listings into %s", saved, out_dir)
    return saved


def send_alerts(cfg: Config, store: Store, *, dry_run: bool = False,
                weekly: bool = False, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    alerts = select_alerts(cfg, store, now=now)

    if weekly:
        digest = build_digest(cfg, store)
        alerts.append((f"weekly:{now.date().isoformat()}", weekly_portfolio(cfg, digest)))

    if not alerts:
        log.info("nothing crossed an alert threshold")
        return 0

    if dry_run:
        for _, message in alerts:
            print(message)
            print("-" * 60)
        log.info("dry run: %d alerts not sent", len(alerts))
        return len(alerts)

    sent = 0
    for key, message in alerts:
        delivered = False
        if cfg.alerts.channels.telegram and telegram.configured():
            delivered |= telegram.send(message)
        if cfg.alerts.channels.email and email_channel.configured(cfg.alerts.email):
            delivered |= email_channel.send(
                cfg.alerts.email, subject="nippon-margin alert", body=message
            )
        if delivered:
            store.mark_alert_sent(key, now)
            sent += 1
        else:
            log.warning("no channel delivered alert %s", key)
    log.info("sent %d/%d alerts", sent, len(alerts))
    return sent


def send_digest(cfg: Config, store: Store, *, dry_run: bool = False) -> bool:
    digest = build_digest(cfg, store)
    message = render_markdown(cfg, digest)
    if dry_run:
        print(message)
        return True
    ok = False
    if cfg.alerts.channels.telegram and telegram.configured():
        ok |= telegram.send(message)
    if cfg.alerts.channels.email and email_channel.configured(cfg.alerts.email):
        from .report import render_html

        ok |= email_channel.send(
            cfg.alerts.email,
            subject=f"nippon-margin digest {digest['day']}",
            body=message,
            html_body=render_html(cfg, digest),
        )
    return ok
