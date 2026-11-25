#!/bin/sh
# Creates an isolated OpenPup home for the arena team so debates never touch your own pup's
# memory or conversations. It copies your LLM config and the key that decrypts it (both stay 0600,
# outside the repo). Re-run after changing models or keys in your own OpenPup.
set -eu
TARGET="${OPENPUP_HOME:-$HOME/.agora-openpup}"
SRC="$HOME/.openpup"
[ -f "$SRC/config.toml" ] || { echo "no $SRC/config.toml: run OpenPup once first" >&2; exit 1; }
mkdir -p "$TARGET/.openpup"
cp "$SRC/config.toml" "$TARGET/.openpup/config.toml"
[ -f "$SRC/.keystore" ] && cp "$SRC/.keystore" "$TARGET/.openpup/.keystore"
chmod 700 "$TARGET" "$TARGET/.openpup"
chmod 600 "$TARGET/.openpup/config.toml" "$TARGET/.openpup/.keystore" 2>/dev/null || true
echo "isolated OpenPup home: $TARGET"
