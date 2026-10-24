# Trade-print feed -> Delta Exchange India execution

Order-flow analytics driven by **perpetual trade flow**, with orders placed on
**Delta Exchange India**. The signal is read from Delta too - the same venue the
order lands on - so the signal price and the fill price are the same price, and
there is no second book to reconcile against.

## Quickstart

On a machine with nothing but a shell and this repository:

    ./bootstrap.sh

That finds or installs a Python 3.10+ interpreter, creates `.venv`, installs the
dependencies, runs the offline suite (309 tests), and then runs the read-only
venue preflight. It sends no orders.

Then:

    ./deploy/run_paper.sh 5        # live feed, reports the order, sends nothing
    make help                      # the same entry points, as make targets

To watch the signals on a phone instead of a terminal, create a bot with
@BotFather and run:

    export TELEGRAM_BOT_TOKEN=...  TELEGRAM_CHAT_ID=...
    python3 telegram_check.py                       # proves the bot can post
    ./deploy/run_paper_telegram.sh 5                # live feed, signals to Telegram, no orders

To place real orders, set a size and use the other runner:

    DELTA_ORDER_SIZE=0.001 ./deploy/run_live.sh 5

`run_paper.sh` never passes `--execute`, and `run_live.sh` refuses to start
without an explicit size, so enabling trading is a deliberate act rather than a
flag appended to the command already in your shell history.

Credentials come from the environment (`DELTA_API_KEY`, `DELTA_API_SECRET`) or
from `config/delta.env`. Never pass them as command-line arguments; arguments
are visible in `ps`.

`AGENTS.md` carries the operational detail: where it runs, the invariants that
have silent failure modes, the venue behaviour that was measured rather than
assumed, and the known gaps. Read it before changing anything.

---

## Read this first

**One venue: signal and execution both on Delta India.** The order goes to
Delta, and the signal is read there too. Signal price and fill price are the
same price, so there is no basis to pay and a backtest measures the strategy
rather than the spread. Delta is the only feed; the OKX path was removed.

The trade-off that comes with that, stated plainly: Delta publishes tens of
prints a minute, so the footprint is coarse. That is accepted, because reading a
busier book elsewhere would mean acting on one book while filling on another,
and that is a different trade with a cost that never appears in a
signal-quality number.

| | Delta India `BTCUSD` |
|---|---|
| Type | perpetual |
| Tick | 0.5 (production) |
| 1 contract | **0.001 BTC** |
| Settles in | **USD** |
| Product id | 27 on production, **84 on testnet** |

Two consequences that matter more than any line of code:

1. **Sizing.** One contract is not one coin. `orderflow/execution/sizing.py`
   converts exposure through the instrument's `contract_value`. Sending a size
   number as if it were coins is out by 1000x and raises nothing.
2. **Tax and liquidity.** Delta India carries 30% tax and 1% TDS on crypto, and
   a thinner book than the global venues. A signal that is profitable on paper
   can be unprofitable after the spread, the tax, and the slippage.

## Risk limits

The loop holds one position at a time and closes it on a rule, never on hope.
Every limit is a CLI flag; `0` disables that rule explicitly.

| Rule | Flag | Default |
|---|---|---|
| Stop loss | `--stop-loss-pct` | 1% |
| Take profit | `--take-profit-pct` | 2% |
| Position cap | `--max-position` | 0.01 BTC |
| Daily loss limit | `--max-daily-loss` | 50 quote |
| Max hold | `--max-hold-seconds` | 3600 |
| Order-failure breaker | `--max-order-failures` | 3 |

Exits are sent `reduce_only`, so an exit can never open a new position. The
daily counter is written to `state/daily_pnl.json` and reloaded on the next run:
a limit that resets when the process restarts is not a limit, because the
losing loop is the thing that restarts the process. A file from a previous day
is discarded rather than carried forward.

The breaker exists because a bad key or an unwhitelisted IP fails every order
the same way. Found by running it: with a testnet-invalid key the loop fired 18
signed requests in 30 seconds, one per trade, none of which could ever succeed.
It now stops after 3 consecutive failures. A success resets the count, so one
transient blip does not end the run, and the breaker blocks new entries only:
an open position keeps getting exit attempts, because abandoning a live
position is worse than a repeated failed request.

