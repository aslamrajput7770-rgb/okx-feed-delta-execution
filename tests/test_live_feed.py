"""Tests for the live_feed entry point: the guards, and the offline dry run.

No network. The guards are tested through `main(argv)`, which returns before any
I/O when it refuses, and the pure helpers are tested directly. The one test that
would need the venue (`delta_mark_price`) is not exercised here on purpose: it is
a thin fetch, and faking it would test the fake rather than the code.
"""

from __future__ import annotations

import pytest

import live_feed
from live_feed import DRY_RUN_TRADES, dry_run_frames, main, order_side


# ------------------------------------------------------- signal venue choice
class TestTheSignalVenue:
    """Delta is the default, and the choice must actually reach the wiring."""

    def test_delta_is_the_default(self):
        """The venue orders go to is the venue the signal should come from."""
        symbol, instrument, ws, feed_cls = live_feed.VENUE_INSTRUMENTS["delta"]
        assert symbol == "BTCUSD"
        assert instrument is live_feed.DELTA_BTCUSD
        assert "public-socket" in ws

    def test_both_venues_are_available(self):
        assert set(live_feed.VENUE_INSTRUMENTS) == {"delta", "okx"}

    def test_an_unknown_venue_is_refused(self):
        with pytest.raises(SystemExit):
            main(["--dry-run", "--venue", "binance"])

    def test_the_okx_mode_is_still_reachable(self, capsys):
        """Cross-venue mode is kept, not deleted."""
        rc = main(["--dry-run", "--venue", "okx", "--order-size", "0.001"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "would trade" in out

    def test_the_delta_dry_run_decodes_to_trades(self, capsys):
        """A dry run in the wrong frame shape reports a healthy feed and no
        trades. Delta is the default, so its tape must actually decode."""
        rc = main(["--dry-run", "--order-size", "0.001"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "trades=600" in out


# ------------------------------------------------------------------ dry run
class TestDryRun:
    def test_dry_run_is_offline_and_terminates(self, capsys):
        """The bug this pins: --dry-run used to open a real socket and hang.

        With no --minutes it now returns immediately, and it must not print the
        live endpoint, because it never connects to one.
        """
        rc = main(["--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "no socket" in out
        assert "wss://" not in out

    def test_dry_run_reports_an_order_without_sending_it(self, capsys):
        rc = main(["--dry-run", "--order-size", "0.001"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "would trade" in out
        assert "No request sent" in out
        assert "risk         :" in out

    def test_dry_run_frames_are_deterministic(self):
        a = dry_run_frames("BTCUSD")
        b = dry_run_frames("BTCUSD")
        assert a == b
        assert len(a) == DRY_RUN_TRADES

    def test_dry_run_tape_is_two_sided(self):
        """A one-sided tape would make the side mapping look correct by accident."""
        import json

        # `r` is the buyer's role, so both roles must appear.
        roles = {json.loads(f)["r"] for f in dry_run_frames("BTCUSD")}
        assert roles == {"t", "m"}

    def test_the_dry_run_tape_decodes_with_the_venue_it_claims(self):
        """The synthetic frames must be readable by that venue's own decoder.

        A dry run in the wrong frame shape reports a healthy feed and zero
        trades, which is the worst possible output from an offline check.
        """
        from orderflow.feed import DeltaTradeFeed, OkxTradeFeed
        from orderflow.feed.base import ReplayTransport

        for venue, inst, cls in (("delta", "BTCUSD", DeltaTradeFeed),
                                 ("okx", "BTC-USDT-SWAP", OkxTradeFeed)):
            feed = cls([inst], lambda v=venue, i=inst: ReplayTransport(
                dry_run_frames(i, n=10, venue=v)))
            stats = feed.run(lambda *_: None)
            assert stats.trades == 10, f"{venue} dry-run tape decoded to {stats.trades}"
            assert stats.protocol_errors == 0

    def test_dry_run_and_execute_are_contradictory(self, capsys):
        rc = main(["--dry-run", "--execute", "--order-size", "0.001"])
        assert rc == 2
        assert "contradictory" in capsys.readouterr().err


class TestReplayIsOffline:
    def test_replay_needs_no_bound_and_touches_no_socket(self, capsys, tmp_path):
        """A capture is an offline path, so it needs no --minutes and no network.

        The basis check must not run here: a capture holds historical prices, so
        comparing them to today's mark price would measure elapsed time.
        """
        from orderflow.feed import record_frames

        cap = tmp_path / "cap.txt"
        record_frames(dry_run_frames("BTC-USDT-SWAP", n=200), cap)

        rc = main(["--replay", str(cap), "--order-size", "0.001"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "replay of" in out
        assert "wss://" not in out
        assert "basis" not in out


# --------------------------------------------------------------- live bound
class TestLiveRunNeedsABound:
    def test_unbounded_live_run_is_refused(self, capsys):
        """`--minutes 0` on a live socket never returns. That is the hang."""
        rc = main([])
        assert rc == 2
        assert "--minutes" in capsys.readouterr().err


# ------------------------------------------------------------ mainnet guard
class TestMainnetConfirmation:
    def test_execute_live_without_confirmation_is_refused(self, capsys):
        rc = main(["--minutes", "1", "--execute", "--live", "--order-size", "0.001"])
        assert rc == 2
        assert "i-know-this-is-live" in capsys.readouterr().err

    def test_execute_needs_a_size(self, capsys):
        rc = main(["--minutes", "1", "--execute"])
        assert rc == 2
        assert "--order-size" in capsys.readouterr().err


# ------------------------------------------------------------------- signal
class TestOrderSide:
    def test_positive_delta_is_a_buy(self):
        assert order_side(5.0) == "buy"

    def test_negative_delta_is_a_sell(self):
        assert order_side(-3.0) == "sell"

    def test_zero_delta_is_no_signal_not_a_sell(self):
        """The bug this pins: zero used to fall through to "sell"."""
        assert order_side(0.0) is None


# -------------------------------------------------------------------- basis
class TestBasis:
    def test_premium_is_positive(self):
        assert live_feed.basis_pct(100.0, 101.0) == pytest.approx(1.0)

    def test_discount_is_negative(self):
        assert live_feed.basis_pct(100.0, 99.0) == pytest.approx(-1.0)

    def test_zero_okx_price_is_refused(self):
        with pytest.raises(ValueError):
            live_feed.basis_pct(0.0, 100.0)
