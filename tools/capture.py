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
import fcntl
import json
import re
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


TRADE_TIME = re.compile(rb'"?t"?\s*:\s*(\d{10,})')


def _tape_seconds(path: Path) -> float:
    """How much wall-clock time a tape already covers, in seconds.

    Read from the venue's own timestamps rather than the line count, because a
    tape that lost time to a disconnect still covers what it covers. The first
    and last prints are enough - frames are appended in order, and `load_tape`
    sorts anyway.
    """
    first = last = None
    with path.open("rb") as fh:
        for line in fh:
            m = TRADE_TIME.search(line)
            if m:
                ts = int(m.group(1)) / 1_000_000.0  # venue times are microseconds
                if first is None:
                    first = ts
                last = ts
    if first is None or last is None:
        return 0.0
    return max(0.0, last - first)


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="capture live trade prints to a tape")
    ap.add_argument("--venue", choices=sorted(VENUES), default="delta")
    ap.add_argument("--symbol", default=None, help="default: BTCUSD on delta, BTC-USDT-SWAP on okx")
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--uri", default=None, help="override the venue socket")
    ap.add_argument("--no-resume", action="store_true",
                    help="start a fresh tape even if --out already has one")
    args = ap.parse_args(argv)

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

    # One writer per tape. The file is opened "w", so a second capture truncates
    # the first one's work and then interleaves with it - the two together
    # produce a tape whose prints are neither complete nor in order, and nothing
    # in the output says so. Taking an exclusive lock before the truncate turns
    # that into a refusal. A 24-hour recording is far too expensive to lose to a
    # second invocation.
    lock_path = out.with_suffix(out.suffix + ".lock")
    lock_fh = lock_path.open("w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"another capture is already writing {out} "
              f"(lock {lock_path}). Refusing to truncate it.", file=sys.stderr)
        return 1

    # Resume rather than truncate when the target already holds a tape.
    #
    # A 24-hour capture is only as durable as the machine running it, and a
    # container restart costs the whole recording - Delta keeps no history, so
    # there is nothing to re-fetch. The tape is append-only and `load_tape`
    # deduplicates on trade id, so appending is safe even across a gap or an
    # overlap. Truncating by default would throw away a day of prints to start a
    # second one, which is never the intent.
    resumed = 0
    if out.exists() and not args.no_resume:
        resumed = sum(1 for line in out.open(encoding="utf-8") if line.strip())

    if resumed:
        # Record the rest of the window, not another full one. `--minutes` means
        # "cover this much", and a resume that ignores what is already on disk
        # silently produces a tape twice as long as asked for.
        covered = _tape_seconds(out)
        if covered >= seconds:
            print(f"{out} already covers {covered / 60:,.1f} min "
                  f"(>= {args.minutes:g}); nothing to record.")
            return 0
        seconds -= covered
        print(f"resuming {out}: {resumed:,} frames, {covered / 60:,.1f} min covered, "
              f"{seconds / 60:,.1f} min to go")

    print(f"capturing {symbol} from {uri} for {seconds / 60:g} min -> {out}")

    mode = "a" if resumed else "w"

    with out.open(mode, encoding="utf-8") as fh:
        def on_frame(raw) -> None:
            nonlocal written
            fh.write("txt:" + str(raw) + "\n")
            written += 1
            # Every frame, not every hundred. This is a page-cache write, not an
            # fsync, so it is cheap; and the module docstring promises it, which
            # matters because the promise is the reason the tool exists. A
            # batched flush loses the last 100 frames on a crash - about two
            # minutes here, and far more on a busier venue.
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

    fcntl.flock(lock_fh, fcntl.LOCK_UN)
    lock_fh.close()

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
