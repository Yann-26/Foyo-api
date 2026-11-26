#!/bin/sh
# Applies pending migrations once, then starts the API.
set -e
alembic upgrade head
# Client IPs (rate limits): X-Forwarded-For is trusted only from FORWARDED_ALLOW_IPS — set it to your reverse
# proxy's address or network (e.g. 172.16.0.0/12 for a proxy container). "*" would let anyone fake their IP.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${WEB_CONCURRENCY:-2}" --proxy-headers \
  --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}"
