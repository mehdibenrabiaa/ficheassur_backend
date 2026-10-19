"""Create (or reset) a FicheAssur admin account, from a terminal.

Usage, from the backend folder:
    .venv/Scripts/python.exe scripts/create_admin.py "Prénom Nom" email@exemple.fr "mot-de-passe"

Uses DATABASE_URL from .env like the app. An existing account with that
email is promoted to admin, activated, and gets the new password.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

import app.models.access_request  # noqa: E402,F401
import app.models.lead  # noqa: E402,F401
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models.broker import Broker, BrokerRole, BrokerStatus  # noqa: E402
from app.security import hash_password  # noqa: E402


async def main(name: str, email: str, password: str) -> None:
    if len(password) < 8:
        sys.exit("Le mot de passe doit faire au moins 8 caractères.")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as db:
        email = email.strip().lower()
        broker = (await db.execute(select(Broker).where(Broker.email == email))).scalar_one_or_none()
        if not broker:
            broker = Broker(email=email, full_name=name, company="FicheAssur")
            db.add(broker)
        broker.role = BrokerRole.admin
        broker.status = BrokerStatus.active
        broker.password_hash = hash_password(password)
        await db.commit()
        print(f"Admin prêt : {email}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    asyncio.run(main(*sys.argv[1:]))
