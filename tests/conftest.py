"""Shared pytest fixtures.

Tests run against a real PostgreSQL when DATABASE_URL points at one (the
Docker/CI profile). A SQLite fallback keeps the full suite runnable offline;
locking-dependent concurrency tests skip themselves there, while the
idempotency retry/recovery interleavings are exercised on every profile
because they rely on fenced conditional UPDATEs, not dialect-specific locks.
"""
from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

# PostgreSQL when provided (docker/CI), otherwise an offline SQLite database.
_DEFAULT_SQLITE = "sqlite:////tmp/researchdb_test.sqlite3"
os.environ.setdefault("DATABASE_URL", os.getenv("TEST_DATABASE_URL", _DEFAULT_SQLITE))

from app import database  # noqa: E402
from app.database import Base, make_engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def engine():
    # Reuse the app's engine factory so SQLite transaction fixes apply in tests
    # exactly as they do in the running service.
    eng = make_engine(os.environ["DATABASE_URL"])
    Base.metadata.drop_all(bind=eng)
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db_session(engine):
    """Clean schema per test for full isolation."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    connection = engine.connect()
    transaction = connection.begin()
    TestingSession = sessionmaker(
        bind=connection, autoflush=False, expire_on_commit=False, future=True
    )
    session = TestingSession()
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture(autouse=True)
def _disable_background_heartbeat(monkeypatch):
    """Default: no background heartbeat threads (deterministic, SQLite-safe).

    Tests that exercise liveness explicitly opt back in by setting
    IDEMPOTENCY_HEARTBEAT_SECONDS to a small value.
    """
    from app.config import settings
    from app.services import idempotency

    monkeypatch.setattr(settings, "IDEMPOTENCY_HEARTBEAT_SECONDS", 10_000_000)
    monkeypatch.setattr(
        idempotency.SlotHeartbeat,
        "start",
        lambda self: self,
    )
    yield


@pytest.fixture()
def client(engine):
    """HTTP client with a fresh schema; the app uses its own engine/sessions."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # Point the app engine at the same database (already configured via env).
    database.engine.dispose()
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


# --------------------------------------------------------------------------- #
# Bundle builders
# --------------------------------------------------------------------------- #

def urn() -> str:
    return f"urn:uuid:{uuid.uuid4()}"


def patient_entry(pid_urn: str, system: str = "http://hospital/mrn", value: str | None = None):
    value = value or f"MRN-{uuid.uuid4().hex[:8]}"
    return {
        "fullUrl": pid_urn,
        "resource": {
            "resourceType": "Patient",
            "identifier": [{"system": system, "value": value}],
            "name": [{"family": "Test", "given": ["Case"]}],
        },
        "request": {"method": "POST", "url": "Patient"},
    }, (system, value)


def observation_entry(
    obs_urn: str,
    subject_ref: str,
    code: str = "GLU",
    value: float = 5.5,
):
    return {
        "fullUrl": obs_urn,
        "resource": {
            "resourceType": "Observation",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
            "subject": {"reference": subject_ref},
            "valueQuantity": {"value": value, "unit": "mmol/L"},
        },
        "request": {"method": "POST", "url": "Observation"},
    }


def report_entry(report_urn: str, subject_ref: str, result_refs: list[str]):
    return {
        "fullUrl": report_urn,
        "resource": {
            "resourceType": "DiagnosticReport",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "24323-8"}]},
            "subject": {"reference": subject_ref},
            "result": [{"reference": r} for r in result_refs],
        },
        "request": {"method": "POST", "url": "DiagnosticReport"},
    }


def make_bundle(entries: list[dict]) -> dict:
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}
