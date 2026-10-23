"""The tape fetcher's id search must find a start that reaches the window.

The fetcher hit a real bug worth pinning. Paging only walks backwards, so the
start id must have its newest print at or after the target. The first version
inverted that test and returned an id *below* the window: the fetch then walked
away from the data and returned zero prints, while the search itself looked
fine. The invariant below is exactly the one that was violated.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import fetch_okx  # noqa: E402

#: A synthetic venue: print timestamps rise with tradeId, and nothing exists
#: below FLOOR. Sparse enough that a probe can land on an empty page.
FLOOR = 1_000_000
NEWEST_ID = 3_000_000
RATE = 1000  # ms per id


def _ts(trade_id: int) -> int:
    return 1_700_000_000_000 + trade_id * RATE


class FakeVenue:
    """Stands in for the endpoint. Counts calls so a slow search is visible."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, inst_id, after, limit=100, tries=5):
        self.calls += 1
        if after is None:
            return [{"tradeId": str(NEWEST_ID), "ts": str(_ts(NEWEST_ID))}]
        trade_id = int(after)
        if trade_id < FLOOR:
            return []
        return [{"tradeId": str(trade_id), "ts": str(_ts(trade_id))}]


@pytest.fixture
def venue(monkeypatch):
    fake = FakeVenue()
    monkeypatch.setattr(fetch_okx, "_get", fake)
    return fake


def reaches_target(trade_id: int, target_ms: int) -> bool:
    return bool(trade_id) and _ts(trade_id) > target_ms


class TestTheStartIdReachesTheWindow:
    def test_a_window_well_in_the_past_is_reached(self, venue):
        target = _ts(2_000_000)  # far below the newest id
        start = fetch_okx.find_trade_id_for("BTC-USDT-SWAP", target)
        assert start is not None
        assert reaches_target(start, target)

    def test_the_start_is_the_lowest_id_that_still_reaches(self, venue):
        """Not merely *a* valid start - the lowest, so paging is not wasteful.

        An id one below the answer must fail to reach; otherwise the search
        stopped early and the walk will re-cover prints it already has.
        """
        target = _ts(1_800_000)
        start = fetch_okx.find_trade_id_for("BTC-USDT-SWAP", target)
        assert reaches_target(start, target)
        assert not reaches_target(start - 1, target)

    def test_the_first_version_of_this_bug_would_fail(self, venue):
        """Regression guard: an id *below* the window must never be returned.

        This is the exact failure. An id whose newest print is older than the
        target looks like a valid bound to a naive bisection, and paging from it
        walks further away from the data.
        """
        target = _ts(2_500_000)
        start = fetch_okx.find_trade_id_for("BTC-USDT-SWAP", target)
        assert _ts(start) > target, "start id is below the window; the fetch would return nothing"

    def test_a_target_at_the_newest_print_returns_the_newest(self, venue):
        start = fetch_okx.find_trade_id_for("BTC-USDT-SWAP", _ts(NEWEST_ID))
        assert start == NEWEST_ID

    def test_a_target_after_the_newest_print_returns_the_newest(self, venue):
        start = fetch_okx.find_trade_id_for("BTC-USDT-SWAP", _ts(NEWEST_ID) + 10_000)
        assert start == NEWEST_ID

    def test_the_search_is_logarithmic_not_linear(self, venue):
        """It must not crawl the id space one step at a time.

        A linear walk over 2,000,000 ids is what made a real fetch look like it
        was working for an hour and then return nothing.
        """
        fetch_okx.find_trade_id_for("BTC-USDT-SWAP", _ts(1_500_000))
        assert venue.calls < 60, f"search took {venue.calls} requests; it should be ~log2(span)"

    def test_a_target_below_the_venue_floor_does_not_hang(self, venue):
        """Below the floor every probe is empty; it must still terminate."""
        start = fetch_okx.find_trade_id_for("BTC-USDT-SWAP", 0)
        assert start is not None  # the newest id; paging just finds nothing
        assert venue.calls < 60


class TestRangeCollection:
    def test_the_range_is_returned_oldest_first_and_deduplicated(self, venue, monkeypatch):
        """A page overlap must not double-count a print."""
        rows = [
            {"tradeId": str(FLOOR + i), "ts": str(_ts(FLOOR + i)), "px": "1", "sz": "1",
             "side": "buy", "instId": "BTC-USDT-SWAP"}
            for i in range(200)
        ]
        by_id = {r["tradeId"]: r for r in rows}

        def page(inst_id, after, limit=100, tries=5):
            if after is None:
                return [by_id[str(FLOOR + 199)]]
            start = int(after)
            window = [by_id[str(i)] for i in range(max(start - 99, FLOOR), start + 1)
                      if str(i) in by_id]
            # Overlap by one print on purpose: the real endpoint does this.
            return list(reversed(window)) or []

        monkeypatch.setattr(fetch_okx, "_get", page)
        out = fetch_okx.fetch_range(
            "BTC-USDT-SWAP", _ts(FLOOR), _ts(FLOOR + 199), quiet=True
        )
        ids = [r["tradeId"] for r in out]
        assert len(ids) == len(set(ids)), "duplicate prints survived collection"
        assert ids == sorted(ids, key=int), "prints are not oldest-first"
