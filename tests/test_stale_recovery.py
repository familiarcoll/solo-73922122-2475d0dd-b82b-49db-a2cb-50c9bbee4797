"""Crash recovery: a pending idempotency record older than the stale threshold
represents a crashed process and must not block retries forever.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import IdempotencyRecord
from tests.conftest import make_bundle, patient_entry, urn


def test_stale_pending_slot_is_recovered(client):
    # Simulate a process that inserted its pending marker then crashed.
    stale_key = "ACC-CRASHED"
    with SessionLocal() as session:
        record = IdempotencyRecord(
            key=stale_key, content_hash="deadbeef", state="pending"
        )
        session.add(record)
        session.commit()
        # Backdate it beyond the staleness threshold.
        record.updated_at = datetime.now(timezone.utc) - timedelta(seconds=300)
        session.commit()

    p, _ = patient_entry(urn(), value="CRASH-RECOVERY")
    resp = client.post(
        "/fhir", json=make_bundle([p]), headers={"Idempotency-Key": stale_key}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["resourceMap"][0]["action"] == "created"

    # Ledger now shows the request as completed.
    with SessionLocal() as session:
        final = session.get(IdempotencyRecord, stale_key)
        assert final.state == "completed"
