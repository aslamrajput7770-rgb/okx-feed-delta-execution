"""Tests for the OKX JSON feed. Real code paths, no mocks, no network.

The two things these tests exist to pin are the two that were silent failures
in the feed this replaces: the taker-side mapping (an inversion inverts delta
while the footprint still looks perfect) and the dedup on `tradeId`.
"""

from __future__ import annotations

import json

import pytest

from orderflow import MarketCalendar, SessionChart, Side, TickGrid
from orderflow.feed.okx import parse_message
from orderflow.feed import (
    FileTransport,
    OkxTradeFeed,
    ProtocolError,
    ReplayTransport,
    is_control,
    record_frames,
    side_from_taker,
    subscribe_message,
)


def trade_frame(px, sz, side, trade_id, ts_ms=1792574700000, inst="BTC-USDT-SWAP"):
    return json.dumps(
        {
            "arg": {"channel": "trades", "instId": inst},
            "data": [
                {
                    "instId": inst,
                    "tradeId": str(trade_id),
                    "px": str(px),
                    "sz": str(sz),
                    "side": side,
                    "ts": str(ts_ms),
                }
            ],
        }
    )


# --------------------------------------------------------------------- side
class TestTakerSideMapping:
    def test_buy_taker_is_buy(self):
        """OKX `buy` = the taker bought = the offer was lifted = Side.BUY."""
        assert side_from_taker("buy") is Side.BUY

    def test_sell_taker_is_sell(self):
        assert side_from_taker("sell") is Side.SELL

    def test_case_insensitive(self):
        assert side_from_taker("BUY") is Side.BUY
        assert side_from_taker("Sell") is Side.SELL

    def test_unknown_side_raises_not_silently_unknown(self):
        """A new venue vocabulary must be an error, not a quiet UNKNOWN."""
        with pytest.raises(ProtocolError):
            side_from_taker("unknown")
        with pytest.raises(ProtocolError):
            side_from_taker(None)

    def test_buy_taker_print_lands_on_the_ask(self):
        """The sign of delta depends on this and nothing else."""
        (ev,) = parse_message(trade_frame(24000.0, 5, "buy", 1))
        assert ev.side is Side.BUY
        assert ev.to_trade().side is Side.BUY

    def test_delta_sign_follows_the_mapping(self):
        """A buy print must make bar delta positive, a sell print negative.

        This is the assertion that catches an inverted side. An inverted feed
        still produces a plausible-looking footprint; only the sign is wrong.
        """
        cal = MarketCalendar()
        chart = SessionChart(TickGrid(0.1), cal, bar_seconds=3600)

        chart.add(parse_message(trade_frame(24000.0, 5, "buy", 1))[0].to_trade())
        assert chart.current.delta > 0, "buy taker must add positive delta"

        chart.add(parse_message(trade_frame(24000.0, 3, "sell", 2))[0].to_trade())
        assert chart.current.delta == pytest.approx(2.0)  # 5 - 3

    def test_sell_only_bar_has_negative_delta(self):
        cal = MarketCalendar()
        chart = SessionChart(TickGrid(0.1), cal, bar_seconds=3600)
        chart.add(parse_message(trade_frame(24000.0, 7, "sell", 1))[0].to_trade())
        assert chart.current.delta == pytest.approx(-7.0)


# ------------------------------------------------------------------- control
class TestControlFrames:
    def test_pong_string_is_control(self):
        assert parse_message("pong") == []
        assert is_control("pong") is True

    def test_subscribe_ack_is_control(self):
        """OKX acks a subscribe with an event dict, not a string."""
        ack = json.dumps({"event": "subscribe", "arg": {"channel": "trades"}})
        # A str frame cannot be positively identified as a trade frame, so it is
        # treated as control. `parse_message` then yields nothing from it.
        assert is_control(ack) is True
        assert parse_message(ack) == []

    def test_subscribe_ack_as_dict_is_control(self):
        assert is_control({"event": "subscribe"}) is True
        assert parse_message({"event": "subscribe"}) == []

    def test_empty_data_list_yields_nothing(self):
        frame = json.dumps({"arg": {"channel": "trades"}, "data": []})
        assert parse_message(frame) == []

    def test_other_channel_is_ignored(self):
        frame = json.dumps(
            {"arg": {"channel": "books5"}, "data": [{"px": "1", "sz": "1"}]}
        )
        assert parse_message(frame) == []

    def test_non_json_text_is_ignored(self):
        assert parse_message("not json at all") == []

    def test_a_trade_frame_is_not_control(self):
        """The positive test: a dict with `data` is a trade frame."""
        frame = json.loads(trade_frame(24000.0, 1, "buy", 1))
        assert is_control(frame) is False


