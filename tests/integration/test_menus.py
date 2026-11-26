"""Menus (spec F14): own dishes, 30 starters, editing the week, copy, generated shopping list."""

from datetime import date, timedelta

from app.core import clock

V1 = "/api/v1"


def _monday(offset_weeks: int = 0) -> date:
    t = clock.today()
    return t - timedelta(days=t.weekday()) + timedelta(weeks=offset_weeks)


def test_catalogue_has_30_starters_and_favourites_are_per_household(client, awa):
    dishes = client.get(f"{V1}/dishes", headers=awa).json()
    starters = [d for d in dishes if not d["own"]]
    assert len(starters) == 30
    assert dishes[0]["id"] == "d-kedjenou" and dishes[0]["favorite"]  # favourites first
    assert all(d["cost"] > 0 for d in starters)
    r = client.put(f"{V1}/dishes/d-garba/favorite", json={"favorite": True}, headers=awa)
    assert r.json()["favorite"] is True
    assert client.patch(f"{V1}/dishes/d-garba", json={"name": "x"}, headers=awa).status_code == 403


def test_own_dish_copy_edit_delete(client, awa):
    body = {
        "name": "Sauce aubergine", "slot": "lunch", "servings": 4, "prep_minutes": 45,
        "ingredients": [{"name": "Aubergines", "quantity": "6 pièces", "price": 900}],
    }
    d = client.post(f"{V1}/dishes", json=body, headers=awa).json()
    assert d["own"] and d["cost"] == 900
    d = client.patch(f"{V1}/dishes/{d['id']}", json={"servings": 6}, headers=awa).json()
    assert d["servings"] == 6
    copy = client.post(f"{V1}/dishes/d-garba/copy", headers=awa).json()
    assert copy["own"] and copy["name"] == "Garba" and copy["id"] != "d-garba"
    assert client.post(f"{V1}/dishes", json={**body, "ingredients": [{"name": "x"}] * 31}, headers=awa).status_code == 422
    assert client.delete(f"{V1}/dishes/{d['id']}", headers=awa).status_code == 204


def test_plan_edit_and_copy_week(client, awa, koffi):
    next_mon = _monday(1)
    meal = client.post(
        f"{V1}/meal-plans",
        json={"date": next_mon.isoformat(), "slot": "dinner", "dish_id": "d-garba", "cook_id": "m-koffi"},
        headers=awa,
    ).json()
    assert (meal["time"], meal["cook_id"], meal["cost"]) == ("19:30", "m-koffi", 4200)
    meal = client.patch(f"{V1}/meal-plans/{meal['id']}", json={"dish_id": "d-attieke"}, headers=awa).json()
    assert meal["dish_name"] == "Attiéké poisson braisé" and meal["cost"] == 7300
    assert client.post(
        f"{V1}/meal-plans", json={"date": next_mon.isoformat(), "slot": "lunch", "dish_id": "d-garba", "cook_id": "m-xx"},
        headers=awa,
    ).status_code == 400

    # Copy this week onto next week: refused while next week has a meal, then replaced.
    body = {"from_week": _monday().isoformat(), "to_week": next_mon.isoformat()}
    r = client.post(f"{V1}/meal-plans/copy", json=body, headers=awa)
    assert r.status_code == 409 and "Remplacer" in r.json()["detail"]
    copied = client.post(f"{V1}/meal-plans/copy", json={**body, "replace": True}, headers=awa).json()
    this = client.get(f"{V1}/meal-plans", headers=awa).json()
    assert sum(len(d["meals"]) for d in copied["days"]) == sum(len(d["meals"]) for d in this["days"])
    assert copied["week"] == next_mon.isoformat()
    assert client.delete(f"{V1}/meal-plans/{copied['days'][0]['meals'][0]['id']}", headers=awa).status_code == 204


def test_generated_list_sums_ingredients_and_keeps_manual_items_and_ticks(client, awa):
    week = _monday().isoformat()
    lst = client.post(f"{V1}/shopping-lists/generate?week={week}", headers=awa).json()
    by_name = {i["name"]: i for i in lst["items"]}
    # Attiéké: attiéké ×3 (Mon/Thu/Sun), garba ×2 (Wed/Sat) — 5 boules each.
    assert by_name["Attiéké"]["quantity"] == "25 boules" and by_name["Attiéké"]["source"] == "5 repas"
    assert by_name["Attiéké"]["price"] == 5000 and by_name["Attiéké"]["auto"]

    manual = client.post(f"{V1}/shopping-lists/items?week={week}", json={"name": "Savon", "quantity": "2"}, headers=awa)
    savon = next(i for i in manual.json()["items"] if i["name"] == "Savon")
    client.patch(f"{V1}/shopping-lists/items/{by_name['Attiéké']['id']}", json={"checked": True}, headers=awa)

    again = {i["name"]: i for i in client.post(f"{V1}/shopping-lists/generate?week={week}", headers=awa).json()["items"]}
    assert again["Savon"]["id"] == savon["id"] and not again["Savon"]["auto"]
    assert again["Attiéké"]["checked"] is True  # the tick survives regeneration
    assert client.delete(f"{V1}/shopping-lists/items/{savon['id']}", headers=awa).status_code == 204


def test_other_adults_can_edit_the_menu(client):
    from tests.conftest import login

    h = login(client, "koffi.k@email.com")
    assert h  # Koffi is an adult: can edit
    assert client.post(f"{V1}/shopping-lists/generate", headers=h).status_code == 200
