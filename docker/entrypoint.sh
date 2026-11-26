#!/bin/sh
# Runs as root only long enough to make the photo folder writable (a disk mounted at start-up, e.g. on
# Render, ignores the permissions set in the image), then runs the command as the unprivileged user.
set -e
if [ "$(id -u)" = "0" ]; then
  mkdir -p "${MEDIA_DIR:-/data/media}"
  chown -R foyo:foyo "${MEDIA_DIR:-/data/media}"
  exec setpriv --reuid=foyo --regid=foyo --init-groups "$@"
fi
exec "$@"
