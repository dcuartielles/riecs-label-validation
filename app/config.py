import re
import subprocess
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

def _git_version() -> str:
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True, text=True,
            cwd=Path(__file__).parent.parent,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "dev"

APP_VERSION = _git_version()


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
