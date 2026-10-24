"""The Delta feed must decode the side correctly, and nothing else may drift.

The side is the whole point. Delta's `r` field is the *buyer's* role, not the
taker's: `"t"` means the buyer was the taker, so the aggressor bought. Reading
it as "the taker's side" inverts every print, which inverts cumulative delta,
which inverts every signal - and the footprint still looks perfect while it
happens. So the mapping is pinned here in both directions, against frames
captured from the live socket.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orderflow.core import Side  # noqa: E402
from orderflow.feed import DELTA_PUBLIC_WS, DeltaTradeFeed  # noqa: E402
from orderflow.feed.base import ProtocolError, ReplayTransport  # noqa: E402
from orderflow.feed.delta import (  # noqa: E402
    parse_message,
    side_from_buyer_role,
    subscribe_message,
)


def frame(**over) -> str:
    """A trade frame shaped exactly like the live socket's."""
    d = {
        "type": "trades",
        "sy": "BTCUSD",
        "p": "81000.5",
        "s": 12.0,
        "r": "t",
        "t": 1792800729508629,
        "ts": 1792800730003122,
    }
    d.update(over)
    return json.dumps(d)


class TestTheSideMapping:
    def test_buyer_as_taker_is_a_buy_aggressor(self):
        """`r="t"` -> the buyer crossed the spread -> the aggressor bought."""
        assert side_from_buyer_role("t") is Side.BUY

    def test_buyer_as_maker_is_a_sell_aggressor(self):
        """`r="m"` -> the buyer was passive -> the seller crossed -> sell."""
        assert side_from_buyer_role("m") is Side.SELL

    def test_the_mapping_is_not_inverted(self):
        """The exact mistake this module exists to prevent, stated as a test."""
        assert side_from_buyer_role("t") is not Side.SELL
        assert side_from_buyer_role("m") is not Side.BUY

    def test_case_does_not_matter(self):
        assert side_from_buyer_role("T") is Side.BUY
        assert side_from_buyer_role("M") is Side.SELL

    def test_an_unknown_role_is_an_error_not_unknown(self):
        """A new vocabulary is a protocol change, not a quiet zero."""
        with pytest.raises(ProtocolError):
            side_from_buyer_role("x")
        with pytest.raises(ProtocolError):
            side_from_buyer_role(None)

    def test_an_unknown_role_in_a_frame_raises(self):
        with pytest.raises(ProtocolError):
            parse_message(frame(r="sideways"))


class TestTheFrameFields:
    def test_a_captured_frame_decodes(self):
        ev = parse_message(frame())[0]
        assert ev.inst_id == "BTCUSD"
        assert ev.price == 81000.5
        assert ev.size == 12.0
        assert ev.side is Side.BUY

    def test_the_trade_time_is_used_not_the_publish_time(self):
        """`t` is when it traded; `ts` is when the server published it.

        Using `ts` would smear every print to the right and, on a replay, make
        the tape disagree with the session it recorded.
        """
        ev = parse_message(frame(t=1_700_000_000_000_000, ts=1_700_000_999_000_000))[0]
        assert ev.ts_ms == 1_700_000_000_000  # microseconds -> milliseconds
        assert ev.ts == 1_700_000_000.0

    def test_the_trade_id_is_the_trade_time(self):
        """Delta sends no trade id; the timestamp is the only stable key.

        Dedup keys on it, so it must be present and must not be the publish
        time, which repeats across prints published in the same millisecond.
        """
        ev = parse_message(frame(t=1792800729508629))[0]
        assert ev.trade_id == "1792800729508629"

    def test_a_price_in_exponent_form_decodes(self):
        """The live socket really does send `"8.1e4"`. An int cast would break."""
        ev = parse_message(frame(p="8.1e4"))[0]
        assert ev.price == 81000.0

    def test_a_float_size_decodes(self):
        """`s` is a JSON number and arrives as a float, not an int."""
        ev = parse_message(frame(s=2.0))[0]
        assert ev.size == 2.0

    def test_a_string_size_decodes(self):
        ev = parse_message(frame(s="3.5"))[0]
        assert ev.size == 3.5

    def test_a_zero_size_print_is_dropped(self):
        assert parse_message(frame(s=0)) == []

    def test_a_non_positive_price_raises(self):
        with pytest.raises(ProtocolError):
            parse_message(frame(p="0"))

    def test_a_missing_field_raises(self):
        with pytest.raises(ProtocolError):
            parse_message(json.dumps({"type": "trades", "sy": "BTCUSD"}))


