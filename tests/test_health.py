"""The watchdog, tested against the two outages it exists to catch.

Both were silent: the daily run reported success and the dashboard was dead.
A check that shared the pipeline's code or its database would have reported
success too, so these feed it the shapes the published page actually took.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nippon_margin.pipeline.health import check_snapshot, describe, load

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


def snapshot(**over):
    base = {
        "generated_at": (NOW - timedelta(hours=2)).isoformat(),
        "opportunities": [
            {"jp_doc_id": f"goonet_{i}", "comps": {"comp_count": 3}} for i in range(4)
        ] + [
            {"jp_doc_id": f"sbtjapan_{i}", "comps": {"comp_count": 2}} for i in range(4)
        ],
    }
    base.update(over)
    return base


def codes(payload, **kw):
    return [p.code for p in check_snapshot(payload, now=NOW, **kw)]


class TestAWorkingDashboard:
    def test_a_healthy_snapshot_has_no_problems(self):
        assert check_snapshot(snapshot(), now=NOW) == []

    def test_a_quiet_market_is_not_a_fault(self):
        """Few opportunities is a market condition. Zero is a bug."""
        few = snapshot(opportunities=[
            {"jp_doc_id": "goonet_1", "comps": {"comp_count": 1}},
            {"jp_doc_id": "sbtjapan_2", "comps": {"comp_count": 1}},
        ])
        assert codes(few, min_opportunities=2) == []

    def test_a_run_a_few_hours_late_does_not_page(self):
        """The cron asks for 05:00 and has started as late as 13:40."""
        late = snapshot(generated_at=(NOW - timedelta(hours=30)).isoformat())
        assert codes(late) == []


class TestTheNovemberOutage:
    """13 November: AutoUncle redesigned, the Swiss year went None, comps need
    one on both sides, so every opportunity scored zero and the export wrote an
    empty array. Runs were green for three days."""

    def test_an_empty_page_is_caught(self):
        assert "empty" in codes(snapshot(opportunities=[]))

    def test_a_page_whose_rows_all_lost_their_comps_is_caught(self):
        """The subtler form: rows are published but none priced against
        anything, so every margin on the page is meaningless."""
        dead = snapshot(opportunities=[
            {"jp_doc_id": f"goonet_{i}", "comps": {"comp_count": 0}} for i in range(40)
        ] + [
            {"jp_doc_id": f"sbtjapan_{i}", "comps": {"comp_count": 0}} for i in range(40)
        ])
        assert "no_comps" in codes(dead)

    def test_one_surviving_source_is_caught(self):
        """Three Japanese adapters have broken on redesigns so far. Losing all
        but one looks like a full page and is a single site's stock."""
        one = snapshot(opportunities=[
            {"jp_doc_id": f"goonet_{i}", "comps": {"comp_count": 3}} for i in range(30)
        ])
        assert "thin_sources" in codes(one, min_sources=2)


class TestTheOctoberOutage:
    """4 October: selectolax 1.0 removed the backend the adapters import. The
    run died before writing anything, so the catalog's log simply stopped and
    the page froze. 13 days."""

    def test_a_stale_page_is_caught(self):
        frozen = snapshot(generated_at=(NOW - timedelta(days=24)).isoformat())
        problems = check_snapshot(frozen, now=NOW)
        assert "stale" in [p.code for p in problems]
        assert "24" in str(problems[0]) or "576" in str(problems[0])

    def test_a_missing_timestamp_is_caught(self):
        assert "no_timestamp" in codes(snapshot(generated_at=None))

    def test_an_unparseable_timestamp_is_caught(self):
        assert "no_timestamp" in codes(snapshot(generated_at="last Tuesday"))


class TestMalformedInput:
    @pytest.mark.parametrize("payload", [[], "", 7, None])
    def test_a_non_object_snapshot_is_unreadable(self, payload):
        assert codes(payload) == ["unreadable"]

    def test_a_missing_opportunities_key_is_caught(self):
        assert "no_opportunities_key" in codes({"generated_at": NOW.isoformat()})

    def test_broken_json_becomes_a_problem_not_an_exception(self):
        assert check_snapshot(load("{not json"), now=NOW)


class TestTheMessage:
    def test_a_healthy_check_says_nothing(self):
        assert describe([], url="https://example.test") == ""

    def test_the_message_names_the_url_and_every_problem(self):
        problems = check_snapshot(snapshot(opportunities=[], generated_at=None), now=NOW)
        text = describe(problems, url="https://example.test/data.json")
        assert "https://example.test/data.json" in text
        for problem in problems:
            assert problem.detail in text
