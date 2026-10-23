"""Tests for the risk layer: caps, stop loss, take profit, daily loss, exits.

These are the tests that matter most in the repository. A risk rule that is only
exercised against a live venue is a rule nobody has checked, so every rule here
is driven with plain numbers and no network.

The supervisor tests use a `RecordingClient` that builds real `OrderRequest`
objects, so the payload path is exercised rather than stubbed: the thing being
tested is which orders the loop decides to send, not whether HTTP works.
"""

from __future__ import annotations

import pytest

from orderflow import Side, Trade
from orderflow.execution import (
    Environment,
    ExitReason,
    OrderRequest,
    OrderResult,
    Position,
    RiskLimits,
    RiskManager,
    RiskState,
)
from live_feed import Supervisor


# --------------------------------------------------------------------- limits
class TestRiskLimits:
    def test_negative_limit_is_refused(self):
        with pytest.raises(ValueError):
            RiskLimits(stop_loss_pct=-1.0)

    def test_zero_disables_a_rule(self):
        limits = RiskLimits(stop_loss_pct=0.0, take_profit_pct=0.0)
        assert not limits.stop_loss_enabled
        assert not limits.take_profit_enabled


# ------------------------------------------------------------------- position
class TestPositionSigns:
    def test_long_gains_when_price_rises(self):
        p = Position(side="buy", entry_price=100.0, size_base=2.0, contracts=1,
                     product_id=27, entry_ts=0.0)
        assert p.pnl_quote(110.0) == pytest.approx(20.0)
        assert p.pnl_quote(90.0) == pytest.approx(-20.0)

    def test_short_gains_when_price_falls(self):
        p = Position(side="sell", entry_price=100.0, size_base=2.0, contracts=1,
                     product_id=27, entry_ts=0.0)
        assert p.pnl_quote(90.0) == pytest.approx(20.0)
        assert p.pnl_quote(110.0) == pytest.approx(-20.0)

    def test_exit_side_is_the_opposite(self):
        long = Position(side="buy", entry_price=100.0, size_base=1.0, contracts=1,
                        product_id=27, entry_ts=0.0)
        short = Position(side="sell", entry_price=100.0, size_base=1.0, contracts=1,
                         product_id=27, entry_ts=0.0)
        assert long.exit_side == "sell"
        assert short.exit_side == "buy"

    def test_pnl_pct_matches_the_side(self):
        long = Position(side="buy", entry_price=100.0, size_base=1.0, contracts=1,
                        product_id=27, entry_ts=0.0)
        short = Position(side="sell", entry_price=100.0, size_base=1.0, contracts=1,
                         product_id=27, entry_ts=0.0)
        assert long.pnl_pct(98.0) == pytest.approx(-2.0)
        assert short.pnl_pct(98.0) == pytest.approx(2.0)

    def test_bad_side_and_bad_price_are_refused(self):
        with pytest.raises(ValueError):
            Position(side="long", entry_price=100.0, size_base=1.0, contracts=1,
                     product_id=27, entry_ts=0.0)
        with pytest.raises(ValueError):
            Position(side="buy", entry_price=0.0, size_base=1.0, contracts=1,
                     product_id=27, entry_ts=0.0)


# -------------------------------------------------------------------- manager
def manager(**kw) -> RiskManager:
    limits = RiskLimits(**kw) if kw else RiskLimits(
        stop_loss_pct=1.0, take_profit_pct=2.0, max_position_base=0.01,
        max_daily_loss_quote=50.0, max_hold_seconds=3600.0,
    )
    return RiskManager(limits=limits, state=RiskState(day_key="2026-10-23"))


def long_at(price=80000.0, size=0.001) -> Position:
    return Position(side="buy", entry_price=price, size_base=size, contracts=1,
                    product_id=27, entry_ts=0.0)


class TestEntryChecks:
    def test_a_reasonable_entry_is_allowed(self):
        assert manager().check_entry(0.001, 80000.0).allowed

    def test_size_above_the_cap_is_refused(self):
        d = manager().check_entry(0.02, 80000.0)
        assert not d.allowed
        assert "cap" in d.reason

    def test_a_second_position_is_refused(self):
        m = manager()
        m.open(long_at())
        d = m.check_entry(0.001, 80000.0)
        assert not d.allowed
        assert "already open" in d.reason

    def test_non_positive_size_and_price_are_refused(self):
        assert not manager().check_entry(0.0, 80000.0).allowed
        assert not manager().check_entry(0.001, 0.0).allowed

    def test_a_halted_manager_refuses_everything(self):
        m = manager()
        m.state.halted = True
        d = m.check_entry(0.001, 80000.0)
        assert not d.allowed
        assert "halted" in d.reason


