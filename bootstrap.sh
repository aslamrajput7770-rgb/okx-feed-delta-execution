#!/usr/bin/env bash
# Prepare this project on a machine that has nothing but a shell.
#
#   ./bootstrap.sh              # set up, test, and check the venue
#   ./bootstrap.sh --no-venue   # set up and test only, skip the network checks
#
# Safe to re-run. Installs a Python 3.10+ interpreter if the system one is too
# old (Amazon Linux 2023 ships 3.9, and dataclass(slots=True) is a syntax error
# there), creates .venv, installs requirements, runs the offline suite, and then
# runs the read-only venue preflight. It places no orders.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo"

say() { printf '\n== %s\n' "$*"; }
warn() { printf '   %s\n' "$*" >&2; }

skip_venue=0
[[ "${1:-}" == "--no-venue" ]] && skip_venue=1

# ------------------------------------------------------------------ interpreter
# 3.10 is the floor: dataclass(slots=True) appears in the engine.
py=""
for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1 &&
       "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        py="$(command -v "$cand")"
        break
    fi
done

if [[ -z "$py" ]]; then
    say "no Python 3.10+ found; installing one"
    if command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3.14 python3.14-pip python3.14-devel
        py=/usr/bin/python3.14
    elif command -v apt-get >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        sudo apt-get update -qq
        sudo apt-get install -y -qq python3 python3-venv python3-pip
        py="$(command -v python3)"
    else
        echo "cannot install Python automatically; install 3.10+ and re-run" >&2
        exit 1
    fi
fi

say "interpreter: $py ($("$py" --version 2>&1))"

# ------------------------------------------------------------------------ venv
say "virtualenv and dependencies"
# `make` is only needed for the Makefile wrappers, but installing it here keeps
# `make help` working on a fresh host instead of failing with command not found.
if ! command -v make >/dev/null 2>&1; then
    if command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y -q make >/dev/null 2>&1 || true
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get install -y -qq make >/dev/null 2>&1 || true
    fi
fi
[[ -d .venv ]] || "$py" -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
echo "   $("./.venv/bin/python" --version)"

# ----------------------------------------------------------------------- tests
say "test suite (offline, no credentials)"
./.venv/bin/python -m pytest tests -q

# -------------------------------------------------------------------- venue
if [[ "$skip_venue" -eq 0 ]]; then
    say "venue preflight (read-only, no orders)"
    if [[ -f config/delta.env ]]; then
        set -a; . config/delta.env; set +a
    fi
    if [[ -z "${DELTA_API_KEY:-}" || -z "${DELTA_API_SECRET:-}" ]]; then
        warn "DELTA_API_KEY / DELTA_API_SECRET are not set."
        warn "Set them as environment secrets, or fill in config/delta.env,"
        warn "then run: ./.venv/bin/python preflight_delta.py --live"
    else
        # The key is IP-locked. Any other address answers
        # ip_not_whitelisted_for_api_key and names the offending client_ip.
        ./.venv/bin/python preflight_delta.py --live || true
    fi
fi

cat <<'EOF'

Ready.

  ./deploy/run_paper.sh 5          # live feed, order reported, nothing sent
  DELTA_ORDER_SIZE=0.001 ./deploy/run_live.sh 5    # real orders

Read AGENTS.md before changing anything.
EOF
