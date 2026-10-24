"""Position sizing.

The bug this module exists to prevent, stated plainly: **one contract is not
one coin.**

    Delta BTCUSD (id 27): 1 contract = 0.001 BTC

A system that decides "buy 1 BTC" and then sends "size = 1" has sent 0.001 BTC,
not 1 BTC. Nothing raises. The order fills, the position looks smaller than
intended, and the only symptom is a P&L that does not match the backtest. This
is the same class of silent error as a missing price-scale division in a tick
feed.

So a size is never a bare number here. It is a `SizedOrder`: a whole number of
*contracts* on a named venue, produced by converting a base-currency exposure
through that venue's `contract_value` and rounding to that venue's lot step.

Rounding is deliberately **down**, toward zero. Rounding up can only ever make
the position larger than the risk model allowed, and an under-hedged leg is a
recoverable problem while an over-sized leg is not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from ..instruments import Instrument


class SizingError(ValueError):
    """The requested exposure cannot be expressed on this venue."""


@dataclass(frozen=True, slots=True)
class VenueRules:
    """The lot arithmetic a venue applies to an order size.

    `lot_step` is in *contracts*: Delta accepts whole contracts.
    `min_contracts` is the smallest order the venue will accept at all.
    """

    lot_step: float
    min_contracts: float

    def __post_init__(self) -> None:
        if self.lot_step <= 0:
            raise ValueError("lot_step must be > 0")
        if self.min_contracts <= 0:
            raise ValueError("min_contracts must be > 0")


#: Verified from the live venue spec (2026-10-22).
DELTA_PERP_RULES = VenueRules(lot_step=1.0, min_contracts=1.0)


@dataclass(frozen=True, slots=True)
class SizedOrder:
    """A size expressed in the units the venue actually accepts."""

    instrument: Instrument
    contracts: float
    #: The base-currency exposure this order really represents.
    base: float
    #: True when rounding moved the size away from the requested exposure.
    rounded: bool

    def __str__(self) -> str:
        coin = self.instrument.underlying or "base"
        return (
            f"{self.instrument.exchange.value}:{self.instrument.symbol} "
            f"{self.contracts:g} contracts = {self.base:g} {coin}"
        )


def _floor_to_step(value: float, step: float) -> float:
    """Round `value` down to a whole multiple of `step`.

    Done in Decimal: `0.1 // 0.01` on floats is 9.0, not 10.0, and a sizing
    layer that silently loses a lot is exactly the failure this module exists
    to prevent.
    """
    if step <= 0:
        raise ValueError("step must be > 0")
    v = Decimal(str(value))
    s = Decimal(str(step))
    n = (v / s).to_integral_value(rounding="ROUND_FLOOR")
    return float(n * s)


def contracts_for_base(
    instrument: Instrument,
    base_amount: float,
    rules: VenueRules,
) -> SizedOrder:
    """How many contracts on `instrument` represent `base_amount` of the coin.

    Raises if the exposure cannot be expressed: a size that rounds to zero is a
    silent no-op otherwise, and "the order did not go" is worth an exception.
    """
    if base_amount <= 0:
        raise SizingError(f"base_amount must be > 0, got {base_amount}")

    raw = instrument.contracts_for_base(base_amount)
    stepped = _floor_to_step(raw, rules.lot_step)

    if stepped < rules.min_contracts:
        raise SizingError(
            f"{base_amount:g} base is {raw:.6g} contracts on "
            f"{instrument.exchange.value}:{instrument.symbol}, below the venue "
            f"minimum of {rules.min_contracts:g} contracts "
            f"(1 contract = {instrument.contract_value:g} base). "
            f"Increase the size or trade a smaller-contract venue."
        )

    actual_base = instrument.base_for_contracts(stepped)
    return SizedOrder(
        instrument=instrument,
        contracts=stepped,
        base=actual_base,
        rounded=not math.isclose(stepped, raw, rel_tol=0, abs_tol=1e-12),
    )


def rebalance(
    signal_instrument: Instrument,
    exec_instrument: Instrument,
    base_amount: float,
    signal_rules: VenueRules,
    exec_rules: VenueRules,
) -> tuple[SizedOrder, SizedOrder]:
    """Size the same exposure on the signal venue and the execution venue.

    Returns both orders so a caller can compare them. The pair is the point:
    the two `contracts` numbers are *different* and neither is interchangeable
    with the other, which is the fact a naive implementation gets wrong.
    """
    return (
        contracts_for_base(signal_instrument, base_amount, signal_rules),
        contracts_for_base(exec_instrument, base_amount, exec_rules),
    )
