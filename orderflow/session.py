"""Crypto session boundaries: a 24/7 market with one reset per UTC day.

Why this replaces the Indian calendars
--------------------------------------
The Indian market needed per-segment calendars (NSE cash 15:30, NSE F&O 15:40,
MCX 23:30/23:55 seasonal) because those sessions genuinely end and the daily
reset is tied to a session open. Crypto has no session open: the venue trades
every second of every day.

What is left is one question: *where does a "day" start?* The answer used here
is **00:00 UTC**, which is 05:30 IST. That instant is the industry's daily
anchor: funding boundaries, daily candles and venue-reported daily volume all
roll on it. Anchoring the reset to IST midnight instead would split the most
liquid part of the global session across two days and put the cumulative-delta
line out of step with every chart the user compares against.

So the calendar keeps the `MarketCalendar` interface — `session_key`,
`session_start`, `is_open`, `bar_bucket_ts` — but every question resolves to a
UTC day rather than a trading window. Downstream code does not change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc

#: The daily boundary, as an hour offset from 00:00 UTC.
DAY_BOUNDARY_HOUR_UTC = 0

#: Crypto never closes. `is_open` exists for interface parity with the
#: equity calendars; it is always True.
ALWAYS_OPEN = True


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """One UTC day, treated as the trading window."""

    market: str
    trading_date: date
    open_at: datetime
    close_at: datetime
    label: str = "utc_day"

    def contains(self, ts: datetime) -> bool:
        return self.open_at <= ts < self.close_at

    def duration_seconds(self) -> float:
        return (self.close_at - self.open_at).total_seconds()

    def __str__(self) -> str:
        return (
            f"{self.market} {self.trading_date} "
            f"{self.open_at:%H:%M}-{self.close_at:%H:%M} UTC ({self.label})"
        )


def day_start(d: date) -> datetime:
    """The reset instant for UTC date `d`."""
    return datetime(
        d.year, d.month, d.day, DAY_BOUNDARY_HOUR_UTC, 0, tzinfo=UTC
    )


def trading_day_of(ts: datetime) -> date:
    """Which trading day `ts` belongs to.

    A timestamp before the boundary belongs to the previous day. With the
    boundary at 00:00 UTC this is just the UTC date, but the arithmetic is
    written out so a non-zero boundary stays correct.
    """
    ts = ts.astimezone(UTC)
    if ts.hour < DAY_BOUNDARY_HOUR_UTC:
        return (ts - timedelta(days=1)).date()
    return ts.date()


class MarketCalendar:
    """Answers time questions for a 24/7 crypto market.

    Kept API-compatible with the equity calendar it replaces, so
    `SessionChart` and `SessionTracker` work unchanged.
    """

    def __init__(self, market: str = "CRYPTO", holidays=None) -> None:
        self.market = market
        #: Accepted for interface parity. Crypto has no holidays; the parameter
        #: exists so a caller that passes one is not silently ignored.
        self.holidays = holidays or set()

    # -------------------------------------------------------------- sessions
    def session_for(self, d: date) -> SessionWindow:
        start = day_start(d)
        return SessionWindow(self.market, d, start, start + timedelta(days=1))

    def session_containing(self, ts: datetime) -> SessionWindow:
        return self.session_for(trading_day_of(ts))

    def is_open(self, ts: datetime) -> bool:
        return True

    # ------------------------------------------------------- session-relative
    def session_key(self, ts: datetime) -> int:
        """Stable integer key for the trading day containing `ts`.

        Never None: crypto has no closed period, so there are no off-session
        trades to drop.
        """
        return trading_day_of(ts).toordinal()

    def session_start(self, ts: datetime) -> datetime:
        return day_start(trading_day_of(ts))

    def session_elapsed(self, ts: datetime) -> float:
        return (ts.astimezone(UTC) - self.session_start(ts)).total_seconds()

    def next_open(self, ts: datetime) -> datetime:
        """The next daily boundary at or after `ts`."""
        ts = ts.astimezone(UTC)
        start = self.session_start(ts)
        return start if start > ts else start + timedelta(days=1)

    # ------------------------------------------------------------- bar grid
    def session_bars(self, ts: datetime, bar_seconds: float) -> int:
        """Which bar of the trading day `ts` falls in, from the daily boundary."""
        return int(self.session_elapsed(ts) // bar_seconds)

    def bar_bucket_ts(self, ts: datetime, bar_seconds: float) -> datetime:
        """The day-aligned start of the bar containing `ts`."""
        start = self.session_start(ts)
        offset = int((ts.astimezone(UTC) - start).total_seconds() // bar_seconds)
        return start + timedelta(seconds=offset * bar_seconds)


@dataclass(slots=True)
class SessionTracker:
    """Tracks daily rollovers and reports when a reset is due."""

    calendar: MarketCalendar
    _current_key: int | None = field(default=None, init=False)

    @property
    def current_key(self) -> int | None:
        return self._current_key

    def observe(self, ts: datetime) -> bool:
        """Feed a timestamp; True when a new trading day just started."""
        key = self.calendar.session_key(ts)
        if self._current_key is None:
            self._current_key = key
            return False
        if key != self._current_key:
            self._current_key = key
            return True
        return False

    def reset(self) -> None:
        self._current_key = None
