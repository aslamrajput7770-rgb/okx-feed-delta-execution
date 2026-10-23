"""Risk management: position sizing caps, stop loss, take profit, daily loss limit.

Why this module exists
----------------------
Before it, the runner computed a signal, sent **one** order, and exited. Nothing
watched the resulting position. A stop loss is not a setting that can be added to
that program: it needs a position that is tracked and an exit path that can
close it. So this module owns three things the runner previously had no concept
of:

* **A position** — what is open, at what price, in what size.
* **An exit rule** — when to close, evaluated on every price tick.
* **A daily budget** — how much can be lost before the day is over.

Units
-----
PnL is measured in the **quote currency** (USD), because that is the number a
trader compares against an account balance. Sizes are base currency (BTC), as
everywhere else in this repository. `Position.pnl_quote()` is the only place the
two are combined.

Purity
------
Nothing here touches the network, the clock, or the disk. Time is passed in,
PnL is passed in, and the persistence of the daily counter lives in
`RiskState` -> a plain dict that the caller writes wherever it likes. That is
what makes every rule in this file testable offline, which matters more here
than anywhere else: a risk rule that is only exercised against a live venue is a
risk rule nobody has checked.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------- limits


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """The numbers that bound what a single run can lose.

    Every one of these is a hard refusal, not a warning. The defaults are
    deliberately small: this repository's own documentation calls the execution
    path a plumbing demonstration, so the default posture is "do not lose much"
    rather than "trade the strategy".

    Set any percentage to 0 to disable that rule. Disabling is explicit and
    visible in the log; there is no silent fallback.
    """

    #: Close the position at this loss, as a percent of entry price.
    stop_loss_pct: float = 1.0
    #: Close the position at this gain, as a percent of entry price.
    take_profit_pct: float = 2.0
    #: Largest position, in base currency. A signal asking for more is refused.
    max_position_base: float = 0.01
    #: Stop trading for the day once realized loss reaches this, in quote (USD).
    max_daily_loss_quote: float = 50.0
    #: Close regardless after this long, so a position cannot be forgotten.
    max_hold_seconds: float = 3600.0

    def __post_init__(self) -> None:
        for name in ("stop_loss_pct", "take_profit_pct", "max_position_base",
                     "max_daily_loss_quote", "max_hold_seconds"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0, got {getattr(self, name)}")

    @property
    def stop_loss_enabled(self) -> bool:
        return self.stop_loss_pct > 0

    @property
    def take_profit_enabled(self) -> bool:
        return self.take_profit_pct > 0

    @property
    def daily_loss_enabled(self) -> bool:
        return self.max_daily_loss_quote > 0

    @property
    def hold_limit_enabled(self) -> bool:
        return self.max_hold_seconds > 0


# ------------------------------------------------------------------- position


@dataclass(slots=True)
class Position:
    """An open position, tracked from fill to close."""

    side: str  # "buy" (long) | "sell" (short)
    entry_price: float
    size_base: float
    contracts: int
    product_id: int
    entry_ts: float
    #: The client order id of the entry, so the exit can reference it.
    entry_client_id: str | None = None

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {self.side!r}")
        if self.entry_price <= 0:
            raise ValueError(f"entry_price must be > 0, got {self.entry_price}")
        if self.size_base <= 0:
            raise ValueError(f"size_base must be > 0, got {self.size_base}")

    @property
    def is_long(self) -> bool:
        return self.side == "buy"

    @property
    def exit_side(self) -> str:
        """The side that closes this position."""
        return "sell" if self.is_long else "buy"

    def pnl_quote(self, price: float) -> float:
        """Unrealized PnL in quote currency at `price`.

        Long gains when price rises, short gains when it falls. This sign is the
        whole reason a position has a side: getting it wrong turns a stop loss
        into a second entry.
        """
        move = price - self.entry_price
        return self.size_base * (move if self.is_long else -move)

    def pnl_pct(self, price: float) -> float:
        """PnL as a percentage of entry price, signed the same way."""
        if self.entry_price == 0:
            return 0.0
        move = (price - self.entry_price) / self.entry_price * 100.0
        return move if self.is_long else -move

    def held_seconds(self, now: float) -> float:
        return max(0.0, now - self.entry_ts)


# ------------------------------------------------------------------- decision


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """The answer to "may I do this?", with a reason either way.

    A refusal always carries a human-readable reason, because a risk rule that
    refuses silently is indistinguishable from a bug.
    """

    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed

    @classmethod
    def allow(cls, reason: str = "ok") -> "RiskDecision":
        return cls(True, reason)

    @classmethod
    def refuse(cls, reason: str) -> "RiskDecision":
        return cls(False, reason)


@dataclass(frozen=True, slots=True)
class ExitSignal:
    """The answer to "should I close now?", with the reason if so."""

    should_exit: bool
    reason: str
    price: float
    pnl_quote: float

    def __bool__(self) -> bool:
        return self.should_exit


class ExitReason:
    """Why an exit fired. `NONE` means the position should stay open."""

    NONE = "none"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    MAX_HOLD = "max_hold"
    KILLED = "daily_loss_halt"
    END_OF_RUN = "end_of_run"


# ---------------------------------------------------------------------- state


@dataclass(slots=True)
class RiskState:
    """The part of the risk system that must survive a restart.

    A daily loss limit that resets when the process restarts is not a daily
    limit: the losing loop is exactly the thing that restarts the process. So
    this is serialized to disk and reloaded by the runner.
    """

    day_key: str
    realized_pnl_quote: float = 0.0
    trades: int = 0
    halted: bool = False

    def to_dict(self) -> dict:
        return {
            "day_key": self.day_key,
            "realized_pnl_quote": self.realized_pnl_quote,
            "trades": self.trades,
            "halted": self.halted,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RiskState":
        return cls(
            day_key=str(data.get("day_key", "")),
            realized_pnl_quote=float(data.get("realized_pnl_quote", 0.0)),
            trades=int(data.get("trades", 0)),
            halted=bool(data.get("halted", False)),
        )

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path, day_key: str) -> "RiskState":
        """Load the counter for `day_key`, or start a fresh one.

        A file for a different day is discarded rather than carried forward:
        that is what "daily" means, and a stale file must not silently extend
        yesterday's loss budget into today.
        """
        path = Path(path)
        if not path.exists():
            return cls(day_key=day_key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(day_key=day_key)
        if str(data.get("day_key", "")) != day_key:
            return cls(day_key=day_key)
        return cls.from_dict(data)


# -------------------------------------------------------------------- manager


@dataclass(slots=True)
class RiskManager:
    """Enforces the limits. Holds at most one position, like the runner does."""

    limits: RiskLimits
    state: RiskState
    position: Position | None = None
    #: Every refusal and exit, for the run log.
    log: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ entry
    def check_entry(self, size_base: float, price: float) -> RiskDecision:
        """May a position of `size_base` be opened at `price`?"""
        if self.state.halted:
            return RiskDecision.refuse(
                f"daily loss limit reached ({self.state.realized_pnl_quote:+.2f} quote); "
                f"trading is halted for {self.state.day_key}"
            )
        if self.position is not None:
            return RiskDecision.refuse(
                f"a {self.position.side} position of {self.position.size_base:g} "
                f"base is already open; one position at a time"
            )
        if size_base <= 0:
            return RiskDecision.refuse(f"size must be > 0, got {size_base}")
        if price <= 0:
            return RiskDecision.refuse(f"price must be > 0, got {price}")
        if size_base > self.limits.max_position_base:
            return RiskDecision.refuse(
                f"size {size_base:g} base exceeds the cap of "
                f"{self.limits.max_position_base:g} base"
            )
        return RiskDecision.allow()

    def open(self, position: Position) -> None:
        """Record a filled entry. Raises if one is already open."""
        if self.position is not None:
            raise RuntimeError("a position is already open")
        self.position = position
        self.state.trades += 1
        self._note(
            f"OPEN {position.side} {position.size_base:g} base @ "
            f"{position.entry_price:g}"
        )

    # ------------------------------------------------------------------- exit
    def check_exit(self, price: float, now: float) -> ExitSignal:
        """Should the open position be closed at `price`?

        Order of checks matters. The daily-loss halt is tested first because a
        halt must close the book, and the hold limit is tested last because it is
        the backstop for everything the other rules missed.
        """
        if self.position is None:
            return ExitSignal(False, ExitReason.NONE, price, 0.0)

        p = self.position
        pnl = p.pnl_quote(price)

        if self.state.halted:
            return ExitSignal(True, ExitReason.KILLED, price, pnl)

        if self.limits.stop_loss_enabled and p.pnl_pct(price) <= -self.limits.stop_loss_pct:
            return ExitSignal(True, ExitReason.STOP_LOSS, price, pnl)

        if self.limits.take_profit_enabled and p.pnl_pct(price) >= self.limits.take_profit_pct:
            return ExitSignal(True, ExitReason.TAKE_PROFIT, price, pnl)

        if self.limits.hold_limit_enabled and p.held_seconds(now) >= self.limits.max_hold_seconds:
            return ExitSignal(True, ExitReason.MAX_HOLD, price, pnl)

        return ExitSignal(False, ExitReason.NONE, price, pnl)

    def close(self, price: float, reason: str) -> float:
        """Record a closed position and return the realized PnL in quote.

        The daily counter is updated here and nowhere else, so there is one
        place where a loss can be forgotten by mistake.
        """
        if self.position is None:
            raise RuntimeError("no position to close")
        p = self.position
        pnl = p.pnl_quote(price)
        self.state.realized_pnl_quote += pnl
        self.position = None
        self._note(f"CLOSE ({reason}) {p.side} @ {price:g} pnl {pnl:+.4f} quote")

        if self.limits.daily_loss_enabled:
            if self.state.realized_pnl_quote <= -self.limits.max_daily_loss_quote:
                self.state.halted = True
                self._note(
                    f"HALT daily loss {self.state.realized_pnl_quote:+.2f} quote "
                    f"reached the limit of -{self.limits.max_daily_loss_quote:g}"
                )
        return pnl

    # ------------------------------------------------------------------- util
    def _note(self, message: str) -> None:
        self.log.append(message)

    @property
    def halted(self) -> bool:
        return self.state.halted

    def unrealized_quote(self, price: float) -> float:
        return 0.0 if self.position is None else self.position.pnl_quote(price)
