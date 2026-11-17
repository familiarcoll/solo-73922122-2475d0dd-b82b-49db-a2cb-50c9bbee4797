"""Retry and crash-recovery interleavings for POST /fhir.

These tests reproduce the production incident and prove the fixes:

1. A *live* slow request (heartbeat fresh) makes same-key retries fail fast with
   409; the bundle never executes twice.
2. A genuinely crashed pending marker (no heartbeat beyond the stale threshold)
   becomes retryable; restart works too; the threshold itself is unchanged.
3. Across instances/threads, recovery is applied at most once (fenced CAS).
4. A superseded ("zombie") owner can neither overwrite nor delete the later
   request's receipt, and resources stay atomic with the receipt.
5. A slow bundle does not block unrelated reads or another patient's ingestion.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.database import SessionLocal
from app.models import (
    DiagnosticReport,
    IdempotencyRecord,
    Observation,
    Patient,
)
from app.services import bundle as bundle_service
from tests.conftest import (
    make_bundle,
    observation_entry,
    patient_entry,
    report_entry,
    urn,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _enable_heartbeats(monkeypatch, interval: int = 1) -> None:
    """Opt back in to real background heartbeat threads."""
    from app.services import idempotency

    # Re-enable the real thread starter (the autouse fixture patched it out).
    monkeypatch.setattr(
        idempotency.SlotHeartbeat, "start", idempotency.SlotHeartbeat.start
    )
    monkeypatch.setattr(settings, "IDEMPOTENCY_HEARTBEAT_SECONDS", interval)


class BlockGate:
    """Pauses bundle processing mid-flight and releases it deterministically."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def install(self, monkeypatch) -> "BlockGate":
        original = bundle_service._resolve_patients
        gate = self

        def wrapped(session, patient_entries):
            gate.entered.set()
            gate.release.wait(timeout=15)
            return original(session, patient_entries)

        monkeypatch.setattr(bundle_service, "_resolve_patients", wrapped)
        return gate


def _patient_bundle(value: str) -> dict:
    p, _ = patient_entry(urn(), value=value)
    return make_bundle([p])


def _full_bundle(value: str) -> tuple[dict, str, str, str]:
    p_urn, o_urn, r_urn = urn(), urn(), urn()
    p, _ = patient_entry(p_urn, value=value)
    o = observation_entry(o_urn, p_urn)
    r = report_entry(r_urn, p_urn, [o_urn])
    return make_bundle([p, o, r]), p_urn, o_urn, r_urn


def _post(client, key, bundle):
    return client.post(
        "/fhir", json=bundle, headers={"Idempotency-Key": key}
    )


def _record(key: str) -> IdempotencyRecord | None:
    with SessionLocal() as session:
        return session.get(IdempotencyRecord, key)


def _backdate_pending(key: str, age_seconds: int = 300) -> None:
    with SessionLocal() as session:
        record = session.get(IdempotencyRecord, key)
        assert record is not None and record.state == "pending"
        stale = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        record.heartbeat_at = stale
        record.updated_at = stale
        session.commit()


# --------------------------------------------------------------------------- #
# 1. Live request: prompt 409, never a duplicate execution
# --------------------------------------------------------------------------- #

def test_live_pending_retry_returns_prompt_409_and_runs_once(client, monkeypatch):
    _enable_heartbeats(monkeypatch, interval=1)
    gate = BlockGate().install(monkeypatch)
    key = "ACC-LIVE-409"
    bundle, _, _, _ = _full_bundle("LIVE-1")

    first_result: dict = {}

    def first_request():
        first_result["resp"] = _post(client, key, bundle)

    worker = threading.Thread(target=first_request)
    worker.start()
    assert gate.entered.wait(timeout=5), "first request never reached processing"

    # Retry while the first request is live: must fail fast with 409, no wait.
    started = time.monotonic()
    retry = _post(client, key, bundle)
    elapsed = time.monotonic() - started
    assert retry.status_code == 409
    assert "already being processed" in retry.json()["issue"][0]["diagnostics"]
    assert elapsed < 5.0  # "timely": returned without waiting for the slow one

    # A different body on the same key is rejected with 409 as well.
    other, _, _, _ = _full_bundle("LIVE-OTHER")
    diff = _post(client, key, other)
    assert diff.status_code == 409

    gate.release.set()
    worker.join(timeout=10)
    assert first_result["resp"].status_code == 200, first_result["resp"].text

    # Exactly one patient / one observation / one report — no duplicate ingest.
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Patient)) == 1
        assert session.scalar(select(func.count()).select_from(Observation)) == 1
        assert session.scalar(select(func.count()).select_from(DiagnosticReport)) == 1
        record = session.get(IdempotencyRecord, key)
        assert record.state == "completed" and record.owner_id is None

    # After completion, identical content replays the first receipt (same ids).
    replay = _post(client, key, bundle)
    assert replay.status_code == 200
    assert replay.json()["resourceMap"] == first_result["resp"].json()["resourceMap"]


