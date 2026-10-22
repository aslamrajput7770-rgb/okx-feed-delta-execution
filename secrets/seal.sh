#!/usr/bin/env bash
# Encrypt a plaintext secrets file into a committable blob.
#
#   ./secrets/seal.sh secrets/all.env
#
# Writes secrets/all.env.enc. The plaintext file is NOT committed (secrets/.gitignore
# excludes it) and should be deleted afterwards. The passphrase is read from the
# terminal and is never written to disk, never passed as an argument (arguments
# are visible in `ps`), and never placed in the repository.
#
# Why encrypt rather than commit the values: a repository is a durable, copyable
# artifact. Its access depends on every token that can read it, and its history is
# effectively permanent. An encrypted blob keeps those properties while making the
# contents useless to anyone who is not holding the passphrase.
set -euo pipefail

src="${1:?usage: seal.sh <plaintext-file>}"
[[ -f "$src" ]] || { echo "no such file: $src" >&2; exit 1; }
out="${src}.enc"

printf 'passphrase: '; read -rs pass; printf '\n'
printf 'repeat: '; read -rs pass2; printf '\n'
[[ "$pass" == "$pass2" ]] || { echo "passphrases differ" >&2; exit 1; }
[[ ${#pass} -ge 12 ]] || { echo "use at least 12 characters" >&2; exit 1; }

printf '%s' "$pass" | openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt \
    -in "$src" -out "$out" -pass stdin
unset pass pass2

chmod 600 "$out"
echo "wrote $out  ($(wc -c < "$out") bytes)"
echo
echo "Verify it decrypts before you delete the plaintext:"
echo "  ./secrets/open.sh $out"
echo
echo "Then: shred -u $src"
