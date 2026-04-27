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
