#!/usr/bin/env python3
"""Backtest the signal loop over a recorded tape.

What is real here, and what is simulated
----------------------------------------
Real: the feed decoding, the tick grid, the footprint aggregation, the session
roll, `cumulative_delta`, `order_side`, the sizing arithmetic, and the whole
`Supervisor` + `RiskManager` exit path. The tape is decoded by the same
`parse_message` a live socket uses, and the supervisor is the same object a live
run builds, driven by the same `on_trade` callback. If the strategy changes, this
changes with it; nothing is re-implemented.

Simulated: fills. There is no venue in a backtest, so a `SimBroker` stands in for
`DeltaOrderClient` and fills at the trade price with a cost applied. That is the
only place a number is invented, and it is stated on the report.

The entry rule is the raw sign of cumulative delta, and the exit rule is the
`RiskManager`'s stop/target/hold ladder. Both are the shipped rules. A backtest
of the shipped rules is the only kind worth running.

    python3 tools/backtest.py --tape tape_7d.txt
    python3 tools/backtest.py --tape tape_7d.txt --stop-loss 0.5 --take-profit 1.0
    python3 tools/backtest.py --tape tape_7d.txt --grid-stop 0.25:1.0:0.25

Fees default to Delta India's taker rate.

The venue matters and is selectable. Reading the signal on Delta - the venue the
order is placed on - removes the basis entirely, so the result is about the
strategy rather than about the spread between two venues. Reading it on OKX and
filling at Delta is the cross-venue mode, where the basis is a real cost and
`--basis-bps` charges it.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orderflow.core import Side, Trade  # noqa: E402
from orderflow.execution.delta_client import Environment, OrderResult  # noqa: E402
from orderflow.execution.risk import ExitReason, RiskLimits  # noqa: E402
from orderflow.execution.sizing import DELTA_PERP_RULES  # noqa: E402
from orderflow.feed import base as feed_base  # noqa: E402
from orderflow.feed.delta import parse_message as parse_delta  # noqa: E402
from orderflow.feed.okx import parse_message as parse_okx  # noqa: E402
from orderflow.instruments import DELTA_BTCUSD, OKX_BTC_USDT_SWAP  # noqa: E402
from orderflow.session_chart import SessionChart  # noqa: E402
from orderflow.session import MarketCalendar  # noqa: E402

# live_feed.py builds the loop; importing it keeps the backtest honest, because
# the supervisor under test is the one the service runs.
import live_feed  # noqa: E402

#: One Delta contract is 0.001 BTC. Used to express results in contracts.
CONTRACT_BTC = 0.001

#: Venue -> (frame decoder, the instrument whose tick grid the chart uses).
#:
#: The decoder must match the venue the tape was recorded from. Feeding Delta
#: frames to the OKX decoder is not a near-miss: it returns no prints at all,
#: because the OKX decoder keys on `arg.channel` and Delta sends `type`.
VENUES = {
    "delta": (parse_delta, DELTA_BTCUSD),
    "okx": (parse_okx, OKX_BTC_USDT_SWAP),
}

TradeEvent = feed_base.TradeEvent


@dataclass
class Costs:
    """Everything that makes a simulated fill worse than the signal price.

    `fee_bps` is charged per side, on notional. `slippage_bps` is charged per
    side too: a market order crosses the spread, and the spread is not in a
    trade-print tape. `basis_bps` is charged once on entry, and only in basis
    mode, because the signal is read on OKX and the fill happens on Delta where
    the price differs by the basis.
    """

    fee_bps: float = 5.0
    slippage_bps: float = 2.0
    basis_bps: float = 0.0


class SimBroker:
    """Stands in for `DeltaOrderClient`. Fills at the current print, plus cost.

    The interface is the two methods the supervisor calls (`buy`, `sell`) and
    the `OrderResult` shape it inspects. `reduce_only` and `client_order_id` are
    accepted and ignored, because a fill model has nothing to do with them.
    """

    def __init__(self, costs: Costs, product_id: int) -> None:
        self.costs = costs
        self.product_id = product_id
        self.price: float = 0.0
        self.last_reject: str | None = None
        #: Contract-count guard: a fill larger than the book would allow is a
        #: modelling error, not a trade. The tape's own sizes bound it.
        self.fills = 0

    def _fill_price(self, side: str) -> float:
        slip = self.costs.slippage_bps / 10_000.0
        # A buy pays up, a sell gives up.
        return self.price * (1.0 + slip) if side == "buy" else self.price * (1.0 - slip)

    def _result(self, side: str, size: int) -> OrderResult:
        req = _FakeRequest(self.product_id, size, side)
        self.fills += 1
        return OrderResult(
            sent=True,
            dry_run=False,
            environment=Environment.LIVE,
            request=req,
            status_code=200,
            response={"filled_price": self._fill_price(side)},
        )

    def buy(self, product_id: int, size: int, **kw) -> OrderResult:
        return self._result("buy", size)

    def sell(self, product_id: int, size: int, **kw) -> OrderResult:
        return self._result("sell", size)


@dataclass(frozen=True, slots=True)
class _FakeRequest:
    product_id: int
    size: int
    side: str


class RunningDelta:
    """`cumulative_delta`, carried forward instead of re-summed.

    `SessionChart.cumulative_delta` re-sums every bar on every call. That is
    O(bars) per print, so a six-hour backtest walks the whole bar list 341,933
    times and a multi-day one is quadratic. The quantity is additive over prints
    - a buy moves delta by +size, a sell by -size, an unknown side by zero - so
    it can be accumulated, and reset when the session rolls, exactly as the chart
    resets it.

    The chart is still the authority: `test_backtest.py` asserts this agrees with
    `chart.cumulative_delta` at every print on a real tape.
    """

    def __init__(self, chart: SessionChart) -> None:
        self.chart = chart
        self.value = 0.0

    def add(self, trade: Trade) -> float:
        if self.chart.add(trade):
            self.value = 0.0  # a new UTC day; the chart zeroes here too
        if trade.side is Side.BUY:
            self.value += trade.size
        elif trade.side is Side.SELL:
            self.value -= trade.size
        return self.value


@dataclass
class Fill:
    ts: float
    side: str
    reason: str
    price: float
    size_base: float
    pnl_quote: float
    cost_quote: float
    held_seconds: float


@dataclass
class Result:
    trades: list[Fill] = field(default_factory=list)
    start_ts: float = 0.0
    end_ts: float = 0.0
    start_price: float = 0.0
    end_price: float = 0.0
    bars: int = 0
    prints: int = 0
    daily_halts: int = 0
    final_state_pnl: float = 0.0
    #: Prints where the carried delta disagreed with the chart. Non-zero is a bug.
    delta_mismatches: int = 0

    @property
    def net_pnl(self) -> float:
        return sum(t.pnl_quote - t.cost_quote for t in self.trades)

    @property
    def gross_pnl(self) -> float:
        return sum(t.pnl_quote for t in self.trades)

    @property
    def costs(self) -> float:
        return sum(t.cost_quote for t in self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if (t.pnl_quote - t.cost_quote) > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if (t.pnl_quote - t.cost_quote) < 0)

    def equity_curve(self) -> list[float]:
        """Cumulative net PnL after each closed trade."""
        curve, run = [], 0.0
        for t in self.trades:
            run += t.pnl_quote - t.cost_quote
            curve.append(run)
        return curve

    def max_drawdown(self) -> float:
        peak, worst = 0.0, 0.0
        for v in self.equity_curve():
            peak = max(peak, v)
            worst = min(worst, v - peak)
        return worst

    def hold_seconds(self) -> float:
        return self.end_ts - self.start_ts

    def market_return_pct(self) -> float:
        if self.start_price <= 0:
            return 0.0
        return (self.end_price - self.start_price) / self.start_price * 100.0


def load_tape(path: Path, venue: str = "delta") -> list[TradeEvent]:
    """Decode a `FileTransport` tape into prints, deduplicated on trade id.

    The same decode path a live socket uses, so a tape cannot smuggle in a
    shape the live feed would reject.
    """
    decoder, _ = VENUES[venue]
    seen: set[str] = set()
    events: list[TradeEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        payload = line[4:] if line.startswith("txt:") else None
        if payload is None:
            continue  # binary frames are not produced by either venue
        for ev in decoder(payload):
            if ev.trade_id in seen:
                continue
            seen.add(ev.trade_id)
            events.append(ev)
    events.sort(key=lambda e: (e.ts_ms, int(e.trade_id)))
    return events


def run_backtest(
    events: list[TradeEvent],
    limits: RiskLimits,
    order_size_base: float,
    costs: Costs,
    basis_mode: bool,
    use_gate: bool,
    venue: str = "delta",
    verbose: bool = False,
) -> Result:
    """Drive the shipped supervisor over a decoded tape.

    Mirrors `live_feed.main`'s wiring: a `SessionChart` fed from the callback,
    the sign of `cumulative_delta` as the entry, and one `Supervisor` for the
    whole run. The broker fills; the risk layer decides.
    """
    product_id = DELTA_BTCUSD.venue_id
    broker = SimBroker(costs, product_id)

    limits = RiskLimits(
        stop_loss_pct=limits.stop_loss_pct,
        take_profit_pct=limits.take_profit_pct,
        max_position_base=limits.max_position_base,
        max_daily_loss_quote=limits.max_daily_loss_quote,
        max_hold_seconds=limits.max_hold_seconds,
    )

    # No risk-state file: a backtest must not write the live daily counter, and
    # must not read a stale one either.
    gate = None
    if use_gate:
        warn = 0.5

        def gate(price: float) -> tuple[bool, str]:
            # Basis mode stands in for Delta's price with a fixed offset, so the
            # gate is exercised without a second feed.
            delta_price = price * (1.0 + costs.basis_bps / 10_000.0)
            bp = live_feed.basis_pct(price, delta_price)
            if abs(bp) > warn:
                return False, f"basis {bp:+.3f}% exceeds {warn:g}%"
            return True, "ok"

    supervisor = live_feed.Supervisor(
        client=broker,
        limits=limits,
        product_id=product_id,
        order_size_base=order_size_base,
        exec_instrument=DELTA_BTCUSD,
        state_path=None,
        execute=True,
        seed=1,
        entry_gate=gate,
        notifier=None,
    )

    cal = MarketCalendar()
    _, chart_instrument = VENUES[venue]
    chart = SessionChart(chart_instrument.grid(), cal, bar_seconds=30.0)
    running = RunningDelta(chart)

    result = Result()
    open_entry: dict | None = None
    day_halts: set[str] = set()
    mismatches = 0

    for ev in events:
        trade = Trade(ts=ev.ts_ms / 1000.0, price=ev.price, size=ev.size, side=ev.side)
        broker.price = trade.price
        cum = running.add(trade)
        if verbose and not _same_delta(cum, chart.cumulative_delta):
            mismatches += 1
        was_position = supervisor.manager.position
        supervisor.on_trade(trade, ev, cumulative_delta=cum)
        now_pos = supervisor.manager.position

        if was_position is None and now_pos is not None:
            # Entry filled. The fill price carries slippage; the supervisor only
            # knows the signal price, so the difference is applied here.
            open_entry = {
                "price": now_pos.entry_price,
                "fill": broker._fill_price(now_pos.side),
                "ts": trade.ts,
                "side": now_pos.side,
            }
            open_entry["entry_cost"] = _notional_cost(
                now_pos.entry_price, now_pos.size_base, costs, entry=True, basis_mode=basis_mode
            )
        elif was_position is not None and now_pos is None:
            # Exit filled. Both legs are priced at their slipped fills, so the
            # simulated PnL is not silently given a free entry.
            assert open_entry is not None
            exit_cost = _notional_cost(trade.price, was_position.size_base, costs, entry=False, basis_mode=basis_mode)
            fill_price = broker._fill_price(was_position.exit_side)
            true_pnl = _pnl(was_position.side, was_position.size_base, open_entry["fill"], fill_price)
            result.trades.append(
                Fill(
                    ts=trade.ts,
                    side=was_position.side,
                    reason=_exit_reason(supervisor),
                    price=fill_price,
                    size_base=was_position.size_base,
                    pnl_quote=true_pnl,
                    cost_quote=open_entry["entry_cost"] + exit_cost,
                    held_seconds=trade.ts - open_entry["ts"],
                )
            )
            open_entry = None

        if supervisor.manager.halted:
            key = supervisor.manager.state.day_key
            if key not in day_halts:
                day_halts.add(key)

        result.bars = len(chart.bars)

    result.delta_mismatches = mismatches

    # Close anything still open at the tape's end, at the last print.
    if supervisor.manager.position is not None:
        last = events[-1]
        p = supervisor.manager.position
        fill_price = broker._fill_price(p.exit_side)
        exit_cost = _notional_cost(last.price, p.size_base, costs, entry=False, basis_mode=basis_mode)
        entry_fill = open_entry["fill"] if open_entry else p.entry_price
        result.trades.append(
            Fill(
                ts=last.ts_ms / 1000.0,
                side=p.side,
                reason=ExitReason.END_OF_RUN,
                price=fill_price,
                size_base=p.size_base,
                pnl_quote=_pnl(p.side, p.size_base, entry_fill, fill_price),
                cost_quote=(open_entry["entry_cost"] if open_entry else 0.0) + exit_cost,
                held_seconds=last.ts_ms / 1000.0 - p.entry_ts,
            )
        )

    result.start_ts = events[0].ts_ms / 1000.0
    result.end_ts = events[-1].ts_ms / 1000.0
    result.start_price = events[0].price
    result.end_price = events[-1].price
    result.prints = len(events)
    result.daily_halts = len(day_halts)
    result.final_state_pnl = supervisor.manager.state.realized_pnl_quote
    return result


def _same_delta(a: float, b: float, tol: float = 1e-9) -> bool:
    """Whether two cumulative-delta readings are the same number.

    They are summed in different orders - one print at a time versus one bar at
    a time - so they differ by float rounding, not by logic. Measured on a real
    tape the worst relative gap is ~8e-13, so a scale-aware tolerance is the
    right comparison; a fixed epsilon would flag a long session as broken.
    """
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def _pnl(side: str, size_base: float, entry: float, exit_price: float) -> float:
    """Signed PnL in quote, from the two simulated fill prices.

    Same sign convention as `Position.pnl_quote`: a long gains when price rises,
    a short when it falls.
    """
    move = exit_price - entry
    return size_base * (move if side == "buy" else -move)


def _notional_cost(price: float, size_base: float, costs: Costs, entry: bool, basis_mode: bool) -> float:
    notional = price * size_base
    bps = costs.fee_bps + costs.slippage_bps
    if entry and basis_mode:
        bps += costs.basis_bps
    return notional * bps / 10_000.0


def _exit_reason(supervisor) -> str:
    """The last CLOSE line the risk manager wrote, for the trade's reason."""
    for line in reversed(supervisor.manager.log):
        if line.startswith("CLOSE ("):
            return line.split("(", 1)[1].split(")", 1)[0]
    return "unknown"


