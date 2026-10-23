"""Live OKX feed to Delta India execution.

    python3 live_feed.py --dry-run                    # no socket, no order
    python3 live_feed.py --minutes 1 --record cap.txt # capture, no orders
    python3 live_feed.py --minutes 1 --execute        # real orders (testnet)

Safety: the run is a **dry run unless `--execute` is passed**, and execution
goes to **testnet unless `--live` is also passed**. Two flags must both be
present before a mainnet order can leave the process.

Credentials come from the environment and are never read from a file argument:

    DELTA_API_KEY  DELTA_API_SECRET

Note on what this program is
----------------------------
OKX is the *signal* venue and Delta India is the *execution* venue. These are
two different contracts, quoted in two different currencies (USDT and USD), on
two different order books, with a basis between them that moves. Acting on an
OKX signal at Delta is therefore a **directional** position carrying basis risk,
not a hedged arbitrage. The `--basis-warn` guard refuses to trade when the two
venues are far enough apart that the basis dominates the signal.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from orderflow import DELTA_BTCUSD, OKX_BTC_USDT_SWAP, MarketCalendar, SessionChart
from orderflow.execution import (
    BASE_URLS,
    DELTA_PERP_RULES,
    DeltaCredentials,
    DeltaOrderClient,
    Environment,
    ProductResolutionError,
    contracts_for_base,
    resolve_perpetual,
)
from orderflow.feed import FileTransport, OkxTradeFeed, ReplayTransport, record_frames
from orderflow.feed.okx import PUBLIC_WS

DEFAULT_INSTRUMENTS = ["BTC-USDT-SWAP"]


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OKX feed -> Delta India execution")
    ap.add_argument("--instruments", nargs="+", default=DEFAULT_INSTRUMENTS)
    ap.add_argument("--minutes", type=float, default=0, help="0 means unbounded")
    ap.add_argument("--bar-seconds", type=float, default=60.0)
    ap.add_argument("--record", type=Path, default=None)
    ap.add_argument("--replay", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true", help="the default; stated for clarity")
    ap.add_argument("--execute", action="store_true", help="place orders")
    ap.add_argument("--live", action="store_true", help="mainnet, not testnet")
    ap.add_argument("--order-size", type=float, default=0.0,
                    help="base-currency exposure per signal; with no --execute this "
                         "only reports the order it would send")
    ap.add_argument("--basis-warn", type=float, default=0.5,
                    help="refuse to trade when |basis| exceeds this percent")
    args = ap.parse_args(argv)

    if args.execute and args.order_size <= 0:
        print("--execute needs a positive --order-size", file=sys.stderr)
        return 2

    environment = Environment.LIVE if args.live else Environment.TESTNET
    allow_live = bool(args.execute)

    client = DeltaOrderClient(environment=environment, allow_live=allow_live,
                              dry_run=not args.execute)
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

        order = contracts_for_base(exec_instrument, args.order_size, DELTA_PERP_RULES)
        side = "buy" if chart.cumulative_delta > 0 else "sell"
        print(f"signal       : cumulative delta {chart.cumulative_delta:g} -> {side}")
        print(f"order size   : {order}")

        if not args.execute:
            print(f"dry run      : would send {side} {int(order.contracts)} contract(s) "
                  f"of product_id={product.venue_id}. No request sent.")
            return 0

        side_fn = client.buy if chart.cumulative_delta > 0 else client.sell
        result = side_fn(product_id=product.venue_id, size=int(order.contracts))
        print(f"order: sent={result.sent} ok={result.ok} "
              f"status={result.status_code} error={result.error}")

    return 0




if __name__ == "__main__":
    raise SystemExit(main())
