"""Resolve a Delta product at runtime, instead of trusting a hardcoded id.

The bug this exists to prevent, found by `preflight_delta.py`:

    PRODUCTION  id 27 -> BTCUSD,        perpetual, 1 contract = 0.001 BTC
    TESTNET     id 27 -> BTCUSD_27Mar,  expired future, 1 contract = 1 BTC
    TESTNET     id 84 -> BTCUSD,        perpetual, 1 contract = 0.001 BTC

**Product ids are not portable between environments.** The testnet contract
sitting on id 27 is an expired March future whose contract value is 1 BTC, a
thousand times the perpetual the sizing layer assumes. A system that hardcodes
`product_id=27` and is pointed at testnet will sign a perfectly valid order for
a thousand times the intended size, and nothing in the response says so.

So the product is resolved from its **symbol** against the host being used, and
the returned spec is checked against what the engine expects. A mismatch is an
error, not a warning: the contract value feeds every size calculation, and a
wrong one is silent until the P&L does not match.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ..instruments import Instrument


class ProductResolutionError(RuntimeError):
    """The venue has no usable product matching the request."""


@dataclass(frozen=True, slots=True)
class ResolvedProduct:
    """A product as the venue reports it, on the host it was read from."""

    venue_id: int
    symbol: str
    contract_type: str
    tick_size: float
    contract_value: float
    state: str

    def as_instrument(self, template: Instrument) -> Instrument:
        """An `Instrument` built from the venue's live spec.

        The venue is the source of truth for tick and contract value, so the
        template contributes only identity (exchange, segment, settle ccy).
        """
        return Instrument(
            symbol=self.symbol,
            exchange=template.exchange,
            segment=template.segment,
            tick_size=self.tick_size,
            contract_value=self.contract_value,
            settle_ccy=template.settle_ccy,
            venue_id=self.venue_id,
            underlying=template.underlying,
        )

    def diff_from(self, template: Instrument) -> list[str]:
        """Every way this product differs from what the engine expects."""
        return self.sizing_problems(template) + self.grid_problems(template)

    def sizing_problems(self, template: Instrument) -> list[str]:
        """Differences that make every position the wrong size.

        These are money-critical: `contract_value` multiplies the order size,
        and a wrong one produces an order that fills at the wrong exposure with
        no error anywhere.
        """
        problems = []
        if self.state != "live":
            problems.append(f"state is {self.state!r}, not 'live'")
        if self.contract_type not in ("perpetual_futures", "futures", "spot"):
            problems.append(f"contract_type is {self.contract_type!r}")
        if self.contract_value != template.contract_value:
            factor = (
                self.contract_value / template.contract_value
                if template.contract_value
                else float("inf")
            )
            problems.append(
                f"contract_value is {self.contract_value}, engine assumes "
                f"{template.contract_value} -> every size would be out by {factor:g}x"
            )
        return problems

    def grid_problems(self, template: Instrument) -> list[str]:
        """Differences that move prices onto a different grid.

        Not money-critical, but not nothing: a tick of 0.1 where the engine
        assumes 0.5 changes which prices are representable, so bar boundaries
        and the footprint grid shift.
        """
        if self.tick_size != template.tick_size:
            return [
                f"tick_size is {self.tick_size}, engine assumes {template.tick_size}"
            ]
        return []


def fetch_products(base_url: str, symbol: str | None = None, timeout: float = 10.0):
    """GET /v2/products, filtered by symbol.

    The venue ignores a `?symbol=` query parameter (checked: it returns all
    ~1000 products regardless), so the filter is applied here. Trusting the
    server-side filter would silently return every product and make an exact
    symbol lookup look ambiguous.
    """
    url = f"{base_url}/v2/products"
    req = urllib.request.Request(url, headers={"User-Agent": "delta-feed-execution"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise ProductResolutionError(
            f"GET {url} -> {exc.code}: {exc.read().decode('utf-8', 'replace')}"
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ProductResolutionError(f"GET {url} failed: {exc}") from exc

    rows = payload.get("result") or []
    if symbol is not None:
        rows = [r for r in rows if r.get("symbol") == symbol]
    return rows


def resolve_perpetual(
    base_url: str,
    symbol: str,
    template: Instrument,
    timeout: float = 10.0,
    strict: bool = True,
    fetcher=None,
) -> tuple[ResolvedProduct, Instrument]:
    """Find the live perpetual for `symbol` on this host, and verify it.

    Returns the resolved product and an `Instrument` built from the venue's own
    numbers. With `strict=True` (the default) a spec mismatch raises, because a
    wrong `contract_value` is a thousand-fold sizing error that raises nothing
    downstream.

    `fetcher` is injectable so the resolution rules are tested offline against
    recorded venue payloads rather than only against the live venue.
    """
    rows = (fetcher or fetch_products)(base_url, symbol=symbol, timeout=timeout)
    if not rows:
        raise ProductResolutionError(
            f"no product named {symbol!r} on {base_url}. The symbol may differ "
            f"between environments; list the products to find the right one."
        )

    live_perps = [
        r
        for r in rows
        if r.get("contract_type") == "perpetual_futures" and r.get("state") == "live"
    ]
    if not live_perps:
        available = ", ".join(
            f"id={r.get('id')} {r.get('contract_type')} {r.get('state')}" for r in rows
        )
        raise ProductResolutionError(
            f"{symbol!r} on {base_url} has no live perpetual. Found: {available}. "
            f"Refusing to fall back to an expired or dated contract: on testnet "
            f"the id that carries the perpetual in production carries an expired "
            f"future here, with a 1000x different contract value."
        )

    if len(live_perps) > 1:
        ids = ", ".join(str(r.get("id")) for r in live_perps[:5])
        more = f" (+{len(live_perps) - 5} more)" if len(live_perps) > 5 else ""
        raise ProductResolutionError(
            f"{symbol!r} is ambiguous on {base_url}: ids {ids}{more} are all live "
            f"perpetuals. Pick one explicitly rather than letting the first win."
        )

    row = live_perps[0]
    product = ResolvedProduct(
        venue_id=int(row["id"]),
        symbol=str(row["symbol"]),
        contract_type=str(row["contract_type"]),
        tick_size=float(row["tick_size"]),
        contract_value=float(row["contract_value"]),
        state=str(row["state"]),
    )

    problems = product.sizing_problems(template)
    if problems and strict:
        raise ProductResolutionError(
            f"{symbol!r} on {base_url} (id {product.venue_id}) does not match the "
            f"engine's spec: " + "; ".join(problems)
        )
    return product, product.as_instrument(template)
