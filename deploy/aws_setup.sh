#!/usr/bin/env bash
# Install this project as a systemd service on an EC2 instance.
#
# Run as root, from a checkout of the repo, on Amazon Linux 2023 or Debian/
# Ubuntu:
#
#   sudo ./deploy/aws_setup.sh
#
# Idempotent: re-running it updates the code, the unit and the environment
# without losing state/.
#
# What this does NOT do, deliberately:
#   * it does not create the EC2 instance, the security group or the key pair.
#     Those belong in the account's own IaC, and a script that creates
#     infrastructure is a script that can delete it.
#   * it does not put credentials on the command line. They are read from the
#     terminal, so they never reach shell history or /proc/<pid>/environ.
#   * it does not enable real trading. SERVICE_MODE=paper is the installed
#     default; switching to live is a separate, explicit edit.
set -euo pipefail

REPO_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="/opt/delta-feed-execution"
ENV_DIR="/etc/delta-feed-execution"
SERVICE_USER="deltafeed"
UNIT="/etc/systemd/system/delta-feed-execution.service"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# Run a command as the service user. setpriv is in util-linux, which both
# Amazon Linux 2023 and Debian/Ubuntu ship; runuser is the fallback.
as_service_user() {
    if command -v setpriv >/dev/null 2>&1; then
        setpriv --reuid "$SERVICE_USER" --regid "$SERVICE_USER" --clear-groups "$@"
    else
        runuser -u "$SERVICE_USER" -- "$@"
    fi
}

[[ $EUID -eq 0 ]] || die "run as root (sudo)"

say "1/8 host identity and clock"
VPS_IP="$(curl -fsS --max-time 10 https://api.ipify.org || true)"
echo "  public IP: ${VPS_IP:-unknown}"
echo "  Whitelist this in Delta > AlgoHub > API before expecting a 200."
# Every signed request carries a timestamp, so a skewed clock fails as
# signature_expired, which reads like a bad key. Sync before anything else.
if command -v dnf >/dev/null 2>&1; then
    dnf install -y -q chrony >/dev/null 2>&1 || true
else
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq chrony ca-certificates curl >/dev/null 2>&1 || true
fi
systemctl enable --now chronyd >/dev/null 2>&1 || systemctl enable --now chrony >/dev/null 2>&1 || true
chronyc makestep >/dev/null 2>&1 || true
chronyc tracking 2>/dev/null | sed -n '1,3p' | sed 's/^/  /' || true

say "2/8 service user"
# A service account with no shell and no home: the process needs to read the
# checkout and write state/, and nothing else.
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "  created ${SERVICE_USER}"
else
    echo "  ${SERVICE_USER} already exists"
fi

say "3/8 Python 3.10+"
# 3.10 is the floor: dataclass(slots=True) is a syntax error before it, and
# Amazon Linux 2023 ships 3.9.
PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1 &&
       "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        PY="$(command -v "$cand")"; break
    fi
done
if [[ -z "$PY" ]]; then
    if command -v dnf >/dev/null 2>&1; then
        dnf install -y -q python3.11 python3.11-pip
        PY=/usr/bin/python3.11
    else
        apt-get install -y -qq python3 python3-venv python3-pip
        PY="$(command -v python3)"
    fi
fi
echo "  interpreter: $PY ($("$PY" --version 2>&1))"

say "4/8 install the code to ${APP_DIR}"
install -d -m 0755 "$APP_DIR"
# rsync keeps state/ on the host: a redeploy must not reset the daily loss
# counter, because that is the one file whose loss changes trading behaviour.
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
        --exclude '.git' --exclude '.venv' --exclude 'state' \
        --exclude '__pycache__' --exclude '.pytest_cache' \
        "$REPO_SRC/" "$APP_DIR/"
else
    ( cd "$REPO_SRC" && tar --exclude=.git --exclude=.venv --exclude=state \
        --exclude=__pycache__ --exclude=.pytest_cache -cf - . ) | tar -xf - -C "$APP_DIR"
fi
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP_DIR/state"
chown -R root:root "$APP_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/state"
# Normalise the modes. rsync -a and tar -p both preserve the source modes, so a
# checkout that was copied from somewhere with a restrictive umask can carry a
# mode-600 source file into the install tree - and the service user then cannot
# read it. The ownership is already correct; this makes the modes agree with it.
# Directories need the execute bit to be traversable at all.
find "$APP_DIR" -path "$APP_DIR/.venv" -prune -o -path "$APP_DIR/state" -prune -o \
    -type d -exec chmod 0755 {} +
find "$APP_DIR" -path "$APP_DIR/.venv" -prune -o -path "$APP_DIR/state" -prune -o \
    -type f -exec chmod 0644 {} +
find "$APP_DIR/deploy" -name '*.sh' -type f -exec chmod 0755 {} +
echo "  installed; state/ owned by ${SERVICE_USER}"

say "5/8 virtualenv and dependencies"
[[ -d "$APP_DIR/.venv" ]] || "$PY" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
echo "  $("$APP_DIR/.venv/bin/python" --version)"

