"""The Japanese auction condition scale, and the shapes it arrives in.

Adapters only ever see the grade as a scrap of page text, so the whole scale --
including S, which sits above 6 -- has to survive that round trip.
"""

from __future__ import annotations

import pytest

from nippon_margin.parse import GRADE_S, parse_grade


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("S", GRADE_S),
        ("Grade S", GRADE_S),
        ("grade: s", GRADE_S),
        ("6", 6.0),
        ("Grade 6", 6.0),
        ("5", 5.0),
        ("4.5", 4.5),
        ("Grade 4", 4.0),
        ("3.5", 3.5),
        ("R", 0.0),
        ("RA", 0.0),
        ("Grade R", 0.0),
        ("Grade RA", 0.0),
    ],
)
def test_parses_the_whole_scale(raw: str, expected: float) -> None:
    assert parse_grade(raw) == expected


def test_s_outranks_six() -> None:
    assert parse_grade("Grade S") > parse_grade("Grade 6")


@pytest.mark.parametrize("raw", [None, "", "Silver", "AT", "Unknown", "-"])
def test_no_grade_stated(raw: str | None) -> None:
    assert parse_grade(raw) is None


def test_a_digit_beats_a_stray_s() -> None:
    """`5 Speed` names a gearbox, not a grade -- but the 5 is still the grade."""
    assert parse_grade("Grade 5 S") == 5.0
