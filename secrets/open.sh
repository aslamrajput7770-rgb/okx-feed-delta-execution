#!/usr/bin/env bash
# Decrypt a sealed blob to stdout.
#
#   ./secrets/open.sh secrets/all.env.enc
#
# Prints to stdout so it can be sourced or piped, and never writes a plaintext
# file unless you redirect it yourself.
set -euo pipefail

src="${1:?usage: open.sh <encrypted-file>}"
[[ -f "$src" ]] || { echo "no such file: $src" >&2; exit 1; }

printf 'passphrase: ' >&2; read -rs pass; printf '\n' >&2
printf '%s' "$pass" | openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
    -in "$src" -pass stdin
