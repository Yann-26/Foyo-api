"""Shopping list generated from the week's menu: the same ingredient over several meals is summed
("Attiéké 5 boules" ×3 → "15 boules"; "500 g" + "1 kg" → "1,5 kg")."""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.security import new_id
from app.infrastructure.db.models import Dish, MealPlan, ShoppingItem

_QTY = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(.*?)\s*$")
# Units converted to a base before summing: (base unit, factor).
_BASE = {"g": ("g", 1), "kg": ("g", 1000), "ml": ("ml", 1), "cl": ("ml", 10), "l": ("ml", 1000)}
_NO_PLURAL = {"g", "kg", "ml", "cl", "l"}


def _num(x: float) -> str:
    return (f"{x:.2f}".rstrip("0").rstrip(".")).replace(".", ",")


def _unit_key(unit: str) -> str:
    u = unit.lower().strip()
    if u in _BASE:
        return _BASE[u][0]
    # "pièce" / "pièces", "boule" / "boules": same unit.
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in u.split())


def parse_quantity(text: str) -> tuple[float, str] | None:
    m = _QTY.match(text or "")
    if not m:
        return None
    return float(m.group(1).replace(",", ".")), m.group(2)


def format_quantity(amount: float, unit_key: str, sample_unit: str) -> str:
    if unit_key == "g":
        return f"{_num(amount / 1000)} kg" if amount >= 1000 else f"{_num(amount)} g"
    if unit_key == "ml":
        return f"{_num(amount / 1000)} L" if amount >= 1000 else f"{_num(amount)} ml"
    if not sample_unit:
        return _num(amount)
    singular = " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in sample_unit.split())
    if amount > 1 and singular.lower() not in _NO_PLURAL:
        words = singular.split()
        words[0] = words[0] + ("" if words[0].endswith(("s", "x")) else "s")
        return f"{_num(amount)} {' '.join(words)}"
    return f"{_num(amount)} {singular}"


@dataclass
class _Line:
    name: str
    place: str
    amount: float = 0.0
    unit_key: str = ""
    sample_unit: str = ""
    loose: list[str] = field(default_factory=list)  # quantities that don't parse ("un peu")
    price: int = 0
    dishes: list[str] = field(default_factory=list)

    @property
    def quantity(self) -> str:
        parts = [format_quantity(self.amount, self.unit_key, self.sample_unit)] if self.amount else []
        return " + ".join(parts + self.loose)[:40]

    @property
    def source(self) -> str:
        unique = list(dict.fromkeys(self.dishes))
        return unique[0] if len(unique) == 1 else f"{len(self.dishes)} repas"


def meal_cost(dish: Dish) -> int:
    return sum(int(i.get("price") or 0) for i in dish.ingredients or [] if not i.get("in_stock"))


def week_lines(db: Session, household_id: str, week_start: date) -> list[_Line]:
    rows = db.execute(
        select(MealPlan, Dish)
        .join(Dish, Dish.id == MealPlan.dish_id)
        .where(
            MealPlan.household_id == household_id,
            MealPlan.date >= week_start,
            MealPlan.date < week_start + timedelta(days=7),
        )
        .order_by(MealPlan.date, MealPlan.time)
    ).all()
    lines: dict[tuple[str, str], _Line] = {}
    for _, dish in rows:
        for ing in dish.ingredients or []:
            if ing.get("in_stock"):
                continue
            name = str(ing.get("name", "")).strip()
            if not name:
                continue
            parsed = parse_quantity(str(ing.get("quantity", "")))
            ukey = _unit_key(parsed[1]) if parsed else "?"
            line = lines.get((name.lower(), ukey))
            if line is None:
                line = lines[(name.lower(), ukey)] = _Line(name=name, place=ing.get("place") or "market", unit_key=ukey)
            if parsed:
                amount, unit = parsed
                base = _BASE.get(unit.lower().strip())
                line.amount += amount * (base[1] if base else 1)
                line.sample_unit = line.sample_unit or unit
            elif ing.get("quantity"):
                line.loose.append(str(ing["quantity"]))
            line.price += int(ing.get("price") or 0)
            line.dishes.append(dish.name)
    return sorted(lines.values(), key=lambda x: (x.place != "market", x.name.lower()))


def generate_shopping_list(db: Session, household_id: str, week_start: date) -> int:
    """Replaces the generated items of the week (items added by hand stay). An ingredient already
    ticked keeps its tick. Returns how many items were generated."""
    old = list(
        db.scalars(
            select(ShoppingItem).where(
                ShoppingItem.household_id == household_id, ShoppingItem.week_start == week_start, ShoppingItem.auto
            )
        )
    )
    ticked = {i.name.lower() for i in old if i.checked}
    db.execute(delete(ShoppingItem).where(ShoppingItem.id.in_([i.id for i in old])))
    lines = week_lines(db, household_id, week_start)
    for line in lines:
        db.add(
            ShoppingItem(
                id=new_id("si"),
                household_id=household_id,
                week_start=week_start,
                name=line.name[:80],
                quantity=line.quantity,
                price=line.price,
                place=line.place if line.place in ("market", "supermarket") else "market",
                source=line.source[:80],
                checked=line.name.lower() in ticked,
                auto=True,
            )
        )
    db.commit()
    return len(lines)
