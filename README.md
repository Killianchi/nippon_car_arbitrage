# nippon-margin

Finds cars that are worth more in Switzerland than they cost to import from
Japan, and ranks them by **margin per franc of capital per expected day of
holding**.

It runs itself: a GitHub Actions cron scrapes Japanese exporter stock and
Swiss classifieds every morning, computes the true landed cost of every
Japanese car (freight, Automobilsteuer, VAT, homologation, MFK, the lot),
matches each one against Swiss comparables, scores the spread, and pushes the
best few to Telegram. A Firebase-hosted dashboard is the daily read.

```
Japanese exporters ─┐
                    ├─► catalog (Firestore) ─► landed cost ─► comps ─► score ─┬─► Telegram
Swiss classifieds ──┘                                                          └─► dashboard
```

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

nippon-margin backfill --local --days 180   # ECB FX history
nippon-margin scrape   --local              # ~90s across four sources
nippon-margin analyze  --local
nippon-margin report   --local --markdown
```

`--local` uses SQLite at `data/nippon.db` and touches no Firebase quota.
Everything below runs identically against Firestore without the flag.

### Commands

| | |
|---|---|
| `scrape` | fetch every enabled source, upsert the catalog, mark stale listings delisted |
| `analyze` | price each Japanese car against the Swiss pool, rank, write daily model stats |
| `report` | daily digest as Markdown (stdout) and/or HTML (`--out out/digest.html`) |
| `alert` | Telegram/email for whatever crossed a threshold (`--dry-run` to preview) |
| `backfill` | seed ECB FX history so the charts have a curve on day one |
| `doctor` | check config, credentials and store connectivity before a real run |
| `runs` | recent run health — per-adapter counts and errors |
| `sources` | list every known adapter |

Useful flags: `--dry-run` (parse and print, write nothing), `--source X` (run
one adapter, even if disabled in config), `--verbose`.

---

## What you have to do by hand

Four things need a human. Everything else is committed.

### 1. Create the Firebase project and a service account

```bash
npm install -g firebase-tools
firebase login
firebase projects:list            # pick one, or:
firebase projects:create nippon-margin
```

Then in the [Firebase console](https://console.firebase.google.com):

1. **Build → Firestore Database → Create database.** Production mode.
   Pick `eur3` (Europe) unless you have a reason not to.
2. **Build → Authentication → Get started.** Enable **Email/Password** and/or
   **Google**. Add yourself under **Users** if using email/password.
3. **⚙ Project settings → Service accounts → Generate new private key.**
   A JSON file downloads. **Do not commit it** — `.gitignore` already blocks
   `service-account*.json`, but the file belongs in a password manager.
4. **⚙ Project settings → General → Your apps → Add app → Web.** Copy the
   config values; you need `apiKey`, `authDomain`, `projectId`, `appId`.

Put your project id in `config.yaml` under `meta.firebase_project_id`.

### 2. Put your Firebase UID into the security rules

`firestore.rules` allow-lists a single UID. Find yours in
**Authentication → Users → User UID**, then:

```bash
# firestore.rules
function allowedUids() {
  return ['abc123YourActualUid'];   # <- replace REPLACE_WITH_YOUR_FIREBASE_UID
}
```

```bash
firebase deploy --only firestore:rules,firestore:indexes --project <your-project>
```

The rules deny **every** client write except the watchlist editor doc, and
gate all reads on that UID list. Never loosen `isOwner()` to
`request.auth != null` — that would let anyone with a Google account read your
deal flow.

### 3. Add the GitHub secrets and variables

**Settings → Secrets and variables → Actions.**

Secrets (encrypted):

| name | value |
|---|---|
| `FIREBASE_SERVICE_ACCOUNT` | the **entire contents** of the service-account JSON, pasted as one value |
| `TELEGRAM_BOT_TOKEN` | from [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHAT_ID` | message your bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[0].message.chat.id` |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | only if you enable email alerts |

Variables (plain — these are public by design; they identify the project, they
authorise nothing):

`FIREBASE_API_KEY`, `FIREBASE_AUTH_DOMAIN`, `FIREBASE_PROJECT_ID`,
`FIREBASE_APP_ID`

### 4. Trigger the first run

**Actions → daily → Run workflow.** It takes ~3 minutes. Optional inputs let
you run a single adapter or do a dry run first.

Verify with **Firestore → Data**: you should see `listings_jp`, `listings_ch`,
`opportunities`, `model_stats_daily`, `fx_rates`, `runs` and `summaries`.

The dashboard deploys automatically after a successful run, and on any push to
`main` that touches `dashboard/`. Its URL is
`https://<project-id>.web.app`.

Locally:

```bash
cd dashboard && cp .env.example .env   # fill in the four values
npm install && npm run dev
```

---

## Configuration

`config.yaml` is the source of truth for **every** business parameter — cost
assumptions, the watchlist, the model-code map, the risk table, scoring
weights, alert thresholds. Nothing is hardcoded in the engine. Changing the
watchlist is a git commit, which also gives you a history of what you believed
the numbers were on any given day.

The one exception: the dashboard's **watchlist editor** writes
`config/watchlist` in Firestore, which the next run merges over the file. Cost
parameters are deliberately *not* editable from a browser.

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
   than asking prices. Weekly cadence is enough.

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

Iterate with `nippon-margin scrape --source tokyocarz --local --dry-run`,
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

## Cost control

Firestore bills per document read and write, so:

- writes are batched 500 at a time;
- each listing carries a content fingerprint — an unchanged car costs a
  `last_seen` touch, not a full rewrite;
- the daily run precomputes `summaries/opportunities` and `summaries/last_run`,
  so opening the dashboard is a handful of reads rather than a few thousand;
- `sources.only_watchlist: true` discards listings that do not resolve to a
  watched model before anything is stored.

The intended steady state is comfortably inside the free tier.

---

## Tests

```bash
pytest -q          # 156 tests
ruff check src tests
```

Both run in CI on every push.

The cost engine and comp matcher are tested hard, because they have to be
right — a wrong VAT base or a bad comp is a real financial decision. Scraping
is allowed to be flaky, so adapter tests run against **frozen captures of the
real pages** rather than the live sites. When a site redesigns, CI fails
loudly instead of the daily run quietly returning zero for a week:

```bash
python scripts/capture_fixture.py exportfrom   # refresh after fixing selectors
```

Several tests exist because of bugs found by running this against live data —
a Quattroporte filed as a GranTurismo, a BMW 3 Series "Gran Turismo" priced
against Maserati comps, an SLK matched as an SL, and AutoUncle's savings badge
being read as a CHF 3,700 asking price for a 911.

---

## Layout

```
config.yaml                  every business parameter
firestore.rules              UID allow-list; client writes denied
src/nippon_margin/
  costs.py                   landed cost engine
  matching.py                comps, scoring, dedupe
  fx.py                      ECB rates + margin impact
  parse.py                   text → numbers, shared by all adapters
  http.py                    throttle, robots, cache, Playwright
  adapters/                  base + registry + per-source modules
  pipeline/                  scrape → analyze → report → alert
  store/                     SQLite (--local) and Firestore backends
dashboard/                   Vite + React + Tailwind, deployed to Hosting
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