say "6/8 test suite (offline, no credentials)"
# Run as the service user, not as root. A test that writes a file must create it
# with the ownership the service will have; run as root it creates root-owned
# files in the install tree, and the service then fails on its first run with a
# PermissionError on its own daily counter. Running here also proves the service
# user can read the checkout and write state/, which is the whole permission
# story for this unit.
#
# The install tree belongs to root, so the bytecode cache, pytest's cache and
# its tmp_path root all go somewhere the service user can write; the code itself
# stays read-only to it. --basetemp is not optional: pytest refuses to reuse a
# /tmp/pytest-of-root left by an earlier run as a different user.
PYCACHE="$(mktemp -d)"
chown "$SERVICE_USER:$SERVICE_USER" "$PYCACHE"
( cd "$APP_DIR" && as_service_user env PYTHONPYCACHEPREFIX="$PYCACHE" \
    ./.venv/bin/python -m pytest tests -q -p no:cacheprovider \
        --basetemp="$PYCACHE/pytest" 2>&1 | tail -2 | sed 's/^/  /' )
rm -rf "$PYCACHE"

# The daily counter is the one file whose loss changes trading behaviour, and it
# is written by the service user at runtime. Prove the directory is writable
# before the unit is installed, rather than discovering it from a crash loop.
if ! as_service_user test -w "$APP_DIR/state"; then
    die "${SERVICE_USER} cannot write ${APP_DIR}/state; the service would fail on its first run"
fi
echo "  state/ is writable by ${SERVICE_USER}"

say "7/8 credentials and mode"
install -d -m 0700 "$ENV_DIR"
umask 077
ENV_FILE="${ENV_DIR}/env"
SVC_FILE="${ENV_DIR}/service.env"

if [[ -f "$ENV_FILE" ]]; then
    echo "  ${ENV_FILE} exists; leaving it alone."
    echo "  To change a value:  sudoedit ${ENV_FILE}"
else
    echo "  Delta credentials (input hidden; blank to skip)."
    printf '  DELTA_API_KEY:    '; read -rs DELTA_API_KEY || true; printf '\n'
    printf '  DELTA_API_SECRET: '; read -rs DELTA_API_SECRET || true; printf '\n'
    printf '  TELEGRAM_BOT_TOKEN: '; read -rs TELEGRAM_BOT_TOKEN || true; printf '\n'
    printf '  TELEGRAM_CHAT_ID:   '; read -r  TELEGRAM_CHAT_ID || true; printf '\n'
    {
        printf 'DELTA_ENVIRONMENT=live\n'
        [[ -n "${DELTA_API_KEY:-}"    ]] && printf 'DELTA_API_KEY=%s\n' "$DELTA_API_KEY"
        [[ -n "${DELTA_API_SECRET:-}" ]] && printf 'DELTA_API_SECRET=%s\n' "$DELTA_API_SECRET"
        [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] && printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TELEGRAM_BOT_TOKEN"
        [[ -n "${TELEGRAM_CHAT_ID:-}"   ]] && printf 'TELEGRAM_CHAT_ID=%s\n' "$TELEGRAM_CHAT_ID"
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    unset DELTA_API_KEY DELTA_API_SECRET TELEGRAM_BOT_TOKEN
    echo "  wrote ${ENV_FILE} (mode 600)"
fi

if [[ -f "$SVC_FILE" ]]; then
    echo "  ${SVC_FILE} exists; leaving it alone."
else
    cat > "$SVC_FILE" <<'SVCEOF'
# Behaviour, not credentials. Edit this to change how the service runs.
#
# paper: live feed, signals to Telegram, no order is sent. This is the default
#        and the safe one; leave it here until the paper runs look right.
# live:  real orders. Also requires DELTA_ORDER_SIZE and
#        I_KNOW_THIS_IS_LIVE=1 below, so it takes three edits, not one.
SERVICE_MODE=paper
SERVICE_MINUTES=60
BAR_SECONDS=30
PAPER_ORDER_SIZE=0.001
# TELEGRAM_EVERY_SIGNAL=1
# MAX_RECONNECTS=10
#
# For SERVICE_MODE=live, uncomment both:
# DELTA_ORDER_SIZE=0.001
# I_KNOW_THIS_IS_LIVE=1
SVCEOF
    chmod 600 "$SVC_FILE"
    echo "  wrote ${SVC_FILE} (mode 600, SERVICE_MODE=paper)"
fi

say "8/8 systemd unit"
install -m 0644 "$APP_DIR/deploy/systemd/delta-feed-execution.service" "$UNIT"
systemctl daemon-reload
systemctl enable delta-feed-execution >/dev/null 2>&1 || true
echo "  installed and enabled (not started)"

cat <<EOF

Done.

Before starting, prove the two things that fail silently:

  set -a; . ${ENV_FILE}; set +a
  cd ${APP_DIR}
  ./.venv/bin/python preflight_delta.py --live     # venue reachable, IP whitelisted
  ./.venv/bin/python telegram_check.py             # the bot can reach the chat

Then start it:

  sudo systemctl start delta-feed-execution
  journalctl -u delta-feed-execution -f

It runs SERVICE_MODE=paper for SERVICE_MINUTES at a time and restarts itself.
To stop it cleanly (this closes any open position and saves the daily counter):

  sudo systemctl stop delta-feed-execution

To go live, edit ${SVC_FILE} and set SERVICE_MODE=live with the two
acknowledgements, then: sudo systemctl restart delta-feed-execution
EOF