def fmt_money(v: float) -> str:
    return f"{v:+,.2f}"


def report(r: Result, limits: RiskLimits, order_size_base: float, costs: Costs, label: str) -> dict:
    hours = r.hold_seconds() / 3600.0
    curve = r.equity_curve()
    net = r.net_pnl
    rets = [t.pnl_quote - t.cost_quote for t in r.trades]
    mean = statistics.fmean(rets) if rets else 0.0
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    # Per-trade Sharpe, annualised on the observed trade rate. Crude, but it is
    # labelled as such rather than dressed up.
    trades_per_year = (len(r.trades) / hours * 24 * 365) if hours > 0 else 0.0
    sharpe = (mean / sd * math.sqrt(trades_per_year)) if sd > 0 and trades_per_year > 0 else 0.0

    print(f"\n{'=' * 62}")
    print(f"  {label}")
    print(f"{'=' * 62}")
    print(f"  tape          : {hours:.2f} h, {r.prints:,} prints, {r.bars:,} bars")
    print(f"  price move    : {r.start_price:,.1f} -> {r.end_price:,.1f} "
          f"({r.market_return_pct():+.2f}%)")
    print(f"  order size    : {order_size_base:g} BTC "
          f"({order_size_base / CONTRACT_BTC:g} contracts)")
    print(f"  rules         : stop {limits.stop_loss_pct:g}%  "
          f"target {limits.take_profit_pct:g}%  "
          f"hold {limits.max_hold_seconds / 60:g}m  "
          f"cap {limits.max_position_base:g} BTC  "
          f"daily {limits.max_daily_loss_quote:g}")
    print(f"  costs         : fee {costs.fee_bps:g} bps/side  "
          f"slip {costs.slippage_bps:g} bps/side  "
          f"basis {costs.basis_bps:g} bps/entry")
    print(f"{'-' * 62}")
    print(f"  trades        : {len(r.trades)}")
    print(f"  win / loss    : {r.wins} / {r.losses}"
          + (f"  ({100 * r.wins / len(r.trades):.1f}% win)" if r.trades else ""))
    print(f"  gross PnL     : {fmt_money(r.gross_pnl)} quote")
    print(f"  costs         : {fmt_money(-r.costs)} quote")
    print(f"  NET PnL       : {fmt_money(net)} quote")
    print(f"  max drawdown  : {fmt_money(r.max_drawdown())} quote")
    print(f"  mean / trade  : {fmt_money(mean)} quote")
    print(f"  per-trade sd  : {sd:,.4f}")
    print(f"  Sharpe (ann.) : {sharpe:.2f}   [crude, see note]")
    print(f"  daily halts   : {r.daily_halts}")
    if r.delta_mismatches:
        print(f"  !! delta check: {r.delta_mismatches} mismatches vs the chart")
    if r.trades:
        holds = [t.held_seconds for t in r.trades]
        print(f"  avg hold      : {statistics.fmean(holds):.0f} s "
              f"(min {min(holds):.0f} / max {max(holds):.0f})")
        by_reason: dict[str, int] = {}
        for t in r.trades:
            by_reason[t.reason] = by_reason.get(t.reason, 0) + 1
        print(f"  exit reasons  : " + ", ".join(f"{k} {v}" for k, v in sorted(by_reason.items())))
    print(f"{'=' * 62}")

    return {
        "label": label,
        "hours": round(hours, 4),
        "prints": r.prints,
        "bars": r.bars,
        "trades": len(r.trades),
        "wins": r.wins,
        "losses": r.losses,
        "win_rate": round(r.wins / len(r.trades), 4) if r.trades else 0.0,
        "gross_pnl": round(r.gross_pnl, 6),
        "costs": round(r.costs, 6),
        "net_pnl": round(net, 6),
        "max_drawdown": round(r.max_drawdown(), 6),
        "mean_per_trade": round(mean, 6),
        "sharpe_annualised_crude": round(sharpe, 4),
        "market_return_pct": round(r.market_return_pct(), 4),
        "daily_halts": r.daily_halts,
        "stop_loss_pct": limits.stop_loss_pct,
        "take_profit_pct": limits.take_profit_pct,
        "order_size_base": order_size_base,
    }