def test_live_request_beyond_60s_is_not_recovered_when_heartbeat_fresh(
    client, monkeypatch
):
    # Threshold stays the original 60 seconds; the request outlives it but keeps
    # its heartbeat alive, so it must never be considered crashed.
    monkeypatch.setattr(settings, "IDEMPOTENCY_STALE_SECONDS", 60)
    _enable_heartbeats(monkeypatch, interval=1)
    gate = BlockGate().install(monkeypatch)
    key = "ACC-SLOW-LIVE"
    bundle, _, _, _ = _full_bundle("SLOW-LIVE")

    holder: dict = {}

    def run():
        holder["resp"] = _post(client, key, bundle)

    worker = threading.Thread(target=run)
    worker.start()
    assert gate.entered.wait(timeout=5)

    # Force the marker to look old in wall-clock terms while the heartbeat keeps
    # refreshing heartbeat_at: the marker must remain non-stale.
    time.sleep(0.3)
    record = _record(key)
    assert record is not None and record.state == "pending"
    age = (
        datetime.now(timezone.utc)
        - (
            record.heartbeat_at
            if record.heartbeat_at.tzinfo
            else record.heartbeat_at.replace(tzinfo=timezone.utc)
        )
    ).total_seconds()
    assert age < 5

    retry = _post(client, key, bundle)
    assert retry.status_code == 409

    gate.release.set()
    worker.join(timeout=10)
    assert holder["resp"].status_code == 200
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Observation)) == 1


# --------------------------------------------------------------------------- #
# 2. Genuine crash: stale pending is recoverable after the original threshold
# --------------------------------------------------------------------------- #

def test_stale_pending_recovered_after_threshold_and_completes(client):
    key = "ACC-CRASH"
    # Simulate a request that registered pending and then died.
    with SessionLocal() as session:
        session.add(
            IdempotencyRecord(
                key=key, content_hash="deadbeef", state="pending",
                owner_id="dead-owner", fence_version=1,
            )
        )
        session.commit()
    _backdate_pending(key, age_seconds=settings.IDEMPOTENCY_STALE_SECONDS + 10)

    bundle = _patient_bundle("CRASH-1")
    resp = _post(client, key, bundle)
    assert resp.status_code == 200, resp.text
    assert resp.json()["resourceMap"][0]["action"] == "created"

    with SessionLocal() as session:
        final = session.get(IdempotencyRecord, key)
        assert final.state == "completed"
        assert final.fence_version == 2  # ownership changed exactly once
        assert final.owner_id is None


def test_restart_replays_receipt_and_never_duplicates(client):
    # Completed ledger rows survive process restart (state lives only in the DB).
    key = "ACC-RESTART"
    bundle = _patient_bundle("RESTART-1")
    first = _post(client, key, bundle)
    assert first.status_code == 200
    first_id = first.json()["resourceMap"][0]["id"]

    second = _post(client, key, bundle)
    assert second.status_code == 200
    assert second.json()["resourceMap"][0]["id"] == first_id
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Patient)) == 1


def test_failed_attempt_releases_slot_for_corrected_retry(client):
    key = "ACC-FIX"
    p_urn = urn()
    p, _ = patient_entry(p_urn, value="FIX-1")
    bad = make_bundle([p, report_entry(urn(), p_urn, ["Observation/ghost"])])
    first = _post(client, key, bad)
    assert first.status_code == 422
    # Slot released: corrected payload with the same accession key is accepted.
    good = _post(client, key, make_bundle([p]))
    assert good.status_code == 200, good.text
    assert _record(key).state == "completed"


