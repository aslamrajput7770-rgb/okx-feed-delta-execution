"""Telegram signal delivery: the same signal the runner trades, sent to a chat.

Why this exists
---------------
The engine can compute a signal and act on it, but there is no way to watch what
it *would* have done without a shell open on the host. Paper mode in particular
is only useful if someone sees its output, so this module turns a signal into a
message on a phone.

Design
------
Everything here is a pure formatting function plus one thin HTTP call, for the
same reason `risk.py` is pure: a delivery path that can only be exercised
against the real Telegram API is a delivery path nobody has checked. The
transport is injected, so the whole path runs offline in the tests.

Two rules are load-bearing:

* **A send failure is never an exception.** A signal that is lost because a chat
  was rate-limited must not take down the feed loop that is watching a position.
  `send()` returns a result either way and the caller logs it.
* **The message is built from numbers, never from a template that assumes they
  exist.** A signal with no price, no delta or no position still produces a
  readable line rather than a `KeyError` in the middle of a tick callback.

Credentials
-----------
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, from the environment only, read
through `TelegramCredentials.from_env()` exactly like the Delta pair. Neither is
ever accepted as a constructor literal, and neither appears in a `repr`.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

ENV_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "TELEGRAM_CHAT_ID"

#: Telegram's own limits, from the Bot API docs. The message cap is the one that
#: bites: a long run log appended to a signal exceeds it and the send is refused
#: with a 400 rather than being truncated by the server.
MAX_MESSAGE_CHARS = 4096
#: `sendMessage` rejects anything slower than this on the caller's side.
DEFAULT_TIMEOUT_SECONDS = 10.0

API_ROOT = "https://api.telegram.org"


class MissingTelegramCredentials(RuntimeError):
    """The token or the chat id is absent, so nothing can be delivered."""


@dataclass(frozen=True, slots=True)
class TelegramCredentials:
    """Bot token and target chat, sourced only from the environment."""

    bot_token: str = field(repr=False)
    chat_id: str = field(repr=False)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "TelegramCredentials":
        """Read `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

        Raises rather than falling back to a default: an empty token produces a
        404 from Telegram that reads like a wrong endpoint rather than a missing
        secret.
        """
        source = os.environ if env is None else env
        token = (source.get(ENV_BOT_TOKEN) or "").strip()
        chat = (source.get(ENV_CHAT_ID) or "").strip()
        missing = [
            name
            for name, val in ((ENV_BOT_TOKEN, token), (ENV_CHAT_ID, chat))
            if not val
        ]
        if missing:
            raise MissingTelegramCredentials(
                f"missing {', '.join(missing)} in the environment. Create a bot "
                f"with @BotFather, then set `export {ENV_BOT_TOKEN}=... "
                f"{ENV_CHAT_ID}=...` and never commit them."
            )
        return cls(bot_token=token, chat_id=chat)

    def __repr__(self) -> str:  # pragma: no cover - trivial, but security-relevant
        return f"TelegramCredentials(bot_token='{self.bot_token[:6]}...', chat_id={self.chat_id!r})"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """The outcome of one delivery attempt. Never raises; always answers."""

    ok: bool
    status_code: int | None = None
    error: str | None = None
    skipped: bool = False

    def __bool__(self) -> bool:
        return self.ok


class Notifier(Protocol):
    """Anything that can deliver a message. The feed loop depends only on this."""

    def send(self, text: str) -> DeliveryResult: ...


class NullNotifier:
    """Delivers nothing, successfully. The default, so a run with no chat works."""

    def send(self, text: str) -> DeliveryResult:
        return DeliveryResult(ok=True, skipped=True, error=None)


