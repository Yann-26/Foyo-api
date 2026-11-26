FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ENVIRONMENT=production
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app
# Only the read-only report: scripts/reset_db.py wipes the database and stays out of the image.
COPY scripts/telemetry_report.py ./scripts/telemetry_report.py
COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/start-api.sh /start-api.sh

# The app runs as this unprivileged user: entrypoint.sh drops root right after fixing the photo folder.
RUN useradd --system --uid 10001 foyo && mkdir -p /data/media && chown -R foyo /app /data \
  && chmod +x /entrypoint.sh /start-api.sh

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/healthz', timeout=4)"
ENTRYPOINT ["/entrypoint.sh"]
# The API by default; the reminder worker runs `python -m app.workers.scheduler` instead.
CMD ["/start-api.sh"]
