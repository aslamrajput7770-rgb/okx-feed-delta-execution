# Backtest results

Real trade prints, the shipped signal loop, simulated fills. The harness imports
`live_feed` and drives the production `Supervisor`; nothing about the strategy is
re-implemented for the backtest. See `tools/backtest.py`.

The signal is read from **Delta India**, the same venue the order goes to, so the
signal price and the fill price are the same price and there is no basis to
charge.

The numbers in the tables below were measured on **OKX** tapes, in a cross-venue
mode that no longer exists. They are kept because the conclusion did not depend
on the venue: the signal lost money on OKX prints, and the reason it lost money -
a raw sign test on cumulative delta - is a property of the rule, not of the book
it reads. But be clear about what that means for reproducing them: the OKX
fetcher and decoder have been removed from the repository, so **the figures below
cannot be re-run today.** A Delta capture is the way forward, and the tables will
be replaced by a same-venue run once one covers a full day.

## How the data was obtained

**The OKX tapes behind these numbers (no longer reproducible).** They came from
OKX's public `history-trades` endpoint, paged backwards on a **tradeId**. The
trap, recorded here because it cost an hour of wall-clock once: `after` takes a
tradeId, not a timestamp. Passing a timestamp is silently ignored and returns the
newest page every time, which looks like it works. The fetcher and its tests are
gone with the rest of the OKX path; this is the note of what it did.

**Delta tapes.** Delta keeps no trade-print history at all. Its REST `trades`
endpoint returns the latest 50 prints and ignores `start` and `end` (tried in
seconds and in microseconds); its only historical endpoint is candles, which
carry no aggressor split and so cannot rebuild the signal; and the venue docs
scope cursor pagination to products, orders and fills. So a Delta tape has to be
**recorded while it happens** - `tools/capture.py`, or `make tape` - and every
backtest is limited to windows the process was running for.

Tapes used, all fetched 2026-10-23 (OKX prints, cross-venue mode):

| Tape | Window | Prints | Hours |
|---|---|---|---|
| `tape_1h` | last hour | 34,789 | 1.0 |
| `tape_6h` | last 6 hours | 341,933 | 6.0 |
| `tape_d1` | 22 Oct 17:43-21:43 UTC | 229,321 | 4.0 |
| `tape_d2` | 21 Oct 17:43-21:43 UTC | 321,974 | 4.0 |
| `tape_d3` | 20 Oct 17:43-21:43 UTC | 228,424 | 4.0 |
| `tape_d4` | 19 Oct 17:43-21:43 UTC | 210,037 | 4.0 |

23 hours of prints across three calendar days.

## Shipped rules, as they stand

Stop 1%, target 2%, hold 3600 s, cap 0.01 BTC, daily loss 50. Order size
0.001 BTC (one Delta contract). Costs 5 bps fee and 2 bps slippage per side.

| Tape | Trades | Win | Gross | Costs | Net | Max DD | Market |
|---|---|---|---|---|---|---|---|
| 1h | 1 | 0/1 | -0.10 | -0.11 | **-0.21** | -0.21 | -0.08% |
| 6h | 6 | 0/6 | -0.55 | -0.68 | **-1.24** | -1.24 | -0.17% |
| d1 | 4 | 1/3 | +0.05 | -0.46 | **-0.41** | -0.46 | +0.22% |
| d2 | 4 | 0/4 | -0.62 | -0.47 | **-1.09** | -1.09 | +0.20% |
| d3 | 4 | 0/4 | -0.45 | -0.46 | **-0.91** | -0.91 | +0.05% |
| d4 | 4 | 0/4 | -0.04 | -0.46 | **-0.50** | -0.50 | +0.11% |
| **all** | **23** | **1/22** | **-1.71** | **-2.65** | **-4.36** | | |

Every window is negative: 0 of 6 tapes, 23 trades, one winner, a 4.3% win rate.
Five of the six windows closed higher than they opened, and the run lost money
in all five.

## What the numbers say

**The stop and the target almost never fire.** On the 6h tape, five of six
exits are the one-hour hold limit. Price did not move 1% in either direction, so
the position is carried to the clock every time. The risk ladder is not the
active mechanism; the hold timer is.

**It is not just costs.** Across all 23 hours the run is **-1.71 gross** before
a single basis point of fee is charged, and with fee and slippage set to zero
the 6h tape still returns **-0.36**. The signal loses before any cost is
charged. That matters, because "profitable before fees" is a story you can
sometimes fix; this is not that.

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
- **No basis is charged.** These runs read the signal on OKX and filled at
  Delta, where the price differs, so they *understate* the cost by the basis.
  Every run above is at zero. A same-venue Delta run has no basis by
  construction, which is the point of reading and filling on one venue.
- **Short windows.** 23 hours across three days is not a regime. It is enough to
  see that the edge is absent in these windows, not enough to bound how bad it
  can be. Two of the three days are one-sided in the same direction.
- **One order size.** 0.001 BTC. Results scale linearly with size; nothing here
  depends on it.

## Reproducing

The tables above were produced from OKX tapes that the repository can no longer
fetch, so these are the commands that would have produced them, kept as a record
of the shape of the runs. On a Delta tape, drop the fetch step - a tape is
recorded, not downloaded:

```bash
python3 tools/capture.py --minutes 60 --out tape_1h.txt
python3 tools/backtest.py --tape tape_1h.txt
python3 tools/backtest.py --tape tape_1h.txt --grid-stop 0.25:3.0:0.25
python3 tools/backtest.py --tape tape_1h.txt --fee-bps 0 --slippage-bps 0
python3 tools/backtest.py --tape tape_1h.txt --max-hold 300
```

## The conclusion, stated plainly

This is a working piece of plumbing: the feed is handled, the risk ladder
enforces, the exits are sent. It is not a profitable strategy, and the
backtests above do not support running it with real money. The entry rule is a
one-line sign test on cumulative delta, and the data says that test has no edge
on these windows.
