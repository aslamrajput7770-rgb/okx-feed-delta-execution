"""Data ingestion: public trade prints over JSON WebSocket.

    bytes/text -> <venue>.parse_message   decode, no I/O
               -> <venue>TradeFeed        dedup, routing, heartbeat, bounds
               -> SessionChart            footprint, delta, profile, daily reset

Two venues are supported, and the choice matters:

* `delta` - Delta Exchange India. This is the venue orders are placed on, so
  reading the signal here means the signal and the fill are the same price.
* `okx`   - OKX. A global venue with far more prints per minute. Reading here
  and trading at Delta introduces the basis as a real cost.

`base` holds the transport, dedup and reconnect machinery both share. The
transport is injected, so the whole path runs offline.
"""

from .base import (
    FeedStats,
    FileTransport,
    ProtocolError,
    ReplayTransport,
    TradeEvent,
    TradeFeed,
    Transport,
    record_frames,
)
from .delta import (
    PUBLIC_WS as DELTA_PUBLIC_WS,
    DeltaTradeFeed,
)
from .okx import (
    DEMO_PUBLIC_WS,
    PING_INTERVAL_SECONDS,
    PING_TEXT,
    PONG_TEXT,
    PUBLIC_WS,
    TRADES_CHANNEL,
    OkxTradeFeed,
    is_control,
    side_from_taker,
    subscribe_message,
    unsubscribe_message,
)

__all__ = [
    # shared machinery
    "TradeFeed",
    "TradeEvent",
    "Transport",
    "ReplayTransport",
    "FileTransport",
    "record_frames",
    "FeedStats",
    "ProtocolError",
    # delta
    "DeltaTradeFeed",
    "DELTA_PUBLIC_WS",
    # okx
    "PUBLIC_WS",
    "DEMO_PUBLIC_WS",
    "TRADES_CHANNEL",
    "PING_TEXT",
    "PONG_TEXT",
    "PING_INTERVAL_SECONDS",
    "side_from_taker",
    "subscribe_message",
    "unsubscribe_message",
    "is_control",
    "OkxTradeFeed",
]