```bash
# 0.5% stop, 1% target, 0.002 BTC cap, stop for the day after 25 quote of loss
python3 live_feed.py --minutes 60 --execute --order-size 0.002 \
    --stop-loss-pct 0.5 --take-profit-pct 1.0 --max-position 0.002 \
    --max-daily-loss 25
```

## Layout

```
orderflow/
  core.py  ticks.py  footprint.py  profile.py  delta.py   # engine, unchanged
  tape.py  dom.py  indicators.py  render.py
  instruments.py     # Delta contract spec and contract-size conversion
  session.py         # 24/7 crypto calendar, resets at 00:00 UTC
  session_chart.py   # daily reset for cumulative delta and the profile
  feed/base.py       # transport, dedup, reconnect, the run loop
  feed/delta.py      # Delta India ingestion, buyer role -> aggressor side
  execution/sizing.py       # contract arithmetic for Delta
  execution/delta_client.py # signed REST orders, dry-run and testnet default
tools/capture.py   # record live prints to a backtest tape
tools/backtest.py  # replay a tape through the shipped rules
live_feed.py       # live runner
```

## Quick start

```bash
pip install -r requirements.txt
python3 -m pytest tests -q          # 309 tests, no network

# Live Delta market data, no orders, no credentials needed
python3 live_feed.py --minutes 1 --record capture.txt

# Replay offline
python3 live_feed.py --replay capture.txt

# Backtest: record a day, then replay it through the shipped rules
make tape                           # 1440 minutes, live, no orders
python3 tools/backtest.py --tape tape.txt
```

## Trading

Orders are off unless asked for, and go to testnet unless asked twice.

```bash
export DELTA_API_KEY=...        # testnet keys first
export DELTA_API_SECRET=...

python3 live_feed.py --dry-run                          # offline, synthetic tape
python3 live_feed.py --execute --order-size 0.001       # testnet order
python3 live_feed.py --execute --order-size 0.001 --live --i-know-this-is-live
```

`--live` and `--execute` are separate on purpose: both must be present before a
mainnet order can leave the process, and a third flag confirms it. The
confirmation lives in the program rather than only in `run_live.sh`, because a
shell script cannot be the only thing standing between a typo and a real order.

A live run also needs `--minutes > 0`. An unbounded socket never returns, so the
old default of `0` made a bare `python3 live_feed.py` hang forever. Use
`--dry-run` for an offline check: it replays a synthetic tape and opens no
socket at all.

## Two things that were silent failures upstream

Both are pinned by tests, and both are worth understanding before changing the
feed.

**1. The aggressor side must not be inverted.** Delta reports the side in a
vocabulary that is easy to read backwards.

Delta reports the **buyer's role**: `r="t"` means the buyer was the taker, so the
aggressor bought; `r="m"` means the buyer was a maker, so the aggressor sold. It
is the buyer's role, not the taker's side, and reading it as the taker's side
inverts every print.

The inversion produces a footprint that renders perfectly with a plausible delta
on every bar — and the sign of every signal is wrong.
`tests/test_delta_protocol.py` asserts that a buy print makes bar delta positive,
which is the assertion that catches it.

**2. Deduplication is on `tradeId`.** A reconnect replays trades already seen.
Without dedup, volume is double-counted at the reconnect boundary and nothing
in the output shows it.

## Credentials

Read from the environment, never from a file argument, never committed:

```
DELTA_API_KEY
DELTA_API_SECRET
```

`DeltaCredentials.__repr__` masks the secret, because a secret that reaches a
log line has leaked. `DeltaCredentials.from_env()` is the only way to build
them: there is no constructor argument for a secret, so a caller cannot pass one
in from a config file or a chat message.

## The signature

Delta signs `method + timestamp + path + query_string + body` with HMAC-SHA256
and sends `api-key`, `signature` and `timestamp` headers. The signature is valid
for **5 seconds**, so it is generated immediately before each send and never
cached. `DeltaOrderClient.check_clock()` refuses to sign when the local clock is
more than 2 seconds from the venue's, because a skewed clock rejects every order
with an error that reads like an auth failure.

> **The venue clock is only advisory.** Delta has no `/v2/time` endpoint (both
> hosts return 404), so the clock is read from the HTTP `Date` header. The live
> host is behind CloudFront, and a cache hit serves the `Date` of the *cached*
> response, so that reading can be stale by minutes. Sync with NTP and treat the
> venue's `signature_expired` rejection as the authoritative signal.

