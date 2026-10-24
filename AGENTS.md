# AGENTS.md

Read this first. It is written so that a fresh session, on a fresh machine,
needs no briefing: everything required to run, test and deploy is here.

## Start here

You are working on an order-flow engine. It reads perpetual trade flow from
**OKX** and places orders on **Delta Exchange India**.

Order of operations for a new session:

1. Run the tests. They are offline and take under a second.
   `python3 -m pytest tests -q` -> 275 passed
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

## State of play

Read this before assuming anything about what is finished.

**Done and on `main`.** Telegram signal delivery (`orderflow/notify/`); the feed
runs in a single pass (it used to run twice, and the second pass - the one that
trades - saw nothing); a dropped socket reconnects instead of raising out of
`run()`; SIGTERM is handled like Ctrl-C, so `systemctl stop` closes the position
and saves the daily counter. `deploy/aws_setup.sh` installs the project as a
systemd unit, and the unit file is in the repository. 272 tests pass.

**Verified, not assumed.** The OKX feed is genuinely live: its trade frames
matched `https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP` to
within a second, with `duplicates=0` and `errors=0`. Paper mode runs the real
supervisor with `execute=False`, so it produces the entries, exits and
notifications a live run would, minus the request.

**Not done, and the reason matters.**

1. **The installer has been run, but not on the target host.** `deploy/aws_setup.sh`
   has now been executed end to end on a clean Debian container: it created the
   service user, installed the code, ran the suite as that user (275 passed),
   proved `state/` is writable by it, wrote both env files and installed the
   unit. That run is what found the two defects described below. What has *not*
   happened is an install on the actual EC2 instance, and no systemd unit has
   ever been started by systemd - this container has no init. Expect the unit
   install and `systemctl start` to be the first genuinely new step there.
2. **The Delta credentials must be rotated.** They are committed in
   `config/delta.env` at the owner's explicit instruction. Do not undo that, but
   do tell the owner to rotate the key and the secret; a committed credential is
   a credential that has to be considered public.
3. **No backtest exists, so nothing here shows the strategy makes money.** The
   tests measure the plumbing. `realized pnl` is measured at the OKX price, not
   at a Delta fill, so it is an upper bound at best.
4. **There is no cooldown after an exit.** The signal is still positive after a
   stop loss fires, so the next tick re-enters; a run can walk into the same loss
   until the daily budget halts it. This is a strategy gap, not a safety one, and
   it is the most likely next piece of real work.

Two deployment defects were found by running the installer rather than reading
it, and both are fixed. They are recorded because the shape of each is easy to
reintroduce:

* **The suite wrote into the directory it was run from.** `--risk-state`
  defaults to the relative `state/daily_pnl.json`, which is correct for the
  service (systemd sets `WorkingDirectory` to the install root) and wrong for a
  test. The installer runs the suite as root, so the file it left in the install
  tree was root-owned, and the service user then hit `PermissionError` on its own
  daily counter - on the first run, in paper mode. `tests/conftest.py` now chdirs
  every test into a temporary directory, and `tests/test_deploy_isolation.py`
  pins it.
* **The suite ran as root in the first place.** It now runs as the service user,
  which is both more honest (the files a test writes get the ownership the
  service will have) and a real check that the service user can read the
  checkout and write `state/`. The installer normalises the install tree's modes
  first, because `rsync -a` and `tar -p` preserve a source mode of 600 and a
  checkout copied under a restrictive umask can carry one.

A good first session is: run the tests, run `deploy/run_paper_telegram.sh 5` and
confirm a message arrives, then decide with the owner whether the next step is
the AWS install or the cooldown.

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

Telegram is a **separate, optional** pair: `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`, read by `TelegramCredentials.from_env()`. The difference in
posture is deliberate. A missing Delta pair is fatal because the run cannot
resolve the venue; a missing Telegram pair is silence, because notifying is a
convenience and a run without it is still a valid run. `--telegram` turns the
silence into an error, so a run that thinks it is notifying and is not refuses to
start instead.

The key is IP-locked. A request from any other address returns
`ip_not_whitelisted_for_api_key`, which also names the offending `client_ip`.

## Commands

