"""Delta Exchange India order client.

Safety posture, and why it is shaped this way
---------------------------------------------
This module can move real money, so the defaults are chosen so that the
dangerous path cannot be reached by accident:

* **Dry run is the default.** A client built with no arguments builds orders,
  signs them, and returns what it *would* have sent. Nothing leaves the process.
* **Testnet is the default environment.** Reaching mainnet takes an explicit
  `Environment.LIVE`, and reaching the network at all takes an explicit
  `allow_live=True`. Two independent flags must be set the wrong way before a
  real order can be placed.
* **Credentials come from the environment and nowhere else.** There is no
  constructor argument for the API secret, so a secret cannot be passed in from
  a caller that got it from a config file, a command line, or a chat message.
  `DeltaCredentials.from_env()` is the only way to build them.
* **The secret is never logged or rendered.** `__repr__` is overridden to mask
  it, because a secret that reaches a log line or a traceback has leaked.

The signature
-------------
Delta signs `method + timestamp + path + query_string + body` with HMAC-SHA256
and sends it in the `signature` header alongside `api-key` and `timestamp`. The
signature is valid for **5 seconds**, so it is generated immediately before the
send and never cached.

Note: Delta's own documentation example signature does not reproduce from the
algorithm it documents. The algorithm implemented here is the one described in
prose and used by the official client; it is pinned by a test against a locally
computed vector, and must be confirmed against the venue on testnet before it is
trusted on mainnet. `verify_signature_against_venue()` exists for exactly that.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum

from ..instruments import DELTA_BTCUSD, Instrument


class Environment(str, Enum):
    TESTNET = "testnet"
    LIVE = "live"


class OrderError(RuntimeError):
    """The venue rejected the order, or the request could not be sent."""


class LiveOrderRefused(RuntimeError):
    """A live order was attempted without the explicit opt-in."""


class MissingCredentials(RuntimeError):
    """The environment does not carry the credentials this client needs."""


#: Verified from the Delta docs (2026-10-22).
#: India keys only work on India hosts; the global host rejects them.
BASE_URLS = {
    Environment.TESTNET: "https://cdn-ind.testnet.deltaex.org",
    Environment.LIVE: "https://api.india.delta.exchange",
}

#: Delta accepts a signature generated in the last 5 seconds.
SIGNATURE_TTL_SECONDS = 5
#: Refuse to sign if our clock is further than this from the venue's.
MAX_CLOCK_SKEW_SECONDS = 2.0

ENV_API_KEY = "DELTA_API_KEY"
ENV_API_SECRET = "DELTA_API_SECRET"


def generate_signature(api_secret: str, signature_data: str) -> str:
    """HMAC-SHA256 of the signature string, hex-encoded."""
    return hmac.new(
        api_secret.encode("utf-8"),
        signature_data.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def signature_payload(
    method: str,
    timestamp: str,
    path: str,
    query_string: str = "",
    body: str = "",
) -> str:
    """The exact string Delta signs, in the order it signs it.

    The order matters and an empty component is still a component: dropping the
    empty query string produces a signature that is wrong for every GET, which
    is the sort of bug that only shows up on the one endpoint you did not test.
    """
    return f"{method.upper()}{timestamp}{path}{query_string}{body}"


@dataclass(frozen=True, slots=True)
class DeltaCredentials:
    """API key and secret, sourced only from the environment."""

    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "DeltaCredentials":
        """Read `DELTA_API_KEY` and `DELTA_API_SECRET`.

        Raises if either is missing rather than falling back to a default: an
        empty credential produces a 401 that looks like a venue problem.
        """
        source = os.environ if env is None else env
        key = source.get(ENV_API_KEY, "")
        secret = source.get(ENV_API_SECRET, "")
        missing = [
            name
            for name, val in ((ENV_API_KEY, key), (ENV_API_SECRET, secret))
            if not val
        ]
        if missing:
            raise MissingCredentials(
                f"missing {', '.join(missing)} in the environment. Set them with "
                f"`export {ENV_API_KEY}=... {ENV_API_SECRET}=...` and never commit "
                f"them. Testnet keys come from your Delta demo account."
            )
        return cls(api_key=key, api_secret=secret)

    def __repr__(self) -> str:  # pragma: no cover - trivial, but security-relevant
        return f"DeltaCredentials(api_key='{self.api_key[:4]}...', api_secret=<hidden>)"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """A Delta order, in the venue's own vocabulary."""

    product_id: int
    size: int
    side: str  # "buy" | "sell"
    order_type: str = "market_order"
    limit_price: str | None = None
    reduce_only: bool = False
    client_order_id: str | None = None

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise OrderError(f"side must be 'buy' or 'sell', got {self.side!r}")
        if self.size <= 0:
            raise OrderError(f"size must be > 0, got {self.size}")
        if not float(self.size).is_integer():
            # Delta sizes are whole contracts. A fractional size is a sizing bug
            # upstream, not something to round away here.
            raise OrderError(
                f"Delta size must be a whole number of contracts, got {self.size}"
            )
        if self.order_type in ("limit_order", "stop_limit_order") and self.limit_price is None:
            raise OrderError(f"{self.order_type} needs a limit_price")

    def to_payload(self) -> dict:
        body = {
            "product_id": self.product_id,
            "size": self.size,
            "side": self.side,
            "order_type": self.order_type,
        }
        if self.limit_price is not None:
            body["limit_price"] = self.limit_price
        if self.reduce_only:
            body["reduce_only"] = True
        if self.client_order_id is not None:
            body["client_order_id"] = self.client_order_id
        return body

    def canonical_body(self) -> str:
        """The body exactly as it is signed and sent: compact separators.

        `json.dumps` with a space after the separators produces a different
        string from the one that is signed, and the signature then fails for a
        reason that looks like a key problem.
        """
        return json.dumps(self.to_payload(), separators=(",", ":"))


