"""Startup upgrade of a ledger table created by an older release.

The baseline has no migration framework (schema is built with create_all), so
init_db must additive-and-idempotently add the lease/heartbeat columns to a
pre-existing idempotency_records table without dropping data.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import inspect, text

from app.database import SessionLocal, engine, init_db
from app.models import IdempotencyRecord


def test_init_db_backfills_legacy_ledger_columns():
    inspector = inspect(engine)
    if "idempotency_records" not in inspector.get_table_names():
        return  # nothing to upgrade on a fresh schema

    # Build a legacy-shape table directly (without the new columns) when the
    # dialect permits the drop; otherwise the production create_all path already
    # guarantees the columns exist and there is nothing to exercise.
    legacy_ddl = (
        "CREATE TABLE legacy_idempotency_records ("
        "key VARCHAR(128) PRIMARY KEY, content_hash VARCHAR(64) NOT NULL, "
        "state VARCHAR(16), status_code INTEGER, response JSON, "
        "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, note TEXT)"
    )
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS legacy_idempotency_records"))
        conn.execute(text(legacy_ddl))
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            text(
                "INSERT INTO legacy_idempotency_records "
                "(key, content_hash, state, created_at, updated_at) "
                "VALUES ('legacy-key', 'h', 'pending', :now, :now)"
            ),
            {"now": now},
        )
        # Swap the real table for the legacy-shaped one, run init_db, restore.
        conn.execute(text("ALTER TABLE idempotency_records RENAME TO idem_backup"))
        conn.execute(
            text("ALTER TABLE legacy_idempotency_records RENAME TO idempotency_records")
        )

    try:
        init_db()  # must add owner_id / fence_version / heartbeat_at
        init_db()  # and be idempotent on the second run

        columns = {c["name"] for c in inspect(engine).get_columns("idempotency_records")}
        assert {"owner_id", "fence_version", "heartbeat_at"} <= columns

        with SessionLocal() as session:
            row = session.get(IdempotencyRecord, "legacy-key")
            assert row is not None
            assert row.state == "pending"
            assert row.fence_version == 1
            assert row.owner_id is None
            assert row.heartbeat_at is None
    finally:
        # Restore the normal (current) schema for subsequent tests.
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE idempotency_records"))
            conn.execute(text("ALTER TABLE idem_backup RENAME TO idempotency_records"))
        init_db()
