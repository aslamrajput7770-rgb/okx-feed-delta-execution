#!/usr/bin/env bash
# The systemd entry point. One bounded run, then exit; systemd starts the next.
#
# Why bounded and not `--minutes 0`:
#   * the daily loss counter is per-day, and a process that never restarts would
#     have to notice midnight itself. A restart does it for free.
#   * a crash loop and a healthy restart look the same from the outside only if
#     the runs are bounded and the exit is clean. `Restart=always` plus a clean
#     exit is what makes `systemctl status` readable.
#   * the engine refuses `--minutes 0` on a live socket on purpose, and that
#     guard is not weakened for the service.
#
# Mode is chosen by SERVICE_MODE in /etc/okx-feed-delta/service.env:
#   paper  (default) live feed, signals to Telegram, nothing sent to Delta
#   live             real orders; also needs DELTA_ORDER_SIZE and the ack below
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

# Sources config/delta.env, then config/delta.local.env, with platform secrets
# winning over both. Under systemd the values come from EnvironmentFile.
. "$repo/deploy/load_env.sh"

mode="${SERVICE_MODE:-paper}"
minutes="${SERVICE_MINUTES:-60}"
order_size="${PAPER_ORDER_SIZE:-0.001}"

# Telegram is the point of running this as a service: nobody is watching a
# shell. A missing pair is a refusal, not a silent run.
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    echo "no Telegram credentials in the environment; refusing to run unseen." >&2
    echo "Set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in /etc/okx-feed-delta/env." >&2
    exit 1
fi

args=(--live --minutes "$minutes" --bar-seconds "${BAR_SECONDS:-30}"
      --order-size "$order_size" --telegram)

if [[ "${TELEGRAM_EVERY_SIGNAL:-0}" == "1" ]]; then
    args+=(--telegram-every-signal)
fi

if [[ -n "${MAX_RECONNECTS:-}" ]]; then
    args+=(--max-reconnects "$MAX_RECONNECTS")
fi

case "$mode" in
    paper)
        echo "mode=paper: orders are reported, never sent"
        ;;
    live)
        # The same two gates as run_live.sh, because a service file is just
        # another way to type the command and must not be the way around them.
        [[ -n "${DELTA_ORDER_SIZE:-}" ]] || {
            echo "SERVICE_MODE=live needs DELTA_ORDER_SIZE explicitly" >&2; exit 2; }
        [[ "${I_KNOW_THIS_IS_LIVE:-0}" == "1" ]] || {
            echo "SERVICE_MODE=live needs I_KNOW_THIS_IS_LIVE=1" >&2; exit 2; }
        args+=(--execute --i-know-this-is-live)
        echo "mode=live: REAL ORDERS ENABLED"
        ;;
    *)
        echo "SERVICE_MODE must be paper or live, got '$mode'" >&2
        exit 2
        ;;
esac

exec "$repo/.venv/bin/python" "$repo/live_feed.py" "${args[@]}"