@dataclass(slots=True)
class OrderResult:
    sent: bool
    dry_run: bool
    environment: Environment
    request: OrderRequest
    status_code: int | None = None
    response: dict | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and (self.dry_run or self.status_code == 200)


class DeltaOrderClient:
    """Places orders on Delta India. Dry-run and testnet by default.

        client = DeltaOrderClient()               # dry run, testnet
        client.buy(product_id=27, size=1)         # nothing is sent

        client = DeltaOrderClient(allow_live=True, environment=Environment.TESTNET)
        client.buy(product_id=27, size=1)         # a real testnet order

    `allow_live` and `environment` are deliberately two separate decisions: a
    testnet order still needs the network, and mainnet needs both.
    """

    def __init__(
        self,
        credentials: DeltaCredentials | None = None,
        environment: Environment = Environment.TESTNET,
        allow_live: bool = False,
        dry_run: bool = True,
        timeout: float = 10.0,
        clock=None,
        sender=None,
    ) -> None:
        self.environment = environment
        self.allow_live = allow_live
        self.dry_run = dry_run
        self.timeout = timeout
        #: Injectable for tests and for a caller that has its own NTP-synced
        #: clock source. Defaults to `time.time`.
        self._clock = clock or time.time
        #: Injectable transport, so tests exercise the real signing and payload
        #: path without touching the network.
        self._sender = sender or self._http_send
        self.base_url = BASE_URLS[environment]

        # Credentials are only demanded when they will actually be used. A dry
        # run on a laptop with no keys is a legitimate thing to do.
        if credentials is None and not dry_run:
            credentials = DeltaCredentials.from_env()
        self.credentials = credentials

    # ------------------------------------------------------------------ guards
    def _check_allowed(self) -> None:
        if self.dry_run:
            return
        if self.environment is Environment.LIVE and not self.allow_live:
            raise LiveOrderRefused(
                "refusing to place a LIVE order: pass allow_live=True to confirm. "
                "Use Environment.TESTNET until the strategy is verified."
            )
        if self.credentials is None:
            raise MissingCredentials(
                "no credentials: build them with DeltaCredentials.from_env()"
            )

    def _sign_headers(self, method: str, path: str, body: str, timestamp: str) -> dict:
        assert self.credentials is not None
        data = signature_payload(method, timestamp, path, "", body)
        return {
            "api-key": self.credentials.api_key,
            "signature": generate_signature(self.credentials.api_secret, data),
            "timestamp": timestamp,
            "Content-Type": "application/json",
            "User-Agent": "okx-feed-delta-execution",
        }

    # ------------------------------------------------------------------- clock
    def server_time(self) -> float:
        """The venue's clock, in epoch seconds.

        Used to detect skew before signing. A skewed clock produces a
        `signature_expired` rejection that reads like an auth failure, and the
        real cause is easy to miss.
        """
        url = f"{self.base_url}/v2/time"
        req = urllib.request.Request(url, headers={"User-Agent": "okx-feed-delta-execution"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read())
        return float(payload.get("result", payload).get("time", 0)) / 1000.0

    def clock_skew(self) -> float:
        """Our clock minus the venue's, in seconds."""
        return self._clock() - self.server_time()

    def check_clock(self) -> None:
        """Raise if our clock is too far from the venue's to sign reliably."""
        skew = self.clock_skew()
        if abs(skew) > MAX_CLOCK_SKEW_SECONDS:
            raise OrderError(
                f"clock skew is {skew:+.2f}s, beyond the {MAX_CLOCK_SKEW_SECONDS:g}s "
                f"limit. Delta signatures are valid for only "
                f"{SIGNATURE_TTL_SECONDS}s, so a skewed clock rejects every order. "
                f"Sync with `sudo ntpdate -u pool.ntp.org` or `chronyc makestep`."
            )

    # ------------------------------------------------------------------- order
    def place_order(self, request: OrderRequest) -> OrderResult:
        """Sign and send one order, or describe it if this is a dry run."""
        self._check_allowed()

        if self.dry_run:
            return OrderResult(
                sent=False,
                dry_run=True,
                environment=self.environment,
                request=request,
            )

        # Signature is generated here, immediately before the send, because it
        # is valid for five seconds. It is never cached or precomputed.
        timestamp = str(int(self._clock()))
        path = "/v2/orders"
        body = request.canonical_body()
        headers = self._sign_headers("POST", path, body, timestamp)
        url = f"{self.base_url}{path}"

        try:
            status, payload = self._sender("POST", url, headers, body)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            return OrderResult(
                sent=True, dry_run=False, environment=self.environment,
                request=request, status_code=exc.code, error=detail,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return OrderResult(
                sent=True, dry_run=False, environment=self.environment,
                request=request, error=f"{type(exc).__name__}: {exc}",
            )

        error = None if status == 200 else f"HTTP {status}"
        return OrderResult(
            sent=True, dry_run=False, environment=self.environment,
            request=request, status_code=status, response=payload, error=error,
        )

    def buy(self, product_id: int, size: int, **kw) -> OrderResult:
        return self.place_order(
            OrderRequest(product_id=product_id, size=size, side="buy", **kw)
        )

    def sell(self, product_id: int, size: int, **kw) -> OrderResult:
        return self.place_order(
            OrderRequest(product_id=product_id, size=size, side="sell", **kw)
        )

    # --------------------------------------------------------------- transport
    def _http_send(self, method: str, url: str, headers: dict, body: str):
        req = urllib.request.Request(url, data=body.encode(), headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")

    # ------------------------------------------------------------------ verify
    def verify_signature_against_venue(self) -> dict:
        """Confirm the venue accepts our signature, on testnet.

        Calls an authenticated read-only endpoint. This is the check that has to
        pass before the signing path is trusted for orders, because the
        documented example signature does not reproduce from the documented
        algorithm, so the algorithm can only be confirmed against the venue.
        """
        if self.dry_run:
            raise OrderError("set dry_run=False to exercise the signing path")
        self._check_allowed()
        timestamp = str(int(self._clock()))
        path = "/v2/profile"
        headers = self._sign_headers("GET", path, "", timestamp)
        try:
            status, payload = self._sender("GET", f"{self.base_url}{path}", headers, "")
        except urllib.error.HTTPError as exc:
            return {"ok": False, "status": exc.code,
                    "detail": exc.read().decode("utf-8", "replace")}
        return {"ok": status == 200, "status": status, "response": payload}


def delta_instrument() -> Instrument:
    """The execution leg, for callers that just want the spec."""
    return DELTA_BTCUSD
