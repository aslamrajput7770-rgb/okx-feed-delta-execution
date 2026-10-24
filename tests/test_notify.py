"""Tests for the Telegram notifier: formatting, delivery, and the no-raise rule.

No network. The transport is injected, which is the same reason the risk layer
takes its clock as an argument: a delivery path that is only exercised against
the real API is a delivery path nobody has checked.

The rules that matter here:

* A send failure must never raise. The caller is a tick callback holding a live
  position; an exception would abandon the feed.
* A missing credential must be visible, not silent. `--telegram` with no token
  refuses to start rather than pretending to notify.
* The formatting must survive missing inputs, because a heartbeat reuses it.
"""

from __future__ import annotations

import json

import pytest

from orderflow.notify import (
    MAX_MESSAGE_CHARS,
    MissingTelegramCredentials,
    NullNotifier,
    TelegramCredentials,
    TelegramNotifier,
    credentials_or_none,
    format_entry,
    format_exit,
    format_run_summary,
    format_signal,
    notifier_from_env,
)


class FakeResponse:
    """A minimal stand-in for an `http.client.HTTPResponse`."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ok_opener(seen: list | None = None):
    def opener(req, timeout=None):
        if seen is not None:
            seen.append(req)
        return FakeResponse(json.dumps({"ok": True, "result": {"message_id": 1}}).encode())
    return opener


def refusing_opener(description="chat not found"):
    def opener(req, timeout=None):
        return FakeResponse(json.dumps({"ok": False, "description": description}).encode())
    return opener


# ------------------------------------------------------------------ credentials


class TestCredentials:
    def test_from_env_reads_both(self):
        creds = TelegramCredentials.from_env(
            {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "-100999"}
        )
        assert creds.bot_token == "123:abc"
        assert creds.chat_id == "-100999"

    def test_missing_token_raises(self):
        with pytest.raises(MissingTelegramCredentials):
            TelegramCredentials.from_env({"TELEGRAM_CHAT_ID": "1"})

    def test_missing_chat_raises(self):
        with pytest.raises(MissingTelegramCredentials):
            TelegramCredentials.from_env({"TELEGRAM_BOT_TOKEN": "1"})

    def test_blank_values_are_missing_not_empty_strings(self):
        """A var set to whitespace is a configuration mistake, not a credential."""
        with pytest.raises(MissingTelegramCredentials):
            TelegramCredentials.from_env(
                {"TELEGRAM_BOT_TOKEN": "   ", "TELEGRAM_CHAT_ID": "1"}
            )

    def test_repr_hides_the_token(self):
        creds = TelegramCredentials(bot_token="123456:secret", chat_id="9")
        text = repr(creds)
        assert "secret" not in text
        assert "123456" in text  # the prefix is shown so a wrong token is visible

    def test_credentials_or_none_is_silent_when_absent(self):
        assert credentials_or_none({}) is None

    def test_notifier_from_env_is_null_without_credentials(self):
        assert isinstance(notifier_from_env({}), NullNotifier)

    def test_notifier_from_env_is_real_with_credentials(self):
        n = notifier_from_env(
            {"TELEGRAM_BOT_TOKEN": "1:a", "TELEGRAM_CHAT_ID": "2"}
        )
        assert isinstance(n, TelegramNotifier)


# ------------------------------------------------------------------ null sink


class TestNullNotifier:
    def test_null_notifier_always_succeeds_and_says_it_skipped(self):
        r = NullNotifier().send("anything")
        assert r.ok is True
        assert r.skipped is True


# -------------------------------------------------------------------- request


class TestRequestBuilding:
    def creds(self):
        return TelegramCredentials(bot_token="TOKEN", chat_id="-42")

    def test_request_is_a_post_to_sendmessage(self):
        req = TelegramNotifier(self.creds()).build_request("hi")
        assert req.method == "POST"
        assert "/botTOKEN/sendMessage" in req.full_url

    def test_body_carries_chat_and_text(self):
        req = TelegramNotifier(self.creds()).build_request("hello world")
        body = req.data.decode()
        assert "chat_id=-42" in body
        assert "hello+world" in body

    def test_preview_is_disabled(self):
        """A price in the message would otherwise render as a link preview."""
        req = TelegramNotifier(self.creds()).build_request("hi")
        assert "disable_web_page_preview=true" in req.data.decode()

    def test_long_message_is_truncated_below_the_api_cap(self):
        req = TelegramNotifier(self.creds()).build_request("x" * (MAX_MESSAGE_CHARS + 500))
        text = req.data.decode()
        assert len(text) < MAX_MESSAGE_CHARS + 200
        assert "truncated" in text

    def test_truncation_keeps_the_head(self):
        """The head carries the signal, so the tail is what goes."""
        body = TelegramNotifier(self.creds()).build_request(
            "SIGNAL-AT-THE-TOP " + "y" * MAX_MESSAGE_CHARS
        ).data.decode()
        assert "SIGNAL-AT-THE-TOP" in body


# ------------------------------------------------------------------- delivery


class TestDelivery:
    def creds(self):
        return TelegramCredentials(bot_token="TOKEN", chat_id="-42")

    def test_success_is_reported(self):
        seen: list = []
        n = TelegramNotifier(self.creds(), opener=ok_opener(seen))
        r = n.send("hi")
        assert r.ok and r.status_code == 200
        assert len(seen) == 1

    def test_refusal_is_returned_not_raised(self):
        n = TelegramNotifier(self.creds(), opener=refusing_opener("chat not found"))
        r = n.send("hi")
        assert not r.ok
        assert "chat not found" in (r.error or "")

    def test_empty_message_is_refused_locally(self):
        """An empty send would be a 400 from Telegram; refusing it here is clearer."""
        n = TelegramNotifier(self.creds(), opener=ok_opener())
        r = n.send("   ")
        assert not r.ok
        assert "empty" in (r.error or "")

    def test_http_error_is_returned_not_raised(self):
        import urllib.error

        def opener(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", {}, None
            )

        r = TelegramNotifier(self.creds(), opener=opener).send("hi")
        assert not r.ok and r.status_code == 429

    def test_network_error_is_returned_not_raised(self):
        import urllib.error

        def opener(req, timeout=None):
            raise urllib.error.URLError("name resolution failed")

        r = TelegramNotifier(self.creds(), opener=opener).send("hi")
        assert not r.ok
        assert "URLError" in (r.error or "")

    def test_non_json_response_is_returned_not_raised(self):
        n = TelegramNotifier(self.creds(), opener=lambda req, timeout=None: FakeResponse(b"<html>"))
        r = n.send("hi")
        assert not r.ok
        assert "JSON" in (r.error or "")


# ----------------------------------------------------------------- formatting


class TestFormatting:
    def test_signal_names_the_side_and_the_price(self):
        text = format_signal(side="buy", price=80000.0, cumulative_delta=12.5)
        assert "BUY" in text
        assert "80,000.00" in text
        assert "12.5000" in text

    def test_sell_is_labelled_short(self):
        assert "SHORT" in format_signal(side="sell", price=1.0, cumulative_delta=-1.0)

    def test_no_signal_is_labelled_as_such(self):
        assert "NO SIGNAL" in format_signal(side=None, price=1.0, cumulative_delta=0.0)

    def test_missing_fields_render_as_na_not_a_crash(self):
        """A heartbeat reuses this with no price and no delta."""
        text = format_signal(side=None, price=None, cumulative_delta=None)
        assert "n/a" in text

    def test_no_basis_line_is_rendered(self):
        """There is one venue, so a basis field would always be noise."""
        assert "basis" not in format_signal(side="buy", price=1.0, cumulative_delta=1.0)

    def test_entry_reports_size_and_contracts(self):
        text = format_entry(side="buy", price=80000.0, size_base=0.005, contracts=5)
        assert "0.005 BTC" in text
        assert "contracts 5" in text

    def test_entry_in_paper_mode_says_so(self):
        text = format_entry(side="buy", price=1.0, size_base=0.001, contracts=1, mode="paper")
        assert "paper" in text

    def test_exit_reports_the_reason_and_pnl(self):
        text = format_exit(side="buy", price=79000.0, entry_price=80000.0,
                           reason="stop_loss", pnl_quote=-1.0)
        assert "stop_loss" in text
        assert "-1.0000" in text

    def test_exit_return_is_signed_by_side(self):
        """A short that falls is a gain; the sign must follow the position."""
        long_text = format_exit(side="buy", price=79000.0, entry_price=80000.0,
                                reason="x", pnl_quote=-1.0)
        short_text = format_exit(side="sell", price=79000.0, entry_price=80000.0,
                                 reason="x", pnl_quote=1.0)
        assert "-1.250%" in long_text
        assert "+1.250%" in short_text

    def test_run_summary_carries_the_counts(self):
        text = format_run_summary(entries=3, exits=3, realized_quote=-1.25, frames=600,
                                  trades=600, duplicates=0, protocol_errors=0)
        assert "entries" in text and "3" in text
        assert "-1.2500" in text

    def test_run_summary_flags_a_halt(self):
        text = format_run_summary(entries=1, exits=1, realized_quote=-50.0, halted=True)
        assert "HALTED" in text


class TestTheMessageNamesTheVenue:
    """The notice must say which book the signal came from.

    It is the field a reader uses to decide whether a fill and a signal are the
    same instrument, so it must name Delta and never a venue the system no
    longer reads.
    """

    def test_signal_names_delta(self):
        text = format_signal(side="buy", price=80_000.0, cumulative_delta=12.0)
        assert "DELTA BTCUSD" in text

    def test_a_signal_never_names_another_venue(self):
        """The stale-label regression: the text used to say OKX by default."""
        text = format_signal(side="buy", price=80_000.0, cumulative_delta=12.0)
        assert "OKX" not in text

    def test_entry_carries_the_venue_through(self):
        text = format_entry(side="buy", price=80_000.0, size_base=0.001, contracts=1,
                            cumulative_delta=12.0, venue="DELTA BTCUSD")
        assert "DELTA BTCUSD" in text

    def test_exit_carries_the_venue_through(self):
        text = format_exit(side="buy", price=80_000.0, entry_price=79_000.0,
                           reason="take_profit", pnl_quote=1.0,
                           venue="DELTA BTCUSD")
        assert "DELTA BTCUSD" in text
