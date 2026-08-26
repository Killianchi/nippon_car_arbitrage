#!/usr/bin/env python3
"""Re-capture an adapter test fixture from the live site.

When a source redesigns, `tests/test_adapters.py` fails. Fix the selectors,
then refresh the frozen page so the tests guard the *new* markup:

    python scripts/capture_fixture.py exportfrom
    python scripts/capture_fixture.py --url https://... --name au911

Fixtures are gzipped because a raw listing page is ~750 KB.
"""

from __future__ import annotations

import argparse
import gzip
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

KNOWN = {
    "exportfrom": "https://exportfrom.jp/stock-list/left-hand-cars",
    "carused": "https://carused.jp/car-list?steering=2",
    "au911": "https://www.autouncle.ch/en/used-cars/porsche/911",
    "beforward": "https://www.beforward.jp/stocklist/",
    "sbtjapan": "https://www.sbtjapan.com/used-cars/",
    "goonet": "https://www.goo-net-exchange.com/usedcars/PORSCHE/911/index.html",
}

# Match the scraper: UA only. An explicit Accept header trips beforward.jp's
# bot challenge, and a fixture captured through that would be a 2 KB stub.
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", nargs="?", choices=sorted(KNOWN),
                        help="a known fixture name")
    parser.add_argument("--url", help="capture an arbitrary URL instead")
    parser.add_argument("--name", dest="out_name", help="fixture name for --url")
    args = parser.parse_args()

    if args.url:
        name = args.out_name or "custom"
        url = args.url
    elif args.name:
        name, url = args.name, KNOWN[args.name]
    else:
        parser.error("give a known fixture name, or --url with --name")
        return 2

    print(f"fetching {url}")
    resp = httpx.get(url, headers={"User-Agent": UA}, timeout=60, follow_redirects=True)
    if resp.status_code != 200:
        print(f"HTTP {resp.status_code} -- not saving", file=sys.stderr)
        return 1
    if len(resp.text) < 20_000:
        print(f"only {len(resp.text)} bytes -- that looks like a bot-challenge "
              f"stub, not a listing page. Not saving.", file=sys.stderr)
        return 1

    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = FIXTURES / f"{name}.html.gz"
    out.write_bytes(gzip.compress(resp.text.encode("utf-8"), 9))
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB gzipped "
          f"from {len(resp.text) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
