"""OKX JSON WebSocket ingestion.

Replaces the Angel One binary protocol entirely. There is no binary decoder
here: OKX sends JSON text frames, so the whole `unpack`/`price_scale`/
`decode_packet` layer is gone.

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
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

from ..core import Side, Trade

#: Public market-data endpoint. No authentication: trade data is public.
PUBLIC_WS = "wss://ws.okx.com:8443/ws/v5/public"
#: Demo/testnet public endpoint.
DEMO_PUBLIC_WS = "wss://wspap.okx.com:8443/ws/v5/public"

TRADES_CHANNEL = "trades"
PING_TEXT = "ping"
PONG_TEXT = "pong"
#: OKX disconnects an idle connection after 30s; ping comfortably inside that.
PING_INTERVAL_SECONDS = 20


class ProtocolError(ValueError):
    """A frame that claims to be a trade but cannot be read as one."""


@dataclass(frozen=True, slots=True)
class TradeEvent:
    """One decoded print, before it becomes a `Trade`."""

    inst_id: str
    trade_id: str
    price: float
    size: float
    side: Side
    ts_ms: int

    @property
    def ts(self) -> float:
        return self.ts_ms / 1000.0

    def to_trade(self) -> Trade:
        return Trade(ts=self.ts, price=self.price, size=self.size, side=self.side)


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


class Transport(Protocol):
    """Anything that can carry frames: a socket, a file, a replay list."""

    is_live: bool

    def connect(self) -> None: ...
    def send(self, data: bytes | str, binary: bool = False) -> None: ...
    def heartbeat(self) -> None: ...
    def recv(self, timeout: float | None = None) -> bytes | str | None: ...
    def close(self) -> None: ...


class ReplayTransport:
    """Replays a fixed list of frames. `None` means the list is exhausted."""

    is_live = False

    def __init__(self, frames: Iterable[bytes | str | None]) -> None:
        self._frames = list(frames)
        self._i = 0
        self.sent: list[bytes | str] = []
        self.heartbeats = 0

    def connect(self) -> None:
        self._i = 0

    def send(self, data: bytes | str, binary: bool = False) -> None:
        self.sent.append(data)

    def heartbeat(self) -> None:
        self.heartbeats += 1

    def recv(self, timeout: float | None = None):
        if self._i >= len(self._frames):
            return None
        f = self._frames[self._i]
        self._i += 1
        return f

    def close(self) -> None:
        pass


class FileTransport:
    """Replays frames recorded to disk, one per line.

    Lines are `txt:<json>` or `bin:<hex>`. Recording a live session once and
    keeping it as a fixture is the cheapest way to make regressions
    reproducible.
    """

    is_live = False

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._frames: list[bytes | str] = []
        self._i = 0
        self.sent: list[bytes | str] = []
        self.heartbeats = 0

    def connect(self) -> None:
        self._frames = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("bin:"):
                self._frames.append(bytes.fromhex(line[4:]))
            elif line.startswith("txt:"):
                self._frames.append(line[4:])
        self._i = 0

    def send(self, data: bytes | str, binary: bool = False) -> None:
        self.sent.append(data)

    def heartbeat(self) -> None:
        self.heartbeats += 1

    def recv(self, timeout: float | None = None):
        if self._i >= len(self._frames):
            return None
        f = self._frames[self._i]
        self._i += 1
        return f

    def close(self) -> None:
        pass


@dataclass(slots=True)
class FeedStats:
    frames: int = 0
    control_frames: int = 0
    trades: int = 0
    duplicates: int = 0
    protocol_errors: int = 0
    unmapped: int = 0
    reconnects: int = 0
    last_error: str | None = None

    def as_dict(self) -> dict:
        return {
            "frames": self.frames,
            "control_frames": self.control_frames,
            "trades": self.trades,
            "duplicates": self.duplicates,
            "protocol_errors": self.protocol_errors,
            "unmapped": self.unmapped,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
        }


class OkxTradeFeed:
    """Drives an injected transport and emits `Trade` objects.

    The transport is injected, never constructed here, so the whole path runs
    offline against a recording or a synthetic frame list.
    """

    def __init__(
        self,
        inst_ids: Iterable[str],
        transport_factory: Callable[[], Transport],
        dedup_window: int = 20_000,
    ) -> None:
        self.inst_ids = list(inst_ids)
        self.transport_factory = transport_factory
        self.dedup_window = dedup_window
        #: Bounded, insertion-ordered set of trade ids already seen.
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.stats = FeedStats()

    # ------------------------------------------------------------------ dedup
    def _is_duplicate(self, key: str) -> bool:
        if key in self._seen:
            return True
        self._seen[key] = None
        while len(self._seen) > self.dedup_window:
            self._seen.popitem(last=False)
        return False

    # ------------------------------------------------------------------- run
    def run(
        self,
        on_trade: Callable[[Trade, TradeEvent], None],
        on_frame: Callable[[object], None] | None = None,
        max_frames: int | None = None,
        max_seconds: float | None = None,
        max_reconnects: int | None = None,
        reconnect_after_seconds: float = 30.0,
    ) -> FeedStats:
        """Consume frames until the transport is exhausted or a bound is hit.

        A bound is not optional in production: a live socket never exhausts, so
        `max_seconds` is what stops a run from running away.

        A live socket also breaks, and a broken one looks exactly like a quiet
        one: `recv` returns nothing either way. The two are told apart here, and
        a drop is reconnected rather than heartbeated forever. `reconnects` in
        the stats counts how often that happened, because a feed that is quietly
        reconnecting every minute is a feed with a problem.
        """
        transport = None
        idle_since: float | None = None
        started = time.monotonic()
        reconnects = 0

        def connect() -> Transport:
            t = self.transport_factory()
            t.connect()
            t.send(subscribe_message(self.inst_ids))
            return t

        def reconnect(reason: str) -> Transport | None:
            nonlocal reconnects
            if max_reconnects is not None and reconnects >= max_reconnects:
                self.stats.last_error = f"reconnect limit reached ({reason})"
                return None
            reconnects += 1
            self.stats.reconnects = reconnects
            self.stats.last_error = reason
            time.sleep(self._backoff(reconnects))
            try:
                return connect()
            except Exception as exc:
                # A venue outage must not take the process down: systemd is not
                # the recovery path for a dropped socket, this loop is.
                self.stats.last_error = f"reconnect failed: {type(exc).__name__}: {exc}"
                return None

        try:
            transport = connect()
            while True:
                if max_frames is not None and self.stats.frames >= max_frames:
                    break
                if max_seconds is not None and time.monotonic() - started > max_seconds:
                    break

                raw = transport.recv(timeout=1.0)

                if raw is None:
                    if not transport.is_live:
                        # A replay is exhausted, and that is the end of the run.
                        break
                    # A live socket is never finished, only quiet or broken.
                    broken = getattr(transport, "dead", False)
                    if broken:
                        reason = "socket dropped: recv failed"
                    elif not self._heartbeat(transport):
                        # The heartbeat already recorded why it failed.
                        reason = self.stats.last_error or "heartbeat failed"
                        broken = True
                    else:
                        # Quiet is normal, so wait. Only silence past the limit
                        # means the socket is half-open: TCP still accepts the
                        # write, so a heartbeat succeeds and proves nothing.
                        if idle_since is None:
                            idle_since = time.monotonic()
                        if time.monotonic() - idle_since > reconnect_after_seconds:
                            broken = True
                            reason = (f"no data for {reconnect_after_seconds:g}s")
                    if not broken:
                        continue
                    idle_since = None
                    transport.close()
                    replacement = reconnect(reason)
                    if replacement is None:
                        break
                    transport = replacement
                    continue

                idle_since = None
                self.stats.frames += 1
                if on_frame is not None:
                    on_frame(raw)

                if isinstance(raw, str) and raw.strip() == PONG_TEXT:
                    self.stats.control_frames += 1
                    continue

                try:
                    events = parse_message(raw)
                except ProtocolError as exc:
                    self.stats.protocol_errors += 1
                    self.stats.last_error = str(exc)
                    continue

                if not events:
                    self.stats.control_frames += 1
                    continue

                for ev in events:
                    if self._is_duplicate(f"{ev.inst_id}:{ev.trade_id}"):
                        self.stats.duplicates += 1
                        continue
                    self.stats.trades += 1
                    on_trade(ev.to_trade(), ev)
        finally:
            if transport is not None:
                transport.close()
        return self.stats

    def _heartbeat(self, transport: Transport) -> bool:
        """Ping the venue. False means the socket is gone, not that it is quiet."""
        try:
            transport.heartbeat()
            return True
        except Exception as exc:
            self.stats.last_error = f"heartbeat failed: {type(exc).__name__}: {exc}"
            return False

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Capped exponential backoff, so a venue outage is not hammered."""
        return min(2.0 ** min(attempt, 5), 30.0)


def record_frames(frames: Iterable[bytes | str], path) -> None:
    """Write frames one per line for `FileTransport`."""
    lines = []
    for f in frames:
        if isinstance(f, (bytes, bytearray)):
            lines.append("bin:" + bytes(f).hex())
        else:
            lines.append("txt:" + str(f))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
