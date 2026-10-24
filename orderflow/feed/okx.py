"""OKX JSON WebSocket ingestion.

The transport, dedup and reconnect machinery live in `base`; this module is
only the OKX protocol on top of it.

Two things about this feed are worth stating, because both were silent-failure
traps in the feed this replaces:

1. **The side is given, not inferred.** OKX's `trades` channel reports the
   *taker* side of each print. The Angel One feed had no aggressor flag, so the
   old `TickReader` had to guess (quote rule, then tick rule, then UNKNOWN).
   That guessing layer is deleted: `side` maps straight onto `Side`.
   `tests/test_okx_protocol.py` pins this, and `diagnose_okx.py` re-checks it
   from a recording, because an inverted side inverts delta while the
   footprint still looks perfect.

2. **Frames are deduplicated on `tradeId`.** A reconnect can replay trades
   already seen. Without dedup the footprint double-counts volume at the
   reconnect boundary, which is invisible in the output.

Control frames are JSON too, so the "is this a tick?" test cannot be "is it not
a string"; it must positively identify a trade frame.
"""

from __future__ import annotations

import json
from typing import Iterable

from ..core import Side
from .base import (  # noqa: F401 - re-exported for existing importers
    FeedStats,
    FileTransport,
    ProtocolError,
    ReplayTransport,
    TradeEvent,
    TradeFeed,
    Transport,
    record_frames,
)

#: Public market-data endpoint. No authentication: trade data is public.
PUBLIC_WS = "wss://ws.okx.com:8443/ws/v5/public"
#: Demo/testnet public endpoint.
DEMO_PUBLIC_WS = "wss://wspap.okx.com:8443/ws/v5/public"

TRADES_CHANNEL = "trades"
PING_TEXT = "ping"
PONG_TEXT = "pong"
#: OKX disconnects an idle connection after 30s; ping comfortably inside that.
PING_INTERVAL_SECONDS = 20


#: OKX reports the taker side. `buy` = the taker bought = the offer was lifted.
_SIDE_BY_TAKER = {"buy": Side.BUY, "sell": Side.SELL}


def side_from_taker(taker_side: str) -> Side:
    """Map OKX's taker side onto the engine's aggressor side.

    Raises rather than guessing: an unrecognised side becomes an error, not a
    silent `Side.UNKNOWN`, because a new venue vocabulary is a real change and
    must not be absorbed quietly.
    """
    try:
        return _SIDE_BY_TAKER[taker_side.lower()]
    except (KeyError, AttributeError):
        raise ProtocolError(f"unrecognised taker side: {taker_side!r}") from None


def subscribe_message(inst_ids: Iterable[str], channel: str = TRADES_CHANNEL) -> str:
    """The subscribe frame, as OKX expects it: TEXT JSON."""
    args = [{"channel": channel, "instId": i} for i in inst_ids]
    return json.dumps({"op": "subscribe", "args": args})


def unsubscribe_message(inst_ids: Iterable[str], channel: str = TRADES_CHANNEL) -> str:
    args = [{"channel": channel, "instId": i} for i in inst_ids]
    return json.dumps({"op": "unsubscribe", "args": args})


def is_control(message: object) -> bool:
    """True for a frame that carries no trades.

    The test is *positive*: a trade frame is a dict with a `data` list. The
    feed this replaces returned True for every non-binary frame, which on a
    JSON feed would have dropped every single tick.
    """
    if isinstance(message, str):
        return True  # "pong", or an unparseable frame
    if not isinstance(message, dict):
        return True
    return "data" not in message


def parse_message(message: object) -> list[TradeEvent]:
    """Decode one frame into zero or more prints.

    Returns an empty list for control frames and for trade frames whose payload
    is empty (OKX sends those on subscribe).
    """
    if isinstance(message, (bytes, bytearray)):
        message = message.decode("utf-8")
    if isinstance(message, str):
        text = message.strip()
        if text in (PONG_TEXT, PING_TEXT) or not text:
            return []
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            return []
    if is_control(message):
        return []

    arg = message.get("arg") or {}
    if arg.get("channel") != TRADES_CHANNEL:
        return []

    out: list[TradeEvent] = []
    for row in message.get("data") or []:
        try:
            inst_id = row["instId"]
            price = float(row["px"])
            size = float(row["sz"])
            ts_ms = int(row["ts"])
            trade_id = str(row["tradeId"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolError(f"malformed trade row: {row!r}") from exc

        if size <= 0:
            # A zero-size print is not a trade; dropping it beats raising, since
            # it carries no information either way.
            continue
        if price <= 0:
            raise ProtocolError(f"non-positive price in trade row: {row!r}")

        out.append(
            TradeEvent(
                inst_id=inst_id,
                trade_id=trade_id,
                price=price,
                size=size,
                side=side_from_taker(row.get("side")),
                ts_ms=ts_ms,
            )
        )
    return out


class OkxTradeFeed(TradeFeed):
    """OKX `trades` channel over an injected transport."""

    def subscribe_message(self, inst_ids: Iterable[str]) -> str:
        return subscribe_message(inst_ids)

    def parse_message(self, message: object) -> list[TradeEvent]:
        return parse_message(message)
