"""
Create or promote a user to admin by email address.
Run this after the first Google login to grant yourself admin access.

Usage:
    python -m scripts.create_admin your@email.com
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from app.database import init_db, SessionLocal
from app.models import User


async def run(email: str):
    await init_db()
    async with SessionLocal() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if user is None:
            user = User(email=email, name=email, is_admin=True)
            db.add(user)
            print(f"Created admin user: {email}")
        else:
            user.is_admin = True
            print(f"Promoted existing user to admin: {email}")

        await db.commit()
    print("Done. Log in with Google to access the admin panel.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.create_admin your@email.com")
        sys.exit(1)
    asyncio.run(run(sys.argv[1]))
