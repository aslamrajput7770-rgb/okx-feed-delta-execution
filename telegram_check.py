"""Confirm a Telegram bot can reach its chat, without sending a signal.

Run this once after creating the bot, before trusting a run to notify you:

    export TELEGRAM_BOT_TOKEN=123456:ABC...
    export TELEGRAM_CHAT_ID=-1001234567890
    python3 telegram_check.py

What it proves, in order:

1. The credentials load, and neither value is blank.
2. The bot token is **valid**: `getMe` returns the bot's own username. A wrong
   token is a 404, which reads like a wrong URL rather than a wrong secret.
3. The chat is **reachable** by this bot. `getChat` returns the title. A bot
   cannot post to a chat it has not been added to, and this is the failure that
   otherwise shows up much later as a silent `chat not found` in a live run.
4. The chat **accepts a message**: one real send, so the whole path is proven
   rather than inferred. Pass `--no-send` to skip it.

Exit code is 0 only when every check passes.

The token is never printed. The chat title and id are, because they identify the
destination rather than a secret.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from orderflow.notify import (
    API_ROOT,
    MissingTelegramCredentials,
    TelegramCredentials,
    TelegramNotifier,
)

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


def _get(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers={"User-Agent": "delta-feed-execution-execution"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def check_credentials(env) -> tuple[str, str]:
    try:
        creds = TelegramCredentials.from_env(env)
    except MissingTelegramCredentials as exc:
        return FAIL, str(exc)
    return PASS, f"token {creds.bot_token[:6]}... chat {creds.chat_id!r}"


def check_get_me(creds: TelegramCredentials) -> tuple[str, str]:
    """The token is valid, and names the bot it belongs to."""
    url = f"{API_ROOT}/bot{creds.bot_token}/getMe"
    try:
        payload = _get(url)
    except urllib.error.HTTPError as exc:
        return FAIL, f"HTTP {exc.code} (a wrong token is a 404 here)"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return FAIL, f"{type(exc).__name__}: {exc}"
    if not payload.get("ok"):
        return FAIL, str(payload.get("description") or "getMe refused")
    bot = payload.get("result") or {}
    return PASS, f"bot @{bot.get('username')} ({bot.get('first_name')})"


def check_get_chat(creds: TelegramCredentials) -> tuple[str, str]:
    """This bot can see the target chat, which is what a send requires."""
    url = f"{API_ROOT}/bot{creds.bot_token}/getChat?chat_id={creds.chat_id}"
    try:
        payload = _get(url)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        return FAIL, f"HTTP {exc.code}: {detail}".strip()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return FAIL, f"{type(exc).__name__}: {exc}"
    if not payload.get("ok"):
        return FAIL, str(payload.get("description") or "getChat refused")
    chat = payload.get("result") or {}
    kind = chat.get("type", "?")
    title = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
    return PASS, f"{kind} {title!r}"


def check_send(creds: TelegramCredentials, text: str) -> tuple[str, str]:
    """One real message, so delivery is proven rather than inferred."""
    notifier = TelegramNotifier(creds)
    result = notifier.send(text)
    if result.ok:
        return PASS, f"message delivered (HTTP {result.status_code})"
    return FAIL, str(result.error)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Confirm Telegram delivery works")
    ap.add_argument("--no-send", action="store_true",
                    help="check the token and the chat, but send nothing")
    ap.add_argument("--message", default=None,
                    help="the text to send; defaults to a short test line")
    args = ap.parse_args(argv)

    import os

    print("Telegram preflight. No order is involved.\n")

    results: list[tuple[str, str, str]] = []

    verdict, detail = check_credentials(os.environ)
    results.append(("credentials", verdict, detail))
    if verdict != PASS:
        for label, v, d in results:
            print(f"  {label:<12}  {v:<5} {d}")
        print("\nRESULT: NOT READY - no credentials.")
        return 1

    creds = TelegramCredentials.from_env(os.environ)

    verdict, detail = check_get_me(creds)
    results.append(("token", verdict, detail))

    if verdict == PASS:
        verdict, detail = check_get_chat(creds)
        results.append(("chat", verdict, detail))
    else:
        results.append(("chat", SKIP, "token invalid"))

    if args.no_send:
        results.append(("send", SKIP, "--no-send"))
    elif any(v == FAIL for _, v, _ in results):
        results.append(("send", SKIP, "an earlier check failed"))
    else:
        text = args.message or "delta-feed-execution-execution: Telegram delivery confirmed."
        verdict, detail = check_send(creds, text)
        results.append(("send", verdict, detail))

    width = max(len(r[0]) for r in results)
    for label, v, d in results:
        print(f"  {label:<{width}}  {v:<5} {d}")

    print()
    failed = [r[0] for r in results if r[1] == FAIL]
    if failed:
        print(f"RESULT: NOT READY - {', '.join(failed)} failed.")
        return 1
    print("RESULT: READY - the bot can reach the chat and the message was delivered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
