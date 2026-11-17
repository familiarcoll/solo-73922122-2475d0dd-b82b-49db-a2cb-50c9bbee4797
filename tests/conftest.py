"""Shared pytest fixtures.

Tests run against a real PostgreSQL by default (DATABASE_URL env). A SQLite
fallback keeps the suite runnable offline; locking-dependent concurrency tests
are skipped there.
"""
from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://research@localhost:5433/researchdb_test?host=/tmp",
)

from app import database  # noqa: E402
from app.database import Base  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(
        os.environ["DATABASE_URL"],
        future=True,
        **(
            {"connect_args": {"check_same_thread": False}}
            if os.environ["DATABASE_URL"].startswith("sqlite")
            else {}
        ),
    )
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
