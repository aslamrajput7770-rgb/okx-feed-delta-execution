# Backtest results

Real OKX trade prints, the shipped signal loop, simulated fills. The harness
imports `live_feed` and drives the production `Supervisor`; nothing about the
strategy is re-implemented for the backtest. See `tools/backtest.py`.

## How the data was obtained

`tools/fetch_okx.py` pages OKX's public `history-trades` endpoint. The one thing
worth knowing: `after` takes a **tradeId**, not a timestamp. Passing a timestamp
is silently ignored and returns the newest page every time, which looks like it
works. Paging on the tradeId walks backwards correctly, and a binary search over
the id space reaches a chosen date - 7 days back was verified.

Tapes used, all fetched 2026-10-23:

| Tape | Window | Prints | Hours |
|---|---|---|---|
| `tape_1h` | last hour | 34,789 | 1.0 |
| `tape_6h` | last 6 hours | 341,933 | 6.0 |
| `tape_d1` | 22 Oct 17:43-21:43 UTC | 229,321 | 4.0 |
| `tape_d2` | 21 Oct 17:43-21:43 UTC | 321,974 | 4.0 |

## Shipped rules, as they stand

Stop 1%, target 2%, hold 3600 s, cap 0.01 BTC, daily loss 50. Order size
0.001 BTC (one Delta contract). Costs 5 bps fee and 2 bps slippage per side.

| Tape | Trades | Win | Gross | Costs | Net | Max DD | Exits |
|---|---|---|---|---|---|---|---|
| 1h | 1 | 0/1 | -0.08 | -0.11 | **-0.20** | -0.20 | end_of_run 1 |
| 6h | 6 | 0/6 | -0.55 | -0.68 | **-1.24** | -1.24 | max_hold 5, end 1 |
| d1 | 4 | 1/3 | +0.05 | -0.46 | **-0.41** | -0.46 | max_hold 3, end 1 |
| d2 | 4 | 0/4 | -0.62 | -0.47 | **-1.09** | -1.09 | max_hold 3, end 1 |

Every window is negative. Four tapes across two days, and the sign does not
change.

## What the numbers say

**The stop and the target almost never fire.** On the 6h tape, five of six
exits are the one-hour hold limit. Price did not move 1% in either direction, so
the position is carried to the clock every time. The risk ladder is not the
active mechanism; the hold timer is.

**It is not just costs.** With fee and slippage set to zero the 6h tape still
returns **-0.36** gross. The signal loses before any cost is charged. That
matters, because "profitable before fees" is a story you can sometimes fix;
this is not that.

**A tighter stop makes it worse.** Sweeping the 6h tape, stop 0.25%/target
0.50% gives **-2.11** over 9 trades, while 0.5% and wider all sit at **-1.24**
over 6. Smaller stops cut the same trades into more pieces and pay more in
costs. Nothing in the sweep is positive.

**Hold length is a monotone lever.** On the 6h tape: 5 min -> -11.15 (72
trades), 15 min -> -3.73, 30 min -> -2.12, 60 min -> -1.24, 120 min -> -0.80.
The damage tracks the trade count almost exactly, which is what a negative
expectancy per trade looks like. Extending the hold does not create an edge; it
just takes fewer losing bets.

**The direction is mostly wrong.** On d1 price rose +0.22% and the run lost
0.41. Cumulative delta pointed one way and price went the other. This is the
known weakness of a raw cumulative-delta sign as an entry: it is a measure of
who was aggressive, not of where price is going next.

## Honest limits of this backtest

- **Fills are simulated.** A print at price P is treated as fillable at P plus
  slippage. Real fills queue, and a 30-second bar's worth of prints is not the
  same as resting liquidity.
- **No basis is charged by default.** In production the signal is read on OKX
  and the order goes to Delta, where the price differs. The `--basis-bps` flag
  charges it on entry; every run above is at zero, so the real result is *worse*
  than shown, not better.
- **Short windows.** 15 hours of tape is not a regime. It is enough to see that
  the edge is absent in these windows, not enough to bound how bad it can be.
- **One order size.** 0.001 BTC. Results scale linearly with size; nothing here
  depends on it.

## Reproducing

```bash
python3 tools/fetch_okx.py --hours 6 --out tape_6h.txt
python3 tools/backtest.py --tape tape_6h.txt
python3 tools/backtest.py --tape tape_6h.txt --grid-stop 0.25:3.0:0.25
python3 tools/backtest.py --tape tape_6h.txt --fee-bps 0 --slippage-bps 0
python3 tools/backtest.py --tape tape_6h.txt --max-hold 300
```

## The conclusion, stated plainly

This is a working piece of plumbing: the feed is handled, the risk ladder
enforces, the exits are sent. It is not a profitable strategy, and the
backtests above do not support running it with real money. The entry rule is a
one-line sign test on cumulative delta, and the data says that test has no edge
on these windows.
