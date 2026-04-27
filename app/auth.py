"""
Google OAuth helpers and session management.
"""
from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request
from starlette.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.config import settings
from app.database import SessionLocal
from app.models import User

oauth = OAuth()
oauth.register(
    name="google",
    client_id=settings.google_client_id,
    client_secret=settings.google_client_secret,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


async def get_current_user(request: Request) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    async with SessionLocal() as db:
        result = await db.execute(
            select(User).where(User.id == user_id).options(selectinload(User.group))
        )
        return result.scalar_one_or_none()


def require_login(request: Request):
    """Use as a dependency; redirects to /login if not authenticated."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return user_id


def login_redirect():
    return RedirectResponse(url="/login", status_code=302)