# -------------------------------------------------------------------- decode
class TestParseMessage:
    def test_fields_map_across(self):
        (ev,) = parse_message(trade_frame(24000.5, 2, "buy", 42, ts_ms=1792574700000))
        assert ev.inst_id == "BTC-USDT-SWAP"
        assert ev.trade_id == "42"
        assert ev.price == pytest.approx(24000.5)
        assert ev.size == pytest.approx(2.0)
        assert ev.ts == pytest.approx(1792574700.0)

    def test_multiple_rows_in_one_frame(self):
        frame = json.dumps(
            {
                "arg": {"channel": "trades"},
                "data": [
                    {"instId": "BTC-USDT-SWAP", "tradeId": "1", "px": "100.0",
                     "sz": "1", "side": "buy", "ts": "1000"},
                    {"instId": "BTC-USDT-SWAP", "tradeId": "2", "px": "100.5",
                     "sz": "2", "side": "sell", "ts": "1001"},
                ],
            }
        )
        events = parse_message(frame)
        assert len(events) == 2
        assert [e.side for e in events] == [Side.BUY, Side.SELL]

    def test_zero_size_row_is_dropped_not_raised(self):
        frame = json.dumps(
            {
                "arg": {"channel": "trades"},
                "data": [{"instId": "X", "tradeId": "1", "px": "100.0",
                          "sz": "0", "side": "buy", "ts": "1"}],
            }
        )
        assert parse_message(frame) == []

    def test_malformed_row_raises_protocol_error(self):
        frame = json.dumps({"arg": {"channel": "trades"}, "data": [{"px": "100"}]})
        with pytest.raises(ProtocolError):
            parse_message(frame)

    def test_negative_price_raises(self):
        frame = json.dumps(
            {
                "arg": {"channel": "trades"},
                "data": [{"instId": "X", "tradeId": "1", "px": "-1",
                          "sz": "1", "side": "buy", "ts": "1"}],
            }
        )
        with pytest.raises(ProtocolError):
            parse_message(frame)

    def test_bytes_frame_decodes(self):
        raw = trade_frame(24000.0, 1, "buy", 1).encode()
        (ev,) = parse_message(raw)
        assert ev.size == pytest.approx(1.0)


# --------------------------------------------------------------------- dedup
class TestDedup:
    def _feed(self, frames):
        return OkxTradeFeed(["BTC-USDT-SWAP"], lambda: ReplayTransport(frames))

    def test_duplicate_trade_id_counted_once(self):
        """A reconnect replays trades; without dedup volume is double-counted."""
        frames = [trade_frame(24000.0, 5, "buy", 999), trade_frame(24000.0, 5, "buy", 999)]
        seen = []
        stats = self._feed(frames).run(lambda t, e: seen.append(t))
        assert len(seen) == 1
        assert stats.trades == 1
        assert stats.duplicates == 1

    def test_distinct_ids_both_counted(self):
        frames = [trade_frame(24000.0, 5, "buy", 1), trade_frame(24000.0, 5, "buy", 2)]
        seen = []
        stats = self._feed(frames).run(lambda t, e: seen.append(t))
        assert len(seen) == 2
        assert stats.duplicates == 0

    def test_same_id_on_two_instruments_is_not_a_duplicate(self):
        """The dedup key is (instId, tradeId), not tradeId alone."""
        frames = [
            trade_frame(24000.0, 5, "buy", 1, inst="BTC-USDT-SWAP"),
            trade_frame(2000.0, 5, "buy", 1, inst="ETH-USDT-SWAP"),
        ]
        seen = []
        self._feed(frames).run(lambda t, e: seen.append(t))
        assert len(seen) == 2

    def test_dedup_window_is_bounded(self):
        feed = OkxTradeFeed(["X"], lambda: ReplayTransport([]), dedup_window=3)
        for i in range(10):
            feed._is_duplicate(f"X:{i}")
        assert len(feed._seen) == 3

    def test_evicted_id_is_accepted_again(self):
        """Documented behaviour of a bounded window: very old ids may repeat."""
        feed = OkxTradeFeed(["X"], lambda: ReplayTransport([]), dedup_window=2)
        assert feed._is_duplicate("X:1") is False
        feed._is_duplicate("X:2")
        feed._is_duplicate("X:3")  # evicts X:1
        assert feed._is_duplicate("X:1") is False