def _truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Keep a message inside Telegram's cap without losing the head.

    The head carries the signal, so the tail is what goes. The marker makes the
    truncation visible: a silently clipped message reads as a complete one.
    """
    if len(text) <= limit:
        return text
    marker = "\n… (truncated)"
    return text[: limit - len(marker)] + marker


class TelegramNotifier:
    """Posts messages to a chat through the Bot API.

    The opener is injected so tests drive the real formatting and the real
    request-building without touching the network.
    """

    def __init__(
        self,
        credentials: TelegramCredentials,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener=None,
        api_root: str = API_ROOT,
    ) -> None:
        self.credentials = credentials
        self.timeout = timeout
        self.api_root = api_root.rstrip("/")
        self._opener = opener if opener is not None else urllib.request.urlopen

    def _url(self) -> str:
        # The token goes in the path, as the Bot API requires. It is never logged.
        return f"{self.api_root}/bot{self.credentials.bot_token}/sendMessage"

    def build_request(self, text: str) -> urllib.request.Request:
        """The exact request that would be sent. Exposed so it can be asserted."""
        body = urllib.parse.urlencode(
            {
                "chat_id": self.credentials.chat_id,
                "text": _truncate(text),
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        return urllib.request.Request(
            self._url(),
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "delta-feed-execution",
            },
            method="POST",
        )

    def send(self, text: str) -> DeliveryResult:
        """Deliver one message. A failure is returned, never raised."""
        if not text.strip():
            return DeliveryResult(ok=False, error="refusing to send an empty message")
        req = self.build_request(text)
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", 200)
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # pragma: no cover - the error path of the error path
                detail = ""
            return DeliveryResult(ok=False, status_code=exc.code,
                                  error=f"HTTP {exc.code}: {detail}".strip())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return DeliveryResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return DeliveryResult(ok=False, status_code=status,
                                  error="response was not JSON")
        if not payload.get("ok", False):
            return DeliveryResult(ok=False, status_code=status,
                                  error=str(payload.get("description") or "telegram refused"))
        return DeliveryResult(ok=True, status_code=status)


def credentials_or_none(env: dict | None = None) -> TelegramCredentials | None:
    """Read the credentials, or return None when they are absent.

    Used by the runner: notifications are optional, so a missing token means
    "do not notify", not "refuse to run". That is the opposite of the Delta
    credentials, where missing values are fatal, and the difference is
    deliberate.
    """
    try:
        return TelegramCredentials.from_env(env)
    except MissingTelegramCredentials:
        return None


def notifier_from_env(env: dict | None = None, **kw) -> Notifier:
    """A `TelegramNotifier` when configured, otherwise a `NullNotifier`."""
    creds = credentials_or_none(env)
    if creds is None:
        return NullNotifier()
    return TelegramNotifier(creds, **kw)


# --------------------------------------------------------------- formatting


def _money(value: float | None, places: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{places}f}"


def _pct(value: float | None, places: int = 3) -> str:
    return "n/a" if value is None else f"{value:+.{places}f}%"


def format_signal(
    *,
    side: str | None,
    price: float | None,
    cumulative_delta: float | None,
    session_volume: float | None = None,
    bars: int | None = None,
    venue: str = "DELTA BTCUSD",
    execution: str = "Delta India BTCUSD",
    mode: str = "paper",
    extra: str | None = None,
) -> str:
    """Render one signal as a Telegram message.

    Every field is optional. A signal always has a side and a price in practice,
    but a heartbeat or a startup notice reuses this with neither, and a formatter
    that assumes its inputs is a formatter that raises inside a tick callback.
    """
    side_label = {"buy": "BUY / LONG", "sell": "SELL / SHORT"}.get(side or "", "NO SIGNAL")
    arrow = {"buy": "🟢", "sell": "🔴"}.get(side or "", "⚪")

    lines = [
        f"{arrow} <b>{side_label}</b>",
        "",
        f"<b>price</b>       {_money(price)}",
        f"<b>cum delta</b>   {_money(cumulative_delta, 4)}",
    ]
    if session_volume is not None:
        lines.append(f"<b>volume</b>      {_money(session_volume, 4)}")
    if bars is not None:
        lines.append(f"<b>bars</b>        {bars}")
    lines += [
        "",
        f"<i>signal</i>    {venue}",
        f"<i>execution</i> {execution}",
        f"<i>mode</i>      {mode}",
    ]
    if extra:
        lines += ["", extra]
    return "\n".join(lines)


def format_entry(
    *,
    side: str,
    price: float,
    size_base: float,
    contracts: int,
    cumulative_delta: float | None = None,
    mode: str = "paper",
    product: str = "Delta India BTCUSD",
    venue: str = "DELTA BTCUSD",
) -> str:
    """A position was opened (or would have been, in paper mode)."""
    verb = "ENTRY" if mode != "paper" else "ENTRY (paper)"
    return format_signal(
        side=side,
        price=price,
        cumulative_delta=cumulative_delta,
        execution=product,
        mode=mode,
        venue=venue,
        extra=(
            f"<b>{verb}</b>\n"
            f"size      {size_base:g} BTC\n"
            f"contracts {contracts}"
        ),
    )


def format_exit(
    *,
    side: str,
    price: float,
    entry_price: float,
    reason: str,
    pnl_quote: float | None,
    held_seconds: float | None = None,
    mode: str = "paper",
    venue: str = "DELTA BTCUSD",
) -> str:
    """A position was closed (or would have been, in paper mode)."""
    verb = "EXIT" if mode != "paper" else "EXIT (paper)"
    ret = None
    if entry_price:
        ret = (price - entry_price) / entry_price * 100.0
        if side == "sell":
            ret = -ret
    lines = [
        f"<b>{verb}</b>  <i>{reason}</i>",
        "",
        f"<b>side</b>    {side}",
        f"<b>entry</b>   {_money(entry_price)}",
        f"<b>exit</b>    {_money(price)}",
        f"<b>return</b>  {_pct(ret)}",
        f"<b>pnl</b>     {_money(pnl_quote, 4)} quote",
    ]
    if held_seconds is not None:
        lines.append(f"<b>held</b>    {held_seconds:,.0f}s")
    lines += ["", f"<i>venue</i>   {venue}", f"<i>mode</i>    {mode}"]
    return "\n".join(lines)


def format_run_summary(
    *,
    entries: int,
    exits: int,
    realized_quote: float,
    frames: int | None = None,
    trades: int | None = None,
    duplicates: int | None = None,
    protocol_errors: int | None = None,
    halted: bool = False,
    mode: str = "paper",
) -> str:
    """The end-of-run report. Mirrors what the runner prints, on the phone."""
    lines = [
        "<b>RUN FINISHED</b>",
        "",
        f"<b>mode</b>      {mode}",
        f"<b>entries</b>   {entries}",
        f"<b>exits</b>     {exits}",
        f"<b>realized</b>  {_money(realized_quote, 4)} quote",
    ]
    if frames is not None:
        lines.append(f"<b>frames</b>    {frames}")
    if trades is not None:
        lines.append(f"<b>trades</b>    {trades}")
    if duplicates is not None:
        lines.append(f"<b>dups</b>      {duplicates}")
    if protocol_errors is not None:
        lines.append(f"<b>errors</b>    {protocol_errors}")
    if halted:
        lines.append("<b>HALTED</b>    daily loss limit reached")
    return "\n".join(lines)
