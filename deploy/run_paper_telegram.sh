#!/usr/bin/env bash
# Paper trade with signal delivery: OKX feed in, signals to Telegram, nothing
# sent to Delta.
#
#   ./deploy/run_paper_telegram.sh              # 5 minutes
#   ./deploy/run_paper_telegram.sh 30 --telegram-every-signal
#
# This is run_paper.sh plus notifications. It never passes --execute, so no
# order leaves the process; the size is only used to say what would have been
# sent.
#
# Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment. Run
# `python3 telegram_check.py` once first: it proves the bot can reach the chat,
# which is the failure that otherwise shows up as silence hours later.
set -euo pipefail

minutes="${1:-5}"
shift || true

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Sources config/delta.env, then config/delta.local.env, with platform secrets
# winning over both. See deploy/load_env.sh.
. "$repo/deploy/load_env.sh"
export DELTA_ENVIRONMENT=live

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    echo "no Telegram credentials: set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID as" >&2
    echo "platform secrets. Check them with: python3 telegram_check.py" >&2
    exit 1
fi

# The feed needs credentials only when it would resolve the venue product. In
# paper mode nothing is sent, but the product is still resolved by symbol, so a
# missing Delta pair is a warning rather than a refusal.
if [[ -z "${DELTA_API_KEY:-}" || -z "${DELTA_API_SECRET:-}" ]]; then
    echo "note: no Delta credentials; the venue spec will not be resolved" >&2
fi

exec "$repo/.venv/bin/python" "$repo/live_feed.py" \
    --live --minutes "$minutes" --bar-seconds 30 \
    --order-size "${PAPER_ORDER_SIZE:-0.001}" \
    --telegram "$@"
