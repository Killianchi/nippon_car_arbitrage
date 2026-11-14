"""The watchdog: does the *published* dashboard still say anything useful?

Every outage so far was silent. The daily run went green, the page
regenerated, and the data was empty or wrong:

* 4 October -- selectolax 1.0 removed the backend the adapters import. The run
  died before writing its run record, so the catalog's own log just stopped
  and nothing anywhere said "failed". 13 days.
* 13 November -- AutoUncle redesigned and the Swiss year went None. Comps need
  a year on both sides, so all 451 opportunities came out with zero comps and
  the export wrote an empty array. Runs reported ok=True throughout. 3 days.

Both would have been caught in a day by looking at the finished artefact
rather than at the job's exit code, which is what this does. It reads the
published JSON over HTTP -- deliberately from outside the pipeline, with none
of its code or its database in the loop -- and asks whether a person opening
the dashboard would see anything.

The checks are about *shape*, not about whether a given car is a bargain: a
quiet market is not a fault, but a market with no cars in it is.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["Problem", "check_snapshot", "describe"]


@dataclass(frozen=True)
class Problem:
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def _parse_when(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def check_snapshot(
    payload: Any,
    *,
    now: datetime | None = None,
    max_age_hours: float = 36.0,
    min_opportunities: int = 1,
    min_sources: int = 1,
) -> list[Problem]:
    """Everything wrong with a published `data.json`, worst first.

    An empty list means a reader would see a working dashboard. The caller
    decides what to do about a non-empty one; nothing here alerts or exits.
    """
    now = now or datetime.now(UTC)
    problems: list[Problem] = []

    if not isinstance(payload, dict):
        return [Problem("unreadable", f"snapshot is {type(payload).__name__}, not an object")]

    generated = _parse_when(payload.get("generated_at"))
    if generated is None:
        problems.append(Problem("no_timestamp", "generated_at is missing or unparseable"))
    else:
        age_hours = (now - generated).total_seconds() / 3600
        if age_hours > max_age_hours:
            problems.append(Problem(
                "stale",
                f"generated {age_hours:.0f}h ago ({generated:%Y-%m-%d %H:%M} UTC), "
                f"limit {max_age_hours:.0f}h -- the daily run is not finishing",
            ))

    opportunities = payload.get("opportunities")
    if not isinstance(opportunities, list):
        problems.append(Problem("no_opportunities_key", "opportunities is missing or not a list"))
        return problems

    if len(opportunities) < min_opportunities:
        problems.append(Problem(
            "empty",
            f"{len(opportunities)} opportunities published (want at least "
            f"{min_opportunities}) -- the pipeline ran but priced nothing",
        ))
        return problems

    # A page full of rows that all failed to find a comp is the 13 November
    # failure exactly: it looks populated from the outside and is worthless.
    with_comps = [
        o for o in opportunities
        if isinstance(o, dict) and ((o.get("comps") or {}).get("comp_count") or 0) > 0
    ]
    if not with_comps:
        problems.append(Problem(
            "no_comps",
            f"none of {len(opportunities)} opportunities has a single Swiss comp -- "
            "the sell side is not matching, so no margin on the page is real",
        ))

    sources = {
        str(o.get("jp_doc_id", "")).split("_")[0]
        for o in opportunities if isinstance(o, dict) and o.get("jp_doc_id")
    }
    sources.discard("")
    if len(sources) < min_sources:
        problems.append(Problem(
            "thin_sources",
            f"opportunities come from {len(sources)} source(s) {sorted(sources)} "
            f"(want at least {min_sources}) -- an adapter has probably stopped parsing",
        ))

    return problems


def describe(problems: list[Problem], *, url: str) -> str:
    """A Telegram-ready message. Empty string when there is nothing wrong."""
    if not problems:
        return ""
    lines = [f"*nippon-margin watchdog* — {len(problems)} problem(s) with the published dashboard", ""]
    lines += [f"• {p.detail}" for p in problems]
    lines += ["", url]
    return "\n".join(lines)


def load(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        return {"generated_at": None, "_parse_error": str(exc)}
