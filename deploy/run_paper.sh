#!/usr/bin/env bash
# Run the strategy in paper mode: OKX feed in, Delta order resolved and sized,
# nothing sent.
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
env_file="${DELTA_ENV_FILE:-$repo/config/delta.env}"
[[ -f "$env_file" ]] || { echo "no env file at $env_file" >&2; exit 1; }

set -a; . "$env_file"; set +a
export DELTA_ENVIRONMENT=live

exec "$repo/.venv/bin/python" "$repo/live_feed.py" \
    --live --minutes "$minutes" --bar-seconds 30 --order-size "${PAPER_ORDER_SIZE:-0.001}" "$@"