class TestStopLossAndTakeProfit:
    def test_long_stop_loss_fires_at_the_limit(self):
        m = manager()
        m.open(long_at(80000.0))
        assert not m.check_exit(79600.0, now=1)          # -0.5%, inside
        s = m.check_exit(79200.0, now=1)                 # -1.0%, the stop
        assert s.should_exit and s.reason == ExitReason.STOP_LOSS
        assert s.pnl_quote == pytest.approx(-0.8)

    def test_long_take_profit_fires_at_the_limit(self):
        m = manager()
        m.open(long_at(80000.0))
        assert not m.check_exit(81200.0, now=1)          # +1.5%
        s = m.check_exit(81600.0, now=1)                 # +2.0%
        assert s.should_exit and s.reason == ExitReason.TAKE_PROFIT

    def test_short_stop_loss_fires_when_price_rises(self):
        m = manager()
        m.open(Position(side="sell", entry_price=80000.0, size_base=0.001,
                        contracts=1, product_id=27, entry_ts=0.0))
        assert not m.check_exit(79200.0, now=1)          # in profit, no exit
        s = m.check_exit(80800.0, now=1)                 # +1% against the short
        assert s.should_exit and s.reason == ExitReason.STOP_LOSS

    def test_disabling_the_stop_means_it_never_fires(self):
        m = manager(stop_loss_pct=0.0, take_profit_pct=2.0, max_position_base=0.01,
                    max_daily_loss_quote=50.0, max_hold_seconds=3600.0)
        m.open(long_at(80000.0))
        assert not m.check_exit(40000.0, now=1)

    def test_no_position_means_no_exit(self):
        assert not manager().check_exit(80000.0, now=1)


class TestHoldLimit:
    def test_position_is_closed_at_the_hold_limit(self):
        m = manager()
        m.open(long_at(80000.0))
        assert not m.check_exit(80000.0, now=3599)
        s = m.check_exit(80000.0, now=3600)
        assert s.should_exit and s.reason == ExitReason.MAX_HOLD


class TestDailyLossLimit:
    def test_realized_loss_accumulates_and_halts(self):
        m = manager(max_daily_loss_quote=1.0, stop_loss_pct=1.0, take_profit_pct=2.0,
                    max_position_base=0.01, max_hold_seconds=0)
        # Two 0.8-quote losses: the second crosses the 1.0 limit.
        for _ in range(2):
            m.open(long_at(80000.0))
            m.close(79200.0, ExitReason.STOP_LOSS)
        assert m.state.realized_pnl_quote == pytest.approx(-1.6)
        assert m.halted
        assert not m.check_entry(0.001, 80000.0).allowed

    def test_a_profit_does_not_halt(self):
        m = manager(max_daily_loss_quote=1.0)
        m.open(long_at(80000.0))
        m.close(81600.0, ExitReason.TAKE_PROFIT)
        assert m.state.realized_pnl_quote > 0
        assert not m.halted

    def test_halt_closes_an_open_position(self):
        m = manager(max_daily_loss_quote=1.0)
        m.open(long_at(80000.0))
        m.state.halted = True
        s = m.check_exit(80000.0, now=1)
        assert s.should_exit and s.reason == ExitReason.KILLED

    def test_trade_count_is_kept(self):
        m = manager()
        m.open(long_at())
        m.close(80000.0, ExitReason.END_OF_RUN)
        assert m.state.trades == 1


# ---------------------------------------------------------------------- state
class TestRiskStatePersistence:
    def test_round_trips_through_disk(self, tmp_path):
        p = tmp_path / "state" / "daily.json"
        s = RiskState(day_key="2026-10-23", realized_pnl_quote=-3.5, trades=4, halted=True)
        s.save(p)
        loaded = RiskState.load(p, "2026-10-23")
        assert loaded.realized_pnl_quote == pytest.approx(-3.5)
        assert loaded.trades == 4
        assert loaded.halted

    def test_a_stale_day_is_discarded(self, tmp_path):
        """Yesterday's loss budget must not extend into today."""
        p = tmp_path / "daily.json"
        RiskState(day_key="2026-10-22", realized_pnl_quote=-99.0, halted=True).save(p)
        loaded = RiskState.load(p, "2026-10-23")
        assert loaded.realized_pnl_quote == 0.0
        assert not loaded.halted
        assert loaded.day_key == "2026-10-23"

    def test_missing_and_corrupt_files_start_fresh(self, tmp_path):
        assert RiskState.load(tmp_path / "nope.json", "d").realized_pnl_quote == 0.0
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert RiskState.load(bad, "d").realized_pnl_quote == 0.0


