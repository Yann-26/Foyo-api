#!/usr/bin/env bash
# Nightly backup of the database and the photos, run on the server (cron: see README › Lightsail).
# Writes BACKUP_DIR/foyo-<UTC date>.dump (pg_dump custom format) and foyo-<date>-media.tar.gz, deletes
# backups older than BACKUP_KEEP_DAYS, and copies the new files to BACKUP_S3_URI if set (AWS CLI needed).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a

dest="${BACKUP_DIR:-/var/backups/foyo}"
keep="${BACKUP_KEEP_DAYS:-14}"
stamp="$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$dest"
umask 077 # backups contain personal data: readable by their owner only

db="$dest/foyo-$stamp.dump"
media="$dest/foyo-$stamp-media.tar.gz"

docker compose exec -T db pg_dump -U foyo -d foyo --format=custom > "$db.part"
mv "$db.part" "$db"
docker compose exec -T api tar -C /data -czf - media > "$media.part"
mv "$media.part" "$media"

# A dump that can't be listed is not a backup.
docker compose exec -T db pg_restore --list < "$db" > /dev/null

find "$dest" -name 'foyo-*' -type f -mtime +"$keep" -delete

if [ -n "${BACKUP_S3_URI:-}" ]; then
  aws s3 cp "$db" "${BACKUP_S3_URI%/}/" --only-show-errors
  aws s3 cp "$media" "${BACKUP_S3_URI%/}/" --only-show-errors
fi

echo "Backup OK: $db ($(du -h "$db" | cut -f1)), $media ($(du -h "$media" | cut -f1))"
