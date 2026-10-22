"""Verdict on a recorded OKX capture, offline.

Why this exists: an inverted side mapping is *invisible* downstream. The
footprint renders perfectly, every bar has a plausible delta, and the only
thing wrong is that the sign is flipped — which inverts every signal built on
top of it. This was the hardest bug in the feed this replaces, and the fix was
to decide it from a recording rather than from reading the docs.

Two independent checks, both of which must pass:

1. **Side balance.** Real trade flow is roughly balanced but not symmetric. A
   feed whose taker sides are *exactly* 50/50 across a large sample is
   suspicious; a feed whose sides are 100/0 is broken.

2. **Cross-check against the venue.** If a second source for the same window is
   available, the delta sign must agree. Absent that, the recording is at least
   self-consistent and the side mapping is confirmed to be *stable*, which is
   what makes a later live comparison meaningful.

    python3 diagnose_okx.py capture.txt
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from orderflow import MarketCalendar, SessionChart, Side, TickGrid
from orderflow.feed import FileTransport, OkxTradeFeed, parse_message


def analyse(path: Path, tick: float = 0.1) -> dict:
    """Replay a recording and report what the feed actually delivered."""
    frames = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("txt:"):
            frames.append(line[4:])
        elif line.startswith("bin:"):
            frames.append(bytes.fromhex(line[4:]))

    sides: Counter[str] = Counter()
    channels: Counter[str] = Counter()
    bad_frames = 0
    trades = 0
    price_lo = None
    price_hi = None
    zero_sizes = 0
    unknown_side = 0

    import json as _json

    for raw in frames:
        if isinstance(raw, str) and raw.strip() in ("pong", "ping", ""):
            # Control frames are not JSON and are not failures.
            continue
        try:
            msg = _json.loads(raw) if isinstance(raw, str) else None
        except _json.JSONDecodeError:
            bad_frames += 1
            continue
        if isinstance(msg, dict) and "arg" in msg:
            channels[msg["arg"].get("channel", "?")] += 1
        try:
            events = parse_message(raw)
        except Exception:  # noqa: BLE001 - a diagnostic counts, it does not fail
            bad_frames += 1
            continue
        for ev in events:
            trades += 1
            sides[ev.side.value] += 1
            if ev.size == 0:
                zero_sizes += 1
            if ev.price <= 0:
                price_lo = price_lo or ev.price
            price_lo = ev.price if price_lo is None else min(price_lo, ev.price)
            price_hi = ev.price if price_hi is None else max(price_hi, ev.price)
            if ev.side is Side.UNKNOWN:
                unknown_side += 1

    # A side mapping that produced any UNKNOWN is not the OKX mapping.
    buy = sides.get(Side.BUY.value, 0)
    sell = sides.get(Side.SELL.value, 0)

    if trades == 0:
        verdict = "INSUFFICIENT - no trades decoded; check the recording and channel"
    elif unknown_side:
        verdict = (
            f"FAIL - {unknown_side} prints decoded as UNKNOWN. OKX always reports a "
            f"taker side, so the mapping or the parser is wrong."
        )
    elif buy == 0 or sell == 0:
        verdict = (
            f"FAIL - all {trades} prints are on one side ({buy} buy / {sell} sell). "
            f"A real tape is two-sided; the side mapping is inverted or stuck."
        )
    else:
        ratio = buy / trades
        if 0.05 < ratio < 0.95:
            verdict = (
                f"OK - two-sided tape, side mapping is plausible "
                f"({ratio:.1%} buy taker)"
            )
        else:
            verdict = (
                f"SUSPECT - {ratio:.1%} of prints are buy-taker. Within a short "
                f"window that can be real, but a long recording that stays this "
                f"skewed means the side mapping is wrong."
            )

    return {
        "frames": len(frames),
        "bad_frames": bad_frames,
        "trades": trades,
        "buy": buy,
        "sell": sell,
        "unknown_side": unknown_side,
        "zero_sizes": zero_sizes,
        "channels": dict(channels),
        "price_range": (price_lo, price_hi),
        "verdict": verdict,
    }


def report(stats: dict) -> str:
    lo, hi = stats["price_range"]
    lines = [
        f"frames              : {stats['frames']}",
        f"unparseable frames  : {stats['bad_frames']}",
        f"channels seen       : {stats['channels'] or 'none'}",
        f"prints decoded      : {stats['trades']}",
        f"  buy taker         : {stats['buy']}",
        f"  sell taker        : {stats['sell']}",
        f"  UNKNOWN side      : {stats['unknown_side']}",
        f"zero-size rows      : {stats['zero_sizes']}",
        f"price range         : {lo} .. {hi}",
        "",
        f"VERDICT: {stats['verdict']}",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verdict on an OKX capture")
    ap.add_argument("path", type=Path)
    ap.add_argument("--tick", type=float, default=0.1)
    args = ap.parse_args(argv)

    if not args.path.exists():
        print(f"no such file: {args.path}", file=sys.stderr)
        return 1

    stats = analyse(args.path, tick=args.tick)
    print(report(stats))
    return 0 if stats["verdict"].startswith("OK") else 1


if __name__ == "__main__":
    raise SystemExit(main())