```bash
python3 -m pytest tests -q                      # 343 tests, offline, ~1.3s
python3 preflight_delta.py                      # testnet, read-only
python3 preflight_delta.py --live               # production, read-only
python3 telegram_check.py                       # bot can reach the chat, sends one message
./deploy/run_paper.sh 5                         # 5 min, live host, NO orders
./deploy/run_paper_telegram.sh 5                # 5 min, NO orders, signals to Telegram
DELTA_ORDER_SIZE=0.001 ./deploy/run_live.sh 5   # real orders, needs the size
python3 live_feed.py --dry-run                  # offline synthetic tape, no socket
python3 live_feed.py --minutes 1 --record c.txt # live Delta feed, no orders
python3 live_feed.py --venue okx --minutes 1    # cross-venue mode (OKX signal)
python3 diagnose_okx.py c.txt                   # OKX side-mapping verdict
python3 live_feed.py --replay c.txt             # offline replay
```

CI (`.github/workflows/ci.yml`) runs the suite on 3.10/3.12/3.13 and runs the
installer end to end on a clean runner, including a paper session as the service
user. Reading the installer would not have caught either install defect; running
it did. If you change `deploy/`, that job is the one to watch.

### Backtesting

`tools/backtest.py` runs the shipped signal loop over a recorded tape. It
imports `live_feed` and drives the real `Supervisor`, so it measures the rules
that run in production rather than a copy of them.

**A Delta tape has to be recorded live.** Delta keeps no trade-print history:
its REST `trades` endpoint returns the latest 50 prints and ignores `start` and
`end` (tried in both seconds and microseconds), its only historical endpoint is
candles, and candles carry no aggressor split so the signal cannot be rebuilt
from them. The docs scope cursor pagination to products, orders and fills. OKX
does have pageable history, and `tools/fetch_okx.py` uses it - but that is the
cross-venue mode now.

```bash
make tape                                       # 1440 min = 1 day, Delta, live
python3 tools/capture.py --venue delta --minutes 1440 --out tape.txt
python3 tools/backtest.py --tape tape.txt                    # shipped rules
python3 tools/backtest.py --tape tape.txt --grid-stop 0.25:3.0:0.25
python3 tools/backtest.py --tape tape.txt --verify-delta     # slow, self-checks
python3 tools/backtest.py --tape tape.txt --venue okx        # OKX-recorded tape
python3 tools/fetch_okx.py --hours 6 --out tape_okx.txt      # OKX history
```

`--venue` selects the decoder and must match the tape. A Delta tape fed to the
OKX decoder decodes to zero prints rather than to garbage, and the tests pin
that, because a silent zero is the failure mode to catch.

Fills are simulated at the print price plus cost; the strategy, risk ladder and
exit logic are the shipped code paths. See `docs/backtest.md` for the results.

Note that with `--venue delta` the basis is zero by construction: the signal
price and the fill price are the same price. That is the point of the default.

## As a service

`deploy/aws_setup.sh` installs the project as a systemd unit on an EC2 instance.
Everything it needs is in this repository, including the unit file and the mode
switch, so the deployed code and the deployed configuration cannot drift apart:
the clone is the deployment.

```bash
sudo ./deploy/aws_setup.sh          # install; does not start, does not go live
sudo systemctl start okx-feed-delta
journalctl -u okx-feed-delta -f
```

Two environment files, so the one edited to change behaviour is not the one
holding a credential:

    /etc/okx-feed-delta/env          DELTA_* and TELEGRAM_*, mode 600
    /etc/okx-feed-delta/service.env  SERVICE_MODE and timings, mode 600

`SERVICE_MODE=paper` is the installed default. `SERVICE_MODE=live` additionally
requires `DELTA_ORDER_SIZE` and `I_KNOW_THIS_IS_LIVE=1` in `service.env`, so
enabling real orders takes three deliberate edits rather than one.

The unit runs **bounded** windows (`SERVICE_MINUTES`, default 60) and
`Restart=always`. That is deliberate:

* the daily loss counter is per-day, and a restart notices midnight for free;
* a bounded clean exit makes `systemctl status` readable, where an unbounded run
  that died is indistinguishable from one that is fine;
* the engine refuses `--minutes 0` on a live socket, and the service does not
  weaken that guard.

`TimeoutStopSec=60` is longer than the default because a stop signal is a
request to close the position, and killing the process mid-close is exactly the
abandoned-position case the SIGTERM handler exists to prevent.

The script does not create infrastructure. EC2 instances, security groups and
key pairs belong in the account's own IaC; a setup script that can create
infrastructure is one that can delete it.

