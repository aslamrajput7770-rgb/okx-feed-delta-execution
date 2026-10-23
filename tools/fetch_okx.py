#!/usr/bin/env python3
"""Fetch real OKX trade prints into a replay tape for the backtester.

Why this exists
---------------
The engine's replay path (`FileTransport`) already replays a recording exactly
as it replays a live session. What it never had was a recording that was not a
few minutes long, so a backtest was impossible. OKX's public REST endpoint
returns 100 prints per call and pages backwards by *tradeId* - not by timestamp,
which is silently ignored and returns the newest page every time. That
distinction is the whole reason this file exists: paging by timestamp looks like
it works and quietly re-fetches the same 100 trades.

It reaches back far enough to be useful. A binary search over the tradeId space
lands on a chosen date, then paging walks forward from there.

    python3 tools/fetch_okx.py --days 7 --out tape_7d.txt
    python3 tools/fetch_okx.py --hours 6 --out tape_6h.txt

Writes the same `txt:<json>` line format `FileTransport` reads, so the output
drops straight into `live_feed.py --replay`.

No credentials: trade prints are public market data.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://www.okx.com/api/v5/market/history-trades"
#: The endpoint rejects a request with no User-Agent.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; okx-backtest-fetch)"}
#: Below this the venue returns an empty page; it bounds the tradeId search.
OLDEST_TRADE_ID = 2_500_000_000


def _get(inst_id: str, after: str | None, limit: int = 100, tries: int = 5) -> list[dict]:
    """One page, newest first. Retries transient failures."""
    url = f"{BASE}?instId={inst_id}&limit={limit}"
    if after is not None:
        url += f"&after={after}"
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.load(resp)
            if payload.get("code") != "0":
                raise RuntimeError(f"venue error {payload.get('code')}: {payload.get('msg')}")
            return payload.get("data") or []
        except Exception as exc:  # noqa: BLE001 - retried below, reported if it survives
            last = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"giving up after {tries} tries: {last}")


def _ts_of(inst_id: str, trade_id: int) -> int | None:
    """Timestamp (ms) of the newest print at or before `trade_id`."""
    rows = _get(inst_id, str(trade_id))
    return int(rows[0]["ts"]) if rows else None


def find_trade_id_for(inst_id: str, target_ms: int) -> int | None:
    """Binary search the tradeId space for the first id at/after `target_ms`.

    tradeId increases with time, so this is a plain bisection. The upper bound is
    the newest id; the lower is the floor the venue stops answering below.
    """
    newest = _get(inst_id, None)
    if not newest:
        return None
    hi = int(newest[0]["tradeId"])
    if int(newest[0]["ts"]) <= target_ms:
        return hi  # the target is in the future; start at the newest print

    lo = OLDEST_TRADE_ID
    if _ts_of(inst_id, lo) is None:
        # Walk up until the venue answers, doubling, then bisect inside that.
        step = 1_000_000
        while _ts_of(inst_id, lo) is None and lo < hi:
            lo += step
            step *= 2
        if lo >= hi:
            return None

    while hi - lo > 1:
        mid = (lo + hi) // 2
        ts = _ts_of(inst_id, mid)
        if ts is None:
            lo = mid
        elif ts > target_ms:
            hi = mid
        else:
            lo = mid
    return lo


def fetch_range(inst_id: str, start_ms: int, end_ms: int, quiet: bool = False) -> list[dict]:
    """Every print with start_ms <= ts <= end_ms, oldest first.

    Paging walks *backwards* from `end_ms` (the endpoint only goes that way),
    collecting into a dict keyed on tradeId so a page overlap cannot duplicate a
    print. The result is reversed at the end.
    """
    cursor = find_trade_id_for(inst_id, end_ms)
    if cursor is None:
        raise RuntimeError("could not locate the end of the requested range")

    by_id: dict[str, dict] = {}
    oldest_seen = end_ms + 1
    pages = 0
    t0 = time.time()

    while True:
        rows = _get(inst_id, str(cursor))
        if not rows:
            break
        pages += 1
        reached_start = False
        for row in rows:
            ts = int(row["ts"])
            if ts > end_ms:
                continue
            if ts < start_ms:
                reached_start = True
                continue
            by_id[row["tradeId"]] = row
        oldest_seen = min(int(r["ts"]) for r in rows)
        cursor = int(rows[-1]["tradeId"])
        if reached_start or oldest_seen <= start_ms:
            break
        if pages % 50 == 0 and not quiet:
            mins = (end_ms - oldest_seen) / 60_000.0
            print(
                f"  ... {len(by_id)} prints, {mins:.1f} min back, "
                f"{time.time() - t0:.0f}s elapsed",
                file=sys.stderr,
            )
        time.sleep(0.1)

    out = sorted(by_id.values(), key=lambda r: (int(r["ts"]), int(r["tradeId"])))
    if not quiet:
        print(f"  {len(out)} prints over {pages} pages in {time.time() - t0:.0f}s", file=sys.stderr)
    return out


def to_tape_lines(rows: list[dict], inst_id: str, chunk: int = 20) -> list[str]:
    """Convert REST rows into OKX trades-channel frames.

    The replay path expects the websocket frame shape, not the REST shape: same
    fields, but wrapped in `{"arg": {...}, "data": [...]}` and with the size key
    renamed `sz` -> `sz` (identical) while the REST `source` field is dropped.
    Batching a few prints per frame keeps the frame count near what a live socket
    would deliver.
    """
    lines: list[str] = []
    for i in range(0, len(rows), chunk):
        batch = [
            {
                "instId": r.get("instId", inst_id),
                "tradeId": r["tradeId"],
                "px": r["px"],
                "sz": r["sz"],
                "side": r["side"],
                "ts": r["ts"],
            }
            for r in rows[i : i + chunk]
        ]
        lines.append("txt:" + json.dumps({"arg": {"channel": "trades", "instId": inst_id}, "data": batch}))
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch OKX trade prints for backtesting.")
    ap.add_argument("--inst", default="BTC-USDT-SWAP")
    ap.add_argument("--days", type=float, default=0.0)
    ap.add_argument("--hours", type=float, default=0.0)
    ap.add_argument("--start", default=None, help="ISO8601 UTC, overrides --days/--hours")
    ap.add_argument("--end", default=None, help="ISO8601 UTC, defaults to now")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    now_ms = int(time.time() * 1000)
    if args.start:
        from datetime import datetime, timezone

        start_ms = int(datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc).timestamp() * 1000)
    else:
        span_ms = int((args.days * 86400 + args.hours * 3600) * 1000)
        if span_ms <= 0:
            print("give --days, --hours or --start", file=sys.stderr)
            return 2
        start_ms = now_ms - span_ms
    if args.end:
        from datetime import datetime, timezone

        end_ms = int(datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc).timestamp() * 1000)
    else:
        end_ms = now_ms

    if start_ms >= end_ms:
        print("start must be before end", file=sys.stderr)
        return 2

    if not args.quiet:
        print(
            f"fetching {args.inst} from {start_ms} to {end_ms} "
            f"({(end_ms - start_ms) / 3600000:.2f} hours)",
            file=sys.stderr,
        )
    rows = fetch_range(args.inst, start_ms, end_ms, quiet=args.quiet)
    if not rows:
        print("no prints returned", file=sys.stderr)
        return 1

    lines = to_tape_lines(rows, args.inst)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    span_min = (int(rows[-1]["ts"]) - int(rows[0]["ts"])) / 60_000.0
    print(
        f"wrote {len(rows)} prints ({span_min:.1f} min) to {args.out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