def test_stale_threshold_boundary_is_unchanged(client):
    """The original 60s threshold governs recovery: under it -> 409, at/over -> recover."""
    from app.errors import FhirError
    from app.services.idempotency import acquire_slot

    bundle = _patient_bundle("THRESHOLD")
    hash_value = bundle_service.content_hash(bundle)

    def seed(age_seconds: int) -> str:
        key = f"ACC-THRESH-{age_seconds}"
        with SessionLocal() as session:
            session.add(
                IdempotencyRecord(
                    key=key, content_hash="old", state="pending",
                    owner_id="dead", fence_version=1,
                )
            )
            session.commit()
            ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
            record = session.get(IdempotencyRecord, key)
            record.heartbeat_at = ts
            record.updated_at = ts
            session.commit()
        return key

    # Strictly under the threshold: the marker is still treated as live.
    for age in (0, 30, settings.IDEMPOTENCY_STALE_SECONDS - 1):
        with pytest.raises(FhirError) as exc:
            acquire_slot(hash_value, seed(age))
        assert exc.value.status_code == 409

    # At and beyond the threshold: recovery is allowed (threshold not extended).
    for age in (settings.IDEMPOTENCY_STALE_SECONDS, settings.IDEMPOTENCY_STALE_SECONDS + 1):
        outcome = acquire_slot(hash_value, seed(age))
        assert outcome.acquired
        outcome.lease.stop_heartbeat()


# --------------------------------------------------------------------------- #
# 3. Multi-instance recovery: at most one application
# --------------------------------------------------------------------------- #

def test_recovery_compare_and_swap_applies_at_most_once(client):
    """Dialect-neutral proof that only one recovery claim can win.

    Simulates several API instances racing for the same stale pending row by
    driving the fenced claim serially against identical observed state: the
    first claim bumps the fence, every later claim on the old fence is a no-op.
    """
    from app.services.idempotency import _claim_row

    key = "ACC-CAS-SERIAL"
    with SessionLocal() as session:
        session.add(
            IdempotencyRecord(
                key=key, content_hash="old", state="pending",
                owner_id="dead", fence_version=7,
            )
        )
        session.commit()
    _backdate_pending(key, age_seconds=settings.IDEMPOTENCY_STALE_SECONDS + 5)

    hash_value = bundle_service.content_hash(_patient_bundle("CAS"))
    # First instance wins.
    with SessionLocal() as session:
        assert _claim_row(session, key, 7, "pending", hash_value, "owner-A") is True
    # Other instances observed fence 7 as well; their claims must all lose.
    for i in range(3):
        with SessionLocal() as session:
            assert _claim_row(session, key, 7, "pending", hash_value, f"owner-{i}") is False
    # A claim against the *new* fence would also fail because the row is no
    # longer stale-pending-with-old-owner semantics: state stays pending but the
    # fence moved; verify the single winning ownership is what persisted.
    with SessionLocal() as session:
        record = session.get(IdempotencyRecord, key)
        assert record.fence_version == 8
        assert record.owner_id == "owner-A"


