"""carused.jp -- LHD stock list.

The site is a Next.js app and looks like it needs a browser, but the App
Router streams the full vehicle records into the page as RSC flight data
(`self.__next_f.push([...])`). Those records are richer and far more stable
than the rendered DOM -- they carry `refno`, `model_code`, `grade`,
`odometer`, `engine_size`, `steering` and the FOB price as typed JSON.

So we parse the flight payload over plain HTTP and keep Playwright as a
config-flip fallback (`renderer: playwright` in config.yaml) for the day the
site stops server-rendering.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable

from ...models import JpListing, PriceTerms, Steering
from ...parse import parse_grade, parse_steering, parse_transmission
from ..base import JpAdapter

log = logging.getLogger(__name__)

# Records we care about always carry a `refno`; that is our anchor.
_ANCHOR = re.compile(r'"refno"\s*:\s*"')


class CarusedAdapter(JpAdapter):
    name = "carused"
    base_url = "https://carused.jp"

    def wait_selector(self) -> str | None:
        return "a[href*='/car-list/detail/']"

    def search_urls(self) -> Iterable[str]:
        # steering=2 is the site's LHD filter.
        for page in range(1, self.source_cfg.max_pages + 1):
            suffix = "" if page == 1 else f"&page={page}"
            yield f"{self.base_url}/car-list?steering=2{suffix}"
        # Per-model pages surface stock the generic list paginates away from.
        for item in self.cfg.watchlist:
            make = _slug(item.make)
            model = _slug(item.model)
            if make and model:
                yield f"{self.base_url}/car-list/{make}/{model}?steering=2"

    def parse_page(self, html: str, url: str) -> list[JpListing]:
        out: list[JpListing] = []
        seen: set[str] = set()
        for record in extract_records(html):
            refno = str(record.get("refno") or "").strip()
            if not refno or refno in seen or record.get("is_sold"):
                continue
            seen.add(refno)
            listing = self._from_record(record)
            if listing:
                out.append(self.finish(listing))
        return out

    def _from_record(self, r: dict[str, Any]) -> JpListing | None:
        make = str(r.get("make") or "").strip()
        model = str(r.get("model") or "").strip()
        if not make and not model:
            return None

        # `price` is the discounted FOB ask; `sale_price` is the list price.
        price = _num(r.get("price")) or _num(r.get("fob_price_for_fob_country")) or _num(
            r.get("sale_price")
        )

        item_url = str(r.get("item_url") or "")
        images = [str(r[k]) for k in ("thumbnail", "thumbnail_url") if r.get(k)]

        grade_raw = r.get("auction_grade") or r.get("grade_score")
        engine_cc = _num(r.get("engine_size"))

        return JpListing(
            source=self.name,
            source_ref=str(r.get("refno")),
            make=make,
            model=model,
            model_code=(str(r["model_code"]).strip() or None) if r.get("model_code") else None,
            variant=(str(r["grade"]).strip() or None) if r.get("grade") else None,
            year=_num(r.get("reg_year")),
            reg_month=_num(r.get("reg_month")),
            mileage_km=_num(r.get("odometer")),
            transmission=parse_transmission(str(r.get("mission") or "")),
            steering=parse_steering(str(r.get("steering") or "")) or Steering.UNKNOWN,
            fuel=(str(r["fuel"]).strip() or None) if r.get("fuel") else None,
            engine_cc=int(engine_cc) if engine_cc else None,
            color=(str(r["color"]).strip() or None) if r.get("color") else None,
            price_usd=float(price) if price else None,
            price_terms=PriceTerms.FOB,
            auction_grade=parse_grade(str(grade_raw)) if grade_raw else None,
            chassis_no=(str(r["chassis_no"]).strip() or None) if r.get("chassis_no") else None,
            description=" ".join(
                str(r.get(k)) for k in ("grade", "drivetrain", "engine_code") if r.get(k)
            ),
            image_urls=images,
            url=self.absolute(item_url),
        )


def extract_records(html: str) -> list[dict[str, Any]]:
    """Vehicle records out of the RSC flight payload.

    The payload arrives JSON-escaped inside a JS string literal, so a plain
    `json.loads` of the page is not on the table. We unescape once, then walk
    braces outward from each `"refno":` anchor to recover a complete object.
    """
    text = html.replace('\\\\"', '"').replace('\\"', '"')
    records: list[dict[str, Any]] = []
    for match in _ANCHOR.finditer(text):
        start = _object_start(text, match.start())
        if start is None:
            continue
        blob = _balanced_object(text, start)
        if not blob:
            continue
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("refno"):
            records.append(obj)
    return records


def _object_start(text: str, pos: int, *, window: int = 4000) -> int | None:
    """Index of the `{` that opens the object containing `pos`."""
    depth = 0
    for i in range(pos, max(pos - window, -1), -1):
        ch = text[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth == 0:
                return i
            depth -= 1
    return None


def _balanced_object(text: str, start: int, *, limit: int = 20_000) -> str | None:
    """The complete `{...}` beginning at `start`, respecting strings and escapes."""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, min(start + limit, len(text))):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _num(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
