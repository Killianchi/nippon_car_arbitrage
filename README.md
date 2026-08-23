# nippon-margin

Finds cars that are worth more in Switzerland than they cost to import from
Japan, and ranks them by **margin per franc of capital per expected day of
holding**.

It runs itself: a GitHub Actions cron scrapes Japanese exporter stock and
Swiss classifieds every morning, computes the true landed cost of every
Japanese car (freight, Automobilsteuer, VAT, homologation, MFK, the lot),
matches each one against Swiss comparables, scores the spread, and pushes the
best few to Telegram. A static dashboard on Cloudflare Pages is the daily read.

```
Japanese exporters ─┐
                    ├─► catalog ─► landed cost ─► comps ─► score ─┬─► Telegram
Swiss classifieds ──┘     ▲                                       └─► dashboard
                          │                                            ▲
              encrypted SQLite on the `data` branch          static data.json
```

**No database service, no cloud account for storage.** Actions runners are
ephemeral, so the catalog lives in this repository: one SQLite file, gzipped
and AES-256-GCM encrypted, force-pushed to an orphan `data` branch each run.
The repo is public; the deal flow is not. The dashboard is a plain static
site that reads a JSON snapshot the run exports — no client SDK, no per-read
billing, and Cloudflare Access in front of it.

---

## The headline metric

```
opportunity_score = margin_pct × liquidity_score ÷ capital_weight
```

A 20% margin on a CHF 25k Macan beats a 20% margin on a CHF 200k G63, because
the Macan ties up an eighth of the money. Liquidity comes from how many Swiss
comps exist, how long they have been listed, and how many sellers have already
cut their price. Seasonality and risk flags apply multipliers on top.

Margin itself is deliberately conservative:

```
gross_margin = swiss_p25 × 0.93 − landed_chf
```

The 25th percentile, not the median — you are the motivated seller. Times 0.93
for negotiation and selling costs. Cost of capital is then subtracted
separately, because how long a car sits is a different question from what it
costs.

### Landed cost

Every franc between the Japanese ask and a car on Swiss plates:

| | |
|---|---|
| FOB × USD/CHF | the asking price in francs |
| + freight | RoRo CHF 3,500, or CHF 2,400/car in a 3-car container — **both** are always computed |
| + marine insurance | configurable, 0 by default |
| **= CIF** | **the customs value** |
| + customs duty | 0% under the Japan–CH FTA — *only with a certificate of origin from the exporter* |
| + Automobilsteuer | 4% of CIF |
| + VAT | 8.1% of (CIF + Automobilsteuer) |
| + homologation | COC, Form 13.20A, MFK, small fixes — CHF 2,000 default, per-model overrides |
| + agent & recon buffer | CHF 800 default |
| **= landed_chf** | |

Calibration case, pinned by a test: a G 63 at **$132,567 FOB** with USD/CHF at
0.80 lands at **CHF 125,965** (RoRo).

A C&F or CIF quote never gets our freight assumption added on top — SBT Japan
publishes a total price, and we use theirs rather than guessing.

---

## Quick start (local, no cloud)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

nippon-margin backfill --days 180   # ECB FX history
nippon-margin scrape                # ~90s across four sources
nippon-margin analyze
nippon-margin report --markdown
```

The catalog is `data/nippon.db`. That is the same file CI works on — it just
pulls it from the `data` branch first and pushes it back afterwards. To see
the dashboard against your own data:

```bash
nippon-margin export                        # -> dashboard/public/data.json
cd dashboard && npm install && npm run dev
```

### Commands

| | |
|---|---|
| `scrape` | fetch every enabled source, upsert the catalog, mark stale listings delisted |
| `analyze` | price each Japanese car against the Swiss pool, rank, write daily model stats |
| `report` | daily digest as Markdown (stdout) and/or HTML (`--out out/digest.html`) |
| `alert` | Telegram/email for whatever crossed a threshold (`--dry-run` to preview) |
| `backfill` | seed ECB FX history so the charts have a curve on day one |
| `export` | write the static JSON snapshot the dashboard reads |
| `sync pull` / `sync push` | restore / persist the encrypted catalog on the `data` branch |
| `doctor` | check config, credentials and catalog before a real run |
| `runs` | recent run health — per-adapter counts and errors |
| `sources` | list every known adapter |

Useful flags: `--dry-run` (parse and print, write nothing), `--source X` (run
one adapter, even if disabled in config), `--db PATH`, `--verbose`.

---

## Setup

```bash
gh auth login          # if you have not already
wrangler login         # npm install -g wrangler
./scripts/setup.sh
```

That script does everything that can be done from a terminal: it generates
the catalog encryption key and stores it as a GitHub secret, prompts for the
Telegram bot token and looks the chat id up for you, creates the Cloudflare
Pages project, and configures the Cloudflare Access policy that keeps the
dashboard private. Re-running it is safe — it never overwrites an existing
secret, and never regenerates the encryption key.

Then:

```bash
gh workflow run daily.yml
```

Verify it worked three ways: the `data` branch now holds `nippon.db.enc`, the
Telegram digest arrives, and `https://nippon-margin.pages.dev` shows the
Opportunities table with today's date in the header.

