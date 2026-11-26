"""French labels used in API payloads (the mobile app displays them as-is)."""

import calendar
from datetime import date, datetime

MONTHS_SHORT = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
MONTHS_FULL = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]
MONTHS_CHART = ["Janv.", "Févr.", "Mars", "Avr.", "Mai", "Juin", "Juil.", "Août", "Sept.", "Oct.", "Nov.", "Déc."]
DAYS_SHORT = ["Lun.", "Mar.", "Mer.", "Jeu.", "Ven.", "Sam.", "Dim."]
DAY_INITIALS = ["L", "M", "M", "J", "V", "S", "D"]


def short_date(d: date) -> str:
    """20 nov."""
    return f"{d.day} {MONTHS_SHORT[d.month - 1]}"


def hm(t: datetime) -> str:
    """9:18"""
    return f"{t.hour}:{t.minute:02d}"


def appointment_label(at: datetime) -> str:
    """Mer. 9 déc. · 9:00"""
    return f"{DAYS_SHORT[at.weekday()]} {short_date(at.date())} · {hm(at)}"


def thousands(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, days_in_month(y, m)))
