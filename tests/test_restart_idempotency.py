"""Restart / response-loss semantics: idempotency state lives only in the DB,
so a brand new process replays the original receipt instead of re-applying.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import make_bundle, patient_entry, urn


def test_restart_replays_receipt_without_duplicate(client, engine):
    p, (system, value) = patient_entry(urn(), value="RESTART-1")
    bundle = make_bundle([p])

    first = TestClient(app).post(
        "/fhir", json=bundle, headers={"Idempotency-Key": "ACC-RESTART"}
    )
    assert first.status_code == 200
    first_id = first.json()["resourceMap"][0]["id"]

    # Simulate response loss: caller never saw the reply and retries on a fresh
    # process (new TestClient / new DB sessions, same database).
    second = TestClient(app).post(
        "/fhir", json=bundle, headers={"Idempotency-Key": "ACC-RESTART"}
    )
    assert second.status_code == 200
    assert second.json()["resourceMap"][0]["id"] == first_id

    # Exactly one patient row for the identifier.
    from sqlalchemy import func, select

    from app.models import Patient, PatientIdentifier

    from app.database import SessionLocal

    with SessionLocal() as session:
        count = session.execute(
            select(func.count())
            .select_from(PatientIdentifier)
            .where(
                PatientIdentifier.system == system,
                PatientIdentifier.value == value,
            )
        ).scalar_one()
        assert count == 1
        total_patients = session.execute(select(func.count()).select_from(Patient)).scalar_one()
        assert total_patients == 1