# ----------------------------------------------------------------- supervisor
class RecordingClient:
    """Builds real OrderRequests so the payload path is exercised, not stubbed."""

    def __init__(self, ok: bool = True) -> None:
        self.sent: list[OrderRequest] = []
        self.ok = ok

    def _send(self, side, product_id, size, **kw) -> OrderResult:
        req = OrderRequest(product_id=product_id, size=size, side=side, **kw)
        self.sent.append(req)
        return OrderResult(
            sent=True, dry_run=False, environment=Environment.TESTNET, request=req,
            status_code=200 if self.ok else 400,
            response={"success": self.ok},
            error=None if self.ok else "HTTP 400",
        )

    def buy(self, product_id, size, **kw):
        return self._send("buy", product_id, size, **kw)

    def sell(self, product_id, size, **kw):
        return self._send("sell", product_id, size, **kw)


def ticks(prices, size=0.01, side=Side.BUY, start_ts=1792574700.0):
    return [Trade(ts=start_ts + i, price=p, size=size, side=side)
            for i, p in enumerate(prices)]


def supervised(client, **kw):
    limits = kw.pop("limits", RiskLimits(
        stop_loss_pct=1.0, take_profit_pct=2.0, max_position_base=0.01,
        max_daily_loss_quote=50.0, max_hold_seconds=3600.0))
    return Supervisor(client=client, limits=limits, product_id=84,
                      order_size_base=kw.pop("order_size_base", 0.001),
                      execute=kw.pop("execute", True), seed=7, **kw)


class TestSupervisorLoop:
    def test_rising_tape_takes_profit(self):
        """A long entered on positive delta exits at +2%.

        The signal stays positive after an exit, so the loop re-enters. That is
        the intended behaviour: this is a loop, not a one-shot order. What is
        asserted is the shape of each round trip, not the count.
        """
        client = RecordingClient()
        s = supervised(client)
        cum = 0.0
        for t in ticks([80000.0] + [80000.0 + 300 * i for i in range(1, 30)]):
            cum += t.size
            s.on_trade(t, cumulative_delta=cum)
        s.close_out()

        assert s.entries >= 1
        assert s.exits >= 1
        assert client.sent[0].side == "buy" and client.sent[0].reduce_only is False
        assert client.sent[1].side == "sell" and client.sent[1].reduce_only is True
        assert "take_profit" in " ".join(s.manager.log)

    def test_falling_tape_stops_out(self):
        client = RecordingClient()
        s = supervised(client)
        cum = 0.0
        for t in ticks([80000.0] + [80000.0 - 300 * i for i in range(1, 30)]):
            cum += t.size
            s.on_trade(t, cumulative_delta=cum)
        s.close_out()

        assert s.entries >= 1
        assert s.exits >= 1
        assert "stop_loss" in " ".join(s.manager.log)
        assert s.realized < 0

    def test_exit_order_is_reduce_only_and_matches_entry_size(self):
        client = RecordingClient()
        s = supervised(client)
        s.on_trade(ticks([80000.0])[0], cumulative_delta=1.0)
        s.on_trade(ticks([82000.0], start_ts=1792574800.0)[0], cumulative_delta=1.0)
        s.close_out()
        entry, exit_ = client.sent[0], client.sent[1]
        assert exit_.reduce_only
        assert exit_.size == entry.size

    def test_nothing_is_left_open_at_the_end(self):
        client = RecordingClient()
        s = supervised(client)
        s.on_trade(ticks([80000.0])[0], cumulative_delta=1.0)
        assert s.manager.position is not None
        s.close_out()
        assert s.manager.position is None
        assert client.sent[-1].reduce_only

    def test_zero_delta_never_enters(self):
        client = RecordingClient()
        s = supervised(client)
        for t in ticks([80000.0, 80100.0, 80200.0]):
            s.on_trade(t, cumulative_delta=0.0)
        assert s.entries == 0
        assert client.sent == []

    def test_a_second_entry_is_refused_while_one_is_open(self):
        client = RecordingClient()
        s = supervised(client)
        s.on_trade(ticks([80000.0])[0], cumulative_delta=1.0)
        s.on_trade(ticks([80000.0], start_ts=1792574800.0)[0], cumulative_delta=5.0)
        assert s.entries == 1
        assert len(client.sent) == 1

    def test_size_above_the_cap_is_refused(self):
        client = RecordingClient()
        s = supervised(client, order_size_base=1.0)   # 1 BTC, cap is 0.01
        s.on_trade(ticks([80000.0])[0], cumulative_delta=1.0)
        assert s.entries == 0
        assert client.sent == []
        assert "REFUSED" in " ".join(s.manager.log)

    def test_a_failed_entry_does_not_open_a_position(self):
        """The venue rejected it, so there is nothing to manage."""
        client = RecordingClient(ok=False)
        s = supervised(client)
        s.on_trade(ticks([80000.0])[0], cumulative_delta=1.0)
        assert s.manager.position is None
        assert s.entries == 0
        assert "ENTRY FAILED" in " ".join(s.manager.log)

    def test_a_failed_exit_keeps_the_position_open(self):
        """Forgetting a position that is still live is how a stop becomes a loss."""
        client = RecordingClient()
        s = supervised(client)
        s.on_trade(ticks([80000.0])[0], cumulative_delta=1.0)
        client.ok = False
        s.on_trade(ticks([82000.0], start_ts=1792574800.0)[0], cumulative_delta=1.0)
        assert s.manager.position is not None
        assert "EXIT FAILED" in " ".join(s.manager.log)

    def test_dry_run_sends_nothing_but_still_tracks(self):
        client = RecordingClient()
        s = supervised(client, execute=False)
        cum = 0.0
        for t in ticks([80000.0] + [80000.0 + 300 * i for i in range(1, 30)]):
            cum += t.size
            s.on_trade(t, cumulative_delta=cum)
        s.close_out()
        assert client.sent == []
        assert s.entries >= 1 and s.exits >= 1

    def test_daily_halt_stops_further_entries(self, tmp_path):
        p = tmp_path / "daily.json"
        client = RecordingClient()
        s = supervised(client, state_path=p, limits=RiskLimits(
            stop_loss_pct=0.01, take_profit_pct=2.0, max_position_base=0.01,
            max_daily_loss_quote=0.1, max_hold_seconds=0.0))
        cum = 0.0
        for t in ticks([80000.0] + [80000.0 - 300 * i for i in range(1, 30)]):
            cum += t.size
            s.on_trade(t, cumulative_delta=cum)
        s.close_out()
        s.save_state()
        assert s.manager.halted
        assert RiskState.load(p, s.manager.state.day_key).halted

    def test_entry_gate_vetoes_the_entry(self):
        client = RecordingClient()
        calls = []
        def gate(price):
            calls.append(price)
            return False, "basis too wide"
        s = supervised(client, entry_gate=gate)
        s.on_trade(ticks([80000.0])[0], cumulative_delta=1.0)
        assert calls == [80000.0]
        assert s.entries == 0
        assert "BLOCKED" in " ".join(s.manager.log)

    def test_entry_gate_allows_when_it_says_so(self):
        client = RecordingClient()
        s = supervised(client, entry_gate=lambda price: (True, "ok"))
        s.on_trade(ticks([80000.0])[0], cumulative_delta=1.0)
        assert s.entries == 1

    def test_hold_limit_closes_a_flat_position(self):
        client = RecordingClient()
        s = supervised(client, limits=RiskLimits(
            stop_loss_pct=0.0, take_profit_pct=0.0, max_position_base=0.01,
            max_daily_loss_quote=0.0, max_hold_seconds=5.0))
        s.on_trade(ticks([80000.0])[0], cumulative_delta=1.0)
        s.on_trade(ticks([80000.0], start_ts=1792574800.0)[0], cumulative_delta=1.0)
        assert s.exits == 1
        assert "max_hold" in " ".join(s.manager.log)


