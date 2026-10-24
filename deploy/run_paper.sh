#!/usr/bin/env bash
# Run the strategy in paper mode: the signal feed in, the Delta order resolved
# and sized, nothing sent.
#
#   ./deploy/run_paper.sh              # 5 minutes, live host, no orders
#   ./deploy/run_paper.sh 30 --record paper.json
#
# Never passes --execute. Orders are placed only by run_live.sh, which is a
# separate file so that enabling trading is a deliberate act rather than a flag
# appended to this one.
set -euo pipefail

minutes="${1:-5}"
shift || true

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
    --live --minutes "$minutes" --bar-seconds 30 --order-size "${PAPER_ORDER_SIZE:-0.001}" \
    --venue "${SIGNAL_VENUE:-delta}" "$@"
