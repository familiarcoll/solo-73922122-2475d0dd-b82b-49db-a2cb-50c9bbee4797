"""Idempotency ledger: slot acquisition, liveness heartbeat, fencing and
crash recovery for POST /fhir submissions.

Correctness goals (see README "重试与恢复语义"):

* A *live* request keeps refreshing ``heartbeat_at``; a concurrent retry with
  the same key fails fast with 409 and never executes the bundle twice.
* A pending row whose heartbeat is older than ``IDEMPOTENCY_STALE_SECONDS``
  (the original 60s threshold, unchanged) is presumed crashed. Recovery is an
  atomic, fence-guarded UPDATE, so across any number of API instances the slot
  changes hands at most once.
* Every terminal write (completion or failure cleanup) is conditional on the
  owner's ``(owner_id, fence_version)``. A superseded "zombie" request can
  therefore neither overwrite nor delete a later request's receipt.
* Completion is written inside the caller's resource transaction, so resources
  and receipt still commit (and roll back) atomically.

Session discipline: slot *acquisition* and failure *cleanup* use short-lived
sessions of their own, so they never interleave with the request's resource
transaction (important for SQLite, which serializes writers). Only
``complete_slot`` joins the request transaction, by design, to keep the
receipt atomic with the resources.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.errors import conflict
from app.models import IdempotencyRecord


def new_owner_id() -> str:
    return uuid.uuid4().hex


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite stores naive datetimes
        return value.replace(tzinfo=timezone.utc)
    return value


def liveness_moment(record: IdempotencyRecord) -> datetime | None:
    """Timestamp deciding whether a pending record is stale.

    ``heartbeat_at`` for rows written by this code; ``updated_at``/``created_at``
    as a fallback for rows left behind by older versions.
    """
    return (
        _aware(record.heartbeat_at)
        or _aware(record.updated_at)
        or _aware(record.created_at)
    )


def marker_age_seconds(
    record: IdempotencyRecord, now: datetime | None = None
) -> float:
    moment = liveness_moment(record)
    if moment is None:
        return 0.0
    return ((now or datetime.now(timezone.utc)) - moment).total_seconds()


# --------------------------------------------------------------------------- #
# Leases
# --------------------------------------------------------------------------- #

@dataclass
class SlotLease:
    """Ownership of a pending idempotency slot."""

    key: str
    owner_id: str
    fence_version: int
    heartbeat: "SlotHeartbeat | None" = None

    def stop_heartbeat(self) -> None:
        if self.heartbeat is not None:
            self.heartbeat.stop()


@dataclass
class SlotOutcome:
    """Either a lease to process the bundle, or a stored response to replay."""

    lease: SlotLease | None = None
    replay: dict | None = None

    @property
    def acquired(self) -> bool:
        return self.lease is not None


class SlotHeartbeat:
    """Background liveness refresher for one in-flight request.

    Each tick uses a short-lived, independent session so it never blocks on the
    request's main resource transaction. Updates are fenced: once ownership is
    lost the thread touches nothing and stops by itself.
    """

    def __init__(
        self,
        key: str,
        owner_id: str,
        fence_version: int,
        interval: float | None = None,
    ) -> None:
        self.key = key
        self.owner_id = owner_id
        self.fence_version = fence_version
        self.interval = max(
            1.0, float(interval if interval is not None else settings.IDEMPOTENCY_HEARTBEAT_SECONDS)
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"idem-hb-{key[:16]}", daemon=True
        )

    def start(self) -> "SlotHeartbeat":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                with SessionLocal() as heartbeat_session:
                    result = heartbeat_session.execute(
                        update(IdempotencyRecord)
                        .where(
                            IdempotencyRecord.key == self.key,
                            IdempotencyRecord.owner_id == self.owner_id,
                            IdempotencyRecord.fence_version == self.fence_version,
                            IdempotencyRecord.state == "pending",
                        )
                        .values(heartbeat_at=datetime.now(timezone.utc))
                    )
                    heartbeat_session.commit()
                    if (result.rowcount or 0) == 0:
                        # Recovered by another instance (or row gone): stop.
                        self._stop.set()
            except Exception:
                # A missed tick is tolerable within the stale threshold; the
                # commit-time fencing in complete_slot is the hard guarantee.
                pass


def _start_heartbeat(key: str, owner_id: str, fence_version: int) -> SlotLease:
    heartbeat = SlotHeartbeat(key, owner_id, fence_version).start()
    return SlotLease(
        key=key, owner_id=owner_id, fence_version=fence_version, heartbeat=heartbeat
    )


# --------------------------------------------------------------------------- #
# Acquisition (own short-lived transactions, independent of the request txn)
# --------------------------------------------------------------------------- #

def _dialect_is_sqlite(session: Session) -> bool:
    return session.bind.dialect.name == "sqlite"  # type: ignore[union-attr]


def _load_record(session: Session, key: str) -> IdempotencyRecord | None:
    if _dialect_is_sqlite(session):
        # populate_existing() bypasses a possibly stale identity-map copy so the
        # decision always reflects the latest committed fence_version/state.
        return (
            session.execute(
                select(IdempotencyRecord)
                .where(IdempotencyRecord.key == key)
                .execution_options(populate_existing=True)
            )
            .scalars()
            .one_or_none()
        )
    # Row lock serializes concurrent retries / recoverers on the same key. The
    # lock is released as soon as this short acquisition transaction commits.
    return session.execute(
        select(IdempotencyRecord)
        .where(IdempotencyRecord.key == key)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _claim_row(
    session: Session,
    key: str,
    expected_fence: int,
    expected_state: str,
    hash_value: str,
    owner_id: str,
) -> bool:
    """Atomically take ownership of an existing row.

    The predicate on (fence_version, state) makes this a compare-and-swap:
    at most one concurrent recoverer can win, even across API instances.
    Returns ``True`` when this caller won ownership, ``False`` otherwise.
    """
    now = datetime.now(timezone.utc)
    result = session.execute(
        update(IdempotencyRecord)
        .where(
            IdempotencyRecord.key == key,
            IdempotencyRecord.fence_version == expected_fence,
            IdempotencyRecord.state == expected_state,
        )
        .values(
            state="pending",
            content_hash=hash_value,
            owner_id=owner_id,
            fence_version=expected_fence + 1,
            heartbeat_at=now,
            updated_at=now,
            status_code=None,
            response=None,
            note=None,
        )
    )
    session.commit()
    return (result.rowcount or 0) > 0


def acquire_slot(hash_value: str, key: str) -> SlotOutcome:
    """Acquire the idempotency slot for *key*.

    Returns a lease when the caller may process the bundle, or a stored
    completed response that must be replayed. Raises 409 when a live request
    still owns the slot, or when a completed submission used different content.

    Deliberately opens its own sessions so the pending marker commits
    independently of the request's resource transaction.
    """
    owner_id = new_owner_id()
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        session.add(
            IdempotencyRecord(
                key=key,
                content_hash=hash_value,
                state="pending",
                owner_id=owner_id,
                fence_version=1,
                heartbeat_at=now,
            )
        )
        try:
            session.commit()
            return SlotOutcome(lease=_start_heartbeat(key, owner_id, 1))
        except IntegrityError:
            session.rollback()

    while True:
        with SessionLocal() as session:
            existing = _load_record(session, key)
            if existing is None:  # pragma: no cover - extremely rare delete race
                continue

            if existing.state == "completed":
                if existing.content_hash != hash_value:
                    raise conflict(
                        f"Idempotency-Key '{key}' was already submitted with "
                        f"different content; reuse the key only for identical retries."
                    )
                return SlotOutcome(replay=existing.response)

            current_fence = existing.fence_version
            new_fence = current_fence + 1

            if existing.state == "failed":
                # The previous attempt ended; the key is reusable immediately.
                if _claim_row(
                    session, key, current_fence, "failed", hash_value, owner_id
                ):
                    return SlotOutcome(
                        lease=_start_heartbeat(key, owner_id, new_fence)
                    )
                continue

            # state == pending: either a live concurrent request or crashed orphan.
            age = marker_age_seconds(existing, now)
            if age < settings.IDEMPOTENCY_STALE_SECONDS:
                # Fresh heartbeat => the owner is alive. Fail fast; never run the
                # bundle a second time while the first request may still commit.
                raise conflict(
                    f"A request with Idempotency-Key '{key}' is already being "
                    f"processed; wait for it to finish or retry after the recovery "
                    f"threshold."
                )

            # Presumed crashed (no heartbeat for longer than the original
            # threshold). Fenced recovery: exactly one instance/request wins.
            if _claim_row(
                session, key, current_fence, "pending", hash_value, owner_id
            ):
                return SlotOutcome(
                    lease=_start_heartbeat(key, owner_id, new_fence)
                )
            # Lost the race to another recoverer; re-read and re-decide.
            continue


# --------------------------------------------------------------------------- #
# Terminal transitions (fenced)
# --------------------------------------------------------------------------- #

def complete_slot(
    session: Session, lease: SlotLease, response: dict
) -> bool:
    """Mark the slot completed inside the caller's resource transaction.

    The update is conditional on the lease, so a zombie owner cannot replace a
    later receipt. Returns ``False`` when ownership was lost; the caller must
    roll back its resource transaction and surface a 409 instead of committing.
    """
    now = datetime.now(timezone.utc)
    result = session.execute(
        update(IdempotencyRecord)
        .where(
            IdempotencyRecord.key == lease.key,
            IdempotencyRecord.owner_id == lease.owner_id,
            IdempotencyRecord.fence_version == lease.fence_version,
            IdempotencyRecord.state == "pending",
        )
        .values(
            state="completed",
            status_code=200,
            response=response,
            owner_id=None,
            heartbeat_at=None,
            updated_at=now,
        )
    )
    return (result.rowcount or 0) > 0


def release_slot(lease: SlotLease) -> bool:
    """Delete a still-owned pending slot after a failed, correctable attempt.

    Fenced delete in its own transaction: a superseded owner changes nothing and
    can never remove a later request's completed receipt.
    """
    with SessionLocal() as session:
        result = session.execute(
            delete(IdempotencyRecord).where(
                IdempotencyRecord.key == lease.key,
                IdempotencyRecord.owner_id == lease.owner_id,
                IdempotencyRecord.fence_version == lease.fence_version,
                IdempotencyRecord.state == "pending",
            )
        )
        session.commit()
        return (result.rowcount or 0) > 0
