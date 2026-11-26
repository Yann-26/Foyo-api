#!/usr/bin/env bash
# Restores a backup made by deploy/backup.sh. REPLACES the current database (and photos, if given).
#   deploy/restore.sh /var/backups/foyo/foyo-<date>.dump [/var/backups/foyo/foyo-<date>-media.tar.gz]
set -euo pipefail
cd "$(dirname "$0")/.."
db="${1:?usage: deploy/restore.sh <file.dump> [media.tar.gz]}"
media="${2:-}"
[ -f "$db" ] || { echo "No such file: $db" >&2; exit 1; }
[ -z "$media" ] || [ -f "$media" ] || { echo "No such file: $media" >&2; exit 1; }

read -r -p "This replaces the current Foyo data with $(basename "$db"). Type RESTORE to continue: " answer
[ "$answer" = "RESTORE" ] || { echo "Cancelled."; exit 1; }

docker compose stop api worker # nothing writes while restoring
docker compose up -d db
until docker compose exec -T db pg_isready -U foyo -d foyo -q; do sleep 1; done
docker compose exec -T db pg_restore -U foyo -d foyo --clean --if-exists --no-owner < "$db"

if [ -n "$media" ]; then
  docker compose run --rm --no-deps --entrypoint sh -v "$(realpath "$media"):/restore.tar.gz:ro" api \
    -c 'rm -rf /data/media/* && tar -C /data -xzf /restore.tar.gz && chown -R foyo:foyo /data/media'
fi

docker compose up -d api worker
echo "Restored. Check https://${DOMAIN:-<your domain>}/healthz"