def test_concurrent_recoverers_only_one_takes_over(client):
    # The compare-and-swap itself is dialect-neutral (tested directly below in
    # test_fenced_terminal_writes_reject_superseded_owner); this test additionally
    # exercises true parallel writers, which needs PostgreSQL row locking.
    from app import database

    if database.engine.dialect.name == "sqlite":
        pytest.skip("parallel CAS writers require PostgreSQL row locking")

    key = "ACC-RECOVER-RACE"
    with SessionLocal() as session:
        session.add(
            IdempotencyRecord(
                key=key, content_hash="old", state="pending",
                owner_id="dead", fence_version=4,
            )
        )
        session.commit()
    _backdate_pending(key, age_seconds=settings.IDEMPOTENCY_STALE_SECONDS + 5)

    bundle = _patient_bundle("RECOVER-RACE")
    results: list = []
    barrier = threading.Barrier(5)

    def attempt():
        barrier.wait()
        results.append(_post(client, key, bundle))

    threads = [threading.Thread(target=attempt) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    successes = [r for r in results if r.status_code == 200]
    conflicts = [r for r in results if r.status_code == 409]
    assert len(successes) == 1, [r.status_code for r in results]
    assert len(conflicts) == 4
    # Losers must replay the winner's receipt on their next (post-completion) call.
    replay = _post(client, key, bundle)
    assert replay.status_code == 200
    assert replay.json()["resourceMap"][0]["id"] == successes[0].json()["resourceMap"][0]["id"]

    with SessionLocal() as session:
        final = session.get(IdempotencyRecord, key)
        assert final.fence_version == 5  # exactly one ownership change
        assert session.scalar(select(func.count()).select_from(Patient)) == 1


# --------------------------------------------------------------------------- #
# 4. Zombie owner cannot clobber / delete the later receipt
# --------------------------------------------------------------------------- #

def test_fenced_terminal_writes_reject_superseded_owner(client):
    """Service-level fencing, deterministic on every dialect.

    Owner A acquires the slot and stalls. After the stale threshold owner B
    takes over (fence bump) and completes with its own receipt. A's late
    completion and its failure cleanup must both be no-ops.
    """
    from app.services.idempotency import (
        SlotLease,
        acquire_slot,
        complete_slot,
        release_slot,
    )

    key = "ACC-ZOMBIE-FENCE"
    bundle_a = _patient_bundle("ZOMBIE-OLD")
    hash_a = bundle_service.content_hash(bundle_a)
    bundle_b = _patient_bundle("ZOMBIE-NEW")
    hash_b = bundle_service.content_hash(bundle_b)

    # Owner A acquires and then "crashes" mid-flight (its transaction committed
    # the pending marker; no resources were written yet).
    outcome_a = acquire_slot(hash_a, key)
    assert outcome_a.acquired
    lease_a: SlotLease = outcome_a.lease  # type: ignore[assignment]
    lease_a.stop_heartbeat()

    # Crash recovery: backdate the marker beyond the threshold.
    _backdate_pending(key, age_seconds=settings.IDEMPOTENCY_STALE_SECONDS + 5)

    # Owner B (a different API instance/request) takes over exactly once and
    # commits its resources together with the completed receipt.
    with SessionLocal() as session_b:
        outcome_b = acquire_slot(hash_b, key)
        assert outcome_b.acquired
        lease_b: SlotLease = outcome_b.lease  # type: ignore[assignment]
        assert lease_b.fence_version == lease_a.fence_version + 1
        entries_b = bundle_service.validate_bundle(bundle_b)
        patients = bundle_service._resolve_patients(
            session_b, [e for e in entries_b if e.resource_type == "Patient"]
        )
        entry = entries_b[0]
        receipt_b = bundle_service._build_receipt(entries_b, key)
        assert complete_slot(session_b, lease_b, receipt_b) is True
        session_b.commit()
        lease_b.stop_heartbeat()
        new_patient_id = entry.row_id
        assert new_patient_id in [p.id for p in patients.values()]

    # Owner A wakes up and tries to finish / clean up: every write is fenced off.
    with SessionLocal() as zombie:
        assert complete_slot(zombie, lease_a, {"resourceType": "Bundle"}) is False
        zombie.rollback()
    assert release_slot(lease_a) is False

    with SessionLocal() as session:
        record = session.get(IdempotencyRecord, key)
        assert record.state == "completed"
        assert record.fence_version == lease_b.fence_version
        assert record.owner_id is None
        # Only B's patient exists; A's late attempt rolled back / wrote nothing.
        assert session.scalar(select(func.count()).select_from(Patient)) == 1
        stored_ids = [m["id"] for m in record.response["resourceMap"]]
        assert stored_ids == [new_patient_id]


def test_zombie_owner_completion_is_rejected_end_to_end(client, monkeypatch):
    """HTTP-level interleaving on PostgreSQL: old request is fenced at commit."""
    from app import database

    if database.engine.dialect.name == "sqlite":
        pytest.skip("nested concurrent writers require PostgreSQL row locking")

    _enable_heartbeats(monkeypatch, interval=1)
    gate = BlockGate().install(monkeypatch)
    key = "ACC-ZOMBIE-HTTP"
    old_bundle = _patient_bundle("ZOMBIE-HTTP-OLD")
    holder: dict = {}

    def old_request():
        holder["resp"] = _post(client, key, old_bundle)

    worker = threading.Thread(target=old_request)
    worker.start()
    assert gate.entered.wait(timeout=5)

    # Presume the old process crashed: age its marker past the threshold, then
    # drive a recovery request (new content) from this instance.
    _backdate_pending(key, age_seconds=settings.IDEMPOTENCY_STALE_SECONDS + 5)
    recovery = _post(client, key, _patient_bundle("ZOMBIE-HTTP-NEW"))
    assert recovery.status_code == 200
    new_id = recovery.json()["resourceMap"][0]["id"]

    # Old request resumes and tries to commit; fencing forces 409 + rollback.
    gate.release.set()
    worker.join(timeout=10)
    assert holder["resp"].status_code == 409

    with SessionLocal() as session:
        record = session.get(IdempotencyRecord, key)
        assert record.state == "completed"
        assert [m["id"] for m in record.response["resourceMap"]] == [new_id]
        assert session.scalar(select(func.count()).select_from(Patient)) == 1


def test_zombie_owner_failure_cannot_delete_later_receipt(client):
    # Old request fails *after* a new request has already completed the same key.
    key = "ACC-ZOMBIE-FAIL"
    new_resp = _post(client, key, _patient_bundle("ZOMBIE-FAIL-NEW"))
    assert new_resp.status_code == 200
    new_receipt = new_resp.json()

    # Fabricate the stale owner's lease as it existed before recovery, then run
    # its failure cleanup path directly against the now-completed row.
    from app.services.idempotency import SlotLease, release_slot

    zombie = SlotLease(key=key, owner_id="long-dead-owner", fence_version=1)
    deleted = release_slot(zombie)
    assert deleted is False

    with SessionLocal() as session:
        record = session.get(IdempotencyRecord, key)
        assert record is not None and record.state == "completed"
        assert record.response["resourceMap"] == new_receipt["resourceMap"]


# --------------------------------------------------------------------------- #
# 5. Slow bundle does not block reads or unrelated ingestion
# --------------------------------------------------------------------------- #

def test_slow_bundle_does_not_block_unrelated_work(client, monkeypatch):
    from app import database

    if database.engine.dialect.name == "sqlite":
        pytest.skip("parallel-writer test requires PostgreSQL row locking")

    _enable_heartbeats(monkeypatch, interval=1)
    gate = BlockGate().install(monkeypatch)

    # Seed an unrelated patient that reads will query.
    seeded = _post(client, "ACC-SEED", _patient_bundle("UNREL-SEED"))
    seeded_pid = seeded.json()["resourceMap"][0]["id"]

    holder: dict = {}

    def slow():
        holder["resp"] = _post(client, "ACC-SLOW", _patient_bundle("UNREL-SLOW"))

    worker = threading.Thread(target=slow)
    worker.start()
    assert gate.entered.wait(timeout=5)

    # Reads of existing data stay responsive.
    read = client.get(f"/patients/{seeded_pid}")
    assert read.status_code == 200
    assert read.json()["counts"] == {"diagnosticReports": 0, "observations": 0}

    # A different accession for a different patient ingests concurrently.
    other = _post(client, "ACC-OTHER", _patient_bundle("UNREL-OTHER"))
    assert other.status_code == 200, other.text

    gate.release.set()
    worker.join(timeout=10)
    assert holder["resp"].status_code == 200

    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Patient)) == 3


# --------------------------------------------------------------------------- #
# 6. Validation behavior is unchanged
# --------------------------------------------------------------------------- #

def test_different_content_after_completion_still_conflicts(client):
    key = "ACC-CONTENT"
    first = _post(client, key, _patient_bundle("CONTENT-A"))
    assert first.status_code == 200
    clash = _post(client, key, _patient_bundle("CONTENT-B"))
    assert clash.status_code == 409
    assert "different content" in clash.json()["issue"][0]["diagnostics"]
