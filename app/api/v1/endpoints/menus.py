from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from app.api.deps import Ctx, CtxDep
from app.api.v1.endpoints.finances import budgets_for, create_operation
from app.application import menus, premium
from app.core import clock
from app.core.exceptions import bad_request, conflict, forbidden, not_found
from app.core.fr import MONTHS_SHORT
from app.core.security import new_id
from app.infrastructure.db.models import Account, Dish, MealPlan, Member, ShoppingItem
from app.schemas.operations import OperationIn

router = APIRouter(tags=["menus"])


class ItemPatchIn(BaseModel):
    checked: bool


Day = date  # field names "date" would shadow the type inside the models
Slot = Literal["breakfast", "lunch", "dinner", "snack"]
Place = Literal["market", "supermarket"]
DEFAULT_TIME = {"breakfast": "07:00", "lunch": "12:30", "dinner": "19:30", "snack": "16:00"}
_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class IngredientIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    quantity: str = Field(default="", max_length=40)
    price: int = Field(default=0, ge=0, le=10_000_000)
    place: Place = "market"
    in_stock: bool = False


class DishIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    slot: Slot
    servings: int = Field(default=4, ge=1, le=30)
    prep_minutes: int = Field(default=30, ge=1, le=600)
    subtitle: str | None = Field(default=None, max_length=120)
    ingredients: list[IngredientIn] = Field(default_factory=list, max_length=30)


class DishPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    slot: Slot | None = None
    servings: int | None = Field(default=None, ge=1, le=30)
    prep_minutes: int | None = Field(default=None, ge=1, le=600)
    subtitle: str | None = Field(default=None, max_length=120)
    ingredients: list[IngredientIn] | None = Field(default=None, max_length=30)


class FavoriteIn(BaseModel):
    favorite: bool


class MealIn(BaseModel):
    date: Day
    slot: Slot
    dish_id: str
    time: str | None = Field(default=None, pattern=_HHMM)
    cook_id: str | None = None
    cost: int | None = Field(default=None, ge=0, le=10_000_000)  # default: the dish's ingredients


class MealPatchIn(BaseModel):
    date: Day | None = None
    slot: Slot | None = None
    dish_id: str | None = None
    time: str | None = Field(default=None, pattern=_HHMM)
    cook_id: str | None = None
    cost: int | None = Field(default=None, ge=0, le=10_000_000)


class CopyWeekIn(BaseModel):
    from_week: Day
    to_week: Day
    replace: bool = False  # the target week already has meals: replace them


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    quantity: str = Field(default="", max_length=40)
    price: int = Field(default=0, ge=0, le=10_000_000)
    place: Place = "market"


class ExpenseIn(BaseModel):
    account_id: str | None = None


def _monday(d: date | None) -> date:
    d = d or clock.today()
    return d - timedelta(days=d.weekday())


def dish_json(d: Dish, favorites: list | None = None) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "slot": d.slot,
        "servings": d.servings,
        "prep_minutes": d.prep_minutes,
        "subtitle": d.subtitle,
        "favorite": d.id in (favorites or []),
        "own": d.household_id is not None,
        "ingredients": d.ingredients,
        "cost": menus.meal_cost(d),
    }


def _dishes(ctx: Ctx):
    return select(Dish).where(or_(Dish.household_id.is_(None), Dish.household_id == ctx.hid))


@router.get("/dishes")
def list_dishes(ctx: CtxDep, slot: str | None = None) -> list[dict]:
    q = _dishes(ctx)
    if slot:
        q = q.where(Dish.slot == slot)
    favs = ctx.household.favorite_dishes or []
    dishes = list(ctx.db.scalars(q.order_by(Dish.name)))
    dishes.sort(key=lambda d: (d.id not in favs, d.household_id is None, d.name.lower()))
    return [dish_json(d, favs) for d in dishes]


@router.get("/dishes/{dish_id}")
def get_dish(dish_id: str, ctx: CtxDep) -> dict:
    return dish_json(_dish(ctx, dish_id), ctx.household.favorite_dishes)


