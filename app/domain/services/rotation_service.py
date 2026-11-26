"""Rotation (spec F4): a rotating task goes to each eligible member in turn."""

from datetime import date

ROTATION_ROLES = ("admin", "adult", "teen")


def rotation_assignee(member_ids: list[str], start: date, day: date, fallback: str | None) -> str | None:
    if not member_ids:
        return fallback
    return member_ids[(day - start).days % len(member_ids)]
