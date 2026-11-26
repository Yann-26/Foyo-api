"""Reminder worker: one process, checks every minute.

    python -m app.workers.scheduler            (docker compose runs it as the `worker` service)

Run a single instance. A second one would not double-send (NotificationLog keys), only waste work.
"""

import logging
import time

from app.core import clock
from app.core.database import SessionLocal
from app.core.logging import setup_logging
from app.infrastructure.notifications.fcm_client import check_config
from app.workers.reminder_jobs import JOBS

log = logging.getLogger("foyo.worker")


def run_once(now=None) -> dict[str, int]:  # noqa: ANN001
    now = now or clock.now()
    counts = {}
    for job in JOBS:
        with SessionLocal() as db:
            try:
                counts[job.__name__] = job(db, now)
            except Exception:
                log.exception("Job %s failed", job.__name__)
                db.rollback()
    return counts


def run_forever(interval: int = 60) -> None:
    setup_logging()
    check_config()
    log.info("Reminder worker started (every %ss)", interval)
    while True:
        started = time.monotonic()
        sent = {k: v for k, v in run_once().items() if v}
        if sent:
            log.info("Sent: %s", sent)
        time.sleep(max(1.0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    run_forever()