def _dish(ctx: Ctx, dish_id: str, own: bool = False) -> Dish:
    d = ctx.db.get(Dish, dish_id)
    if d is None or d.household_id not in (None, ctx.hid):
        raise not_found("Plat")
    if own and d.household_id is None:
        raise forbidden("Plat du catalogue : faites-en une copie pour le modifier.")
    return d


@router.post("/dishes", status_code=status.HTTP_201_CREATED)
def create_dish(body: DishIn, ctx: CtxDep) -> dict:
    """A household's own recipe (spec F14)."""
    ctx.require_edit()
    d = Dish(
        id=new_id("d"),
        household_id=ctx.hid,
        name=body.name.strip(),
        slot=body.slot,
        servings=body.servings,
        prep_minutes=body.prep_minutes,
        subtitle=body.subtitle,
        favorite=False,
        ingredients=[i.model_dump() for i in body.ingredients],
    )
    ctx.db.add(d)
    ctx.db.commit()
    return dish_json(d, ctx.household.favorite_dishes)


@router.post("/dishes/{dish_id}/copy", status_code=status.HTTP_201_CREATED)
def copy_dish(dish_id: str, ctx: CtxDep) -> dict:
    """Makes a catalogue dish one's own, to adjust quantities and prices."""
    ctx.require_edit()
    src = _dish(ctx, dish_id)
    d = Dish(
        id=new_id("d"), household_id=ctx.hid, name=src.name[:80], slot=src.slot, servings=src.servings,
        prep_minutes=src.prep_minutes, subtitle=src.subtitle, favorite=False, ingredients=list(src.ingredients or []),
    )
    ctx.db.add(d)
    ctx.db.commit()
    return dish_json(d, ctx.household.favorite_dishes)


@router.patch("/dishes/{dish_id}")
def patch_dish(dish_id: str, body: DishPatchIn, ctx: CtxDep) -> dict:
    ctx.require_edit()
    d = _dish(ctx, dish_id, own=True)
    data = body.model_dump(exclude_unset=True)
    if "name" in data:
        data["name"] = data["name"].strip()
    for k, v in data.items():
        setattr(d, k, v)
    ctx.db.commit()
    return dish_json(d, ctx.household.favorite_dishes)


