#!/bin/sh
# Daily logical backup of the arena database (run from cron after the close; see docs/DEPLOY.md).
#   BACKUP_DIR (default /srv/alphagora/backups) · BACKUP_KEEP_DAYS (default 14)
set -eu
cd "$(dirname "$0")/.."
DIR="${BACKUP_DIR:-/srv/alphagora/backups}"
KEEP="${BACKUP_KEEP_DAYS:-14}"
mkdir -p "$DIR" && chmod 700 "$DIR"
f="$DIR/agora-$(date -u +%Y%m%d-%H%M).sql.gz"
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges' \
  | gzip -9 > "$f.tmp"
[ -s "$f.tmp" ] || { rm -f "$f.tmp"; echo "backup: empty dump, aborting" >&2; exit 1; }
mv "$f.tmp" "$f" && chmod 600 "$f"
find "$DIR" -name 'agora-*.sql.gz' -mtime +"$KEEP" -delete
echo "backup: $f ($(du -h "$f" | cut -f1))"
