"""nippon-margin command line.

Every command runs identically on a laptop and in GitHub Actions against the
same SQLite catalog, so what you debug locally is what runs at 07:00. The only
difference in CI is that the catalog is pulled from, and pushed back to, the
encrypted `data` branch around the run (`sync pull` / `sync push`).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .adapters.registry import all_source_names
from .config import load_config
from .store import DEFAULT_DB_PATH, open_store

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Japan → Switzerland car arbitrage engine.",
)
console = Console()


def _configure_logging(verbose: bool) -> None:
    load_dotenv()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _setup(verbose: bool, config_path: str, db: str | None = None):
    _configure_logging(verbose)
    cfg = load_config(config_path)
    store = open_store(cfg, db_path=db)
    # config.yaml is the whole truth now: the watchlist is edited by commit,
    # which is also what gives you a history of what you were hunting when.
    return cfg, store


# --------------------------------------------------------------------------
@app.command()
def scrape(
    source: str | None = typer.Option(None, "--source", "-s",
                                         help="Run one adapter only, even if disabled in config."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Parse and report, write nothing."),
    db: str | None = typer.Option(None, "--db", help="SQLite path (default data/nippon.db)."),
    config_path: str = typer.Option("config.yaml", "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Fetch every enabled source and upsert into the catalog."""
    from .pipeline.scrape import scrape as run_scrape

    if source and source not in all_source_names():
        raise typer.BadParameter(f"unknown source. Known: {', '.join(all_source_names())}")

    cfg, store = _setup(verbose, config_path, db)
    try:
        run = asyncio.run(run_scrape(cfg, store, only=source, dry_run=dry_run))
    finally:
        store.close()

    table = Table(title=f"scrape {run.id}", show_edge=False)
    table.add_column("source")
    table.add_column("ok", justify="center")
    table.add_column("listings", justify="right")
    table.add_column("secs", justify="right")
    table.add_column("error", overflow="fold")
    for result in run.adapters:
        table.add_row(
            result.source,
            "[green]✓[/]" if result.ok else "[red]✗[/]",
            str(result.count),
            f"{result.duration_s:.0f}",
            result.error or "",
        )
    console.print(table)
    console.print(f"[bold]{run.jp_count}[/] Japanese, [bold]{run.ch_count}[/] Swiss listings")
    if not run.ok:
        raise typer.Exit(code=1)


