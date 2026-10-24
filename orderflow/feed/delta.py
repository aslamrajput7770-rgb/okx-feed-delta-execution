"""Delta Exchange India JSON WebSocket ingestion.

Delta is the venue this system *executes* on. Reading the signal from the same
venue the order goes to removes the basis: an OKX signal filled at Delta carries
whatever spread the two venues have, which is a cost that shows up nowhere in a
signal-quality measurement. Reading and trading one venue makes the backtest
about the strategy instead of about the spread.

Protocol, from the venue's own spec (verified against the live socket):

    -> {"type":"subscribe",
        "payload":{"channels":[{"name":"trades","symbols":["BTCUSD"]}]}}

    <- {"type":"trades","sy":"BTCUSD","p":"72141.5","s":1.0,
        "r":"m","t":1775800366578410,"ts":1775800367003029}

The field that needs care is `r`. It is the **buyer's** role, not the taker's:
`"t"` means the buyer was the taker, so the aggressor was the **buyer**; `"m"`
means the buyer was a maker, so the buyer was passive and the aggressor was the
**seller**. Reading `r` as "the taker's side" inverts every print, which inverts
delta, which inverts every signal - while the footprint still looks perfect.
`tests/test_delta_protocol.py` pins the mapping in both directions.

Two smaller traps, both seen on the live socket:

* `p` arrives as a JSON **string** and is sometimes in exponent form (`"8.1e4"`),
  so it must go through `float()` and never through an integer cast.
* `s` arrives as a JSON number and is a **float** (`2.0`), not an int.

`t` is the trade time in microseconds and is what the engine uses. `ts` is the
server publish time, which is later and would smear every print to the right.
"""

from __future__ import annotations

import json
from typing import Iterable

from ..core import Side
from .base import (  # noqa: F401 - re-exported so one import serves both venues
    FeedStats,
    FileTransport,
    ProtocolError,
    ReplayTransport,
    TradeEvent,
    TradeFeed,
    Transport,
    record_frames,
)

#: Public market-data socket. No authentication: trade data is public.
#:
#: The `trades` channel is *not* on `socket.india.delta.exchange`. Subscribing
#: there returns `{"error":"subscription forbidden on this channel. Use
#: appropriate pod."}` - a message that reads like a permissions problem and is
#: actually the wrong hostname.
PUBLIC_WS = "wss://public-socket.india.delta.exchange"

TRADES_CHANNEL = "trades"
PING_MESSAGE = json.dumps({"type": "ping"})


#: `r` is the BUYER's role. The aggressor is the taker.
#:
#:   r="t" -> buyer was the taker    -> the aggressor bought
#:   r="m" -> buyer was a maker      -> the aggressor sold
_SIDE_BY_BUYER_ROLE = {"t": Side.BUY, "m": Side.SELL}


def side_from_buyer_role(buyer_role: str) -> Side:
    """Map Delta's `r` onto the engine's aggressor side.

    Raises rather than guessing. A new role vocabulary is a real protocol
    change; absorbing it as `UNKNOWN` would silently zero the delta it feeds.
    """
    try:
        return _SIDE_BY_BUYER_ROLE[buyer_role.lower()]
    except (KeyError, AttributeError):
        raise ProtocolError(f"unrecognised buyer role: {buyer_role!r}") from None


def subscribe_message(symbols: Iterable[str]) -> str:
    """The subscribe frame, as Delta expects it: TEXT JSON."""
    return json.dumps(
        {
            "type": "subscribe",
            "payload": {"channels": [{"name": TRADES_CHANNEL, "symbols": list(symbols)}]},
        }
    )


def unsubscribe_message(symbols: Iterable[str]) -> str:
    return json.dumps(
        {
            "type": "unsubscribe",
            "payload": {"channels": [{"name": TRADES_CHANNEL, "symbols": list(symbols)}]},
        }
    )


def parse_message(message: object) -> list[TradeEvent]:
    """Decode one frame into zero or more prints.

    Returns an empty list for control frames: the subscription ack, pongs, and
    the heartbeat replies. Delta sends a subscription ack that carries a
    `channels` list and no trades, which must not be mistaken for data.
    """
    if isinstance(message, (bytes, bytearray)):
        message = message.decode("utf-8")
    if isinstance(message, str):
        text = message.strip()
        if not text:
            return []
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            return []
    if not isinstance(message, dict):
        return []
    if message.get("type") != TRADES_CHANNEL:
        # Subscription acks, errors and pongs all land here. The check is
        # positive - a trade frame says `"type": "trades"` - so an unfamiliar
        # control frame cannot be read as a trade.
        return []

    try:
        inst_id = message["sy"]
        price = float(message["p"])
        size = float(message["s"])
        #: Microseconds since epoch. `ts` is the publish time, which lags; using
        #: it would push every print later than it happened.
        ts_ms = int(message["t"]) // 1000
        trade_id = f"{int(message['t'])}"
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError(f"malformed trade frame: {message!r}") from exc

    if size <= 0:
        # A zero-size print carries no information; dropping it beats raising.
        return []
    if price <= 0:
        raise ProtocolError(f"non-positive price in trade frame: {message!r}")

    return [
        TradeEvent(
            inst_id=inst_id,
            trade_id=trade_id,
            price=price,
            size=size,
            side=side_from_buyer_role(message.get("r")),
            ts_ms=ts_ms,
        )
    ]


class DeltaTradeFeed(TradeFeed):
    """Delta India `trades` channel over an injected transport."""

    def subscribe_message(self, inst_ids: Iterable[str]) -> str:
        return subscribe_message(inst_ids)

    def parse_message(self, message: object) -> list[TradeEvent]:
        return parse_message(message)