# ------------------------------------------------------------------ end to end
class TestEndToEnd:
    def test_frames_to_footprint(self):
        """Frames in, footprint out, through the real pipeline."""
        frames = [
            json.dumps({"event": "subscribe"}),  # ack
            trade_frame(24000.0, 5, "buy", 1),
            trade_frame(24000.5, 2, "sell", 2),
            "pong",
            trade_frame(24000.0, 3, "buy", 3),
        ]
        cal = MarketCalendar()
        chart = SessionChart(TickGrid(0.1), cal, bar_seconds=3600)
        feed = OkxTradeFeed(["BTC-USDT-SWAP"], lambda: ReplayTransport(frames))
        stats = feed.run(lambda t, e: chart.add(t))

        assert stats.trades == 3
        assert stats.protocol_errors == 0
        assert stats.control_frames == 2  # the subscribe ack and the "pong"
        assert chart.session_volume == pytest.approx(10.0)
        assert chart.cumulative_delta == pytest.approx(5 - 2 + 3)

    def test_subscribe_is_sent_as_text(self):
        transport = ReplayTransport([])
        feed = OkxTradeFeed(["BTC-USDT-SWAP"], lambda: transport)
        feed.run(lambda t, e: None)
        assert len(transport.sent) == 1
        sent = json.loads(transport.sent[0])
        assert sent["op"] == "subscribe"
        assert sent["args"] == [{"channel": "trades", "instId": "BTC-USDT-SWAP"}]

    def test_subscribe_message_shape(self):
        msg = json.loads(subscribe_message(["A", "B"]))
        assert msg["op"] == "subscribe"
        assert len(msg["args"]) == 2

    def test_protocol_error_is_counted_not_raised(self):
        bad = json.dumps({"arg": {"channel": "trades"}, "data": [{"px": "1"}]})
        feed = OkxTradeFeed(["X"], lambda: ReplayTransport([bad]))
        stats = feed.run(lambda t, e: None)
        assert stats.protocol_errors == 1
        assert stats.last_error is not None
        assert stats.trades == 0

    def test_replay_ends_at_exhaustion(self):
        """`recv() is None` on a replay means finished, not idle."""
        feed = OkxTradeFeed(["X"], lambda: ReplayTransport([trade_frame(1.0, 1, "buy", 1)]))
        stats = feed.run(lambda t, e: None)
        assert stats.frames == 1

    def test_max_frames_bounds_the_run(self):
        frames = [trade_frame(24000.0, 1, "buy", i) for i in range(50)]
        feed = OkxTradeFeed(["X"], lambda: ReplayTransport(frames))
        stats = feed.run(lambda t, e: None, max_frames=10)
        assert stats.frames == 10

    def test_recording_round_trips_through_file_transport(self, tmp_path):
        frames = [trade_frame(24000.0, 5, "buy", 1), "pong",
                  trade_frame(24000.5, 2, "sell", 2)]
        path = tmp_path / "rec.txt"
        record_frames(frames, path)

        cal = MarketCalendar()
        chart = SessionChart(TickGrid(0.1), cal, bar_seconds=3600)
        feed = OkxTradeFeed(["BTC-USDT-SWAP"], lambda: FileTransport(path))
        stats = feed.run(lambda t, e: chart.add(t))

        assert stats.trades == 2
        assert chart.cumulative_delta == pytest.approx(3.0)
