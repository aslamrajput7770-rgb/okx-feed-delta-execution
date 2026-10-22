"""Data ingestion: OKX public trades over JSON WebSocket.

    bytes/text -> okx.parse_message   decode, no I/O
               -> OkxTradeFeed        dedup, routing, heartbeat, bounds
               -> SessionChart        footprint, delta, profile, daily reset

The transport is injected, so the whole path runs offline.
"""

from .okx import (
    DEMO_PUBLIC_WS,
    PING_INTERVAL_SECONDS,
    PING_TEXT,
    PONG_TEXT,
    PUBLIC_WS,
    TRADES_CHANNEL,
    FeedStats,
    FileTransport,
    OkxTradeFeed,
    ProtocolError,
    ReplayTransport,
    TradeEvent,
    Transport,
    is_control,
    parse_message,
    record_frames,
    side_from_taker,
    subscribe_message,
    unsubscribe_message,
)

__all__ = [
    "PUBLIC_WS",
    "DEMO_PUBLIC_WS",
    "TRADES_CHANNEL",
    "PING_TEXT",
    "PONG_TEXT",
    "PING_INTERVAL_SECONDS",
    "ProtocolError",
    "TradeEvent",
    "side_from_taker",
    "subscribe_message",
    "unsubscribe_message",
    "is_control",
    "parse_message",
    "Transport",
    "ReplayTransport",
    "FileTransport",
    "record_frames",
    "FeedStats",
    "OkxTradeFeed",
]