To pull the catalog down to your laptop afterwards:

```bash
DATA_ENCRYPTION_KEY=... nippon-margin sync pull
```

### If you would rather do it by hand

<details>
<summary>The same steps, manually</summary>

**1. Generate the catalog encryption key.** The catalog is committed to this
**public** repository, so it is encrypted. Keep a copy somewhere safe — losing
it does not break the scraper, but the stored catalog becomes unreadable and
every `first_seen` date and price-history point goes with it.

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

**2. Add the GitHub secrets.** Settings → Secrets and variables → Actions.

| name | value |
|---|---|
| `DATA_ENCRYPTION_KEY` | the key you just generated |
| `TELEGRAM_BOT_TOKEN` | from [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHAT_ID` | message your bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[0].message.chat.id` |
| `CLOUDFLARE_API_TOKEN` | My Profile → API Tokens → Create Token → **Cloudflare Pages: Edit** |
| `CLOUDFLARE_ACCOUNT_ID` | Workers & Pages → the ID in the sidebar |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | only if you enable email alerts |

`GITHUB_TOKEN` is provided automatically; the workflow uses it to push the
encrypted catalog, which is why `daily.yml` declares `contents: write`.

**3. Create the Pages project and lock it down.**

```bash
wrangler pages project create nippon-margin --production-branch=main
```

Then **Cloudflare dashboard → Zero Trust → Access → Applications → Add an
application → Self-hosted**:

- **Application domain**: `nippon-margin.pages.dev` (or your custom domain)
- **Policy**: Action *Allow*, Include → *Emails* → your address only
- Leave the default *Block* for everything else

This part is not optional. The Pages project has no access control of its own;
without the Access policy the dashboard is a public URL with your entire deal
flow on it. Sign-in is a one-time email code per device, which on a phone is a
single tap from the daily Telegram message. Both are free.

</details>

## Configuration

`config.yaml` is the source of truth for **every** business parameter — cost
assumptions, the watchlist, the model-code map, the risk table, scoring
weights, alert thresholds. Nothing is hardcoded in the engine. Changing the
watchlist is a git commit, which also gives you a history of what you believed
the numbers were on any given day.

The dashboard's **Watchlist page** is a builder, not an editor: it gives you
the `watchlist:` YAML block to paste, with a link straight to the GitHub edit
view. It deliberately cannot write anything. Keeping the watchlist in git is
the point — you get a dated record of what you were hunting and why, and
nothing in a browser can quietly change a tax rate.

Per-model overrides live on each watchlist entry:

```yaml
- key: porsche_911
  make: Porsche
  model: "911"
  aliases: ["996", "997", "991", "Carrera", "Turbo S", "GT3"]
  model_codes: ["997M9701", "996", "991"]
  body: coupe
  max_km: 120000
  min_grade: 4.0
  homologation_mfk_chf: 2500        # 911s cost more to put through the MFK
  risk_notes:
    - "997.1: bore scoring inspection mandatory before bidding"
    - "996: IMS bearing history required"
```

`model_code_map` bridges the two sides of the trade: Japanese exporters list
`463276`, Swiss sites list `G 63 AMG`. Without that table, comp matching finds
almost nothing.

---

## Sources

**Japan (buy side)** — four hand-written adapters, each verified against a
frozen capture of the real page in `tests/fixtures/`:

| source | how |
|---|---|
| `exportfrom.jp` | static grid + a labelled spec table on the detail page |
| `carused.jp` | parses the Next.js RSC payload rather than the DOM, which yields typed `model_code` / `grade` / `odometer` / `steering` over plain HTTP |
| `beforward.jp` | scrapes the make → numeric-id table at run time, then walks per-make stock lists |
| `sbtjapan.com` | same, plus it publishes a **total (C&F) price**, so we use their freight number instead of our estimate |

`japan-partner`, `tokyocarz`, `japanesecartrade` and `ts-export` ship as
declarative specs (`src/nippon_margin/adapters/specs.py`), disabled by
default — see *Adding a source* below.

**Switzerland (sell side)** — priority order, per the ToS reality:

1. **AutoScout24 official partner/API access** if you obtain it. There is no
   AutoScout24 scraper in this repo and there should not be: their ToS forbids
   it.
2. **AutoUncle.ch** (enabled) — aggregates AutoScout24, Autolina and comparis,
   and exposes the two fields the underlying portals hide: **days listed** and
   **price-change history**. That is the liquidity signal, and it is why this
   is the single highest-value source.
3. `autolina`, `carforyou`, `tutti`, `comparis` — polite, once-daily,
   disabled by default.
4. `ricardo` — **completed** auctions, i.e. real transaction prices rather
   than asking prices. The daily workflow runs this one on Sundays only;
   enable it in `config.yaml` once you have confirmed its selectors.

Every source can be disabled from `config.yaml` without touching code.

### Politeness

One request per 2 seconds per domain (enforced centrally — adapters cannot
opt out), robots.txt honoured, every response cached to disk and uploaded as a
30-day Actions artifact so a parse failure can be debugged offline instead of
by re-scraping.

One wrinkle worth knowing: the fetcher sends a **User-Agent and nothing else**.
Adding an `Accept` header makes beforward.jp answer a 2 KB stub under HTTP 202
instead of the real stock list. If you need extra headers for a specific
deployment, add them under `sources.http.extra_headers`.

### Adding a source

Most sites are the same shape — a repeating card with a link, a title, a price
and a spec strip. Describe that shape and you are done:

```python
# src/nippon_margin/adapters/specs.py
TOKYO_CARZ = SourceSpec(
    name="tokyocarz",
    base_url="https://www.tokyocarz.com",
    url_template="{base}/stock-list?keyword={query}&page={page}",
    card="div.stock-item",
    link="a[href]",
    title="h2, h3, .car-title",
    price=".price, .fob-price",
    default_terms="FOB",
)
JP_SPECS = {s.name: s for s in (..., TOKYO_CARZ)}
```

Then one line in `config.yaml`:

```yaml
  japan:
    tokyocarz: { enabled: true, max_pages: 3, renderer: "http" }
```

Iterate with `nippon-margin scrape --source tokyocarz --dry-run`,
which prints exactly what parsed. `{base}`, `{query}`, `{make}`, `{model}` and
`{page}` are substituted; set `renderer: "playwright"` for JS-rendered sites.

Sites that need real logic (a JSON payload, a runtime id lookup) get a module
in `adapters/jp/` or `adapters/ch/` subclassing `JpAdapter` / `ChAdapter` —
implement `search_urls()` and `parse_page()`; throttling, robots, caching,
rendering, pagination cut-off and error isolation are handled for you.

---

## Alerting

Fires when any of these happen, each rate-limited by a per-key cooldown:

- an opportunity crosses `alerts.opportunity_score_threshold` **and** clears
  the minimum margin in both % and francs;
- a new listing beats the best score ever recorded for its model;
- a tracked model's Japanese median price drops more than 5% in a week — the
  buy side just got cheaper while the Swiss ask did not move;
- USD/CHF or JPY/CHF moves more than 2% in a week. FX is a core margin driver,
  not background noise: because freight and the FOB price sit inside the
  customs value, Automobilsteuer and VAT amplify every FX move by ~12%.

Sundays also get a portfolio of the best five candidates in each capital tier
(<CHF 30k / 30–80k / >80k).

Every alerted listing is archived — full HTML plus a screenshot — into the
run's artifact. Japanese stock turns over fast and exporters edit listings;
when you are negotiating in three weeks, *"the ad said 48,000 km on 22 August"*
is worth having on disk.

---

## Cost, and what it runs on

Nothing here bills. The full stack is a GitHub repository, GitHub Actions
(~4 minutes a day, free for public repos), Cloudflare Pages with Access (free
tier), and Telegram. There is no database service and no storage account.

The catalog stays small on purpose:

- `sources.only_watchlist: true` discards listings that do not resolve to a
  watched model before anything is stored;
- the `data` branch is force-pushed rather than appended to, so history does
  not accumulate multi-hundred-KB binaries — point-in-time history lives
  *inside* the database (`price_history`, `model_stats_daily`) where it can
  actually be queried;
- the dashboard snapshot caps opportunities, photos and comp links, so the
  page stays a ~350 KB download on a phone rather than growing without bound.

Today: a 2.8 MB catalog compresses to a ~250 KB encrypted blob.

## Security posture

This repository is public; the data is not.

- The catalog on the `data` branch is AES-256-GCM encrypted with a key that
  only exists as a GitHub secret. Fresh salt and nonce per write, so two
  commits of identical data differ — git history leaks nothing about how much
  changed day to day. Tampering fails the auth tag rather than decrypting to
  garbage.
- `dashboard/public/data.json` is the whole catalog in plaintext and is
  **gitignored**. It is generated at build time and only ever served from
  behind Cloudflare Access.
- The dashboard is gated by Cloudflare Access, not by anything in the client
  bundle. There is no auth code to get wrong.
- Credentials are redacted from every git error this tool raises, so a failed
  push cannot print a token into an Actions log.
- Secrets are read from the environment only — never from `config.yaml`, so a
  config commit can never leak one.

## Tests

```bash
pytest -q          # 190 tests
ruff check src tests
```

Both run in CI on every push.

The cost engine, the comp matcher and the state encryption are tested hard,
because they have to be right — a wrong VAT base is a real financial decision,
and a broken state blob is the whole catalog. Scraping
is allowed to be flaky, so adapter tests run against **frozen captures of the
real pages** rather than the live sites. When a site redesigns, CI fails
loudly instead of the daily run quietly returning zero for a week:

```bash
python scripts/capture_fixture.py exportfrom   # refresh after fixing selectors
```

Several tests exist because of bugs found by running this against live data —
a Quattroporte filed as a GranTurismo, a BMW 3 Series "Gran Turismo" priced
against Maserati comps, an SLK matched as an SL, AutoUncle's savings badge
being read as a CHF 3,700 asking price for a 911, and trend windows anchored
to `date.today()` that reported "no movement" after a few failed runs.

`tests/test_statesync.py` runs against a real bare git repository rather than
mocks, because the failure being guarded against is "the push silently did not
authenticate", which a mock would never catch.

---

## Layout

```
config.yaml                  every business parameter
scripts/setup.sh             one-shot secrets + Cloudflare setup
src/nippon_margin/
  costs.py                   landed cost engine
  matching.py                comps, scoring, dedupe
  fx.py                      ECB rates + margin impact
  parse.py                   text → numbers, shared by all adapters
  http.py                    throttle, robots, cache, Playwright
  crypto.py                  AES-256-GCM for the committed catalog
  statesync.py               pull/push the catalog on the `data` branch
  adapters/                  base + registry + per-source modules
  pipeline/                  scrape → analyze → report → alert → export
  store/                     SQLite catalog
dashboard/                   Vite + React + Tailwind → Cloudflare Pages
tests/fixtures/              frozen captures of real listing pages
```

---

## Known limits

- **`carused.jp` blocks aggressive IPs.** The adapter is verified against a
  captured payload and parses 25 records from it, but the site returned 403 to
  the machine this was built on after repeated probing. Check its count on the
  Runs page after the first Actions run.
- **Second-tier adapters are unverified.** The four declarative Japanese
  sources and the four non-AutoUncle Swiss ones ship disabled with plausible
  selectors. Enable one and iterate with `--source X --dry-run`.
- **Comp quality is only as good as the mileage data.** A Swiss listing with
  no mileage is dropped rather than guessed at, because a 30k-km car and a
  200k-km car are not the same trade.
- **Auction grades are rarely published on export sites.** Where a grade is
  missing the risk flag is silent; confirm before bidding.
- **The dashboard is as fresh as the last run**, not live. The header shows
  the snapshot age and turns amber past 36 hours, which is the signal that the
  daily workflow has stopped working.
- **Losing `DATA_ENCRYPTION_KEY` loses the catalog history.** The scraper
  rebuilds from scratch, but `first_seen` dates and price history do not come
  back. Keep a copy outside GitHub.
