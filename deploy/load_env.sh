#!/usr/bin/env bash
# Source the venue configuration, then the local credentials if present.
#
#   . deploy/load_env.sh
#
# Precedence, lowest to highest:
#   1. config/delta.env        committed, non-secret configuration
#   2. config/delta.local.env  untracked credentials, git-ignored
#   3. the environment         platform secrets already exported
#
# The environment wins last so that a platform secret always overrides a file
# left on disk. Nothing here prints a value.

_repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Remember anything the platform already injected. Sourcing a file overwrites
# the environment, so without this a stale file on disk would silently beat a
# freshly rotated platform secret.
_keep_key="${DELTA_API_KEY:-}"
_keep_secret="${DELTA_API_SECRET:-}"

if [[ -f "$_repo/config/delta.env" ]]; then
    set -a; . "$_repo/config/delta.env"; set +a
fi
if [[ -f "$_repo/config/delta.local.env" ]]; then
    set -a; . "$_repo/config/delta.local.env"; set +a
fi

[[ -n "$_keep_key" ]] && export DELTA_API_KEY="$_keep_key"
[[ -n "$_keep_secret" ]] && export DELTA_API_SECRET="$_keep_secret"
unset _keep_key _keep_secret

# Placeholders from the committed file are not credentials.
for _v in DELTA_API_KEY DELTA_API_SECRET; do
    if [[ "${!_v:-}" == REPLACE_* ]]; then
        unset "$_v"
    fi
done
unset _v _repo