`diagnose_okx.py` exits non-zero when the verdict is not `OK`. It is a check,
not a report.

`run_paper.sh` never passes `--execute`. `run_live.sh` refuses to start without
an explicit `DELTA_ORDER_SIZE` and has no default. The split is deliberate:
enabling trading should take a distinct command, not a flag appended to the one
already in your shell history.

**The feed is run exactly once.** `main()` attaches the supervisor to the single
`feed.run()` pass. Running it twice does not work, and this was a real bug: dedup
is on `(instId, tradeId)`, so a second pass over the same frames delivers zero
trades to its callback. The supervised pass therefore traded nothing while the
first pass reported a healthy feed — visible as `entries=0` on any offline
replay, where the two passes see identical frames. Anything the run needs in
order to decide must be resolved before the first frame arrives.

**Paper mode runs the real supervisor**, with `execute=False`. `--order-size`
with `--telegram` and no `--execute` therefore produces the same entries, exits
and notifications a live run would, minus the request. A paper path that skipped
the supervisor would prove nothing about the live path.

**`--telegram-every-signal` is off by default on purpose.** The signal is the sign
of cumulative delta, which flips rarely but stays put for every tick that keeps
it; notifying per tick would send thousands of identical messages. The flag
notifies on a change of sign while flat, which is useful for a short test and
noisy over a session.

**`--dry-run` and `--replay` are offline and must stay offline.** `--dry-run`
replays a synthetic tape (`dry_run_frames`); `--replay` replays a capture. Neither
fetches anything, not even the product spec, so both return immediately on a
machine with no network. `--dry-run` used to open a real socket and, with the old
`--minutes 0` default, hang forever.

**The basis check runs only on a live feed.** A capture holds historical prices,
so comparing them to today's mark price measures elapsed time rather than the
spread between the venues. Sizing still runs offline, from the static
`DELTA_BTCUSD` template.

**A live run needs `--minutes > 0`.** Refusing the unbounded case is what stops
a bare invocation from hanging on a socket that never closes.

**Mainnet needs a third flag.** `--execute --live` also requires
`--i-know-this-is-live`. The guard is in the program, not only in `run_live.sh`,
because calling `live_feed.py` directly must not be a way around it.

**`--basis-warn` refuses, and it is wired.** OKX and Delta are different
instruments, so the spread between them is risk the delta signal does not see.
The guard reads the Delta mark price from the public ticker and returns non-zero
when `|basis|` exceeds the limit; on a live order it also refuses when the basis
cannot be read at all. It used to be a dead function with a flag that claimed to
refuse and did nothing.

**Zero cumulative delta is no signal.** `order_side(0.0)` returns `None` and no
order is sent. It used to fall through to `sell`.

## Invariants

These are load-bearing. Each one has a silent failure mode if broken.

**The taker side is never inferred.** OKX reports it. `buy` = taker bought =
volume on the ask = `Side.BUY`. Inverting this inverts every signal while
leaving the footprint looking correct.
`tests/test_okx_protocol.py::TestTakerSideMapping` catches it.

**A dropped socket is reconnected, never heartbeated forever.** A dead socket
and a quiet one both make `recv` return nothing, so the loop cannot tell them
apart from the return value alone. `_LiveOkxTransport.dead` is set when `recv`
raises anything other than a timeout, and `_heartbeat()` is guarded so a failed
ping reconnects instead of raising out of `run()`. A heartbeat that *succeeds*
proves nothing — a half-open TCP socket accepts the write — so silence past
`reconnect_after_seconds` also counts as a drop. Backoff is capped at 30s so an
outage is not a request storm. `stats.reconnects` is printed and sent in the run
summary, because a feed that is quietly reconnecting is a feed with a problem.
`tests/test_feed_resilience.py` pins all of this.

**SIGTERM is handled like Ctrl-C.** `systemctl stop` and an EC2 stop send
SIGTERM, never SIGINT. `_exit_on_sigterm()` maps SIGTERM and SIGHUP onto
`KeyboardInterrupt`, which is the path that already closes the position and
saves the daily counter. Without it the interpreter dies on the default
disposition, the position is abandoned and the loss budget resets on restart —
the one moment the risk limits matter most. Installed at the top of `main()`,
before any socket is opened.

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

