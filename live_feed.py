"""Live trade-print feed to Delta India execution.

    python3 live_feed.py --dry-run                     # offline synthetic feed, no socket
    python3 live_feed.py --minutes 1 --record cap.txt  # capture, no orders
    python3 live_feed.py --minutes 1 --execute         # real orders (testnet)
    python3 live_feed.py --minutes 1 --execute --live  # real orders (mainnet)

The signal is read from **Delta Exchange India**, which is also the venue the
orders go to. There is therefore no basis between the signal price and the fill
price, and nothing to reconcile between two books.

Safety: the run is a **dry run unless `--execute` is passed**, and execution
goes to **testnet unless `--live` is also passed**. Two flags must both be
present before a mainnet order can leave the process. A mainnet order also
needs `--i-know-this-is-live`, so that the guard lives in the program and not
only in `run_live.sh`: a shell script cannot be the only thing standing between
a typo and a real order.

Credentials come from the environment and are never read from a file argument:

    DELTA_API_KEY  DELTA_API_SECRET
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:  # Optional: only live mode needs it, and the offline paths must not.
    import websocket  # type: ignore
except ImportError:  # pragma: no cover - exercised by the ImportError path
    websocket = None  # type: ignore[assignment]

from orderflow import (
    DELTA_BTCUSD,
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
from orderflow.feed import (
    DELTA_PUBLIC_WS,
    DeltaTradeFeed,
    FileTransport,
    ReplayTransport,
    record_frames,
)
from orderflow.notify import (
    NullNotifier,
    format_entry,
    format_exit,
    format_run_summary,
    format_signal,
    notifier_from_env,
)

#: Delta's BTC perpetual. This is both the signal source and the venue orders
#: are placed on - there is no second book to read or to reconcile against.
SIGNAL_SYMBOL = "BTCUSD"
SIGNAL_INSTRUMENT = DELTA_BTCUSD
SIGNAL_WS = DELTA_PUBLIC_WS
SIGNAL_FEED = DeltaTradeFeed
DEFAULT_INSTRUMENTS = [SIGNAL_SYMBOL]

#: How many synthetic trades `--dry-run` generates. Enough to produce a couple
#: of bars and a non-zero cumulative delta, so the order path is exercised.
DRY_RUN_TRADES = 600


def make_transport(inst_ids, uri: str, replay_path=None):
    """Build the transport. Replay is the offline path."""
    if replay_path is not None:
        return lambda: FileTransport(replay_path)

    def factory():
        return _LiveTransport(inst_ids, uri)

    return factory


class _LiveTransport:
    """A real market-data socket. The only place in this package that opens one."""

    is_live = True

    def __init__(self, inst_ids, uri: str, timeout: float = 10.0) -> None:
        self.inst_ids = list(inst_ids)
        self.uri = uri
        self.timeout = timeout
        self._ws = None
        self.recorded: list[str] = []
        #: Set once a recv fails. A closed socket returns nothing on every later
        #: call, so without this the loop cannot tell a dead socket from a quiet
        #: one and heartbeats into the void.
        self.dead = False

    def connect(self) -> None:
        if websocket is None:
            raise ImportError(
                "live mode needs 'websocket-client': pip install websocket-client"
            )
        self._ws = websocket.create_connection(self.uri, timeout=self.timeout)
        # A reconnect reuses this object, so the drop flag is cleared here rather
        # than in __init__: a stale flag would force a reconnect on the first
        # quiet second of the new socket.
        self.dead = False

    def send(self, data, binary: bool = False) -> None:
        if self._ws is None:
            raise RuntimeError("transport is not connected")
        self._ws.send(data)

    def heartbeat(self) -> None:
        if self._ws is not None:
            # Delta wants a JSON frame, not a bare "ping". Sending the wrong one
            # keeps the TCP connection alive while the venue stops sending data,
            # which reads as a quiet market rather than a dead socket.
            self._ws.send(json.dumps({"type": "ping"}))

    def recv(self, timeout=None):
        if self._ws is None:
            raise RuntimeError("transport is not connected")
        if timeout is not None:
            self._ws.settimeout(timeout)
        try:
            raw = self._ws.recv()
        except Exception as exc:
            if websocket is not None and isinstance(exc, websocket.WebSocketTimeoutException):
                # A quiet second is normal on a live socket, and is not a drop.
                return None
            # Anything else is the socket going away. Recorded, because the next
            # call returns nothing too and would otherwise read as idle.
            self.dead = True
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
        max_consecutive_failures: int = 3,
        notifier=None,
        notify_mode: str = "paper",
        venue: str = "DELTA BTCUSD",
    ) -> None:
        self.client = client
        self.limits = limits
        self.product_id = product_id
        self.order_size_base = order_size_base
        self.exec_instrument = exec_instrument
        self.execute = execute
        self.state_path = state_path
        #: A circuit breaker. A rejected credential or a blocked IP fails every
        #: order identically, and retrying it on every trade turns one bad
        #: config into a request storm against the venue. Consecutive failures
        #: are counted and the loop stops; a success resets the count, so a
        #: single transient blip does not end the run.
        self.max_consecutive_failures = max_consecutive_failures
        self.consecutive_failures = 0
        self.stopped = False
        #: Optional signal delivery. Defaults to a sink that drops everything, so
        #: a run without Telegram behaves exactly as it did before this existed.
        self.notifier = notifier if notifier is not None else NullNotifier()
        self.notify_mode = notify_mode
        self.venue = venue
        #: Delivery failures, for the run log. Never fatal: a lost message must
        #: not stop the loop that is watching an open position.
        self.notify_errors: list[str] = []

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
        self._last_ts: float | None = None

    # ------------------------------------------------------------------ entry
    def _client_order_id(self) -> str:
        self._seq += 1
        return f"ofd-{self._seed}-{self._seq}"

    def notify(self, text: str) -> None:
        """Deliver a message, and record a failure without raising.

        The loop is a tick callback: an exception here would abandon the feed and
        leave a position unwatched, which is a far worse outcome than a lost
        notification. This is the only place a notification is delivered, so the
        same rule covers entries, exits, signals and the run summary.
        """
        result = self.notifier.send(text)
        if not result.ok and not result.skipped:
            self.notify_errors.append(result.error or "unknown delivery failure")
            self.manager._note(f"NOTIFY FAILED: {result.error}")

    def _enter(self, side: str, price: float, ts: float, cumulative_delta: float | None = None) -> None:
        """Size, check, and (if executing) send the entry order."""
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
        self.notify(
            format_entry(
                side=side,
                price=price,
                size_base=sized.base,
                contracts=int(sized.contracts),
                cumulative_delta=cumulative_delta,
                mode=self.notify_mode,
                product=f"Delta India id {self.product_id}",
                venue=self.venue,
            )
        )

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
        self.notify(
            format_exit(
                side=p.side,
                price=price,
                entry_price=p.entry_price,
                reason=reason,
                pnl_quote=p.pnl_quote(price),
                held_seconds=p.held_seconds(self._last_ts if self._last_ts else p.entry_ts),
                mode=self.notify_mode,
                venue=self.venue,
            )
        )

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
        self._last_ts = ts

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
        self._enter(side, price, ts, cumulative_delta=cumulative_delta)

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
    """A synthetic, deterministic two-sided tape, in Delta's frame shape.

    Exists so `--dry-run` is genuinely offline: the flag promises no socket, and
    this is what makes that true. `random.Random(seed)` rather than the module
    RNG, so two dry runs produce identical output.

    The shape must be the decoder's shape. A frame in any other venue's format
    decodes to nothing, so a dry run built from the wrong one would report a
    healthy feed and zero trades.
    """
    import random

    rng = random.Random(seed)
    price = 60000.0
    ts_ms = 1_792_574_700_000
    frames = []
    for i in range(n):
        price += rng.choice([-0.5, 0.0, 0.0, 0.5])
        side = "buy" if rng.random() > 0.45 else "sell"
        size = rng.uniform(1.0, 40.0)  # contracts: Delta's unit
        frames.append(json.dumps({
            "type": "trades", "sy": inst_id, "p": f"{price:.1f}", "s": round(size, 3),
            # `r` is the buyer's role: "t" is a buy aggressor, "m" a sell.
            "r": "t" if side == "buy" else "m",
            "t": (ts_ms + i * 1000) * 1000, "ts": (ts_ms + i * 1000) * 1000,
        }))
    return frames


def _exit_on_sigterm() -> None:
    """Make SIGTERM behave like Ctrl-C.

    This is what a service manager sends: `systemctl stop` and an EC2 instance
    stop both deliver SIGTERM, never SIGINT. Without this the interpreter dies on
    the default disposition, so the `KeyboardInterrupt` handlers that close the
    position and save the daily counter never run. The position is abandoned and
    the loss budget resets on restart, which is the one moment the risk limits
    matter most.

    Raising `KeyboardInterrupt` rather than exiting from the handler keeps the
    shutdown on the single path that already exists, so there is one place where
    a position is closed rather than two that can drift.
    """
    def handler(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="trade-print feed -> Delta India execution")
    ap.add_argument("--instruments", nargs="+", default=None,
                    help="default: the venue's BTC perpetual")
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
    ap.add_argument("--telegram", action="store_true",
                    help="send signals to Telegram. Needs TELEGRAM_BOT_TOKEN and "
                         "TELEGRAM_CHAT_ID in the environment; a missing pair is an "
                         "error when this flag is present, and silence otherwise")
    ap.add_argument("--telegram-every-signal", action="store_true",
                    help="also notify on each signal change while flat, not only on "
                         "entries and exits. Noisy on a live feed; useful for a test")
    ap.add_argument("--max-reconnects", type=int, default=None,
                    help="give up after this many socket drops (default: retry "
                         "forever, which is what a service wants). Set a bound when "
                         "a silent feed is worse than a stopped one.")
    args = ap.parse_args(argv)

    if args.instruments is None:
        args.instruments = [SIGNAL_SYMBOL]
    args.instruments = list(args.instruments)

    # Installed before any socket is opened, so a stop signal that arrives during
    # the run is handled the same way a Ctrl-C is.
    _exit_on_sigterm()

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

    # Telegram is opt-in. `--telegram` with no credentials is a configuration
    # error, not a silent no-op: a run that thinks it is notifying and is not is
    # worse than one that refuses to start.
    notifier = NullNotifier()
    notify_mode = "live" if args.execute else "paper"
    if args.telegram:
        notifier = notifier_from_env()
        if isinstance(notifier, NullNotifier):
            print(f"--telegram needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the "
                  f"environment.", file=sys.stderr)
            return 2
        print(f"telegram   : enabled ({notify_mode} mode)")
    else:
        print(f"telegram   : disabled (pass --telegram to send signals)")

    if args.dry_run:
        print(f"venue      : offline synthetic tape ({DRY_RUN_TRADES} prints), no socket")
    elif args.replay is not None:
        print(f"venue      : replay of {args.replay}, no socket")
    else:
        print(f"venue      : DELTA {SIGNAL_WS}")
    print(f"execution  : Delta {environment.value} "
          f"({'orders enabled' if args.execute else 'DRY RUN, no orders'})")
    if args.execute:
        print(f"size       : {args.order_size:g} BTC per signal")
        creds = DeltaCredentials.from_env()
        print(f"credentials: {creds!r}")

    cal = MarketCalendar()
    chart = SessionChart(SIGNAL_INSTRUMENT.grid(), cal, bar_seconds=args.bar_seconds)

    capture: list[str] = []
    transport_holder = {}

    def factory():
        if args.dry_run:
            return ReplayTransport(dry_run_frames(args.instruments[0]))
        t = make_transport(args.instruments, SIGNAL_WS, replay_path=args.replay)()
        transport_holder["t"] = t
        return t

    feed = SIGNAL_FEED(args.instruments, factory)
    max_seconds = args.minutes * 60.0 if args.minutes > 0 else None

    # The feed is run exactly once, and the supervisor is attached to that one
    # pass. Running it twice does not work: dedup is on `(instId, tradeId)`, so a
    # second pass over the same frames delivers zero trades to its callback and
    # the supervised pass would trade nothing while reporting a healthy feed.
    #
    # Everything the run needs to decide therefore has to be resolved before the
    # first frame arrives.
    supervisor = None
    limits = None
    notify_mode = "live" if args.execute else "paper"

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

        if not args.execute:
            where = "static template" if product is None else f"product_id={product.venue_id}"
            print(f"dry run      : would trade up to "
                  f"{int(contracts_for_base(exec_instrument, args.order_size, DELTA_PERP_RULES).contracts)} "
                  f"contract(s) of {where}, with the exits above. No request sent.")

        # Paper mode runs the same supervisor as a live run, with `execute=False`
        # so no request leaves the process. The loop, the risk rules and the
        # notifications are therefore the ones a live run would use, which is the
        # only version of paper mode worth having: a paper path that skips the
        # supervisor proves nothing about the live path.
        supervisor = Supervisor(
            client=client,
            limits=limits,
            product_id=product.venue_id if product is not None else DELTA_BTCUSD.venue_id,
            order_size_base=args.order_size,
            exec_instrument=exec_instrument,
            state_path=args.risk_state,
            execute=args.execute,
            seed=int(time.time()),
            max_consecutive_failures=args.max_order_failures,
            notifier=notifier,
            notify_mode=notify_mode,
            venue=f"DELTA {args.instruments[0]}",
        )
        if supervisor.manager.state.day_key:
            print(f"risk day     : {supervisor.manager.state.day_key} "
                  f"realized so far {supervisor.realized:+.4f} quote")
        if supervisor.manager.halted:
            print("HALTED       : the daily loss limit was already reached for this day. "
                  "No orders will be sent.", file=sys.stderr)
            return 1

        print(f"supervising  : entry/exit loop is live; "
              f"{'orders are placed' if args.execute else 'paper, nothing is sent'}")
    elif args.telegram:
        print("telegram   : idle - pass --order-size to run the signal loop")

    #: The sign last announced, so `--telegram-every-signal` notifies on a change
    #: rather than on every tick that keeps the sign.
    last_signalled: str | None = None

    def on_trade(trade, event):
        nonlocal last_signalled
        chart.add(trade)
        if args.record is not None:
            capture.append(json.dumps({"event": "trade", "instId": event.inst_id,
                                       "tradeId": event.trade_id}))
        if supervisor is None:
            return
        cum = chart.cumulative_delta
        # An optional per-signal notice, off by default: on a live feed the sign
        # flips rarely but the notification would still arrive on every tick that
        # keeps it, so it is gated on the signal actually changing.
        if args.telegram_every_signal and supervisor.manager.position is None:
            side = order_side(cum)
            if side is not None and side != last_signalled:
                last_signalled = side
                # Routed through the supervisor so a delivery failure is logged
                # and swallowed, the same as an entry or exit notice.
                supervisor.notify(format_signal(
                    side=side, price=trade.price, cumulative_delta=cum,
                    session_volume=chart.session_volume, bars=len(chart.bars),
                    mode=notify_mode,
                ))
        supervisor.on_trade(trade, event, cumulative_delta=cum)

    try:
        stats = feed.run(
            on_trade,
            max_seconds=max_seconds,
            max_reconnects=args.max_reconnects,
        )
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        if supervisor is not None:
            supervisor.close_out(ExitReason.END_OF_RUN)
            supervisor.save_state()
        return 130
    finally:
        holder = transport_holder.get("t")
        if holder is not None and args.record is not None and holder.recorded:
            record_frames(holder.recorded, args.record)
            print(f"recorded {len(holder.recorded)} frames to {args.record}")

    print(f"frames={stats.frames} trades={stats.trades} "
          f"duplicates={stats.duplicates} control={stats.control_frames} "
          f"errors={stats.protocol_errors} reconnects={stats.reconnects}")
    if stats.last_error:
        print(f"last error  : {stats.last_error}", file=sys.stderr)
    print(f"bars={len(chart.bars)} volume={chart.session_volume:g} "
          f"cum_delta={chart.cumulative_delta:g}")

    if stats.trades == 0:
        print("no trades: the socket connected but delivered nothing. "
              "That is a failure, not a quiet market.", file=sys.stderr)
        return 1

    if supervisor is None:
        return 0

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
    if args.telegram:
        # Routed through the supervisor, so a failed summary is recorded the same
        # way a failed entry notice is.
        supervisor.notify(format_run_summary(
            entries=supervisor.entries, exits=supervisor.exits,
            realized_quote=supervisor.realized,
            frames=stats.frames, trades=stats.trades,
            duplicates=stats.duplicates, protocol_errors=stats.protocol_errors,
            halted=supervisor.manager.halted, mode=notify_mode,
        ))

    for err in supervisor.notify_errors:
        print(f"  NOTIFY FAILED: {err}", file=sys.stderr)
    # A stopped loop or a position left open at the venue is not a successful
    # run, and the exit code is how a service manager and a script both see it.
    return 1 if (supervisor.stopped or supervisor.manager.position is not None) else 0


if __name__ == "__main__":
    raise SystemExit(main())
