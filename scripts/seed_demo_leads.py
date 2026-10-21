"""Local dev only: fill the marketplace with fake leads, so the espace pro's
"Acheter des leads" page has something to show before real leads arrive
from New World Courtage.

Usage, from the backend folder:
    venv/Scripts/python.exe scripts/seed_demo_leads.py [count]

Uses DATABASE_URL from .env -- refuses to run against PostgreSQL so it can
never write fake leads into a real database.
"""

import asyncio
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
import app.models.access_request  # noqa: E402,F401
import app.models.broker  # noqa: E402,F401
import app.models.commerce  # noqa: E402,F401
from app.models.lead import Lead  # noqa: E402

CITIES = [("75011", "Paris"), ("69003", "Lyon"), ("13008", "Marseille"), ("31000", "Toulouse"), ("33000", "Bordeaux"),
          ("59000", "Lille"), ("44000", "Nantes"), ("67000", "Strasbourg"), ("06000", "Nice"), ("92100", "Boulogne-Billancourt"),
          ("93200", "Saint-Denis"), ("35000", "Rennes"), ("34000", "Montpellier"), ("76000", "Rouen")]
FIRST = ["Camille", "Lucas", "Inès", "Hugo", "Sarah", "Karim", "Julie", "Thomas", "Yasmine", "Nicolas", "Léa", "Mehdi"]
LAST = ["Martin", "Bernard", "Petit", "Durand", "Leroy", "Moreau", "Benali", "Fournier", "Girard", "Haddad", "Roux"]

DETAILS = {
    "AUTO": lambda: {"Âge du conducteur": random.choice(["18-25 ans", "26-40 ans", "41-60 ans", "60 ans et +"]),
                     "Véhicule": random.choice(["Citadine", "Berline", "SUV", "Utilitaire léger"]),
                     "Sinistres (3 ans)": random.choice(["Aucun", "1", "2 et +"]),
                     "Actuellement assuré": random.choice(["Oui", "Non"])},
    "MOTO": lambda: {"Deux-roues": random.choice(["Scooter 125", "Roadster", "Sportive", "Trail"]),
                     "Cylindrée": random.choice(["125 cm³", "500 cm³", "750 cm³", "1000 cm³ +"]),
                     "Permis depuis": random.choice(["Moins de 2 ans", "2 à 5 ans", "Plus de 5 ans"])},
    "AUTO_PRO": lambda: {"Activité": random.choice(["Garage mécanique", "Carrosserie", "Négoce VO", "Convoyage"]),
                         "Véhicules confiés / an": random.choice(["< 100", "100 à 500", "> 500"]),
                         "Chiffre d'affaires": random.choice(["< 200 k€", "200 k€ – 1 M€", "> 1 M€"])},
    "COMMERCIAL_AUTO": lambda: {"Activité": random.choice(["Taxi", "VTC", "Livraison", "Flotte d'entreprise"]),
                                "Nombre de véhicules": random.choice(["1", "2 à 5", "6 à 20", "20 +"])},
}


async def main(count: int) -> None:
    if not settings.database_url.startswith("sqlite"):
        sys.exit("Refusé : ce script ne tourne que sur la base SQLite locale.")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        for _ in range(count):
            t = random.choice(list(DETAILS))
            zip_code, city = random.choice(CITIES)
            first, last = random.choice(FIRST), random.choice(LAST)
            db.add(Lead(
                insurance_type=t, zip_code=zip_code, city=city, first_name=first, last_name=last,
                email=f"{first}.{last}{random.randint(1, 999)}@exemple.fr".lower().replace("è", "e").replace("é", "e"),
                phone=f"+336{random.randint(10000000, 99999999)}", details=DETAILS[t](), opt_in_marketing=True,
                created_at=now - timedelta(minutes=random.randint(2, 60 * 24 * 10)),
            ))
        await db.commit()
    print(f"{count} leads de démonstration ajoutés.")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 40))