**No hedging leg.** Nothing is traded on the signal venue. In the default
`--venue delta` mode there is no basis to carry, because the signal and the fill
are the same book. In `--venue okx` mode the position is directional and carries
basis risk, and the docs and the README say so.

**No persistence.** Sessions are in memory. A restart loses the day's profile.

**Reconnection exists now, and the drop is told apart from silence.** A dead
socket and a quiet one both make `recv` return nothing, so `dead` is set when
`recv` raises anything other than a timeout, and a failed heartbeat reconnects
rather than raising out of `run()`. A heartbeat that succeeds proves nothing (a
half-open socket still accepts the write), so silence past
`reconnect_after_seconds` is also a drop. `FeedStats.reconnects` is incremented
and reported.

**One position at a time.** The engine computes cumulative delta over the session
and the `Supervisor` loop enters on the sign of that delta, watches the position
on every trade, and exits on stop loss, take profit, hold limit, or the daily
loss halt. The limits live in `RiskLimits` and are all CLI flags:

| Rule | Flag | Default |
|---|---|---|
| Stop loss | `--stop-loss-pct` | 1% |
| Take profit | `--take-profit-pct` | 2% |
| Position cap | `--max-position` | 0.01 BTC |
| Daily loss limit | `--max-daily-loss` | 50 quote |
| Max hold | `--max-hold-seconds` | 3600 |

`0` disables a rule explicitly. Exits are sent `reduce_only`, and a failed exit
leaves the position recorded as open, because it is still live at the venue.

The daily counter persists to `state/daily_pnl.json` (override with
`--risk-state`, disable with `--no-risk-state`). A limit that resets on restart
is not a limit: the losing loop is the thing that restarts the process. A file
from a previous day is discarded rather than carried forward.

The remaining gaps are strategy-level, not safety-level: the entry rule is still
the raw sign of cumulative delta, and re-entry after an exit is immediate
whenever the signal still points the same way. Treat it as a plumbing
demonstration with working guards, not a tuned strategy.

**One signal, one position.** The supervisor holds at most one position at a
time, checking the exit before considering an entry. The `--basis-warn` guard,
the mainnet confirmation flag and the zero-delta check bound what this can do,
and the risk limits cap it, but none of them is a claim that the signal has an
edge.

**No cooldown between an exit and the next entry.** The signal is still positive
after a stop loss fires, so the next tick re-enters. A run can therefore walk
into the same loss repeatedly until the daily budget halts it. The daily loss
limit is what stops this, not the strategy.

**Reported PnL is measured at the signal price, not at a Delta fill.** The exit
price is the last trade price on the signal venue, and no fill, fee or slippage
from the execution venue enters the number. It is the price the signal was
computed from, not the price the position would have been closed at, so
`realized pnl` is an upper bound at best. Reading the real fill requires a
position query this repository does not make. With `--venue delta` the two
prices are at least from the same book.

**The signal has no demonstrated edge.** A backtest exists now
(`tools/backtest.py`), it runs the shipped rules, and on every tape measured it
loses money - before fees as well as after. See `docs/backtest.md`. The tests
measure the plumbing, which works; they say nothing about profitability. Treat
any profitability claim about this repository as unverified until a run of the
backtest on a full day of Delta prints says otherwise.

**A Delta tape is a recording, not a download.** Because Delta keeps no
trade-print history, every backtest is limited to windows that were captured
while the process was running. There is no way to go back and fill a gap.

## Things not to do

- Do not add a Binance feed. Binance returns HTTP 451 from this environment
  (verified from a US egress IP, so it is not an India-only block). Relocating
  the process does not help.
- Do not add geo-restriction bypass logic of any kind.
- Do not add an `api_secret` constructor argument "for convenience".
- Do not default `dry_run` to False.
- Do not make `diagnose_okx.py` exit 0 on a `SUSPECT` or `FAIL` verdict.
- Do not commit a private key, and do not ask for one in chat.
- Do not commit a Telegram bot token or chat id either. They go in the
  environment like the Delta pair; a committed token is a token anyone can post
  with.
- Do not let a notification failure raise. The notifier is called from a tick
  callback that is watching an open position; an exception there abandons the
  feed. `DeliveryResult` exists so the failure can be logged and swallowed.
- Do not notify from inside `RiskManager`. It is pure by design, and the
  supervisor is the layer that owns I/O.
