"""Data ingestion: Delta Exchange India trade prints over JSON WebSocket.

    bytes/text -> delta.parse_message   decode, no I/O
               -> DeltaTradeFeed        dedup, routing, heartbeat, bounds
               -> SessionChart          footprint, delta, profile, daily reset

Delta is the only source, and deliberately so: it is the venue orders are placed
on, so the signal price and the fill price are the same price and there is no
basis between two books to account for.

`base` holds the transport, dedup and reconnect machinery. The transport is
injected, so the whole path runs offline.
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
]
