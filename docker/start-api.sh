#!/bin/sh
# The API: applies pending migrations once, then serves on $PORT (Render sets 10000; 8000 elsewhere).
set -e
alembic upgrade head
# Client IPs (rate limits): X-Forwarded-For is trusted only from FORWARDED_ALLOW_IPS — set it to your reverse
# proxy's address or network (e.g. 172.16.0.0/12 for a proxy container; "*" on Render, where the service is
# only reachable through Render's proxy). "*" on a directly reachable port would let anyone fake their IP.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers "${WEB_CONCURRENCY:-2}" \
  --proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}"
