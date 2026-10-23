#!/usr/bin/env bash
# Place real orders. Separated from run_paper.sh on purpose: turning on trading
# should take a distinct command, not a flag on the habit you already have.
#
#   ./deploy/run_live.sh 5            # 5 minutes, orders enabled
#
# Requires DELTA_ORDER_SIZE to be set explicitly. There is no default, so a
# missing value is an error rather than an accidental order.
set -euo pipefail

minutes="${1:-0}"
[[ "$minutes" =~ ^[0-9]+$ ]] || { echo "usage: run_live.sh <minutes>" >&2; exit 2; }
[[ -n "${DELTA_ORDER_SIZE:-}" ]] || { echo "set DELTA_ORDER_SIZE explicitly" >&2; exit 2; }

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Sources config/delta.env, then config/delta.local.env, with platform secrets
# winning over both. See deploy/load_env.sh.
. "$repo/deploy/load_env.sh"
export DELTA_ENVIRONMENT=live

if [[ -z "${DELTA_API_KEY:-}" || -z "${DELTA_API_SECRET:-}" ]]; then
    echo "no Delta credentials: set DELTA_API_KEY / DELTA_API_SECRET as platform" >&2
    echo "secrets, or fill in config/delta.local.env (git-ignored)." >&2
    exit 1
fi

exec "$repo/.venv/bin/python" "$repo/live_feed.py" \
    --live --minutes "$minutes" --bar-seconds 30 \
    --order-size "$DELTA_ORDER_SIZE" --execute
