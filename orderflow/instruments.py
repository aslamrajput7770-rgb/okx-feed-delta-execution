"""Crypto instruments, and the contract-size arithmetic.

The trap this module exists to prevent: **one contract is not one coin.** A
position of "1" on one venue and a position of "1" on another are different
amounts of bitcoin, and a system that copies a size between them is out by a
large factor without any error being raised.

Verified against the live exchange spec (2026-10-22):

    Delta BTCUSD : tick 0.5   1 contract = 0.001 BTC  settles USD
                   (id 27 in production, id 84 on testnet)

So `size` is always in *contracts* and is only meaningful together with the
instrument's `contract_value`. Convert through `contracts_for_base()` rather
than copying a number between instruments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .ticks import TickGrid


class Exchange(str, Enum):
    DELTA = "DELTA"  # Delta Exchange India, the signal source and the venue


class Segment(str, Enum):
    SWAP = "SWAP"  # perpetual future
    SPOT = "SPOT"
    FUT = "FUT"  # dated future
    OPT = "OPT"


class SessionKind(str, Enum):
    CRYPTO_24_7 = "CRYPTO_24_7"  # no session boundary; resets at 00:00 UTC


@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradable contract with its exact tick and contract-size arithmetic."""

    symbol: str
    exchange: Exchange
    segment: Segment
    tick_size: float
    #: Base-currency amount in ONE contract. This is the field that silently
    #: breaks cross-venue sizing when it is assumed to be 1.
    contract_value: float
    settle_ccy: str = "USDT"
    #: Venue-side numeric product id, where the venue uses one.
    #:
    #: **Do not treat this as portable.** Product ids are assigned per
    #: environment: Delta carries the BTCUSD perpetual on id 27 in production
    #: but on id 84 on testnet, where id 27 is an expired March future whose
    #: contract value is 1000x larger. Anything that places an order must call
    #: `resolve_perpetual()` and use the id it returns, not this field.
    venue_id: int | None = None
    session: SessionKind = SessionKind.CRYPTO_24_7
    underlying: str | None = None

    def __post_init__(self) -> None:
        if self.tick_size <= 0:
            raise ValueError("tick_size must be > 0")
        if self.contract_value <= 0:
            raise ValueError("contract_value must be > 0")

    def grid(self, **kwargs) -> TickGrid:
        """A strict tick grid for this instrument."""
        return TickGrid(self.tick_size, **kwargs)

    def base_for_contracts(self, contracts: float) -> float:
        """Base-currency amount for a whole number of contracts."""
        return contracts * self.contract_value

    def contracts_for_base(self, base: float) -> float:
        """Contracts needed to express a base-currency amount.

        Not rounded here: rounding is a venue rule (Delta takes whole contracts)
        and belongs to the execution client, which knows the venue.
        """
        return base / self.contract_value

    @property
    def full_name(self) -> str:
        return f"{self.exchange.value}:{self.symbol}:{self.segment.value}"


# --------------------------------------------------------------------------
# Verified venue specs. Refresh from the venue at startup; these are the
# defaults the tests and the offline path use.
# --------------------------------------------------------------------------
DELTA_BTCUSD = Instrument(
    symbol="BTCUSD",
    exchange=Exchange.DELTA,
    segment=Segment.SWAP,
    tick_size=0.5,
    contract_value=0.001,  # 1 contract = 0.001 BTC
    settle_ccy="USD",
    #: Production id only. Testnet uses 84; resolve at runtime before ordering.
    venue_id=27,
)


class UnknownSymbol(KeyError):
    """Raised when a symbol has no registered instrument."""


@dataclass(slots=True)
class InstrumentMap:
    """Maps venue symbols onto `Instrument`, and finds the hedge leg.

    Keyed on `(exchange, symbol)`. A bare-symbol lookup is a latent collision:
    the same ticker can exist on more than one venue and name different
    contracts with different sizes.
    """

    _by_key: dict[tuple[Exchange, str], Instrument] = field(default_factory=dict)
    _by_symbol: dict[str, Instrument] = field(default_factory=dict)

    def register(self, instrument: Instrument) -> None:
        key = (instrument.exchange, instrument.symbol)
        existing = self._by_key.get(key)
        if existing is not None and existing is not instrument:
            raise ValueError(
                f"{instrument.symbol} on {instrument.exchange.value} already maps to "
                f"{existing.full_name}; refusing to overwrite with {instrument.full_name}"
            )
        self._by_key[key] = instrument
        self._by_symbol.setdefault(instrument.symbol, instrument)

    def by_symbol(self, exchange: Exchange, symbol: str) -> Instrument:
        try:
            return self._by_key[(exchange, symbol)]
        except KeyError:
            raise UnknownSymbol(
                f"no instrument registered for {symbol!r} on {exchange.value}"
            ) from None

    def sole(self, symbol: str) -> Instrument:
        """The one instrument registered under `symbol`, across all venues.

        Raises if two venues both claim the symbol, because silently picking
        one is how a size gets copied between two different contracts.
        """
        matches = [i for (_, s), i in self._by_key.items() if s == symbol]
        if not matches:
            raise UnknownSymbol(f"no instrument registered for symbol {symbol!r}")
        if len(matches) > 1:
            venues = ", ".join(i.full_name for i in matches)
            raise UnknownSymbol(
                f"symbol {symbol!r} is ambiguous across venues ({venues}); "
                f"look it up with by_symbol(exchange, symbol)"
            )
        return matches[0]

    def __len__(self) -> int:
        return len(self._by_key)


def build_default_map() -> InstrumentMap:
    """The verified Delta instrument, ready to use."""
    m = InstrumentMap()
    m.register(DELTA_BTCUSD)
    return m
