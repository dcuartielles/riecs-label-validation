from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from app.auth import oauth
from app.config import settings
from app.database import SessionLocal
from app.models import User

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/review", status_code=302)
    google_configured = bool(settings.google_client_id)
    return templates.TemplateResponse(request, "login.html", {
        "google_configured": google_configured,
    })


@router.get("/auth/google")
async def auth_google(request: Request):
    redirect_uri = f"{settings.base_url}/auth/google/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/google/callback")
async def auth_google_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo")
    if not userinfo:
        return RedirectResponse(url="/login?error=1", status_code=302)

    email = userinfo["email"]
    google_id = userinfo["sub"]
    name = userinfo.get("name", email)

    async with SessionLocal() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if user is None:
            # Auto-create account on first login; admin assigns group later
            user = User(email=email, google_id=google_id, name=name)
            db.add(user)
            await db.commit()
            await db.refresh(user)
        elif user.google_id is None:
            user.google_id = google_id
            await db.commit()

    request.session["user_id"] = user.id
    return RedirectResponse(url="/review", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
