"""Outbound notifications: signal delivery to a chat.

    telegram : bot-token delivery, pure formatting, injectable transport

The notifier is optional everywhere it is used. A run with no Telegram
credentials is a normal run, not a broken one, so `notifier_from_env()` returns
a `NullNotifier` rather than raising.
"""

from .telegram import (
    API_ROOT,
    DEFAULT_TIMEOUT_SECONDS,
    ENV_BOT_TOKEN,
    ENV_CHAT_ID,
    MAX_MESSAGE_CHARS,
    DeliveryResult,
    MissingTelegramCredentials,
    Notifier,
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

__all__ = [
    "ENV_BOT_TOKEN",
    "ENV_CHAT_ID",
    "API_ROOT",
    "MAX_MESSAGE_CHARS",
    "DEFAULT_TIMEOUT_SECONDS",
    "MissingTelegramCredentials",
    "TelegramCredentials",
    "DeliveryResult",
    "Notifier",
    "NullNotifier",
    "TelegramNotifier",
    "credentials_or_none",
    "notifier_from_env",
    "format_signal",
    "format_entry",
    "format_exit",
    "format_run_summary",
]
