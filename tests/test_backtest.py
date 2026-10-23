"""The backtest harness must measure the shipped rules, not a copy of them.

A backtest is only worth reading if it exercises the code that runs in
production. These tests pin the three ways that can quietly stop being true:
the loop is re-implemented instead of imported, the tape is decoded by a second
parser, or the simulated broker drifts away from the interface the supervisor
calls.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import backtest  # noqa: E402
import live_feed  # noqa: E402
from orderflow.execution.risk import RiskLimits  # noqa: E402


def frame(rows, inst="BTC-USDT-SWAP"):
    return "txt:" + json.dumps({"arg": {"channel": "trades", "instId": inst}, "data": rows})


def row(trade_id, px, sz, side, ts):
    return {"instId": "BTC-USDT-SWAP", "tradeId": str(trade_id), "px": str(px),
            "sz": str(sz), "side": side, "ts": str(ts)}


def write_tape(path: Path, rows) -> Path:
    path.write_text(frame(rows) + "\n", encoding="utf-8")
    return path


def tape_with(tmp_path: Path, rows) -> Path:
    """Write a one-frame tape and return its path."""
    return write_tape(tmp_path / "t.txt", rows)


class TestTapeDecoding:
    def test_a_tape_decodes_through_the_live_parser(self, tmp_path):
        """Not a second parser: `parse_message`, the one a socket uses."""
        p = write_tape(tmp_path / "t.txt", [row(1, 100.0, 1.0, "buy", 1000)])
        events = backtest.load_tape(p)
        assert len(events) == 1
        assert events[0].trade_id == "1"
        assert events[0].price == 100.0

    def test_duplicate_trade_ids_are_dropped(self, tmp_path):
        """A reconnect replays prints; a backtest must not double-count them."""
        rows = [row(1, 100.0, 1.0, "buy", 1000), row(1, 100.0, 1.0, "buy", 1000)]
        events = backtest.load_tape(write_tape(tmp_path / "t.txt", rows))
        assert len(events) == 1

    def test_prints_come_back_in_time_order(self, tmp_path):
        rows = [row(2, 101.0, 1.0, "buy", 2000), row(1, 100.0, 1.0, "buy", 1000)]
        events = backtest.load_tape(write_tape(tmp_path / "t.txt", rows))
        assert [e.ts_ms for e in events] == [1000, 2000]

    def test_an_unknown_side_is_rejected_not_defaulted(self, tmp_path):
        """The feed raises on a new vocabulary; the tape must not soften that."""
        from orderflow.feed import ProtocolError

        p = write_tape(tmp_path / "t.txt", [row(1, 100.0, 1.0, "sideways", 1000)])
        with pytest.raises(ProtocolError):
            backtest.load_tape(p)

    def test_a_binary_frame_is_skipped(self, tmp_path):
        p = tmp_path / "t.txt"
        p.write_text("bin:deadbeef\n" + frame([row(1, 100.0, 1.0, "buy", 1000)]) + "\n")
        assert len(backtest.load_tape(p)) == 1


class TestTheLoopIsTheShippedLoop:
    def test_the_supervisor_under_test_is_live_feeds(self):
        """Import identity: a refactor of live_feed must move the backtest too."""
        assert backtest.live_feed.Supervisor is live_feed.Supervisor
        assert backtest.live_feed.basis_pct is live_feed.basis_pct

    def test_the_broker_answers_the_calls_the_supervisor_makes(self):
        broker = backtest.SimBroker(backtest.Costs(), 27)
        broker.price = 50_000.0
        buy = broker.buy(product_id=27, size=1)
        sell = broker.sell(product_id=27, size=1, reduce_only=True, client_order_id="x")
        assert buy.ok and sell.ok
        assert buy.status_code == 200


class TestFillModel:
    def test_a_buy_fills_above_and_a_sell_below_the_print(self):
        """Slippage must cost, never pay. A sign error here flatters every run."""
        broker = backtest.SimBroker(backtest.Costs(slippage_bps=10.0), 27)
        broker.price = 100.0
        assert broker._fill_price("buy") > 100.0
        assert broker._fill_price("sell") < 100.0

    def test_pnl_sign_follows_the_position_side(self):
        """A long and a short over the same move must differ in sign."""
        assert backtest._pnl("buy", 1.0, 100.0, 110.0) == pytest.approx(10.0)
        assert backtest._pnl("sell", 1.0, 100.0, 110.0) == pytest.approx(-10.0)

    def test_costs_are_charged_on_both_legs(self):
        costs = backtest.Costs(fee_bps=10.0, slippage_bps=0.0)
        entry = backtest._notional_cost(100_000.0, 0.001, costs, entry=True, basis_mode=False)
        exit_ = backtest._notional_cost(100_000.0, 0.001, costs, entry=False, basis_mode=False)
        # notional = 100_000 * 0.001 = 100 quote; 10 bps of it is 0.1.
        assert entry == pytest.approx(0.1)
        assert exit_ == pytest.approx(0.1)

    def test_basis_is_charged_on_entry_only(self):
        costs = backtest.Costs(fee_bps=0.0, slippage_bps=0.0, basis_bps=50.0)
        entry = backtest._notional_cost(100_000.0, 0.001, costs, entry=True, basis_mode=True)
        exit_ = backtest._notional_cost(100_000.0, 0.001, costs, entry=False, basis_mode=True)
        assert entry == pytest.approx(0.5)
        assert exit_ == pytest.approx(0.0)

    def test_basis_is_off_when_basis_mode_is_off(self):
        costs = backtest.Costs(fee_bps=0.0, slippage_bps=0.0, basis_bps=50.0)
        assert backtest._notional_cost(100_000.0, 0.001, costs, entry=True, basis_mode=False) == 0.0


class TestTheRunUsesTheRiskLadder:
    def test_a_zero_delta_is_not_a_signal(self):
        """Delta of exactly zero is the absence of a signal, not a weak one.

        Tested on the shipped function rather than through a tape: no tape can
        reach the branch, because the first print always moves delta off zero.
        """
        assert live_feed.order_side(0.0) is None
        assert live_feed.order_side(0.5) == "buy"
        assert live_feed.order_side(-0.5) == "sell"

    def test_a_stop_loss_exits_a_losing_long(self, tmp_path):
        """Rising delta opens a long; a fall past the stop must close it."""
        rows = [row(1, 100.0, 5.0, "buy", 1000), row(2, 100.0, 5.0, "buy", 2000)]
        # A hard drop: the long is well past a 1% stop.
        rows += [row(3, 90.0, 1.0, "sell", 3000)]
        events = backtest.load_tape(tape_with(tmp_path, rows))
        limits = RiskLimits(stop_loss_pct=1.0, take_profit_pct=0.0, max_hold_seconds=0.0)
        r = backtest.run_backtest(events, limits, 0.001, backtest.Costs(), basis_mode=False, use_gate=False)
        assert len(r.trades) == 1
        assert r.trades[0].side == "buy"
        assert r.trades[0].reason == "stop_loss"
        assert r.trades[0].pnl_quote < 0

    def test_a_take_profit_exits_a_winning_long(self, tmp_path):
        rows = [row(1, 100.0, 5.0, "buy", 1000), row(2, 100.0, 5.0, "buy", 2000)]
        rows += [row(3, 110.0, 1.0, "sell", 3000)]
        events = backtest.load_tape(tape_with(tmp_path, rows))
        limits = RiskLimits(stop_loss_pct=0.0, take_profit_pct=2.0, max_hold_seconds=0.0)
        r = backtest.run_backtest(events, limits, 0.001, backtest.Costs(), basis_mode=False, use_gate=False)
        assert len(r.trades) == 1
        assert r.trades[0].reason == "take_profit"
        assert r.trades[0].pnl_quote > 0

    def test_an_open_position_is_closed_at_the_end_of_the_tape(self, tmp_path):
        rows = [row(1, 100.0, 5.0, "buy", 1000), row(2, 100.5, 1.0, "buy", 2000)]
        events = backtest.load_tape(tape_with(tmp_path, rows))
        limits = RiskLimits(stop_loss_pct=0.0, take_profit_pct=0.0, max_hold_seconds=0.0)
        r = backtest.run_backtest(events, limits, 0.001, backtest.Costs(), basis_mode=False, use_gate=False)
        assert len(r.trades) == 1
        assert r.trades[0].reason == "end_of_run"

    def test_the_position_cap_refuses_an_oversized_order(self, tmp_path):
        """The cap is a refusal, and a refusal must mean no trade."""
        rows = [row(1, 100.0, 5.0, "buy", 1000), row(2, 100.5, 1.0, "buy", 2000)]
        events = backtest.load_tape(tape_with(tmp_path, rows))
        limits = RiskLimits(max_position_base=0.0001, stop_loss_pct=0.0,
                            take_profit_pct=0.0, max_hold_seconds=0.0)
        r = backtest.run_backtest(events, limits, 0.001, backtest.Costs(), basis_mode=False, use_gate=False)
        assert len(r.trades) == 0


class TestRunningDeltaMatchesTheChart:
    """The carried delta must equal the chart's, print for print.

    `run_backtest` accumulates delta instead of calling `chart.cumulative_delta`,
    because that call re-sums every bar and makes a long backtest quadratic. The
    substitution is only sound if the two agree, so this pins it on a tape that
    crosses a session boundary.
    """

    def test_the_carried_delta_equals_the_chart_at_every_print(self, tmp_path):
        from orderflow.core import Trade
        from orderflow.instruments import OKX_BTC_USDT_SWAP
        from orderflow.session import MarketCalendar
        from orderflow.session_chart import SessionChart

        rows = []
        # Two UTC days, so the roll path is exercised, with mixed sides.
        for i in range(200):
            day = 1_700_000_000 + i * 900  # steps of 15 min
            side = "buy" if i % 3 else "sell"
            rows.append(row(i + 1, 100.0 + (i % 7), 1.0 + (i % 5), side, day * 1000))

        events = backtest.load_tape(tape_with(tmp_path, rows))
        chart = SessionChart(OKX_BTC_USDT_SWAP.grid(), MarketCalendar(), bar_seconds=30.0)
        running = backtest.RunningDelta(chart)

        for ev in events:
            trade = Trade(ts=ev.ts_ms / 1000.0, price=ev.price, size=ev.size, side=ev.side)
            mine = running.add(trade)
            assert backtest._same_delta(mine, chart.cumulative_delta), (
                f"carried {mine} vs chart {chart.cumulative_delta} at {ev.ts_ms}"
            )

    def test_the_delta_resets_when_the_day_rolls(self, tmp_path):
        from orderflow.core import Trade
        from orderflow.instruments import OKX_BTC_USDT_SWAP
        from orderflow.session import MarketCalendar
        from orderflow.session_chart import SessionChart

        # A heavy buy day, then a light buy day. A reset shows as a smaller
        # running value after the boundary.
        rows = [row(i + 1, 100.0, 10.0, "buy", (1_700_000_000 + i * 60) * 1000) for i in range(30)]
        rows += [row(100 + i, 100.0, 1.0, "buy", (1_700_100_000 + i * 60) * 1000) for i in range(5)]

        events = backtest.load_tape(tape_with(tmp_path, rows))
        chart = SessionChart(OKX_BTC_USDT_SWAP.grid(), MarketCalendar(), bar_seconds=30.0)
        running = backtest.RunningDelta(chart)
        values = []
        for ev in events:
            trade = Trade(ts=ev.ts_ms / 1000.0, price=ev.price, size=ev.size, side=ev.side)
            values.append(running.add(trade))
        assert max(values[:30]) == pytest.approx(300.0)
        # After the roll the value is only the new day's volume, not 300 + 5.
        assert values[-1] == pytest.approx(5.0)

    def test_the_run_reports_no_mismatch_when_checked(self, tmp_path):
        rows = [row(i + 1, 100.0 + i, 1.0, "buy" if i % 2 else "sell", (1000 + i * 60) * 1000)
                for i in range(40)]
        events = backtest.load_tape(tape_with(tmp_path, rows))
        r = backtest.run_backtest(
            events, RiskLimits(), 0.001, backtest.Costs(),
            basis_mode=False, use_gate=False, verbose=True,
        )
        assert r.delta_mismatches == 0


class TestReportedNumbers:
    def test_net_is_gross_minus_costs(self, tmp_path):
        rows = [row(1, 100.0, 5.0, "buy", 1000), row(2, 100.0, 5.0, "buy", 2000),
                row(3, 110.0, 1.0, "sell", 3000)]
        events = backtest.load_tape(tape_with(tmp_path, rows))
        limits = RiskLimits(stop_loss_pct=0.0, take_profit_pct=2.0, max_hold_seconds=0.0)
        r = backtest.run_backtest(events, limits, 0.001, backtest.Costs(), basis_mode=False, use_gate=False)
        assert r.net_pnl == pytest.approx(r.gross_pnl - r.costs)
        assert r.costs > 0

    def test_the_equity_curve_ends_at_net_pnl(self, tmp_path):
        rows = [row(1, 100.0, 5.0, "buy", 1000), row(2, 100.0, 5.0, "buy", 2000),
                row(3, 110.0, 1.0, "sell", 3000)]
        events = backtest.load_tape(tape_with(tmp_path, rows))
        limits = RiskLimits(stop_loss_pct=0.0, take_profit_pct=2.0, max_hold_seconds=0.0)
        r = backtest.run_backtest(events, limits, 0.001, backtest.Costs(), basis_mode=False, use_gate=False)
        curve = r.equity_curve()
        assert curve and curve[-1] == pytest.approx(r.net_pnl)

    def test_the_drawdown_is_never_positive(self, tmp_path):
        rows = [row(1, 100.0, 5.0, "buy", 1000), row(2, 100.0, 5.0, "buy", 2000),
                row(3, 90.0, 1.0, "sell", 3000)]
        events = backtest.load_tape(tape_with(tmp_path, rows))
        limits = RiskLimits(stop_loss_pct=1.0, take_profit_pct=0.0, max_hold_seconds=0.0)
        r = backtest.run_backtest(events, limits, 0.001, backtest.Costs(), basis_mode=False, use_gate=False)
        assert r.max_drawdown() <= 0.0

    def test_a_backtest_writes_no_risk_state(self, tmp_path, monkeypatch):
        """The live daily counter must not be touched by a simulation."""
        monkeypatch.chdir(tmp_path)
        rows = [row(1, 100.0, 5.0, "buy", 1000), row(2, 100.5, 1.0, "buy", 2000)]
        events = backtest.load_tape(tape_with(tmp_path, rows))
        backtest.run_backtest(events, RiskLimits(), 0.001, backtest.Costs(),
                              basis_mode=False, use_gate=False)
        assert not (tmp_path / "state").exists()
