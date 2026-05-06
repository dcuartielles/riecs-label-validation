from fastapi.templating import Jinja2Templates
from app.config import APP_VERSION

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["APP_VERSION"] = APP_VERSION
