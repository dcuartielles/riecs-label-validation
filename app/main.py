from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from app.config import settings
from app.database import init_db
from app.routers import auth, review, admin, stats, export, infograph, labels

app = FastAPI(title="Data Labelling Validation")

app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)


class NgrokSkipWarning(BaseHTTPMiddleware):
    """Adds the header that suppresses the ngrok browser warning page."""
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["ngrok-skip-browser-warning"] = "true"
        return response


app.add_middleware(NgrokSkipWarning)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)
app.include_router(review.router)
app.include_router(admin.router)
app.include_router(stats.router)
app.include_router(export.router)
app.include_router(infograph.router)
app.include_router(labels.router)


@app.on_event("startup")
async def startup():
    await init_db()


@app.get("/")
async def root(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/review", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@app.get("/_screenshot_auth")
async def screenshot_auth(request: Request, uid: int):
    """Localhost-only shim used by the screenshot script to bypass OAuth."""
    if request.client.host not in ("127.0.0.1", "::1"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    request.session["user_id"] = uid
    return JSONResponse({"ok": True})
