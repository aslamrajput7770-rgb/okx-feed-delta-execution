"""Live OKX feed to Delta India execution.

    python3 live_feed.py --dry-run                    # offline synthetic feed, no socket
    python3 live_feed.py --minutes 1 --record cap.txt # capture, no orders
    python3 live_feed.py --minutes 1 --execute        # real orders (testnet)
    python3 live_feed.py --minutes 1 --execute --live # real orders (mainnet)

Safety: the run is a **dry run unless `--execute` is passed**, and execution
goes to **testnet unless `--live` is also passed**. Two flags must both be
present before a mainnet order can leave the process. A mainnet order also
needs `--i-know-this-is-live`, so that the guard lives in the program and not
only in `run_live.sh`: a shell script cannot be the only thing standing between
a typo and a real order.

Credentials come from the environment and are never read from a file argument:

    DELTA_API_KEY  DELTA_API_SECRET

Note on what this program is
----------------------------
OKX is the *signal* venue and Delta India is the *execution* venue. These are
two different contracts, quoted in two different currencies (USDT and USD), on
two different order books, with a basis between them that moves. Acting on an
OKX signal at Delta is therefore a **directional** position carrying basis risk,
not a hedged arbitrage. `--basis-warn` refuses to trade when the two venues are
far enough apart that the basis dominates the signal.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from orderflow import (
    DELTA_BTCUSD,
    OKX_BTC_USDT_SWAP,
    MarketCalendar,
    SessionChart,
    trading_day_of,
)
from orderflow.execution import (
    BASE_URLS,
    DELTA_PERP_RULES,
    DeltaCredentials,
    DeltaOrderClient,
    Environment,
    ExitReason,
    Position,
    ProductResolutionError,
    RiskLimits,
    RiskManager,
    RiskState,
    SizedOrder,
    contracts_for_base,
    resolve_perpetual,
)
from orderflow.feed import FileTransport, OkxTradeFeed, ReplayTransport, record_frames
from orderflow.feed.okx import PUBLIC_WS

DEFAULT_INSTRUMENTS = ["BTC-USDT-SWAP"]

#: How many synthetic trades `--dry-run` generates. Enough to produce a couple
#: of bars and a non-zero cumulative delta, so the order path is exercised.
DRY_RUN_TRADES = 600


def make_transport(inst_ids, record_path=None, replay_path=None, max_seconds=None):
    """Build the transport. Replay and record are the offline paths."""
    if replay_path is not None:
        return lambda: FileTransport(replay_path)

    def factory():


        return _LiveOkxTransport(inst_ids, PUBLIC_WS)

    return factory


class _LiveOkxTransport:
    """A real OKX socket. The only place in this package that opens one."""

    is_live = True

    def __init__(self, inst_ids, uri: str, timeout: float = 10.0) -> None:
        self.inst_ids = list(inst_ids)
        self.uri = uri
        self.timeout = timeout
        self._ws = None
        self.recorded: list[str] = []

    def connect(self) -> None:
        try:
            import websocket  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "live mode needs 'websocket-client': pip install websocket-client"
            ) from exc
        self._ws = websocket.create_connection(self.uri, timeout=self.timeout)

    def send(self, data, binary: bool = False) -> None:
        if self._ws is None:
            raise RuntimeError("transport is not connected")
        self._ws.send(data)

    def heartbeat(self) -> None:
        if self._ws is not None:
            self._ws.send("ping")

    def recv(self, timeout=None):
        if self._ws is None:
            raise RuntimeError("transport is not connected")
        if timeout is not None:
            self._ws.settimeout(timeout)
        try:
            raw = self._ws.recv()
        except Exception:
            return None
        if isinstance(raw, str):
            self.recorded.append(raw)
        return raw

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            finally:
                self._ws = None


def basis_pct(okx_price: float, delta_price: float) -> float:
    """Delta's premium over OKX, as a percentage of the OKX price."""
    if okx_price <= 0:
        raise ValueError("okx_price must be > 0")
    return (delta_price - okx_price) / okx_price * 100.0


