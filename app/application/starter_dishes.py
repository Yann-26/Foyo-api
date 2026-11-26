"""The 30 starter dishes every household sees (spec F14). Prices are indicative (Abidjan markets, FCFA)
for the whole dish; households copy a dish to adjust it. Loaded on every start: new or changed starters
reach existing installations, ids never change."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.infrastructure.db.models import Dish

M, S = "market", "supermarket"

# id, name, slot, servings, prep minutes, subtitle, [(ingredient, quantity, price, place)]
STARTERS: list[tuple] = [
    # Breakfast
    ("d-omelette", "Omelette, pain, thé", "breakfast", 5, 15, None,
     [("Œufs", "10 pièces", 1000, M), ("Pain", "2 baguettes", 400, M), ("Thé", "1 boîte", 500, S)]),
    ("d-bouillie", "Bouillie de mil", "breakfast", 5, 20, None,
     [("Mil", "500 g", 500, M), ("Lait concentré", "1 boîte", 700, S), ("Sucre", "250 g", 300, S)]),
    ("d-pain-beurre", "Pain beurre, café au lait", "breakfast", 5, 10, None,
     [("Pain", "2 baguettes", 400, M), ("Beurre", "250 g", 1200, S), ("Lait en poudre", "400 g", 2500, S),
      ("Café", "1 sachet", 600, S)]),
    ("d-akassa", "Akassa, sauce piment", "breakfast", 5, 15, None,
     [("Akassa", "5 boules", 500, M), ("Piment", "1 tas", 200, M), ("Poisson fumé", "2 pièces", 1000, M)]),
    ("d-galettes", "Galettes de mil (gnomi)", "breakfast", 5, 30, None,
     [("Farine de mil", "500 g", 600, M), ("Sucre", "250 g", 300, S), ("Huile", "0,5 L", 800, S)]),
    ("d-bouillie-riz", "Bouillie de riz au lait", "breakfast", 5, 25, None,
     [("Riz brisé", "500 g", 400, M), ("Lait concentré", "1 boîte", 700, S), ("Sucre", "250 g", 300, S)]),
    # Lunch
    ("d-riz-arachide", "Riz sauce arachide", "lunch", 6, 60, None,
     [("Riz parfumé", "1 kg", 900, S), ("Pâte d’arachide", "500 g", 1000, S), ("Viande de bœuf", "1 kg", 3500, M),
      ("Tomates fraîches", "4 pièces", 400, M)]),
    ("d-alloco", "Alloco, œufs", "lunch", 5, 25, None,
     [("Banane plantain", "2 régimes", 2000, M), ("Huile", "1,5 L", 2400, S), ("Œufs", "5 pièces", 500, M)]),
    ("d-foutou", "Foutou sauce graine", "lunch", 6, 90, None,
     [("Graine de palme", "1 boîte", 1500, M), ("Igname", "2 pièces", 2000, M), ("Viande de bœuf", "500 g", 1750, M),
      ("Poisson fumé", "2 pièces", 1000, M)]),
    ("d-riz-gras", "Riz gras au poulet", "lunch", 6, 60, None,
     [("Riz parfumé", "1 kg", 900, S), ("Poulet", "1 kg", 3000, M), ("Tomates fraîches", "6 pièces", 600, M),
      ("Concentré de tomate", "1 boîte", 300, S), ("Oignons", "3 pièces", 300, M)]),
    ("d-placali", "Placali sauce kopè", "lunch", 6, 60, None,
     [("Placali", "6 boules", 1200, M), ("Gombo", "500 g", 500, M), ("Poisson fumé", "2 pièces", 1000, M),
      ("Crabe", "4 pièces", 1500, M)]),
    ("d-tchep", "Tchep (riz au poisson)", "lunch", 6, 75, None,
     [("Riz brisé", "1 kg", 800, M), ("Poisson capitaine", "1 kg", 4000, M), ("Chou", "1 pièce", 500, M),
      ("Carottes", "500 g", 500, M), ("Concentré de tomate", "1 boîte", 300, S)]),
    ("d-sauce-gombo", "Riz sauce gombo", "lunch", 6, 60, None,
     [("Riz parfumé", "1 kg", 900, S), ("Gombo", "500 g", 500, M), ("Viande de bœuf", "500 g", 1750, M),
      ("Poisson fumé", "1 pièce", 500, M)]),
    ("d-attieke-poulet", "Attiéké poulet braisé", "lunch", 5, 45, None,
     [("Attiéké", "5 boules", 1000, M), ("Poulet", "1,5 kg", 4500, M), ("Oignons", "3 pièces", 300, M),
      ("Tomates fraîches", "4 pièces", 400, M)]),
    ("d-haricots", "Haricots rouges, riz", "lunch", 6, 90, None,
     [("Haricots rouges", "1 kg", 1200, M), ("Riz brisé", "1 kg", 800, M), ("Huile", "0,5 L", 800, S)]),
    ("d-spaghetti", "Spaghetti sauce viande", "lunch", 5, 35, None,
     [("Spaghetti", "1 kg", 1000, S), ("Viande hachée", "500 g", 2000, S), ("Tomates fraîches", "4 pièces", 400, M),
      ("Oignons", "2 pièces", 200, M)]),
    ("d-ignames-frites", "Igname frite, poisson", "lunch", 5, 30, None,
     [("Igname", "1 pièce", 1000, M), ("Poisson carpe", "2 pièces", 3000, M), ("Huile", "1 L", 1600, S)]),
    # Dinner
    ("d-kedjenou", "Kedjenou de poulet", "dinner", 6, 50, "Plat principal · mes recettes",
     [("Poulet", "1,5 kg", 4500, M), ("Tomates fraîches", "6 pièces", 600, M), ("Oignons", "3 pièces", 300, M),
      ("Aubergines", "2 pièces", 400, M), ("Piment, ail, gingembre", "1 sachet", 1000, M)]),
    ("d-attieke", "Attiéké poisson braisé", "dinner", 5, 35, None,
     [("Attiéké", "5 boules", 1000, M), ("Poisson carpe", "4 pièces", 6000, M), ("Tomates fraîches", "3 pièces", 300, M)]),
    ("d-garba", "Garba", "dinner", 5, 20, None,
     [("Attiéké", "5 boules", 1000, M), ("Thon", "1 kg", 3000, M), ("Piment", "1 tas", 200, M)]),
    ("d-sauce-claire", "Foutou sauce claire", "dinner", 6, 75, None,
     [("Igname", "2 pièces", 2000, M), ("Poulet", "1 kg", 3000, M), ("Tomates fraîches", "4 pièces", 400, M),
      ("Aubergines", "2 pièces", 400, M)]),
    ("d-sauce-feuilles", "Riz sauce feuilles", "dinner", 6, 70, None,
     [("Riz brisé", "1 kg", 800, M), ("Feuilles de patate", "2 tas", 400, M), ("Huile rouge", "0,5 L", 700, M),
      ("Viande de bœuf", "500 g", 1750, M)]),
    ("d-soupe-poisson", "Soupe de poisson, pain", "dinner", 5, 40, None,
     [("Poisson capitaine", "1 kg", 4000, M), ("Pain", "2 baguettes", 400, M), ("Tomates fraîches", "3 pièces", 300, M)]),
    ("d-brochettes", "Brochettes et alloco", "dinner", 5, 40, None,
     [("Viande de bœuf", "1 kg", 3500, M), ("Banane plantain", "1 régime", 1000, M), ("Huile", "1 L", 1600, S)]),
    ("d-mafe", "Mafé de bœuf", "dinner", 6, 80, None,
     [("Viande de bœuf", "1 kg", 3500, M), ("Pâte d’arachide", "500 g", 1000, S), ("Riz parfumé", "1 kg", 900, S),
      ("Carottes", "500 g", 500, M)]),
    ("d-poulet-dg", "Poulet DG", "dinner", 6, 60, None,
     [("Poulet", "1,5 kg", 4500, M), ("Banane plantain", "1 régime", 1000, M), ("Carottes", "500 g", 500, M),
      ("Haricots verts", "250 g", 500, M)]),
    ("d-ragout-igname", "Ragoût d’igname", "dinner", 6, 60, None,
     [("Igname", "2 pièces", 2000, M), ("Viande de bœuf", "500 g", 1750, M), ("Tomates fraîches", "4 pièces", 400, M)]),
    # Snacks
    ("d-beignets", "Beignets (gbofloto)", "snack", 6, 40, None,
     [("Farine de blé", "1 kg", 900, S), ("Sucre", "250 g", 300, S), ("Levure", "1 sachet", 200, S),
      ("Huile", "1 L", 1600, S)]),
    ("d-fruits", "Fruits de saison", "snack", 5, 5, None,
     [("Mangues", "5 pièces", 1000, M), ("Bananes", "1 main", 500, M)]),
    ("d-arachides", "Arachides grillées, bissap", "snack", 5, 15, None,
     [("Arachides", "500 g", 600, M), ("Fleurs de bissap", "200 g", 500, M), ("Sucre", "500 g", 600, S)]),
]

assert len(STARTERS) == 30 and len({s[0] for s in STARTERS}) == 30


def ingredients_json(items: list[tuple]) -> list[dict]:
    return [{"name": n, "quantity": q, "price": p, "in_stock": False, "place": pl} for n, q, p, pl in items]


def ensure_starter_dishes(db: Session, _retry: bool = True) -> int:
    """Creates or refreshes the shared catalogue. Returns how many rows changed. Safe when several API
    processes start at once: if another one inserted a dish first, it starts over and finds it."""
    changed = 0
    for did, name, slot, servings, prep, subtitle, ingr in STARTERS:
        values = {
            "name": name, "slot": slot, "servings": servings, "prep_minutes": prep, "subtitle": subtitle,
            "ingredients": ingredients_json(ingr),
        }
        d = db.get(Dish, did)
        if d is None:
            db.add(Dish(id=did, household_id=None, favorite=False, **values))
            changed += 1
        elif d.household_id is None and any(getattr(d, k) != v for k, v in values.items()):
            for k, v in values.items():
                setattr(d, k, v)
            changed += 1
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if not _retry:
            raise
        return ensure_starter_dishes(db, _retry=False)
    return changed