> **Unverified against the venue.** Delta's own documentation example signature
> does not reproduce from the algorithm it documents (checked). The algorithm
> implemented here is the one described in prose and used by the official
> client. It is pinned by a test against a locally computed vector, but it has
> **not** been confirmed against the live venue. Run
> `DeltaOrderClient.verify_signature_against_venue()` on **testnet** before
> trusting it with a real order.

## Product ids are not portable

Found by `preflight_delta.py` while building this, and worth stating plainly:

```
PRODUCTION  id 27 -> BTCUSD        perpetual, tick 0.5, 1 contract = 0.001 BTC
TESTNET     id 27 -> BTCUSD_27Mar  EXPIRED future, tick 0.5, 1 contract = 1 BTC
TESTNET     id 84 -> BTCUSD        perpetual, tick 0.1, 1 contract = 0.001 BTC
```

The testnet contract on id 27 is an expired March future with a contract value a
**thousand times** the perpetual. Code that hardcodes `product_id=27` and is
pointed at testnet signs a perfectly valid order for 1000x the intended size,
and nothing in the response says so.

So the execution leg is resolved **by symbol** at runtime, per host, and the
returned spec is checked before any order is built. `contract_value` and `state`
mismatches stop the run; a `tick_size` difference is reported but allowed,
because it moves the price grid without changing the position size.

```bash
python3 preflight_delta.py          # testnet keys; read-only, places no order
```

## What is deliberately absent

- **No geo-block bypass.** Binance blocks this environment with HTTP 451 from
  the US as well as from India, so relocating to another region does not help.
  Bypassing a venue's geographic restrictions is not something this project
  does.
- **No binary decoder.** Delta sends JSON, so the Angel One
  `unpack`/`price_scale` layer is gone rather than ported.
- **No side inference.** The quote rule and tick rule are deleted. Delta states
  the side, so the old guessing path — and the `Side.UNKNOWN` bucket it produced
  — no longer exists on this feed.

## Licence

MIT, inherited from the original repository.

## Running it

Paper mode takes the signal feed and reports the order it would send, without
sending one:

    ./deploy/run_paper.sh 5          # five minutes, live host, no orders

Output:

    venue      : DELTA wss://public-socket.india.delta.exchange
    execution  : Delta live (DRY RUN, no orders)
    frames=4271 trades=4238 duplicates=0 control=33 errors=0
    bars=11 volume=17288.7 cum_delta=5119.94
    execution leg: id 27 BTCUSD tick 0.5 1 contract = 0.001 base
    signal       : cumulative delta 5119.94 -> buy
    order size   : DELTA:BTCUSD 1 contracts = 0.001 base
    dry run      : would send buy 1 contract(s) of product_id=27. No request sent.

The product id is resolved from the venue on every run, never assumed. On
production id 27 is the BTCUSD perpetual; on testnet id 27 is an expired future
with a 1000x larger contract value, which is why the resolver exists.

Placing real orders is a different script, and it refuses to start without an
explicit size:

    DELTA_ORDER_SIZE=0.001 ./deploy/run_live.sh 5

The separation is deliberate: enabling trading should take a distinct command
rather than a flag appended to the one already in your shell history.

### Running as a service

On the host that holds the whitelisted IP, install it as a systemd unit:

    sudo ./deploy/aws_setup.sh        # installs, does not start, does not go live
    sudo systemctl start delta-feed-execution
    journalctl -u delta-feed-execution -f

The unit file and the mode switch are both in this repository, so the deployed
code and the deployed configuration cannot drift: the clone is the deployment.
`SERVICE_MODE=paper` is the installed default; going live takes three explicit
edits in `/etc/delta-feed-execution/service.env` (`SERVICE_MODE=live` plus
`DELTA_ORDER_SIZE` and `I_KNOW_THIS_IS_LIVE=1`).

It runs bounded windows and restarts itself, so the per-day loss counter resets
at midnight for free. `systemctl stop` closes any open position and saves that
counter before exiting: SIGTERM is handled as a request to shut down cleanly,
not as a kill.

### Verifying that nothing was sent

The account's own history is the check that matters:

    filled orders on this account: 0
    open positions: 0

`preflight_delta.py --live` performs the read-only checks, and the runner never
passes `--execute`.
