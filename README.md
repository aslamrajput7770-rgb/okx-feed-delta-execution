# OKX feed → Delta Exchange India execution

Order-flow analytics driven by **OKX perpetual trade flow**, with orders placed
on **Delta Exchange India** (`BTCUSD`, product id `27`).

This is a port of `morning-order-flow-real-engine-compete`. The analytics engine
is unchanged; the market-data feed and the session calendar are replaced.

## Read this first

**This is not arbitrage.** OKX is the *signal* venue, Delta India is the
*execution* venue. They are two different contracts:

| | OKX `BTC-USDT-SWAP` | Delta India `BTCUSD` |
|---|---|---|
| Type | perpetual | perpetual |
| Tick | 0.1 | 0.5 (production) |
| 1 contract | **0.01 BTC** | **0.001 BTC** |
| Settles in | **USDT** | **USD** |
| Product id | `BTC-USDT-SWAP` | 27 on production, **84 on testnet** |

They are quoted in different currencies, on different order books, and the
price difference (the *basis*) moves. Acting on an OKX signal at Delta is a
**directional position carrying basis risk**, not a hedged trade. There is no
hedging leg here, because a hedge needs both venues and this account only
trades one.

Two consequences that matter more than any line of code:

1. **Sizing.** One contract is not one coin. `orderflow/execution/sizing.py`
   converts exposure through each venue's `contract_value`. Copying a size
   number between venues is out by 10x and raises nothing.
2. **Tax and liquidity.** Delta India carries 30% tax and 1% TDS on crypto, and
   thinner books than OKX. A signal that is profitable on paper can be
   unprofitable after the spread, the tax, and the slippage.

## Layout

```
orderflow/
  core.py  ticks.py  footprint.py  profile.py  delta.py   # engine, unchanged
  tape.py  dom.py  indicators.py  render.py
  instruments.py     # OKX and Delta contract specs, cross-venue conversion
  session.py         # 24/7 crypto calendar, resets at 00:00 UTC
  session_chart.py   # daily reset for cumulative delta and the profile
  feed/okx.py        # JSON WebSocket ingestion, taker side, dedup on tradeId
  execution/sizing.py       # contract arithmetic between the two venues
  execution/delta_client.py # signed REST orders, dry-run and testnet default
live_feed.py       # live runner
diagnose_okx.py    # verdict on a recorded capture
```

## Quick start

```bash
pip install -r requirements.txt
python3 -m pytest tests -q          # 143 tests, no network

# Live OKX market data, no orders, no credentials needed
python3 live_feed.py --minutes 1 --record capture.txt

# Check the recording: confirms the side mapping is not inverted
python3 diagnose_okx.py capture.txt

# Replay offline
python3 live_feed.py --replay capture.txt
```

## Trading

Orders are off unless asked for, and go to testnet unless asked twice.

```bash
export DELTA_API_KEY=...        # testnet keys first
export DELTA_API_SECRET=...

python3 live_feed.py --execute --order-size 0.001    # testnet order
python3 live_feed.py --execute --order-size 0.001 --live   # mainnet
```

`--live` and `--execute` are separate on purpose: both must be present before a
mainnet order can leave the process.

## Two things that were silent failures upstream

Both are pinned by tests, and both are worth understanding before changing the
feed.

**1. The taker side must not be inverted.** OKX reports the taker side of each
print. `buy` means the taker bought, so the offer was lifted and volume belongs
on the ask. An inverted mapping produces a footprint that renders perfectly with
a plausible delta on every bar — and the sign of every signal is wrong.
`tests/test_okx_protocol.py::TestTakerSideMapping` asserts that a buy print makes
bar delta positive, which is the assertion that catches it.
`diagnose_okx.py` re-checks it against a recording.

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
  OKX and Coinbase are reachable and are used instead. Bypassing a venue's
  geographic restrictions is not something this project does.
- **No binary decoder.** OKX sends JSON, so the Angel One `unpack`/`price_scale`
  layer is gone rather than ported.
- **No side inference.** The quote rule and tick rule are deleted. OKX states
  the side, so the old guessing path — and the `Side.UNKNOWN` bucket it produced
  — no longer exists on this feed.

## Licence

MIT, inherited from the original repository.
