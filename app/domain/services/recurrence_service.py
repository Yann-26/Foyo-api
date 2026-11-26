"""Is a task due on a given day? (spec §5.3 recurrences)."""

from datetime import date


def is_due(recurrence: dict, start: date, day: date) -> bool:
    if day < start:
        return False
    kind = recurrence.get("kind", "daily")
    interval = max(1, int(recurrence.get("interval") or 1))
    weekdays = recurrence.get("weekdays") or []
    match kind:
        case "once":
            return day == start
        case "daily":
            return (day - start).days % interval == 0
        case "weekly":
            return day.isoweekday() in weekdays if weekdays else day.isoweekday() == start.isoweekday()
        case "monthly":
            months = (day.year - start.year) * 12 + day.month - start.month
            return day.day == start.day and months % interval == 0
        case "everyNDays":
            return (day - start).days % interval == 0
        case "custom":
            return day.isoweekday() in weekdays
    return False



class Recurrence:
    """French label of a recurrence, as in the app (e-mails)."""

    _DAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]

    @staticmethod
    def label(r: dict) -> str:
        kind, weekdays, n = r.get("kind"), r.get("weekdays") or [], r.get("interval") or 1
        if kind == "daily" or (kind == "weekly" and len(weekdays) == 7):
            return "tous les jours"
        if kind == "weekly":
            return ", ".join(Recurrence._DAYS[d - 1] for d in weekdays)
        if kind == "monthly":
            return "chaque mois" if n == 1 else f"tous les {n} mois"
        if kind == "everyNDays":
            return f"tous les {n} jours"
        return "une fois" if kind == "once" else "personnalisée"
