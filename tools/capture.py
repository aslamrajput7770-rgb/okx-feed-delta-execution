#!/usr/bin/env python3
"""Capture live trade prints to a replay tape.

Delta Exchange India keeps no trade-print history - the REST `trades` endpoint
returns the latest 50 prints and ignores `start`/`end`, and the only historical
endpoint is candles. So a tape of prints cannot be downloaded after the fact;
it has to be recorded while it happens. That is the whole reason this tool
exists, and it is why it flushes to disk on every frame: a capture that dies at
hour 23 with everything in memory has cost 23 hours.

    python3 tools/capture.py --venue delta --minutes 1440 --out tape.txt

The output is `FileTransport` format, so `tools/backtest.py` reads it directly
and `live_feed.py --replay` can re-run the session it recorded.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orderflow.feed import DELTA_PUBLIC_WS, DeltaTradeFeed, PUBLIC_WS, OkxTradeFeed  # noqa: E402

try:
    import websocket
except ImportError:  # pragma: no cover - only hit without the optional dep
    websocket = None


VENUES = {
    "delta": (DELTA_PUBLIC_WS, DeltaTradeFeed),
    "okx": (PUBLIC_WS, OkxTradeFeed),
}


class LiveTransport:
    """A real socket. The only place in this tool that opens one."""

    is_live = True

    def __init__(self, uri: str, timeout: float = 30.0) -> None:
        self.uri = uri
        self.timeout = timeout
        self._ws = None
        self.dead = False

    def connect(self) -> None:
        if websocket is None:
            raise ImportError(
                "capture needs 'websocket-client': pip install websocket-client"
            )
        self._ws = websocket.create_connection(self.uri, timeout=self.timeout)
        self.dead = False

    def send(self, data, binary: bool = False) -> None:
        if self._ws is None:
            raise RuntimeError("transport is not connected")
        self._ws.send(data)

    def heartbeat(self) -> None:
        if self._ws is not None:
            self._ws.send(json.dumps({"type": "ping"}))

    def recv(self, timeout=None):
        if self._ws is None:
            raise RuntimeError("transport is not connected")
        if timeout is not None:
            self._ws.settimeout(timeout)
        try:
            return self._ws.recv()
        except Exception as exc:
            if websocket is not None and isinstance(
                exc, websocket.WebSocketTimeoutException
            ):
                return None  # a quiet second, not a drop
            self.dead = True
            return None

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            finally:
                self._ws = None


def main() -> int:
    ap = argparse.ArgumentParser(description="capture live trade prints to a tape")
    ap.add_argument("--venue", choices=sorted(VENUES), default="delta")
    ap.add_argument("--symbol", default=None, help="default: BTCUSD on delta, BTC-USDT-SWAP on okx")
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--uri", default=None, help="override the venue socket")
    args = ap.parse_args()

    uri, feed_cls = VENUES[args.venue]
    symbol = args.symbol or ("BTCUSD" if args.venue == "delta" else "BTC-USDT-SWAP")
    uri = args.uri or uri
    seconds = args.minutes * 60.0

    stop = {"now": False}

    def on_signal(*_):
        stop["now"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    written = 0

    print(f"capturing {symbol} from {uri} for {args.minutes:g} min -> {out}")

    with out.open("w", encoding="utf-8") as fh:
        def on_frame(raw) -> None:
            nonlocal written
            fh.write("txt:" + str(raw) + "\n")
            written += 1
            if written % 100 == 0:
                fh.flush()

        feed = feed_cls([symbol], lambda: LiveTransport(uri))
        stats = feed.run(
            on_trade=lambda trade, ev: None,
            on_frame=on_frame,
            max_seconds=seconds,
            # A capture may not stop: a venue blip must reconnect, not end the
            # run, so the reconnect count is unbounded and only reported.
            max_reconnects=None,
            reconnect_after_seconds=20.0,
        )
        fh.flush()

    elapsed = time.monotonic() - started
    print(f"wrote {written} frames, {stats.trades} prints in {elapsed:.0f}s")
    print(f"  duplicates {stats.duplicates}  protocol errors {stats.protocol_errors}"
          f"  reconnects {stats.reconnects}")
    if stats.last_error:
        print(f"  last error: {stats.last_error}")
    print(f"  ended {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