class TestControlFrames:
    def test_the_subscription_ack_is_not_a_trade(self):
        """The ack carries `channels` and no `type: trades`, and must be ignored."""
        ack = json.dumps({"channels": [{"name": "trades", "symbols": ["BTCUSD"]}],
                          "type": "subscriptions"})
        assert parse_message(ack) == []

    def test_a_subscription_error_is_not_a_trade(self):
        """The wrong-pod error must not be read as data."""
        err = json.dumps({"channels": [{"error": "subscription forbidden on this channel. "
                                                 "Use appropriate pod.", "name": "trades"}],
                          "type": "subscriptions"})
        assert parse_message(err) == []

    def test_a_pong_is_not_a_trade(self):
        assert parse_message(json.dumps({"type": "pong"})) == []

    def test_an_unparseable_frame_is_not_a_trade(self):
        assert parse_message("not json at all") == []

    def test_a_non_dict_frame_is_not_a_trade(self):
        assert parse_message(json.dumps([1, 2, 3])) == []

    def test_bytes_frames_decode(self):
        assert len(parse_message(frame().encode())) == 1


class TestSubscribeFrame:
    def test_the_subscribe_shape_matches_the_venue_spec(self):
        d = json.loads(subscribe_message(["BTCUSD"]))
        assert d["type"] == "subscribe"
        assert d["payload"]["channels"] == [{"name": "trades", "symbols": ["BTCUSD"]}]

    def test_the_public_socket_is_the_one_with_the_trades_channel(self):
        """Not `socket.india.delta.exchange`: that one refuses the channel."""
        assert DELTA_PUBLIC_WS == "wss://public-socket.india.delta.exchange"
        assert "public-socket" in DELTA_PUBLIC_WS


class TestTheFeedLoop:
    def test_the_feed_drives_the_callback_once_per_print(self):
        frames = [subscribe_ack(), frame(r="t"), frame(r="m", t=1792800729508630)]
        feed = DeltaTradeFeed(["BTCUSD"], lambda: ReplayTransport(frames))
        got = []
        stats = feed.run(lambda trade, ev: got.append((trade.side, ev.price)))
        assert stats.trades == 2
        assert got == [(Side.BUY, 81000.5), (Side.SELL, 81000.5)]

    def test_a_repeated_frame_is_deduplicated(self):
        """A reconnect replays prints; the footprint must not double-count."""
        f = frame()
        feed = DeltaTradeFeed(["BTCUSD"], lambda: ReplayTransport([f, f, f]))
        stats = feed.run(lambda *_: None)
        assert stats.trades == 1
        assert stats.duplicates == 2

    def test_the_ack_does_not_count_as_a_trade(self):
        feed = DeltaTradeFeed(["BTCUSD"], lambda: ReplayTransport([subscribe_ack()]))
        stats = feed.run(lambda *_: None)
        assert stats.trades == 0
        assert stats.control_frames == 1

    def test_a_malformed_frame_does_not_stop_the_run(self):
        """One bad frame is counted and skipped; the feed keeps going."""
        frames = [json.dumps({"type": "trades", "sy": "BTCUSD"}), frame()]
        feed = DeltaTradeFeed(["BTCUSD"], lambda: ReplayTransport(frames))
        stats = feed.run(lambda *_: None)
        assert stats.protocol_errors == 1
        assert stats.trades == 1

    def test_a_delta_feed_is_a_trade_feed(self):
        from orderflow.feed import TradeFeed

        assert issubclass(DeltaTradeFeed, TradeFeed)


def subscribe_ack() -> str:
    return json.dumps({"channels": [{"name": "trades", "symbols": ["BTCUSD"]}],
                       "type": "subscriptions"})
