import re
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    google_client_id: str = ""
    google_client_secret: str = ""
    secret_key: str = "dev-secret-change-in-production"
    database_url: str = "sqlite+aiosqlite:///./labelling.db"
    base_url: str = "http://localhost:8000"

    class Config:
        env_file = ".env"


settings = Settings()

APP_VERSION = "v005"


def find_latest_dataset() -> Path:
    """Return the highest-versioned dataset file in input_data/."""
    candidates = list(Path("input_data").glob("*.xlsx"))
    versioned = []
    for p in candidates:
        m = re.search(r'_v(\d+)', p.stem, re.IGNORECASE)
        if m:
            versioned.append((int(m.group(1)), p))
    if not versioned:
        raise FileNotFoundError("No versioned dataset file found in input_data/")
    versioned.sort(key=lambda x: x[0])
    _, latest = versioned[-1]
    return latest