@app.command()
def analyze(
    db: str | None = typer.Option(None, "--db"),
    config_path: str = typer.Option("config.yaml", "--config", "-c"),
    top: int = typer.Option(15, "--top", help="How many rows to print."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Price every Japanese listing against the Swiss pool and rank it."""
    from .pipeline.analyze import analyze as run_analyze

    cfg, store = _setup(verbose, config_path, db)
    try:
        opportunities = run_analyze(cfg, store)
    finally:
        store.close()

    table = Table(title="top opportunities", show_edge=False)
    for name, justify in (
        ("#", "right"), ("car", "left"), ("JP $", "right"), ("landed", "right"),
        ("CH p25", "right"), ("margin", "right"), ("%", "right"),
        ("liq", "right"), ("score", "right"), ("tier", "left"), ("flags", "right"),
    ):
        table.add_column(name, justify=justify)

    for i, o in enumerate([x for x in opportunities if x.opportunity_score > 0][:top], 1):
        table.add_row(
            str(i),
            " ".join(filter(None, [str(o.year or ""), o.make, (o.variant or o.model)[:22]])),
            f"{o.price_usd:,.0f}" if o.price_usd else "-",
            f"{o.landed_roro.landed_chf:,.0f}" if o.landed_roro else "-",
            f"{o.comps.swiss_p25:,.0f}" if o.comps.swiss_p25 else "-",
            f"{o.gross_margin_chf:,.0f}" if o.gross_margin_chf else "-",
            f"{o.margin_pct * 100:.0f}%" if o.margin_pct else "-",
            f"{o.liquidity_score:.2f}",
            f"{o.opportunity_score:.3f}",
            o.capital_tier,
            str(len(o.risk_flags)),
        )
    console.print(table)
    console.print(f"{len(opportunities)} priced, "
                  f"{sum(1 for o in opportunities if o.opportunity_score > 0)} with positive margin")


@app.command()
def report(
    out: Path | None = typer.Option(None, "--out", "-o", help="Write the HTML digest here."),
    markdown: bool = typer.Option(False, "--markdown", help="Print Markdown to stdout."),
    top: int = typer.Option(10, "--top"),
    db: str | None = typer.Option(None, "--db"),
    config_path: str = typer.Option("config.yaml", "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Render the daily digest as HTML and/or Markdown."""
    from .pipeline.report import build_digest, render_html, render_markdown

    cfg, store = _setup(verbose, config_path, db)
    try:
        digest = build_digest(cfg, store, top_n=top)
        md = render_markdown(cfg, digest)
        if markdown or not out:
            console.print(md)
        if out:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(render_html(cfg, digest), encoding="utf-8")
            console.print(f"[green]wrote[/] {out}")
    finally:
        store.close()


@app.command()
def alert(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print alerts instead of sending."),
    digest: bool = typer.Option(False, "--digest", help="Send the full daily digest too."),
    weekly: bool = typer.Option(False, "--weekly", help="Include the weekly tier portfolio."),
    evidence: bool = typer.Option(False, "--evidence",
                                  help="Archive HTML + screenshots of alerted listings."),
    evidence_dir: Path = typer.Option(Path("evidence"), "--evidence-dir"),
    db: str | None = typer.Option(None, "--db"),
    config_path: str = typer.Option("config.yaml", "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Send Telegram/email alerts for whatever crossed a threshold."""
    from .pipeline.alert import archive_evidence, send_alerts, send_digest

    cfg, store = _setup(verbose, config_path, db)
    try:
        count = send_alerts(cfg, store, dry_run=dry_run, weekly=weekly)
        if digest:
            send_digest(cfg, store, dry_run=dry_run)
        if evidence and not dry_run:
            top = [
                o for o in store.load_opportunities(limit=50)
                if o.opportunity_score >= cfg.alerts.opportunity_score_threshold
            ][:10]
            asyncio.run(archive_evidence(cfg, top, evidence_dir))
    finally:
        store.close()
    console.print(f"{count} alert(s) {'previewed' if dry_run else 'sent'}")


@app.command()
def backfill(
    fx: bool = typer.Option(True, "--fx/--no-fx", help="Backfill ECB FX history."),
    days: int = typer.Option(90, "--days"),
    db: str | None = typer.Option(None, "--db"),
    config_path: str = typer.Option("config.yaml", "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Seed historical data so the charts have a curve on day one."""
    from .fx import backfill_fx
    from .http import Fetcher

    cfg, store = _setup(verbose, config_path, db)

    async def _run() -> int:
        async with Fetcher(cfg.sources.http) as fetcher:
            return await backfill_fx(fetcher, store, days=days) if fx else 0

    try:
        count = asyncio.run(_run())
    finally:
        store.close()
    console.print(f"[green]backfilled[/] {count} FX days")


@app.command()
def export(
    out: Path = typer.Option(Path("dashboard/public/data.json"), "--out", "-o"),
    db: str | None = typer.Option(None, "--db"),
    config_path: str = typer.Option("config.yaml", "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Write the static JSON snapshot the dashboard reads."""
    from .pipeline.export import export as run_export

    cfg, store = _setup(verbose, config_path, db)
    try:
        path = run_export(cfg, store, out)
    finally:
        store.close()
    console.print(f"[green]wrote[/] {path} ({path.stat().st_size / 1024:.0f} KB)")


sync_app = typer.Typer(no_args_is_help=True, help="Move the catalog to and from the `data` branch.")
app.add_typer(sync_app, name="sync")


@sync_app.command("pull")
def sync_pull(
    db: str | None = typer.Option(None, "--db"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Restore the encrypted catalog from the `data` branch."""
    from .statesync import pull

    _configure_logging(verbose)
    target = Path(db or DEFAULT_DB_PATH)
    restored = pull(target)
    if restored:
        console.print(f"[green]restored[/] {target} ({target.stat().st_size / 1024:.0f} KB)")
    else:
        console.print("[yellow]no stored state[/] — starting a fresh catalog")


@sync_app.command("push")
def sync_push(
    db: str | None = typer.Option(None, "--db"),
    message: str | None = typer.Option(None, "--message", "-m"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Encrypt the catalog and force-push it to the `data` branch."""
    from .statesync import push

    _configure_logging(verbose)
    target = Path(db or DEFAULT_DB_PATH)
    push(target, message=message)
    console.print(f"[green]pushed[/] {target} to the data branch")


@app.command()
def sources():
    """List every known source adapter."""
    for name in all_source_names():
        console.print(f"  {name}")


@app.command()
def doctor(
    db: str | None = typer.Option(None, "--db"),
    config_path: str = typer.Option("config.yaml", "--config", "-c"),
):
    """Check config, credentials and store connectivity before a real run."""
    import os

    load_dotenv()
    cfg = load_config(config_path)
    console.print(f"[green]✓[/] config parsed: {len(cfg.watchlist)} watched models")

    enabled_jp = [k for k, v in cfg.sources.japan.items() if v.enabled]
    enabled_ch = [k for k, v in cfg.sources.switzerland.items() if v.enabled]
    console.print(f"[green]✓[/] sources enabled: JP {enabled_jp} · CH {enabled_ch}")

    for var, label in (
        ("TELEGRAM_BOT_TOKEN", "Telegram token"),
        ("TELEGRAM_CHAT_ID", "Telegram chat id"),
    ):
        mark = "[green]✓[/]" if os.environ.get(var) else "[yellow]○[/]"
        console.print(f"{mark} {label}")

    key = os.environ.get("DATA_ENCRYPTION_KEY", "").strip()
    if not key:
        console.print("[green]✓[/] catalog stored unencrypted (plain gzip) — "
                      "set DATA_ENCRYPTION_KEY to encrypt it")
    elif len(key) < 16:
        console.print("[red]✗[/] DATA_ENCRYPTION_KEY is too short (use 32 random "
                      "chars, or unset it to store the catalog unencrypted)")
    else:
        console.print("[green]✓[/] catalog will be encrypted (AES-256-GCM)")

    try:
        store = open_store(cfg, db_path=db)
        runs_seen = store.recent_runs(limit=1)
        fx = store.latest_fx()
        console.print(f"[green]✓[/] catalog reachable; last run: "
                      f"{runs_seen[0].id if runs_seen else 'none yet'}")
        console.print(
            f"{'[green]✓[/]' if fx else '[yellow]○[/]'} FX: "
            + (f"{fx.day} USD/CHF {fx.usd_chf:.4f}" if fx else "none stored -- run `backfill`")
        )
        store.close()
    except Exception as exc:  # noqa: BLE001 - this command exists to report exactly this
        console.print(f"[red]✗[/] catalog unreachable: {exc}")
        raise typer.Exit(code=1) from exc


@app.command()
def runs(
    limit: int = typer.Option(10, "--limit", "-n"),
    db: str | None = typer.Option(None, "--db"),
    config_path: str = typer.Option("config.yaml", "--config", "-c"),
):
    """Show recent run health -- per-adapter counts and errors."""
    cfg, store = _setup(False, config_path, db)
    try:
        table = Table(title="recent runs", show_edge=False)
        for col in ("run", "ok", "JP", "CH", "sources", "errors"):
            table.add_column(col, overflow="fold")
        for run in store.recent_runs(limit=limit):
            table.add_row(
                run.id,
                "[green]✓[/]" if run.ok else "[red]✗[/]",
                str(run.jp_count),
                str(run.ch_count),
                ", ".join(f"{a.source}:{a.count}" for a in run.adapters),
                "; ".join(run.errors[:2]),
            )
        console.print(table)
    finally:
        store.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