@router.delete("/dishes/{dish_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_dish(dish_id: str, ctx: CtxDep) -> None:
    ctx.require_edit()
    d = _dish(ctx, dish_id, own=True)
    planned = ctx.db.scalars(
        select(MealPlan.id).where(MealPlan.dish_id == d.id, MealPlan.date >= clock.today())
    ).first()
    if planned:
        raise conflict("Ce plat est au menu des prochains jours : retirez-le du menu d’abord.")
    ctx.db.delete(d)  # past meals that used it go with it
    ctx.household.favorite_dishes = [x for x in ctx.household.favorite_dishes or [] if x != d.id]
    ctx.db.commit()


@router.put("/dishes/{dish_id}/favorite")
def set_favorite(dish_id: str, body: FavoriteIn, ctx: CtxDep) -> dict:
    d = _dish(ctx, dish_id)
    favs = [x for x in ctx.household.favorite_dishes or [] if x != d.id]
    ctx.household.favorite_dishes = [*favs, d.id] if body.favorite else favs
    ctx.db.commit()
    return dish_json(d, ctx.household.favorite_dishes)


def _items(ctx: Ctx, week: date) -> list[ShoppingItem]:
    return list(
        ctx.db.scalars(
            select(ShoppingItem)
            .where(ShoppingItem.household_id == ctx.hid, ShoppingItem.week_start == week)
            .order_by(ShoppingItem.id)
        )
    )


def _meal(ctx: Ctx, meal_id: str) -> MealPlan:
    m = ctx.db.get(MealPlan, meal_id)
    if m is None or m.household_id != ctx.hid:
        raise not_found("Repas")
    return m


def _check_cook(ctx: Ctx, member_id: str) -> None:
    m = ctx.db.get(Member, member_id)
    if m is None or m.household_id != ctx.hid or not m.active:
        raise bad_request("Membre inconnu dans ce foyer")


def meal_json(mp: MealPlan, dish: Dish) -> dict:
    return {
        "id": mp.id, "date": mp.date.isoformat(), "slot": mp.slot, "time": mp.time, "dish_id": dish.id,
        "dish_name": dish.name, "cook_id": mp.cook_id, "cost": mp.cost,
    }


@router.post("/meal-plans", status_code=status.HTTP_201_CREATED)
def add_meal(body: MealIn, ctx: CtxDep) -> dict:
    ctx.require_edit()
    dish = _dish(ctx, body.dish_id)
    cook = body.cook_id or ctx.me.id
    _check_cook(ctx, cook)
    mp = MealPlan(
        id=new_id("mp"), household_id=ctx.hid, date=body.date, slot=body.slot,
        time=body.time or DEFAULT_TIME[body.slot], dish_id=dish.id, cook_id=cook,
        cost=menus.meal_cost(dish) if body.cost is None else body.cost,
    )
    ctx.db.add(mp)
    ctx.db.commit()
    return meal_json(mp, dish)


@router.patch("/meal-plans/{meal_id}")
def patch_meal(meal_id: str, body: MealPatchIn, ctx: CtxDep) -> dict:
    ctx.require_edit()
    mp = _meal(ctx, meal_id)
    data = body.model_dump(exclude_unset=True)
    if "dish_id" in data:
        dish = _dish(ctx, data["dish_id"])
        if "cost" not in data:
            data["cost"] = menus.meal_cost(dish)
    if data.get("cook_id"):
        _check_cook(ctx, data["cook_id"])
    if "slot" in data and "time" not in data:
        data["time"] = DEFAULT_TIME[data["slot"]]
    for k, v in data.items():
        if v is not None:
            setattr(mp, k, v)
    ctx.db.commit()
    return meal_json(mp, ctx.db.get(Dish, mp.dish_id))


@router.delete("/meal-plans/{meal_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_meal(meal_id: str, ctx: CtxDep) -> None:
    ctx.require_edit()
    ctx.db.delete(_meal(ctx, meal_id))
    ctx.db.commit()


@router.post("/meal-plans/copy")
def copy_week(body: CopyWeekIn, ctx: CtxDep) -> dict:
    """"Copier la semaine": same dishes, cooks and times, 7 days later (or any week)."""
    ctx.require_edit()
    src, dst = _monday(body.from_week), _monday(body.to_week)
    if src == dst:
        raise bad_request("Choisissez une autre semaine.")

    def week(start: date):
        return select(MealPlan).where(
            MealPlan.household_id == ctx.hid, MealPlan.date >= start, MealPlan.date < start + timedelta(days=7)
        )

    meals = list(ctx.db.scalars(week(src)))
    if not meals:
        raise bad_request("La semaine à copier est vide.")
    existing = list(ctx.db.scalars(week(dst)))
    if existing and not body.replace:
        raise conflict(f"Cette semaine a déjà {len(existing)} repas. Remplacer ?")
    for m in existing:
        ctx.db.delete(m)
    shift = dst - src
    for m in meals:
        ctx.db.add(
            MealPlan(
                id=new_id("mp"), household_id=ctx.hid, date=m.date + shift, slot=m.slot, time=m.time,
                dish_id=m.dish_id, cook_id=m.cook_id, cost=m.cost,
            )
        )
    ctx.db.commit()
    return meal_plans(ctx, dst)


@router.get("/meal-plans")
def meal_plans(ctx: CtxDep, week: date | None = Query(default=None)) -> dict:
    """Screen 22 — week starting on Monday."""
    start = _monday(week)
    rows = ctx.db.execute(
        select(MealPlan, Dish)
        .join(Dish, Dish.id == MealPlan.dish_id)
        .where(MealPlan.household_id == ctx.hid, MealPlan.date >= start, MealPlan.date < start + timedelta(days=7))
        .order_by(MealPlan.date, MealPlan.time)
    ).all()
    days = {start + timedelta(days=i): [] for i in range(7)}
    for mp, dish in rows:
        days[mp.date].append(
            {
                "id": mp.id,
                "date": mp.date.isoformat(),
                "slot": mp.slot,
                "time": mp.time,
                "dish_id": dish.id,
                "dish_name": dish.name,
                "cook_id": mp.cook_id,
                "cost": mp.cost,
            }
        )
    return {
        "week": start.isoformat(),
        "week_label": _week_label(start),
        "total_cost": sum(mp.cost for mp, _ in rows),
        "items_to_buy": sum(not i.checked for i in _items(ctx, start)),
        # All 7 days: an empty one is shown as "non planifié" (spec F14).
        "days": [{"date": d.isoformat(), "meals": meals} for d, meals in days.items()],
    }


def _week_label(start: date) -> str:
    end = start + timedelta(days=6)
    return f"Semaine du {start.day} au {end.day} {MONTHS_SHORT[end.month - 1]}"


def shopping_json(ctx: Ctx, start: date) -> dict:
    today = clock.today()
    food = next((b for b in budgets_for(ctx, today.year, today.month) if b["category_id"] == "alimentation"), None)
    return {
        "week": start.isoformat(),
        "week_label": _week_label(start),
        "food_budget_left": (food["planned"] - food["spent"]) if food else 0,
        "items": [
            {
                "id": i.id,
                "auto": i.auto,
                "name": i.name,
                "quantity": i.quantity,
                "price": i.price,
                "place": i.place,
                "source": i.source,
                "checked": i.checked,
            }
            for i in _items(ctx, start)
        ],
    }


@router.get("/shopping-lists")
def shopping_list(ctx: CtxDep, week: date | None = Query(default=None)) -> dict:
    """Screen 24 — generated from the week's meal plans."""
    return shopping_json(ctx, _monday(week))


@router.post("/shopping-lists/generate")
def generate(ctx: CtxDep, week: date | None = Query(default=None)) -> dict:
    """Rebuilds the list from the week's menu: same ingredients summed, items added by hand kept."""
    ctx.require_edit()
    start = _monday(week)
    menus.generate_shopping_list(ctx.db, ctx.hid, start)
    return shopping_json(ctx, start)


@router.post("/shopping-lists/items", status_code=status.HTTP_201_CREATED)
def add_item(body: ItemIn, ctx: CtxDep, week: date | None = Query(default=None)) -> dict:
    start = _monday(week)
    ctx.db.add(
        ShoppingItem(
            id=new_id("si"), household_id=ctx.hid, week_start=start, name=body.name.strip(), quantity=body.quantity,
            price=body.price, place=body.place, source=None, checked=False, auto=False,
        )
    )
    ctx.db.commit()
    return shopping_json(ctx, start)


@router.delete("/shopping-lists/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(item_id: str, ctx: CtxDep) -> None:
    i = ctx.db.get(ShoppingItem, item_id)
    if i is None or i.household_id != ctx.hid:
        raise not_found("Article")
    ctx.db.delete(i)
    ctx.db.commit()


@router.patch("/shopping-lists/items/{item_id}")
def check_item(item_id: str, body: ItemPatchIn, ctx: CtxDep) -> dict:
    i = ctx.db.get(ShoppingItem, item_id)
    if i is None or i.household_id != ctx.hid:
        raise not_found("Article")
    i.checked = body.checked
    ctx.db.commit()
    return {"id": i.id, "checked": i.checked}


@router.post("/shopping-lists/expense", status_code=status.HTTP_201_CREATED)
def shopping_to_expense(ctx: CtxDep, body: ExpenseIn | None = None, week: date | None = Query(default=None)) -> dict:
    """Module link (Complet only): the checked items of the week become one food expense."""
    if not premium.module_links_active(ctx.household):
        premium.require_premium(ctx.household, "La dépense depuis la liste de courses (formule Complet)")
        raise forbidden("Disponible dans la formule Complet")
    start = _monday(week)
    total = sum(i.price for i in _items(ctx, start) if i.checked)
    if total <= 0:
        raise bad_request("Aucun article coché")
    account_id = body.account_id if body and body.account_id else None
    if account_id is None:
        acc = ctx.db.scalars(
            select(Account).where(Account.household_id == ctx.hid, ~Account.is_private).order_by(Account.balance.desc())
        ).first()
        if acc is None:
            raise bad_request("Aucun compte partagé")
        account_id = acc.id
    op = create_operation(
        ctx,
        OperationIn(
            type="expense", amount=total, category_id="alimentation", account_id=account_id,
            label=f"Courses — {_week_label(start).lower()}", origin="shoppingList",
        ),
    )
    ctx.db.commit()
    return {"operation_id": op.id, "amount": total}