def delta_mark_price(base_url: str, symbol: str, timeout: float = 10.0) -> float:
    """The venue's mark price, from the public ticker. No credentials needed.

    `mark_price` is used rather than `close` because it is the price the venue
    values the contract at, which is the number the basis should be measured
    against.
    """
    url = f"{base_url}/v2/tickers/{symbol}"
    req = urllib.request.Request(url, headers={"User-Agent": "okx-feed-delta-execution"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read() or b"{}")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        raise RuntimeError(f"GET {url} failed: {exc}") from exc
    result = payload.get("result") or {}
    mark = result.get("mark_price")
    if mark is None:
        raise RuntimeError(f"{url} returned no mark_price")
    return float(mark)


def order_side(cumulative_delta: float) -> str | None:
    """Which way the signal points, or None when there is no signal.

    A cumulative delta of exactly zero is not a weak signal in either
    direction: it is the absence of one. Sending a sell on it would be an order
    with nothing behind it, so the caller is told there is nothing to do.
    """
    if cumulative_delta > 0:
        return "buy"
    if cumulative_delta < 0:
        return "sell"
    return None


class Supervisor:
    """Runs the entry/exit loop over a live feed.

    This is the piece the runner did not have. Before it, `main()` computed one
    signal, sent one order, and exited, leaving the position open with nothing
    watching it. A stop loss is impossible in that shape: something has to hold
    the position and check the price against the stop on every tick.

    The loop is deliberately small and synchronous, because the feed callback is
    synchronous. It holds at most one position, like everything else here.

        supervisor = Supervisor(client=..., limits=..., product_id=27)
        feed.run(supervisor.on_trade)   # enters and exits inside the callback
        supervisor.close_out(now)       # nothing is left open at the end

    `on_trade` is called once per trade, which is the only clock the feed
    offers. Time is taken from `trade.ts` rather than the wall clock so a replay
    behaves exactly like the live run it is a recording of.
    """

    def __init__(
        self,
        client,
        limits: RiskLimits,
        product_id: int,
        order_size_base: float,
        exec_instrument=DELTA_BTCUSD,
        state_path=None,
        execute: bool = False,
        seed: int = 1,
        entry_gate=None,
        max_consecutive_failures: int = 3,
    ) -> None:
        self.client = client
        self.limits = limits
        self.product_id = product_id
        self.order_size_base = order_size_base
        self.exec_instrument = exec_instrument
        self.execute = execute
        self.state_path = state_path
        #: An extra veto applied at entry time, on top of the limits. Used for
        #: the basis check, which needs a price and therefore cannot run before
        #: the feed has produced one.
        self.entry_gate = entry_gate
        #: A circuit breaker. A rejected credential or a blocked IP fails every
        #: order identically, and retrying it on every trade turns one bad
        #: config into a request storm against the venue. Consecutive failures
        #: are counted and the loop stops; a success resets the count, so a
        #: single transient blip does not end the run.
        self.max_consecutive_failures = max_consecutive_failures
        self.consecutive_failures = 0
        self.stopped = False

        day_key = ""
        self.manager = RiskManager(
            limits=limits,
            state=RiskState.load(state_path, day_key) if state_path else RiskState(day_key=""),
        )
        #: Entry signal is the sign of cumulative delta. It is read from the
        #: chart the caller is already feeding; the supervisor only needs the
        #: last value, so it is passed in on each call.
        self._seq = 0
        self._seed = seed
        self.entries = 0
        self.exits = 0
        self._day_key = day_key
        self._last_price: float | None = None

    # ------------------------------------------------------------------ entry
    def _client_order_id(self) -> str:
        self._seq += 1
        return f"ofd-{self._seed}-{self._seq}"

    def _enter(self, side: str, price: float, ts: float) -> None:
        """Size, check, and (if executing) send the entry order."""
        if self.entry_gate is not None:
            allowed, why = self.entry_gate(price)
            if not allowed:
                self.manager._note(f"ENTRY BLOCKED by gate: {why}")
                return

        try:
            sized: SizedOrder = contracts_for_base(
                self.exec_instrument, self.order_size_base, DELTA_PERP_RULES
            )
        except Exception as exc:  # SizingError
            self.manager._note(f"ENTRY SKIPPED: cannot size {self.order_size_base:g} "
                               f"base: {exc}")
            return

        decision = self.manager.check_entry(sized.base, price)
        if not decision:
            self.manager._note(f"ENTRY REFUSED: {decision.reason}")
            return

        cid = self._client_order_id()
        position = Position(
            side=side,
            entry_price=price,
            size_base=sized.base,
            contracts=int(sized.contracts),
            product_id=self.product_id,
            entry_ts=ts,
            entry_client_id=cid,
        )

        if self.execute:
            fn = self.client.buy if side == "buy" else self.client.sell
            result = fn(product_id=self.product_id, size=int(sized.contracts))
            if not result.ok:
                self._note_failure(
                    f"ENTRY FAILED: {side} {int(sized.contracts)} contract(s) "
                    f"status={result.status_code} error={result.error}"
                )
                return
            self.consecutive_failures = 0
            self.manager._note(f"ENTRY SENT: {cid}")

        self.manager.open(position)
        self.entries += 1

    # ------------------------------------------------------------------- exit
    def _exit(self, price: float, reason: str) -> None:
        p = self.manager.position
        assert p is not None
        if self.execute:
            fn = self.client.sell if p.exit_side == "sell" else self.client.buy
            result = fn(
                product_id=self.product_id,
                size=p.contracts,
                reduce_only=True,
                client_order_id=self._client_order_id(),
            )
            if not result.ok:
                self._note_failure(
                    f"EXIT FAILED ({reason}): status={result.status_code} "
                    f"error={result.error}. Position is still open."
                )
                # A failed exit must not be recorded as closed: the position is
                # still live at the venue, and forgetting it is how a stop loss
                # becomes a bigger loss.
                return
            self.consecutive_failures = 0

        self.manager.close(price, reason)
        self.exits += 1

    # ------------------------------------------------------------------- util
    def _note_failure(self, message: str) -> None:
        """Record a venue failure and stop the loop if they keep coming.

        An invalid key or an unwhitelisted IP fails every request the same way.
        Without this, the loop retries on every trade and turns a config mistake
        into hundreds of signed requests against the venue.
        """
        self.consecutive_failures += 1
        self.manager._note(message)
        if self.consecutive_failures >= self.max_consecutive_failures:
            self.stopped = True
            self.manager._note(
                f"STOPPED after {self.consecutive_failures} consecutive venue "
                f"failures. This is not a transient error; check the credentials, "
                f"the whitelisted IP, and the account. No further orders."
            )

    # --------------------------------------------------------------- callback
    def on_trade(self, trade, event=None, cumulative_delta: float | None = None) -> None:
        """One tick: check the exit first, then consider an entry.

        Exit before entry, always. If both were possible on the same tick, the
        risk is in the position that is already open.
        """
        price = trade.price
        ts = trade.ts
        self._last_price = price

        day_key = str(trading_day_of(datetime.fromtimestamp(ts, tz=timezone.utc)))
        if day_key != self._day_key:
            self._day_key = day_key
            if self.state_path:
                self.manager.state = RiskState.load(self.state_path, day_key)
            self.manager._note(f"NEW DAY {day_key}: daily loss budget reset")

        if self.manager.position is not None:
            signal = self.manager.check_exit(price, now=ts)
            if signal:
                self._exit(price, signal.reason)
            return

        if self.stopped:
            return
        if self.manager.halted:
            return

        if cumulative_delta is None:
            return
        side = order_side(cumulative_delta)
        if side is None:
            return
        self._enter(side, price, ts)

    # ------------------------------------------------------------ end of run
    def close_out(self, reason: str = ExitReason.END_OF_RUN) -> None:
        """Close any open position. The runner calls this before returning.

        Without it a run would end with a live position and nothing watching it,
        which is the exact failure the exit loop exists to remove.
        """
        if self.manager.position is None:
            return
        price = self._last_price
        if price is None:
            return
        self._exit(price, reason)

    # ----------------------------------------------------------------- report
    def save_state(self) -> None:
        if self.state_path:
            self.manager.state.save(self.state_path)

    @property
    def realized(self) -> float:
        return self.manager.state.realized_pnl_quote


def dry_run_frames(inst_id: str, n: int = DRY_RUN_TRADES, seed: int = 1) -> list[str]:
    """A synthetic, deterministic two-sided tape, as OKX trade frames.

    Exists so `--dry-run` is genuinely offline: the flag promises no socket, and
    this is what makes that true. `random.Random(seed)` rather than the module
    RNG, so two dry runs produce identical output.
    """
    import random

    rng = random.Random(seed)
    price = 60000.0
    ts_ms = 1_792_574_700_000
    frames = []
    for i in range(n):
        price += rng.choice([-0.5, 0.0, 0.0, 0.5])
        side = "buy" if rng.random() > 0.45 else "sell"
        frames.append(
            json.dumps(
                {
                    "arg": {"channel": "trades", "instId": inst_id},
                    "data": [
                        {
                            "instId": inst_id,
                            "tradeId": str(i),
                            "px": f"{price:.1f}",
                            "sz": f"{rng.uniform(0.01, 0.4):.2f}",
                            "side": side,
                            "ts": str(ts_ms + i * 1000),
                        }
                    ],
                }
            )
        )
    return frames


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OKX feed -> Delta India execution")
    ap.add_argument("--instruments", nargs="+", default=DEFAULT_INSTRUMENTS)
    ap.add_argument("--minutes", type=float, default=0, help="0 means unbounded")
    ap.add_argument("--bar-seconds", type=float, default=60.0)
    ap.add_argument("--record", type=Path, default=None)
    ap.add_argument("--replay", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="offline: synthetic tape, no socket, no order. Also the "
                         "behaviour when neither --execute nor a socket is wanted.")
    ap.add_argument("--execute", action="store_true", help="place orders")
    ap.add_argument("--live", action="store_true", help="mainnet, not testnet")
    ap.add_argument("--i-know-this-is-live", dest="confirm_live", action="store_true",
                    help="required alongside --execute --live; a mainnet order must "
                         "be confirmed in the program, not only in run_live.sh")
    ap.add_argument("--order-size", type=float, default=0.0,
                    help="base-currency exposure per signal; with no --execute this "
                         "only reports the order it would send")
    ap.add_argument("--basis-warn", type=float, default=0.5,
                    help="refuse to trade when |basis| exceeds this percent")
    ap.add_argument("--stop-loss-pct", type=float, default=1.0,
                    help="close the position at this loss, percent of entry (0 disables)")
    ap.add_argument("--take-profit-pct", type=float, default=2.0,
                    help="close the position at this gain, percent of entry (0 disables)")
    ap.add_argument("--max-position", type=float, default=0.01,
                    help="largest position in base currency; a bigger signal is refused")
    ap.add_argument("--max-daily-loss", type=float, default=50.0,
                    help="stop trading for the day after this realized loss, in quote (0 disables)")
    ap.add_argument("--max-hold-seconds", type=float, default=3600.0,
                    help="close regardless after this long (0 disables)")
    ap.add_argument("--risk-state", type=Path, default=Path("state/daily_pnl.json"),
                    help="where the daily loss counter is persisted across restarts")
    ap.add_argument("--no-risk-state", dest="risk_state", action="store_const", const=None,
                    help="do not persist the daily counter (testing only)")
    ap.add_argument("--max-order-failures", type=int, default=3,
                    help="stop after this many consecutive venue failures; a bad key "
                         "or a blocked IP fails every order the same way")
    args = ap.parse_args(argv)

    if args.execute and args.order_size <= 0:
        print("--execute needs a positive --order-size", file=sys.stderr)
        return 2

    # The guard lives here, not only in run_live.sh: calling this file directly
    # must not be a way around it.
    if args.execute and args.live and not args.confirm_live:
        print("refusing --execute --live without --i-know-this-is-live", file=sys.stderr)
        print("mainnet orders are real money. Add the flag if that is what you want.",
              file=sys.stderr)
        return 2

    environment = Environment.LIVE if args.live else Environment.TESTNET
    allow_live = bool(args.execute)

    # `--dry-run` means offline, and the flag has to actually say so: a live
    # socket with `--minutes 0` never returns, which is not a dry run.
    offline = args.dry_run or args.replay is not None
    if args.dry_run and args.execute:
        print("--dry-run and --execute are contradictory", file=sys.stderr)
        return 2
    if not offline and args.minutes <= 0:
        print("a live run needs --minutes > 0; an unbounded socket never returns. "
              "Use --dry-run for an offline check.", file=sys.stderr)
        return 2

    client = DeltaOrderClient(environment=environment, allow_live=allow_live,
                              dry_run=not args.execute)
    if args.dry_run:
        print(f"venue      : offline synthetic tape ({DRY_RUN_TRADES} prints), no socket")
    elif args.replay is not None:
        print(f"venue      : replay of {args.replay}, no socket")
    else:
        print(f"venue      : OKX {PUBLIC_WS}")
    print(f"execution  : Delta {environment.value} "
          f"({'orders enabled' if args.execute else 'DRY RUN, no orders'})")
    if args.execute:
        print(f"size       : {args.order_size:g} BTC per signal")
        creds = DeltaCredentials.from_env()
        print(f"credentials: {creds!r}")

    cal = MarketCalendar()
    chart = SessionChart(OKX_BTC_USDT_SWAP.grid(), cal, bar_seconds=args.bar_seconds)

    capture: list[str] = []
    transport_holder = {}

    def factory():
        if args.dry_run:
            return ReplayTransport(dry_run_frames(args.instruments[0]))
        t = make_transport(args.instruments, replay_path=args.replay)()
        transport_holder["t"] = t
        return t

    feed = OkxTradeFeed(args.instruments, factory)

    def on_trade(trade, event):
        chart.add(trade)
        if args.record is not None:
            capture.append(json.dumps({"event": "trade", "instId": event.inst_id,
                                       "tradeId": event.trade_id}))

    max_seconds = args.minutes * 60.0 if args.minutes > 0 else None
    try:
        stats = feed.run(on_trade, max_seconds=max_seconds)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        holder = transport_holder.get("t")
        if holder is not None and args.record is not None and holder.recorded:
            record_frames(holder.recorded, args.record)
            print(f"recorded {len(holder.recorded)} frames to {args.record}")

    print(f"frames={stats.frames} trades={stats.trades} "
          f"duplicates={stats.duplicates} control={stats.control_frames} "
          f"errors={stats.protocol_errors}")
    print(f"bars={len(chart.bars)} volume={chart.session_volume:g} "
          f"cum_delta={chart.cumulative_delta:g}")

    if stats.trades == 0:
        print("no trades: the socket connected but delivered nothing. "
              "That is a failure, not a quiet market.", file=sys.stderr)
        return 1

    # Resolve and size the order whenever a size was requested, not only when
    # orders are enabled. A dry run that skips this prints nothing about what it
    # would have done, which is the one thing it exists to show.
    if args.order_size > 0:
        if offline:
            # --dry-run and --replay both promise no socket, and resolving the
            # product is an HTTP call. Use the static template and say so,
            # rather than quietly reaching the network.
            product, exec_instrument = None, DELTA_BTCUSD
            print(f"execution leg: {DELTA_BTCUSD.symbol} (static template; "
                  f"the venue spec is not fetched offline)")
        else:
            # Resolve by symbol, never by a hardcoded id: production carries the
            # perpetual on id 27 while testnet carries an expired future there.
            try:
                product, exec_instrument = resolve_perpetual(
                    BASE_URLS[environment], DELTA_BTCUSD.symbol, DELTA_BTCUSD
                )
            except ProductResolutionError as exc:
                print(f"cannot resolve the execution leg: {exc}", file=sys.stderr)
                return 1

            print(f"execution leg: id {product.venue_id} {product.symbol} "
                  f"tick {product.tick_size} 1 contract = {product.contract_value} base")
            if product.grid_problems(DELTA_BTCUSD):
                print(f"  note: {'; '.join(product.grid_problems(DELTA_BTCUSD))}")

        limits = RiskLimits(
            stop_loss_pct=args.stop_loss_pct,
            take_profit_pct=args.take_profit_pct,
            max_position_base=args.max_position,
            max_daily_loss_quote=args.max_daily_loss,
            max_hold_seconds=args.max_hold_seconds,
        )
        print(f"risk         : SL {limits.stop_loss_pct:g}% TP {limits.take_profit_pct:g}% "
              f"cap {limits.max_position_base:g} base "
              f"daily loss {limits.max_daily_loss_quote:g} quote "
              f"hold {limits.max_hold_seconds:g}s")
        print(f"risk state   : {args.risk_state or 'not persisted'}")

        # The basis is the risk that the two venues are not the same instrument:
        # a position taken on an OKX signal at Delta carries whatever the spread
        # between them does. The check needs a price, so it runs at entry time
        # through the gate rather than here.
        #
        # Only meaningful on a live feed: a replayed capture holds historical
        # prices, so comparing them to today's mark price measures the elapsed
        # time, not the basis.
        gate = None
        if args.basis_warn > 0 and not offline:
            def gate(price: float, _limit=args.basis_warn):
                try:
                    delta_px = delta_mark_price(BASE_URLS[environment], DELTA_BTCUSD.symbol)
                except RuntimeError as exc:
                    if args.execute:
                        return False, f"basis unreadable ({exc})"
                    return True, f"basis unreadable ({exc}), not executing"
                b = basis_pct(price, delta_px)
                if abs(b) > _limit:
                    return False, (f"|basis| {abs(b):.3f}% exceeds {_limit:g}% "
                                   f"(OKX {price:g} vs Delta {delta_px:g})")
                return True, f"basis {b:+.3f}%"

        if not args.execute:
            where = "static template" if product is None else f"product_id={product.venue_id}"
            print(f"dry run      : would trade up to "
                  f"{int(contracts_for_base(exec_instrument, args.order_size, DELTA_PERP_RULES).contracts)} "
                  f"contract(s) of {where}, with the exits above. No request sent.")
            return 0

        supervisor = Supervisor(
            client=client,
            limits=limits,
            product_id=product.venue_id,
            order_size_base=args.order_size,
            exec_instrument=exec_instrument,
            state_path=args.risk_state,
            execute=True,
            seed=int(time.time()),
            entry_gate=gate,
            max_consecutive_failures=args.max_order_failures,
        )
        if supervisor.manager.state.day_key:
            print(f"risk day     : {supervisor.manager.state.day_key} "
                  f"realized so far {supervisor.realized:+.4f} quote")
        if supervisor.manager.halted:
            print("HALTED       : the daily loss limit was already reached for this day. "
                  "No orders will be sent.", file=sys.stderr)
            return 1

        # The entry/exit loop is attached to the feed that is about to run, so
        # the session is measured and traded in a single pass.
        def on_trade_supervised(trade, event):
            chart.add(trade)
            if args.record is not None:
                capture.append(json.dumps({"event": "trade", "instId": event.inst_id,
                                           "tradeId": event.trade_id}))
            supervisor.on_trade(trade, event, cumulative_delta=chart.cumulative_delta)

        print("supervising  : entry/exit loop is live; positions are watched and closed")
        try:
            stats = feed.run(on_trade_supervised, max_seconds=max_seconds)
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            supervisor.close_out(ExitReason.END_OF_RUN)
            supervisor.save_state()
            return 130

        # Nothing is left open when the process returns.
        supervisor.close_out(ExitReason.END_OF_RUN)
        supervisor.save_state()

        print(f"trades       : entries={supervisor.entries} exits={supervisor.exits}")
        print(f"realized pnl : {supervisor.realized:+.4f} quote")
        if supervisor.manager.halted:
            print("HALTED       : daily loss limit reached; trading stopped for this day.")
        if supervisor.stopped:
            print(f"STOPPED      : {supervisor.consecutive_failures} consecutive venue "
                  f"failures. The venue is rejecting orders; check the credentials, "
                  f"the whitelisted IP, and the account.", file=sys.stderr)
        if supervisor.manager.position is not None:
            print(f"WARNING      : a {supervisor.manager.position.side} position of "
                  f"{supervisor.manager.position.size_base:g} base is STILL OPEN at "
                  f"the venue. The exit could not be sent. Close it by hand.",
                  file=sys.stderr)
        for line in supervisor.manager.log:
            print(f"  {line}")
        return 1 if (supervisor.stopped or supervisor.manager.position is not None) else 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
