# AGENTS.md

Working notes for this repository. Read before changing anything.

## What this is

Order-flow analytics on **OKX** perpetual trade flow, orders on **Delta
Exchange India**. Ported from `morning-order-flow-real-engine-compete`, which
used Angel One SmartStream for both.

The engine (`core`, `ticks`, `footprint`, `profile`, `delta`, `tape`, `dom`,
`indicators`, `render`) is copied verbatim and its tests pass unmodified. Do not
edit those files: `tests/test_engine.py` is the contract.

## Commands

```bash
python3 -m pytest tests -q                  # 143 tests, no network, ~0.2s
python3 live_feed.py --minutes 1 --record c.txt   # live OKX, no orders
python3 diagnose_okx.py c.txt               # side-mapping verdict
python3 live_feed.py --replay c.txt         # offline replay
python3 preflight_delta.py                  # testnet key check, read-only
```

`diagnose_okx.py` exits non-zero when the verdict is not `OK`. It is a check,
not a report.

## Invariants

These are load-bearing. Each one has a silent failure mode if broken.

**The taker side is never inferred.** OKX reports it. `buy` = taker bought =
volume on the ask = `Side.BUY`. Inverting this inverts every signal while
leaving the footprint looking correct.
`tests/test_okx_protocol.py::TestTakerSideMapping` catches it.

**Dedup is on `(instId, tradeId)`.** Not `tradeId` alone: ids are only unique
within an instrument. A reconnect replays trades and double-counts volume.

**`is_control` is a positive test.** A trade frame is a dict with a `data` list.
The old feed returned True for every non-binary frame, which on a JSON feed
drops every tick. Do not "fix" it by inverting the test.

**A size is never a bare number.** `SizedOrder` carries the instrument. OKX
contracts are 0.01 BTC, Delta contracts are 0.001 BTC. Use
`contracts_for_base()`, never arithmetic on a size from the other venue.

**Rounding is always down.** An over-sized leg is worse than an under-sized one.
`_floor_to_step` uses `Decimal`: `0.1 // 0.01` is 9.0 on floats.

**The execution leg is resolved by symbol, never by a hardcoded product id.**
Product ids are not portable between environments: production carries the BTCUSD
perpetual on id 27, testnet carries an expired March future on id 27 (contract
value 1 BTC, i.e. 1000x) and the perpetual on id 84. Use `resolve_perpetual()`
and let the venue's own spec build the `Instrument`.

**A `contract_value` mismatch is fatal; a `tick_size` mismatch is not.**
`contract_value` multiplies the order size, so a wrong one is a silent
thousand-fold error. `tick_size` only moves the price grid. See
`sizing_problems()` vs `grid_problems()`.

**Credentials come from the environment.** There is no constructor argument for
the API secret, and there must not be. `DELTA_API_KEY` / `DELTA_API_SECRET`
only. Never commit them, never pass them on a command line.

**Live orders need two flags.** `dry_run=False` and `allow_live=True`, and
`Environment.LIVE` on top. Testnet is the default environment; dry run is the
default mode.

**The session resets at 00:00 UTC**, which is 05:30 IST. Not IST midnight:
global funding, daily candles and venue daily volume all roll on 00:00 UTC, and
resetting elsewhere puts cumulative delta out of step with every chart the user
compares against.

**`BASE_UNITS = 10**8` in `ticks.py` is not to be touched.** Multi-decimal
crypto prices depend on it.

## Known gaps

**The Delta signature is not verified against the venue.** Delta's documented
example signature does not reproduce from the documented algorithm (verified
by hand: expected `ad767fead0bdbe91...`, computed `4e38dda3e6477092...`). The
implementation follows the prose description. Confirm it with
`verify_signature_against_venue()` on testnet before placing a real order.

**The venue clock is only advisory.** Delta publishes no `/v2/time` endpoint
(both hosts 404), so `server_time()` reads the HTTP `Date` header. The live host
is behind CloudFront, and a cache hit serves the `Date` of the *cached*
response, so the reading can be minutes stale. `check_clock()` is therefore a
sanity check, not a guarantee. Sync with NTP and treat the venue's
`signature_expired` rejection as the authoritative signal.

**No hedging leg.** OKX is a signal source only; nothing is traded there. The
position is directional and carries basis risk. The docs and the README say so
and should keep saying so.

**No persistence.** Sessions are in memory. A restart loses the day's profile.

**`live_feed.py` has no reconnection logic.** The transport returns `None` on a
socket error and the run ends. `FeedStats.reconnects` exists and is never
incremented.

## Things not to do

- Do not add a Binance feed. Binance returns HTTP 451 from this environment
  (verified from a US egress IP, so it is not an India-only block). Relocating
  the process does not help.
- Do not add geo-restriction bypass logic of any kind.
- Do not add an `api_secret` constructor argument "for convenience".
- Do not default `dry_run` to False.
- Do not make `diagnose_okx.py` exit 0 on a `SUSPECT` or `FAIL` verdict.

## Delta India: verified against the live venue (2026-10-22)

Checked on the real host from a whitelisted IP, not assumed. Each of these cost
a debugging cycle, so do not re-derive them.

**Product ids are per environment.** Production carries the BTCUSD perpetual on
id 27; testnet carries it on id 84 and puts an expired March future on 27 whose
contract value is 1000x larger. Never hardcode an id; call `resolve_perpetual()`.

**The clock comes from the `Date` response header.** `/v2/time` 404s on both
hosts. Two traps: `HEAD` returns 404 on the live host (use a one-byte `Range`
GET, which returns 206), and `/v2/products` is CDN-cached for 60s so a cache hit
replays a stale `Date` (measured: `Age: 39` produced a 40s phantom skew). A
unique query parameter forces an origin response.

**401 codes are distinguishable, and the difference matters.**

    ip_not_whitelisted_for_api_key  the key is valid, the calling IP is not
                                    whitelisted. The error carries `client_ip`.
    invalid_api_key                 no such key
    Signature Mismatch              key and IP are fine; the secret is wrong.
                                    The error carries `signature_data`, the exact
                                    string the venue expected, which is the
                                    quickest way to confirm the payload format.

**Signing payload format is confirmed correct by the venue itself.** For
`GET /v2/profile` the venue reported `signature_data` as
`GET<timestamp>/v2/profile`, which is exactly what `signature_payload()` builds
(method + timestamp + path + query + body). No query string, no leading slash
issues.

**A key with Trading permission requires a whitelisted IP.** Read-only keys are
usable for preflight without Trading.

**Python 3.10 or newer is required.** `dataclass(slots=True)` is a syntax error
on 3.9, which is what Amazon Linux 2023 ships as `python3`.
