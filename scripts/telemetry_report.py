"""Usage and crash summary for the last N days (default 7).

    .venv/bin/python -m scripts.telemetry_report [days]
"""

import sys
from datetime import timedelta

from sqlalchemy import func, select

from app.core import clock
from app.core.database import SessionLocal
from app.infrastructure.db.models import CrashReport, TelemetryEvent


def report(days: int = 7) -> str:
    since = clock.now() - timedelta(days=days)
    lines = [f"Depuis {since:%d/%m/%Y %H:%M} ({days} j)"]
    with SessionLocal() as db:
        installs = db.scalar(select(func.count(func.distinct(TelemetryEvent.install_id))).where(TelemetryEvent.at >= since))
        lines.append(f"Installations actives (statistiques activées) : {installs}")
        lines.append("\nÉvénements :")
        for name, n in db.execute(
            select(TelemetryEvent.name, func.count()).where(TelemetryEvent.at >= since).group_by(TelemetryEvent.name).order_by(func.count().desc())
        ):
            lines.append(f"  {n:>6}  {name}")
        # Counted here, not with GROUP BY: Postgres can't group by a JSON column.
        counts: dict[str, int] = {}
        for props in db.scalars(
            select(TelemetryEvent.props).where(TelemetryEvent.at >= since, TelemetryEvent.name == "screen_view")
        ):
            screen = str((props or {}).get("screen"))
            counts[screen] = counts.get(screen, 0) + 1
        if counts:
            lines.append("\nÉcrans les plus vus :")
            for screen, n in sorted(counts.items(), key=lambda x: -x[1])[:15]:
                lines.append(f"  {n:>6}  {screen}")
        lines.append("\nPlantages (regroupés) :")
        for fp, n, phones, error, version in db.execute(
            select(
                CrashReport.fingerprint,
                func.count(),
                func.count(func.distinct(CrashReport.install_id)),
                func.max(CrashReport.error),
                func.max(CrashReport.app_version),
            )
            .where(CrashReport.at >= since)
            .group_by(CrashReport.fingerprint)
            .order_by(func.count().desc())
            .limit(20)
        ):
            lines.append(f"  {n:>4}× sur {phones} tél. · v{version} · {error[:90]}  [{fp[:8]}]")
    return "\n".join(lines)


if __name__ == "__main__":
    print(report(int(sys.argv[1]) if len(sys.argv) > 1 else 7))
