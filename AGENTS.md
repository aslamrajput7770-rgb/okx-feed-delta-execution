# AGENTS.md

Read this first. It is written so that a fresh session, on a fresh machine,
needs no briefing: everything required to run, test and deploy is here.

## Start here

You are working on an order-flow engine. It reads perpetual trade flow from
**OKX** and places orders on **Delta Exchange India**.

Order of operations for a new session:

1. Run the tests. They are offline and take under a second.
   `python3 -m pytest tests -q` -> 166 passed
2. Read `config/delta.env` for the venue, the credentials and the product notes.
   If the file is absent, the credentials come from the environment instead
   (`DELTA_API_KEY`, `DELTA_API_SECRET`).
3. Confirm the venue accepts the signature before doing anything else:
   `python3 preflight_delta.py --live`
4. To run the strategy, use `deploy/run_paper.sh`. It sends nothing. Do not
   invent a new way to run it.

The production host is described under "Where it runs". Reach it with the key
from the `VPS_SSH_KEY` secret if the platform provides one; otherwise ask the
owner, and do not paste a private key into a chat.

## What this is

Ported from `morning-order-flow-real-engine-compete`, which used Angel One
SmartStream for both the feed and the execution.

The engine (`core`, `ticks`, `footprint`, `profile`, `delta`, `tape`, `dom`,
`indicators`, `render`) is copied verbatim and its tests pass unmodified. Do not
edit those files: `tests/test_engine.py` is the contract.

## Where it runs

Production runs on a small AWS instance, because the venue requires a
whitelisted IP for any key that can trade and a sandbox or home IP changes
without warning.

    host     3.108.53.100   (AWS ap-south-1, Mumbai, static)
    user     ec2-user
    OS       Amazon Linux 2023
    python   /usr/bin/python3.14   (3.10+ is required, see below)
    repo     ~/okx-feed-delta-execution
    venv     ~/okx-feed-delta-execution/.venv
    pulls    read-only deploy key at ~/.ssh/deploy_key, alias github-deploy

    ~/.ssh/config:
      Host github-deploy
        HostName github.com
        User git
        IdentityFile ~/.ssh/deploy_key
        IdentitiesOnly yes

    git remote: git@github-deploy:aslamrajput7770-rgb/okx-feed-delta-execution.git

The deploy key is read-only and scoped to this repository alone, so a leaked
key grants no write access and reaches nothing else.

## Credentials

Precedence:

1. `DELTA_API_KEY` / `DELTA_API_SECRET` in the environment. This is the
   preferred source, because a platform secret is injected into every session
   automatically and never touches the repository or its history.
2. `config/delta.env`.

Never pass a secret as a command-line argument; arguments are visible in `ps`.
Never add an `api_secret` constructor argument. `DeltaCredentials.from_env()` is
the only entry point.

The key is IP-locked. A request from any other address returns
`ip_not_whitelisted_for_api_key`, which also names the offending `client_ip`.

## Commands

```bash
python3 -m pytest tests -q                      # 166 tests, offline, ~0.2s
python3 preflight_delta.py                      # testnet, read-only
python3 preflight_delta.py --live               # production, read-only
./deploy/run_paper.sh 5                         # 5 min, live host, NO orders
DELTA_ORDER_SIZE=0.001 ./deploy/run_live.sh 5   # real orders, needs the size
python3 live_feed.py --minutes 1 --record c.txt # live OKX feed, no orders
python3 diagnose_okx.py c.txt                   # side-mapping verdict
python3 live_feed.py --replay c.txt             # offline replay
```

`diagnose_okx.py` exits non-zero when the verdict is not `OK`. It is a check,
not a report.

`run_paper.sh` never passes `--execute`. `run_live.sh` refuses to start without
an explicit `DELTA_ORDER_SIZE` and has no default. The split is deliberate:
enabling trading should take a distinct command, not a flag appended to the one
already in your shell history.

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

