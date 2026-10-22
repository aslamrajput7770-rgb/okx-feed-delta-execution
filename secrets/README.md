# Sealed credentials

Credentials for this project live here as **encrypted blobs**. The plaintext is
never committed.

## Why encrypted, and not the raw values

A repository is a durable, copyable artifact. Anyone who can read it can read
every revision of it, and a value survives deletion in history. The one token
that can read a private repository can also read every secret inside it, so
"private" is a weaker control than it looks. Encrypting keeps the convenience of
having credentials next to the code while making them useless to anyone without
the passphrase.

## Sealing a file

    cat > secrets/all.env <<'ENVFILE'
    DELTA_API_KEY=...
    DELTA_API_SECRET=...
    ENVFILE
    chmod 600 secrets/all.env

    ./secrets/seal.sh secrets/all.env          # prompts for a passphrase twice

    ./secrets/open.sh secrets/all.env.enc      # verify BEFORE destroying plaintext

    shred -u secrets/all.env

    git add secrets/all.env.enc && git commit -m "Seal credentials"

`secrets/.gitignore` excludes `*.env` and `*.txt`, so the plaintext cannot be
added by accident.

## Opening it

    ./secrets/open.sh secrets/all.env.enc > /tmp/all.env
    chmod 600 /tmp/all.env
    set -a; . /tmp/all.env; set +a
    rm -f /tmp/all.env

## The passphrase

Keep it somewhere that is not this repository and not any chat log. If it is
lost the blob is unrecoverable; there is no back door.

## The EC2 private key

A PEM private key is itself a file credential, and the same procedure works on
it, but rotating the key pair is stronger than encrypting it. A key that has
travelled through a chat log has been exposed, and no amount of later encryption
changes that. Prefer rotation; see the root README.
