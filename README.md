# nippon-margin catalog

`nippon.db.gz` is the scraper's SQLite catalog, gzipped, behind an 8-byte `NMPLAIN1` header
(see `src/nippon_margin/crypto.py` on `main`).

This branch is force-pushed by the daily workflow and shares no history
with the source. Do not merge it into `main`.

Restore locally with:

    nippon-margin sync pull

Or by hand:

    tail -c +9 nippon.db.gz | gunzip > nippon.db