**Live orders need three things.** `dry_run=False`, `allow_live=True`, and
`Environment.LIVE`. Testnet is the default environment and dry run is the
default mode.

**The session resets at 00:00 UTC**, which is 05:30 IST. Not IST midnight:
global funding, daily candles and venue daily volume all roll on 00:00 UTC, and
resetting elsewhere puts cumulative delta out of step with every chart the user
compares against.

**`BASE_UNITS = 10**8` in `ticks.py` is not to be touched.** Multi-decimal
crypto prices depend on it.

**Python 3.10 or newer.** `dataclass(slots=True)` is a syntax error on 3.9,
which is what Amazon Linux 2023 ships as `python3`. Use `/usr/bin/python3.14`.

## Verified against the live venue

Measured on the real host from a whitelisted IP, not assumed. Each of these cost
a debugging cycle, so do not re-derive them.

**The signature is correct and accepted.** `preflight_delta.py --live` reports
`signature PASS`: the venue accepted the signature on `GET /v2/profile`, and the
wallet read returns the account's assets. The earlier suspicion that Delta's
documented example did not reproduce was a documentation artefact, not an
implementation fault.

**Signing payload format is confirmed by the venue itself.** For `GET /v2/profile`
the venue reported `signature_data` as `GET<timestamp>/v2/profile`, exactly what
`signature_payload()` builds (method + timestamp + path + query + body).

**401 codes are distinguishable, and the difference matters.**

    ip_not_whitelisted_for_api_key  the key is valid, the calling IP is not
                                    whitelisted. Carries `client_ip`.
    invalid_api_key                 no such key on this host
    Signature Mismatch              key and IP are fine; the secret is wrong.
                                    Carries `signature_data`, the exact signed
                                    string, which is the fastest way to confirm
                                    the payload format.

**A key resolves differently per host, which identifies its environment.**
Same credentials, same IP, both hosts:

    testnet  cdn-ind.testnet.deltaex.org  -> invalid_api_key
    live     api.india.delta.exchange     -> Signature Mismatch

So `invalid_api_key` on testnet plus `Signature Mismatch` on live means the key
is a production key, and the preflight needs `--live`. A testnet key shows the
reverse. Both hosts are reachable from ap-south-1.

**The clock comes from the `Date` response header.** `/v2/time` 404s on both
hosts. Two traps, both now handled in `_venue_date_header()`: `HEAD` returns 404
on the live host (a one-byte `Range` GET returns 206), and `/v2/products` is
CDN-cached for 60s so a cache hit replays a stale `Date` (measured: `Age: 39`
produced a 40s phantom skew). A unique query parameter forces an origin
response. With both fixed, the measured skew is under a second.

**A key with Trading permission requires a whitelisted IP.** A read-only key is
usable for the preflight without Trading.

## Known gaps

**No hedging leg.** OKX is a signal source only; nothing is traded there. The
position is directional and carries basis risk. The docs and the README say so
and should keep saying so.

**No persistence.** Sessions are in memory. A restart loses the day's profile.

**`live_feed.py` has no reconnection logic.** The transport returns `None` on a
socket error and the run ends. `FeedStats.reconnects` exists and is never
incremented.

**One signal, one order.** The engine computes cumulative delta over the session
and sends a single order at the end of the run, sized by `--order-size`. There is
no entry/exit loop and no risk management. Treat it as a plumbing demonstration
until that changes.

## Things not to do

- Do not add a Binance feed. Binance returns HTTP 451 from this environment
  (verified from a US egress IP, so it is not an India-only block). Relocating
  the process does not help.
- Do not add geo-restriction bypass logic of any kind.
- Do not add an `api_secret` constructor argument "for convenience".
- Do not default `dry_run` to False.
- Do not make `diagnose_okx.py` exit 0 on a `SUSPECT` or `FAIL` verdict.
- Do not commit a private key, and do not ask for one in chat.
