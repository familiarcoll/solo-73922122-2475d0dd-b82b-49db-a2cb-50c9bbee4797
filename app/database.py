"""Database engine, session factory and schema creation."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        # Needed for FOR UPDATE / cross-thread access in the SQLite test profile.
        return {"connect_args": {"timeout": 30, "check_same_thread": False}}
    return {"pool_pre_ping": True}


def make_engine(url: str):
    """Create an engine with the dialect-specific transaction fixes applied."""
    eng = create_engine(url, future=True, **_engine_kwargs(url))
    if eng.dialect.name == "sqlite":
        _install_sqlite_transaction_fix(eng)
    return eng


def _install_sqlite_transaction_fix(eng) -> None:
    # The stdlib pysqlite driver opens its own implicit transaction on the first
    # DML statement and silently commits it when RELEASE SAVEPOINT is executed
    # (its do_begin is a no-op, so no explicit BEGIN ever wraps the savepoint).
    # That made a later session.rollback() a no-op after begin_nested(), which
    # broke atomicity of failed bundles on the SQLite test profile. Put the
    # driver in manual mode and let SQLAlchemy own every transaction boundary
    # with an explicit BEGIN; PostgreSQL is unaffected by this branch.
    @event.listens_for(eng, "connect")
    def _sqlite_manual_transactions(dbapi_connection, _connection_record) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(eng, "begin")
    def _sqlite_emit_begin(connection) -> None:
        connection.connection.dbapi_connection.execute("BEGIN")


engine = make_engine(settings.DATABASE_URL)
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


def _backfill_idempotency_columns() -> None:
    """Add lease/heartbeat columns to a ledger table created by an older version.

    The project creates its schema with ``create_all`` (no migration framework);
    these statements are additive and idempotent, so a restart against a
    pre-existing database upgrades it in place. A fresh schema already has the
    columns and each check is a no-op there.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "idempotency_records" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("idempotency_records")}
    additions = [
        ("owner_id", "VARCHAR(64)"),
        ("fence_version", "INTEGER"),
        ("heartbeat_at", "TIMESTAMP WITH TIME ZONE"),
    ]
    with engine.begin() as conn:
        for name, ddl_type in additions:
            if name in existing:
                continue
            conn.execute(
                text(f"ALTER TABLE idempotency_records ADD COLUMN {name} {ddl_type}")
            )
        # Backfill: legacy pending rows become fence v1 owned by nobody; they are
        # still recoverable once their timestamp passes the stale threshold.
        if "fence_version" not in existing:
            conn.execute(
                text("UPDATE idempotency_records SET fence_version = 1 "
                     "WHERE fence_version IS NULL")
            )


def init_db() -> None:
    # Import models so they register on the metadata before create_all.
    from app import models

    assert models.Base is Base
    Base.metadata.create_all(bind=engine)
    _backfill_idempotency_columns()
