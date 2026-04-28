from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from app.config import settings

engine = create_async_engine(settings.database_url, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add columns introduced after initial schema (safe to run repeatedly)
        for stmt in [
            "ALTER TABLE taxonomy_labels ADD COLUMN is_user_created INTEGER DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN started_by INTEGER REFERENCES users(id)",
        ]:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass  # column already exists
