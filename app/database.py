"""Database engine, session factory and schema creation."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        # Needed for FOR UPDATE / cross-thread access in the SQLite test profile.
        return {"connect_args": {"timeout": 30, "check_same_thread": False}}
    return {"pool_pre_ping": True}


engine = create_engine(settings.DATABASE_URL, future=True, **_engine_kwargs(settings.DATABASE_URL))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, rollback on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    # Import models so they register on the metadata before create_all.
    from app import models

    assert models.Base is Base
    Base.metadata.create_all(bind=engine)
