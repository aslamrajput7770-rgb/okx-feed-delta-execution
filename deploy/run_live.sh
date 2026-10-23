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
env_file="${DELTA_ENV_FILE:-$repo/config/delta.env}"
[[ -f "$env_file" ]] || { echo "no env file at $env_file" >&2; exit 1; }

set -a; . "$env_file"; set +a
export DELTA_ENVIRONMENT=live

exec "$repo/.venv/bin/python" "$repo/live_feed.py" \
    --live --minutes "$minutes" --bar-seconds 30 \
    --order-size "$DELTA_ORDER_SIZE" --execute