class TestCircuitBreaker:
    """A bad key or a blocked IP fails every order the same way.

    Without a breaker the loop retries on every trade, which turns one config
    mistake into a request storm against the venue.
    """

    def test_repeated_failures_stop_the_loop(self):
        client = RecordingClient(ok=False)
        s = supervised(client)
        s.max_consecutive_failures = 3
        for i in range(50):
            s.on_trade(ticks([80000.0], start_ts=1792574700.0 + i)[0],
                       cumulative_delta=1.0)
        assert s.stopped
        assert client.sent and len(client.sent) == 3
        assert "STOPPED after 3 consecutive venue failures" in " ".join(s.manager.log)

    def test_a_success_resets_the_counter(self):
        """One transient blip must not end the run."""
        client = RecordingClient(ok=False)
        s = supervised(client)
        s.max_consecutive_failures = 2
        s.on_trade(ticks([80000.0])[0], cumulative_delta=1.0)      # 1 failure
        client.ok = True
        s.on_trade(ticks([80000.0], start_ts=1792574800.0)[0], cumulative_delta=1.0)
        assert s.consecutive_failures == 0
        assert not s.stopped
        assert s.entries == 1

    def test_the_breaker_does_not_block_exits(self):
        """An open position must keep getting exit attempts.

        Abandoning a live position is worse than a repeated failed request, so
        the breaker stops new entries only.
        """
        client = RecordingClient()
        s = supervised(client)
        s.on_trade(ticks([80000.0])[0], cumulative_delta=1.0)
        assert s.manager.position is not None
        client.ok = False
        s.stopped = True
        s.on_trade(ticks([82000.0], start_ts=1792574800.0)[0], cumulative_delta=1.0)
        assert len(client.sent) == 2
        assert "EXIT FAILED" in " ".join(s.manager.log)
