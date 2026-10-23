#!/usr/bin/env bash
#
# One-shot setup for a fresh Debian/Ubuntu VPS that will run this project
# against Delta Exchange India.
#
# Why a dedicated VPS: Delta requires a whitelisted IP for any key that can
# trade, and a home or sandbox IP changes without warning. A VPS gives one
# stable address to whitelist once.
#
# Run as root, from a checkout of this repo:
#
#   sudo ./deploy/vps_setup.sh
#
# It prompts for the credentials and writes them to /etc/okx-feed-delta/env
# with mode 0600; they are never committed. Nothing here places an order.

set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_DIR="/etc/okx-feed-delta"
ENV_FILE="${ENV_DIR}/env"
PY=python3

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"

say "1/6 host identity"
VPS_IP="$(curl -fsS --max-time 10 https://api.ipify.org || true)"
[[ -n "$VPS_IP" ]] || die "could not determine the public IP"
echo "  public IP: ${VPS_IP}"
echo "  Whitelist this in Delta > AlgoHub > API before expecting a 200."

say "2/6 packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip chrony curl ca-certificates

say "3/6 clock"
# A skewed clock makes every signature fail with signature_expired, and the
# symptom looks like a broken key rather than a broken clock. Sync first.
systemctl enable --now chrony >/dev/null 2>&1 || true
chronyc makestep >/dev/null 2>&1 || true
sleep 1
if command -v chronyc >/dev/null 2>&1; then
  chronyc tracking | sed -n '1,3p' | sed 's/^/  /'
fi

say "4/6 credentials"
# Read the values from the terminal rather than from the command line. A
# credential passed as `sudo DELTA_API_KEY=... ./vps_setup.sh` lands in the
# invoking shell's history and in /proc/<pid>/environ, both of which outlive
# the run. Reading from stdin keeps it out of both.
install -d -m 0700 "$ENV_DIR"
umask 077
if [[ -n "${DELTA_API_KEY:-}" && -n "${DELTA_API_SECRET:-}" ]]; then
  echo "  using DELTA_API_KEY / DELTA_API_SECRET from the environment."
else
  printf '  Delta API key:    '; read -rs DELTA_API_KEY; printf '\n'
  printf '  Delta API secret: '; read -rs DELTA_API_SECRET; printf '\n'
fi
if [[ -n "${DELTA_API_KEY:-}" && -n "${DELTA_API_SECRET:-}" ]]; then
  {
    printf 'DELTA_API_KEY=%s\n' "$DELTA_API_KEY"
    printf 'DELTA_API_SECRET=%s\n' "$DELTA_API_SECRET"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  unset DELTA_API_KEY DELTA_API_SECRET
  echo "  wrote ${ENV_FILE} (mode 600)"
else
  echo "  no credentials supplied; skipping."
  echo "  Create the file yourself before running the preflight:"
  echo "    install -d -m 0700 ${ENV_DIR}"
  echo "    printf 'DELTA_API_KEY=%s\\nDELTA_API_SECRET=%s\\n' KEY SECRET > ${ENV_FILE}"
  echo "    chmod 600 ${ENV_FILE}"
fi

say "5/6 virtualenv and dependencies"
if [[ ! -d "${REPO_DIR}/.venv" ]]; then
  "$PY" -m venv "${REPO_DIR}/.venv"
fi
"${REPO_DIR}/.venv/bin/pip" install -q --upgrade pip
"${REPO_DIR}/.venv/bin/pip" install -q -r "${REPO_DIR}/requirements.txt"
echo "  installed into ${REPO_DIR}/.venv"

say "6/6 test suite (offline, no credentials needed)"
( cd "$REPO_DIR" && ./.venv/bin/python -m pytest tests -q 2>&1 | tail -2 | sed 's/^/  /' )

cat <<EOF

Done.

Run the preflight (read-only, places no orders):

  set -a; . ${ENV_FILE}; set +a
  cd ${REPO_DIR}
  ./.venv/bin/python preflight_delta.py --live

Expected once ${VPS_IP} is whitelisted:

  signature      PASS
  clock          PASS
  wallet read    PASS
  execution leg  PASS  id 27 BTCUSD live

A 401 ip_not_whitelisted_for_api_key means the key is fine and the whitelist
step is still pending.
EOF