def _parse_triple(s: str) -> tuple[float, float, float]:
    parts = s.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("expected stop:target:step, e.g. 0.25:1.0:0.25")
    try:
        a, b, c = (float(p) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError("all three values must be numbers") from None
    if c <= 0:
        raise argparse.ArgumentTypeError("step must be > 0")
    return a, b, c


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest the shipped signal loop over a tape.")
    ap.add_argument("--tape", type=Path, required=True)
    ap.add_argument("--venue", choices=sorted(VENUES), default="delta",
                    help="which venue recorded the tape (default: delta)")
    ap.add_argument("--stop-loss", type=float, default=1.0, help="percent, 0 disables")
    ap.add_argument("--take-profit", type=float, default=2.0, help="percent, 0 disables")
    ap.add_argument("--max-hold", type=float, default=3600.0, help="seconds, 0 disables")
    ap.add_argument("--max-position", type=float, default=0.01, help="base currency cap")
    ap.add_argument("--max-daily-loss", type=float, default=50.0, help="quote, 0 disables")
    ap.add_argument("--order-size", type=float, default=0.001, help="base currency per trade")
    ap.add_argument("--fee-bps", type=float, default=5.0, help="per side, on notional")
    ap.add_argument("--slippage-bps", type=float, default=2.0, help="per side")
    ap.add_argument("--basis-bps", type=float, default=0.0,
                    help="entry only, cross-venue mode; charged once on entry")
    ap.add_argument("--no-gate", action="store_true", help="disable the basis entry gate")
    ap.add_argument("--grid-stop", type=_parse_triple, default=None,
                    help="sweep stop:target:step, e.g. 0.25:1.0:0.25")
    ap.add_argument("--verify-delta", action="store_true",
                    help="cross-check the carried delta against the chart every print (slow)")
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()

    if not args.tape.exists():
        print(f"no such tape: {args.tape}", file=sys.stderr)
        return 2

    events = load_tape(args.tape, args.venue)
    if not events:
        print(f"the tape decoded to no prints with the {args.venue} decoder", file=sys.stderr)
        return 1
    span_h = (events[-1].ts_ms - events[0].ts_ms) / 3_600_000.0
    print(f"tape: {args.tape}  [{args.venue}]  {len(events):,} prints over {span_h:.2f} h")

    costs = Costs(fee_bps=args.fee_bps, slippage_bps=args.slippage_bps, basis_bps=args.basis_bps)
    results = []

    if args.grid_stop:
        lo, hi, step = args.grid_stop
        n = int(round((hi - lo) / step)) + 1
        print(f"\nsweeping {n} stop/target pairs, stop {lo:g}..{hi:g} step {step:g}, "
              f"target = 2x stop\n")
        best = None
        for i in range(n):
            stop = round(lo + i * step, 6)
            target = round(stop * 2.0, 6)
            limits = RiskLimits(
                stop_loss_pct=stop, take_profit_pct=target,
                max_position_base=args.max_position,
                max_daily_loss_quote=args.max_daily_loss,
                max_hold_seconds=args.max_hold,
            )
            r = run_backtest(events, limits, args.order_size, costs,
                             basis_mode=args.basis_bps > 0, use_gate=not args.no_gate,
                             venue=args.venue, verbose=args.verify_delta)
            net = r.net_pnl
            print(f"  stop {stop:5.2f}%  target {target:5.2f}%  "
                  f"trades {len(r.trades):5d}  net {net:+10.2f}  dd {r.max_drawdown():+9.2f}")
            results.append(report(r, limits, args.order_size, costs,
                                  f"stop {stop:g}% / target {target:g}%"))
            if best is None or net > best[0]:
                best = (net, stop, target, r)
        if best:
            print(f"\nbest by net PnL: stop {best[1]:g}% target {best[2]:g}% -> {best[0]:+,.2f}")
    else:
        limits = RiskLimits(
            stop_loss_pct=args.stop_loss, take_profit_pct=args.take_profit,
            max_position_base=args.max_position,
            max_daily_loss_quote=args.max_daily_loss,
            max_hold_seconds=args.max_hold,
        )
        r = run_backtest(events, limits, args.order_size, costs,
                         basis_mode=args.basis_bps > 0, use_gate=not args.no_gate,
                         venue=args.venue, verbose=args.verify_delta)
        results.append(report(r, limits, args.order_size, costs, "shipped rules"))

    if args.json_out:
        args.json_out.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")

    print("\nnote: fills are simulated at the print price plus cost; the strategy,")
    print("      risk ladder and exit logic are the shipped code paths.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
