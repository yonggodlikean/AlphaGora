#!/bin/sh
# Installs an OpenPup CLI release on this host for the pup-team container (mounted read-only from /opt/openpup).
#
#   ./deploy/fetch-openpup.sh               # latest GitHub release
#   ./deploy/fetch-openpup.sh v0.2.2        # a specific release
#   ./deploy/fetch-openpup.sh --rollback    # switch back to the previously installed release
#   ./deploy/fetch-openpup.sh --list        # show installed releases
#
# Layout (relative symlinks, so they also resolve inside the container):
#   /opt/openpup/releases/v0.2.2/openpup
#   /opt/openpup/current  -> releases/v0.2.2
#   /opt/openpup/previous -> releases/<the one before>
#
# Environment:
#   OPENPUP_ROOT           install root (default /opt/openpup; needs write access, e.g. run with sudo)
#   OPENPUP_REPO           GitHub repo (default openpup/openpup)
#   OPENPUP_DOWNLOAD_BASE  release download base, for a proxy/mirror
#                          (default https://github.com/$OPENPUP_REPO/releases/download)
#   OPENPUP_PLATFORM       override platform, e.g. linux-x86_64 (default: detected from uname)
#   OPENPUP_SKIP_EXEC_CHECK=1  install without running the binary (only for testing on another platform)
#   OPENPUP_NO_RESTART=1   do not restart the pup-team container afterwards
set -eu

ROOT="${OPENPUP_ROOT:-/opt/openpup}"
REPO="${OPENPUP_REPO:-openpup/openpup}"
BASE="${OPENPUP_DOWNLOAD_BASE:-https://github.com/$REPO/releases/download}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

say() { printf '%s\n' "fetch-openpup: $*"; }
die() { printf '%s\n' "fetch-openpup: $*" >&2; exit 1; }

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

restart_container() {
  [ "${OPENPUP_NO_RESTART:-0}" = "1" ] && return 0
  command -v docker >/dev/null 2>&1 || return 0
  if (cd "$HERE" && docker compose ps --status running --services 2>/dev/null | grep -qx pup-team); then
    say "restarting pup-team"
    (cd "$HERE" && docker compose restart pup-team)
  fi
}

switch_to() {   # $1 = release dir name, e.g. v0.2.2
  old="$(readlink "$ROOT/current" 2>/dev/null || true)"
  # -n: replace the symlink itself (plain `mv`/`ln -sf` onto a symlink-to-dir would write INTO the directory)
  ln -sfn "releases/$1" "$ROOT/current"
  if [ -n "$old" ] && [ "$old" != "releases/$1" ]; then
    ln -sfn "$old" "$ROOT/previous"
  fi
  say "current -> releases/$1"
}

case "${1:-latest}" in
  --list)
    [ -d "$ROOT/releases" ] || die "nothing installed under $ROOT"
    ls -1 "$ROOT/releases"
    say "current: $(readlink "$ROOT/current" 2>/dev/null || echo none) · previous: $(readlink "$ROOT/previous" 2>/dev/null || echo none)"
    exit 0 ;;
  --rollback)
    prev="$(readlink "$ROOT/previous" 2>/dev/null || true)"
    [ -n "$prev" ] && [ -x "$ROOT/$prev/openpup" ] || die "no previous release to roll back to"
    switch_to "$(basename "$prev")"
    restart_container
    exit 0 ;;
  -h|--help)
    sed -n '2,24p' "$0"; exit 0 ;;
esac

# ---------------------------------------------------------------- platform
if [ -n "${OPENPUP_PLATFORM:-}" ]; then
  PLATFORM="$OPENPUP_PLATFORM"
else
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  case "$(uname -m)" in
    x86_64|amd64) arch=x86_64 ;;
    aarch64|arm64) arch=aarch64 ;;
    *) die "unsupported CPU architecture $(uname -m)" ;;
  esac
  PLATFORM="$os-$arch"
fi

# ---------------------------------------------------------------- version
TAG="${1:-latest}"
if [ "$TAG" = "latest" ]; then
  # The /releases/latest page redirects to /releases/tag/<tag>; no API token or JSON parsing needed.
  loc="$(curl -fsSI --max-time 30 "https://github.com/$REPO/releases/latest" | tr -d '\r' | sed -n 's/^[Ll]ocation: //p' | tail -1)"
  TAG="${loc##*/}"
  [ -n "$TAG" ] && [ "$TAG" != "latest" ] || die "could not resolve the latest release of $REPO (set a version: $0 v0.2.2)"
fi
case "$TAG" in v*) ;; *) TAG="v$TAG" ;; esac
VER="${TAG#v}"
ASSET="openpup-cli-$VER-$PLATFORM.tar.gz"
DEST="$ROOT/releases/$TAG"

if [ -x "$DEST/openpup" ]; then
  say "$TAG already installed"
  switch_to "$TAG"
  restart_container
  exit 0
fi

# ---------------------------------------------------------------- download & install
mkdir -p "$ROOT/releases" || die "cannot create $ROOT (run with sudo, or set OPENPUP_ROOT)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
say "downloading $ASSET"
curl -fsSL --retry 3 --max-time 600 -o "$TMP/$ASSET" "$BASE/$TAG/$ASSET" \
  || die "download failed: $BASE/$TAG/$ASSET (no build for $PLATFORM in $TAG? behind a firewall? try OPENPUP_DOWNLOAD_BASE)"
SUM="$(sha256 "$TMP/$ASSET")"
tar -xzf "$TMP/$ASSET" -C "$TMP"
BIN="$(find "$TMP" -type f -name openpup | head -1)"
[ -n "$BIN" ] || die "$ASSET does not contain an 'openpup' binary"
chmod +x "$BIN"

if [ "${OPENPUP_SKIP_EXEC_CHECK:-0}" != "1" ]; then
  got="$("$BIN" --version 2>/dev/null || true)"
  case "$got" in
    *"$VER"*) say "verified: $got" ;;
    *) die "downloaded binary does not run here or reports '$got' (expected $VER)" ;;
  esac
fi

mkdir -p "$DEST.tmp"
cp "$BIN" "$DEST.tmp/openpup"
[ -f "$(dirname "$BIN")/openpupd" ] && cp "$(dirname "$BIN")/openpupd" "$DEST.tmp/openpupd"
printf '%s  %s\n' "$SUM" "$ASSET" > "$DEST.tmp/SHA256"
printf '%s\n' "$TAG" > "$DEST.tmp/VERSION"
chmod -R a+rX "$DEST.tmp"
mv "$DEST.tmp" "$DEST"
say "installed $TAG ($PLATFORM, sha256 $SUM)"
switch_to "$TAG"
restart_container
