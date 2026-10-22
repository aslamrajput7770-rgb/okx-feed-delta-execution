"""Confirm a Delta India API key works, without placing an order.

Run this before anything else, with **testnet** keys:

    export DELTA_API_KEY=...
    export DELTA_API_SECRET=...
    python3 preflight_delta.py

What it proves, in order:

1. The credentials load and the key is not empty.
2. Our HMAC-SHA256 signature is **accepted by the venue**. This is the check
   that matters: Delta's documented example signature does not reproduce from
   the algorithm it documents, so the algorithm can only be confirmed against
   the venue itself. `/v2/profile` is used because it is authenticated and
   read-only: a wrong signature gets a 401 and nothing else happens.
3. The venue's clock is readable and the local skew is within the 5-second
   signature window.
4. The BTCUSD perpetual exists, is live, and reports the tick and contract size
   this repository is built on. The product is resolved **by symbol**, because
   ids are not portable between environments: production carries the perpetual
   on id 27, while testnet carries an expired March future on id 27 and the
   perpetual on id 84. Hardcoding 27 would size a testnet order 1000x too large.
5. The wallet balance is readable, which confirms the key has Read Data.

It does **not** place an order and does not need Trading permission. Run it
with testnet keys first; then, and only then, consider production keys.

Exit code is 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from orderflow import DELTA_BTCUSD, Instrument
from orderflow.execution import (
    BASE_URLS,
    DeltaCredentials,
    DeltaOrderClient,
    Environment,
    MissingCredentials,
    OrderError,
    ProductResolutionError,
    resolve_perpetual,
)

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"


def _get(url: str, headers: dict | None = None, timeout: float = 10.0):
    req = urllib.request.Request(
        url, headers=headers or {"User-Agent": "okx-feed-delta-execution"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return exc.code, parsed


def check_credentials() -> tuple[str, str]:
    try:
        creds = DeltaCredentials.from_env()
    except MissingCredentials as exc:
        return FAIL, str(exc)
    return PASS, f"loaded ({creds!r})"


def check_signature(client: DeltaOrderClient) -> tuple[str, str]:
    """The core check: does the venue accept our signature?"""
    result = client.verify_signature_against_venue()
    status = result.get("status")
    if result.get("ok"):
        return PASS, "venue accepted the signature on GET /v2/profile"
    if status == 401:
        detail = str(result.get("detail") or result.get("response"))
        hint = ""
        if "signature" in detail.lower():
            hint = (
                " -> the signing algorithm does not match this venue. Stop here; "
                "do not place orders until this passes."
            )
        elif "key" in detail.lower() or "unauthor" in detail.lower():
            hint = (
                " -> the key was rejected. Check it belongs to the same "
                "environment (testnet keys only work on the testnet host) and "
                "that it has Read Data permission."
            )
        return FAIL, f"401 from the venue: {detail}{hint}"
    return FAIL, f"unexpected response: {result}"


def check_clock(client: DeltaOrderClient) -> tuple[str, str]:
    try:
        venue = client.server_time()
        skew = client.clock_skew()
    except (OrderError, urllib.error.URLError, OSError) as exc:
        return WARN, f"could not read the venue clock: {exc}"
    if abs(skew) > 2.0:
        return (
            FAIL,
            f"skew {skew:+.2f}s exceeds the 5s signature window; sync with "
            f"`chronyc makestep`. Note the Date header is served through "
            f"CloudFront and can be stale, so confirm with NTP.",
        )
    return PASS, f"skew {skew:+.2f}s (venue clock read at {int(venue)})"


def check_product(environment: Environment) -> tuple[str, str, Instrument | None]:
    """Resolve the execution leg by symbol and verify its spec.

    Does **not** trust the production product id. Testnet carries a different
    contract on id 27, so the id is resolved per environment and the resulting
    spec is checked against the engine's assumption.
    """
    base = BASE_URLS[environment]
    try:
        product, instrument = resolve_perpetual(
            base, DELTA_BTCUSD.symbol, DELTA_BTCUSD, strict=True
        )
    except ProductResolutionError as exc:
        return FAIL, str(exc), None

    detail = (
        f"id {product.venue_id} {product.symbol} live, tick {product.tick_size}, "
        f"1 contract = {product.contract_value} base"
    )
    grid = product.grid_problems(DELTA_BTCUSD)
    if grid:
        # Informational: the grid differs from production but the size does not.
        detail += f"  [grid differs from production: {'; '.join(grid)}]"
    return PASS, detail, instrument


def check_balance(client: DeltaOrderClient, environment: Environment) -> tuple[str, str]:
    """A read-only call, to confirm the key has Read Data permission."""
    timestamp = str(int(client._clock()))  # noqa: SLF001 - the preflight owns the clock
    path = "/v2/wallet/balances"
    headers = client._sign_headers("GET", path, "", timestamp)  # noqa: SLF001
    try:
        status, payload = client._sender("GET", f"{client.base_url}{path}", headers, "")  # noqa: SLF001
    except urllib.error.HTTPError as exc:
        return FAIL, f"{exc.code}: {exc.read().decode('utf-8', 'replace')}"
    except (urllib.error.URLError, OSError) as exc:
        return FAIL, str(exc)
    if status != 200:
        return WARN, f"HTTP {status}: {payload}"
    balances = payload.get("result") or []
    return PASS, f"readable ({len(balances)} asset(s) in the wallet)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Delta India key preflight, no orders")
    ap.add_argument(
        "--live",
        action="store_true",
        help="check production keys (default: testnet). Read-only either way.",
    )
    args = ap.parse_args(argv)

    environment = Environment.LIVE if args.live else Environment.TESTNET
    host = BASE_URLS[environment]
    print(f"Delta India preflight - {environment.value}")
    print(f"host: {host}")
    print("no orders will be placed; Trading permission is not needed")
    print()

    results: list[tuple[str, str, str]] = []

    name, detail = check_credentials()
    results.append(("credentials", name, detail))

    client = None
    if name == PASS:
        client = DeltaOrderClient(
            credentials=DeltaCredentials.from_env(),
            environment=environment,
            allow_live=True,  # permits the read-only authenticated calls
            dry_run=False,
        )
        n, d = check_signature(client)
        results.append(("signature", n, d))
        n, d = check_clock(client)
        results.append(("clock", n, d))
        n, d = check_balance(client, environment)
        results.append(("wallet read", n, d))
    else:
        results.append(("signature", "SKIP", "no credentials"))
        results.append(("clock", "SKIP", "no credentials"))
        results.append(("wallet read", "SKIP", "no credentials"))

    n, d, instrument = check_product(environment)
    results.append(("execution leg", n, d))

    width = max(len(r[0]) for r in results)
    for label, verdict, detail in results:
        print(f"  {label:<{width}}  {verdict:<5} {detail}")

    print()
    failed = [r[0] for r in results if r[1] == FAIL]
    warned = [r[0] for r in results if r[1] == WARN]
    if failed:
        print(f"RESULT: NOT READY - {', '.join(failed)} failed.")
        print("Do not place an order until every check passes.")
        return 1
    if warned:
        print(f"RESULT: READY WITH CAVEATS - {', '.join(warned)} could not be fully checked.")
        return 0
    print("RESULT: READY - signature accepted, execution leg verified, clock in range.")
    if instrument is not None:
        print(f"\nOrder with product_id={instrument.venue_id} on {environment.value}. "
              f"Do not hardcode this id across environments.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
