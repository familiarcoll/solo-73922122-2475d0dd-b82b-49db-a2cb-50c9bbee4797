"""Validation and transactional processing of FHIR R4 transaction Bundles.

Guarantees implemented here:

* At most 20 entries, restricted to Patient / Observation / DiagnosticReport.
* Patients are conditionally created on identifier (system/value); the database
  unique constraint plus row locks make concurrent identical submissions create
  exactly one patient.
* Observations and reports are created (POST) or version-checked updates
  (PUT + If-Match); a stale/missing version rejects the whole bundle.
* Out-of-order urn:uuid forward references are resolved after parsing.
* Every report and its observations must belong to the same patient; moving an
* observation to another patient is rejected unless every referencing report is
* corrected in the same bundle.
* All writes commit in one transaction: any failure leaves nothing behind.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import (
    FhirError,
    bad_request,
    conflict,
    not_found,
    precondition_failed,
    unprocessable,
)
from app.fhir_utils import etag, parse_etag
from app.models import (
    DiagnosticReport,
    IdempotencyRecord,
    Observation,
    Patient,
    PatientIdentifier,
    ReportLink,
    ResourceVersion,
)

ALLOWED_TYPES = {"Patient", "Observation", "DiagnosticReport"}
URN_UUID_RE = re.compile(r"^urn:uuid:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ABSOLUTE_REF_RE = re.compile(r"^(Patient|Observation|DiagnosticReport)/([A-Za-z0-9_.\-]+)$")


# --------------------------------------------------------------------------- #
# Parsed representation
# --------------------------------------------------------------------------- #

@dataclass
class ParsedEntry:
    index: int
    resource_type: str
    method: str                      # POST | PUT
    url: str
    resource: dict
    full_url: str | None = None
    if_match: int | None = None
    update_id: str | None = None    # target resource id for PUT
    subject_ref: str | None = None  # raw subject reference string
    # Filled in during database processing
    row_id: str | None = None
    version: int | None = None
    action: str | None = None       # created | updated | existing
    subject_pid: str | None = None  # resolved patient id
    result_obs_ids: list[str] = field(default_factory=list)


def _is_sqlite(session: Session) -> bool:
    return session.bind.dialect.name == "sqlite"  # type: ignore[union-attr]


def _locked(session: Session, model: object, row_id: str):
    """session.get with row locking where the dialect supports it."""
    if _is_sqlite(session):
        return session.get(model, row_id)
    return session.get(model, row_id, with_for_update=True)


def content_hash(bundle: dict) -> str:
    """Stable hash so key-order/whitespace differences still count as same content."""
    canonical = json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Validation (no database access)
# --------------------------------------------------------------------------- #

def validate_bundle(bundle: object) -> list[ParsedEntry]:
    if not isinstance(bundle, dict):
        raise bad_request("Request body must be a JSON FHIR Bundle object.")
    if bundle.get("resourceType") != "Bundle":
        raise bad_request("Request body must have resourceType 'Bundle'.")
    if bundle.get("type") != "transaction":
        raise bad_request("Only Bundle.type 'transaction' is accepted.")
    raw_entries = bundle.get("entry")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise bad_request("Bundle.entry must be a non-empty array.")
    if len(raw_entries) > settings.MAX_BUNDLE_ENTRIES:
        raise bad_request(
            f"Bundle contains {len(raw_entries)} entries; limit is {settings.MAX_BUNDLE_ENTRIES}."
        )

    entries: list[ParsedEntry] = []
    seen_full_urls: set[str] = set()
    seen_bundle_identifiers: set[tuple[str, str]] = set()

    for i, raw in enumerate(raw_entries):
        if not isinstance(raw, dict) or not isinstance(raw.get("resource"), dict):
            raise bad_request(f"Bundle.entry[{i}] must contain a 'resource' object.")
        resource = raw["resource"]
        rtype = resource.get("resourceType")
        if rtype not in ALLOWED_TYPES:
            raise bad_request(
                f"Bundle.entry[{i}].resource.resourceType '{rtype}' is not allowed; "
                f"supported: {sorted(ALLOWED_TYPES)}."
            )
        request = raw.get("request")
        if not isinstance(request, dict):
            raise bad_request(f"Bundle.entry[{i}] must contain a 'request' object.")
        method = request.get("method")
        if method not in ("POST", "PUT"):
            raise bad_request(
                f"Bundle.entry[{i}].request.method '{method}' not supported; use POST or PUT."
            )
        url = request.get("url")
        if not isinstance(url, str) or not url:
            raise bad_request(f"Bundle.entry[{i}].request.url is required.")
        if url != rtype and not url.startswith(rtype + "/"):
            raise bad_request(
                f"Bundle.entry[{i}].request.url '{url}' does not match resource type {rtype}."
            )

        full_url = raw.get("fullUrl")
        if full_url is not None:
            if not isinstance(full_url, str) or not URN_UUID_RE.match(full_url):
                raise bad_request(
                    f"Bundle.entry[{i}].fullUrl must be a urn:uuid reference."
                )
            if full_url in seen_full_urls:
                raise bad_request(f"Duplicate fullUrl '{full_url}' inside bundle.")
            seen_full_urls.add(full_url)

        if_match_raw = request.get("ifMatch")
        if_match = None
        if if_match_raw is not None:
            if_match = parse_etag(if_match_raw) if isinstance(if_match_raw, str) else None
            if if_match is None:
                raise bad_request(
                    f"Bundle.entry[{i}].request.ifMatch '{if_match_raw}' is not a valid version ETag."
                )
        if method == "POST" and if_match is not None:
            raise bad_request(f"Bundle.entry[{i}]: ifMatch is only valid on PUT updates.")

        update_id = None
        if method == "PUT":
            if if_match is None:
                raise bad_request(
                    f"Bundle.entry[{i}]: PUT requires request.ifMatch (e.g. 'W/\"1\"')."
                )
            update_id = resource.get("id")
            if "/" in url:
                url_id = url.split("/", 1)[1]
                if update_id is not None and update_id != url_id:
                    raise bad_request(
                        f"Bundle.entry[{i}]: resource.id conflicts with request.url id."
                    )
                update_id = update_id or url_id
            if not update_id:
                raise bad_request(
                    f"Bundle.entry[{i}]: PUT requires the resource id in resource.id or request.url."
                )

        entry = ParsedEntry(
            index=i,
            resource_type=rtype,
            method=method,
            url=url,
            resource=resource,
            full_url=full_url,
            if_match=if_match,
            update_id=update_id,
        )

        if rtype == "Patient":
            _validate_patient_entry(entry, seen_bundle_identifiers)
        elif rtype == "Observation":
            _validate_referencing_entry(entry, "subject", "Patient", i)
        elif rtype == "DiagnosticReport":
            _validate_referencing_entry(entry, "subject", "Patient", i)
            results = resource.get("result")
            if not isinstance(results, list) or not results:
                raise bad_request(
                    f"Bundle.entry[{i}]: DiagnosticReport.result must be a non-empty array."
                )
            for ref in results:
                _validate_ref_shape(ref, i, "DiagnosticReport.result", "Observation")

        entries.append(entry)

    _resolve_urn_references(entries)
    return entries


def _identifier_pairs(resource: dict) -> list[tuple[str, str]]:
    identifiers = resource.get("identifier")
    if not isinstance(identifiers, list) or not identifiers:
        raise bad_request("Patient resources must carry at least one identifier.")
    pairs: list[tuple[str, str]] = []
    for ident in identifiers:
        if not isinstance(ident, dict):
            raise bad_request("Patient.identifier entries must be objects.")
        value = ident.get("value")
        if not isinstance(value, str) or not value.strip():
            raise bad_request("Patient.identifier.value is required.")
        system = ident.get("system") or ""
        if not isinstance(system, str):
            raise bad_request("Patient.identifier.system must be a string.")
        pairs.append((system, value.strip()))
    return pairs


def _validate_patient_entry(entry: ParsedEntry, seen: set[tuple[str, str]]) -> None:
    if entry.method == "PUT":
        raise bad_request(
            f"Bundle.entry[{entry.index}]: Patient supports conditional create (POST) only."
        )
    pairs = _identifier_pairs(entry.resource)
    for pair in pairs:
        if pair in seen:
            raise bad_request(
                f"Bundle.entry[{entry.index}]: duplicate patient identifier "
                f"system='{pair[0]}' value='{pair[1]}' inside this bundle."
            )
        seen.add(pair)


def _validate_ref_shape(ref: object, index: int, path: str, expected_type: str) -> str:
    if not isinstance(ref, dict) or not isinstance(ref.get("reference"), str):
        raise bad_request(f"Bundle.entry[{index}]: {path} entries need a 'reference' string.")
    reference = ref["reference"].strip()
    if URN_UUID_RE.match(reference) or ABSOLUTE_REF_RE.match(reference):
        kind = reference.split("/", 1)[0] if "/" in reference else None
        if kind is not None and kind != expected_type:
            raise bad_request(
                f"Bundle.entry[{index}]: {path} reference '{reference}' must point to "
                f"a {expected_type}."
            )
        return reference
    raise bad_request(
        f"Bundle.entry[{index}]: {path} reference '{reference}' must be a urn:uuid or "
        f"an absolute '{expected_type}/<id>' reference."
    )


def _validate_referencing_entry(
    entry: ParsedEntry, field_name: str, expected_type: str, index: int
) -> None:
    subject = entry.resource.get(field_name)
    ref = _validate_ref_shape(subject, index, field_name, expected_type)
    entry.subject_ref = ref


def _resolve_urn_references(entries: list[ParsedEntry]) -> None:
    """Bind every urn:uuid reference to its bundle entry; forward refs allowed."""
    by_urn: dict[str, ParsedEntry] = {}
    for entry in entries:
        if entry.full_url:
            by_urn[entry.full_url] = entry

    def resolve(reference: str, index: int, path: str, expected_type: str) -> ParsedEntry:
        target = by_urn.get(reference)
        if target is None:
            raise unprocessable(
                f"Bundle.entry[{index}]: {path} reference '{reference}' cannot be resolved "
                f"inside the bundle."
            )
        if target.resource_type != expected_type:
            raise unprocessable(
                f"Bundle.entry[{index}]: {path} reference '{reference}' points to "
                f"{target.resource_type}, expected {expected_type}."
            )
        return target

    for entry in entries:
        if entry.resource_type in ("Observation", "DiagnosticReport"):
            ref = entry.subject_ref
            if ref.startswith("urn:uuid:"):
                resolve(ref, entry.index, "subject", "Patient")
        if entry.resource_type == "DiagnosticReport":
            for ref_obj in entry.resource["result"]:
                ref = ref_obj["reference"].strip()
                if ref.startswith("urn:uuid:"):
                    resolve(ref, entry.index, "DiagnosticReport.result", "Observation")


# --------------------------------------------------------------------------- #
# Idempotency ledger
# --------------------------------------------------------------------------- #

def acquire_idempotency_slot(session: Session, key: str, hash_value: str) -> dict | None:
    """Return a stored completed response to replay, or None to proceed.

    Uses its own committed transaction so a crash mid-processing leaves a
    recoverable 'pending' marker rather than a half-applied bundle.
    """
    while True:
        record = IdempotencyRecord(key=key, content_hash=hash_value, state="pending")
        session.add(record)
        try:
            session.commit()
            return None
        except IntegrityError:
            session.rollback()
            existing = session.get(IdempotencyRecord, key)
            if existing is None:  # pragma: no cover - extremely rare race
                continue
            if existing.state == "completed":
                if existing.content_hash != hash_value:
                    raise conflict(
                        f"Idempotency-Key '{key}' was already submitted with different "
                        f"content; reuse the key only for identical retries."
                    )
                return existing.response  # type: ignore[return-value]
            # pending: either a live concurrent request or a crashed orphan.
            # Only a *fresh* marker blocks; once it is older than the stale
            # threshold the owning process is presumed crashed and the slot is
            # recovered regardless of payload hash (a crashed request's hash is
            # normally different from the retry anyway).
            updated_at = existing.updated_at
            if updated_at.tzinfo is None:  # SQLite stores naive datetimes
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - updated_at).total_seconds()
            if age < settings.IDEMPOTENCY_STALE_SECONDS:
                raise conflict(
                    f"A request with Idempotency-Key '{key}' is already being processed."
                )
            session.query(IdempotencyRecord).filter_by(key=key).delete()
            session.commit()
            # Loop and re-insert; a concurrent recovery loses on the unique index.
            continue


def store_completed_response(session: Session, key: str, response: dict) -> None:
    record = session.get(IdempotencyRecord, key)
    if record is None:  # pragma: no cover - defensive
        record = IdempotencyRecord(key=key, content_hash="", state="completed")
        session.add(record)
    record.state = "completed"
    record.status_code = 200
    record.response = response


# --------------------------------------------------------------------------- #
# Transactional processing
# --------------------------------------------------------------------------- #

def process_bundle(session: Session, bundle: dict, idempotency_key: str) -> dict:
    entries = validate_bundle(bundle)
    hash_value = content_hash(bundle)

    replay = acquire_idempotency_slot(session, idempotency_key, hash_value)
    if replay is not None:
        return _canonicalize(replay)

    try:
        patient_entries = [e for e in entries if e.resource_type == "Patient"]
        observation_entries = [e for e in entries if e.resource_type == "Observation"]
        report_entries = [e for e in entries if e.resource_type == "DiagnosticReport"]

        patient_rows = _resolve_patients(session, patient_entries)
        _resolve_subjects(session, entries, patient_rows)
        obs_rows = _resolve_observations(session, observation_entries, patient_rows, entries)
        report_rows = _resolve_reports(session, report_entries, entries, obs_rows)

        _validate_patient_alignment(entries, obs_rows, report_rows, session)

        _apply_observations(session, observation_entries, obs_rows, patient_rows, entries)
        _apply_reports(session, report_entries, report_rows, obs_rows, entries)

        receipt = _build_receipt(entries, idempotency_key)
        store_completed_response(session, idempotency_key, receipt)
        session.commit()
        return _canonicalize(receipt)
    except FhirError:
        # Roll back all resource writes and release the idempotency slot so the
        # caller may correct the payload and retry with the same accession key.
        session.rollback()
        session.query(IdempotencyRecord).filter_by(key=idempotency_key).delete()
        session.commit()
        raise
    except Exception:
        session.rollback()
        try:
            session.query(IdempotencyRecord).filter_by(key=idempotency_key).delete()
            session.commit()
        except Exception:  # pragma: no cover - defensive
            session.rollback()
        raise


# --------------------------------------------------------------------------- #
# Patient resolution / conditional create
# --------------------------------------------------------------------------- #

def _resolve_patients(session: Session, patient_entries: list[ParsedEntry]) -> dict[str, Patient]:
    """Return mapping entry index/urn -> Patient row, creating when needed."""
    result: dict[str, Patient] = {}
    if not patient_entries:
        return result

    all_pairs: set[tuple[str, str]] = set()
    entry_pairs: dict[int, list[tuple[str, str]]] = {}
    for entry in patient_entries:
        pairs = _identifier_pairs(entry.resource)
        entry_pairs[entry.index] = pairs
        all_pairs.update(pairs)

    # Lock every matching identifier row so concurrent bundles serialize here.
    systems = {p[0] for p in all_pairs}
    values = {p[1] for p in all_pairs}
    stmt = select(PatientIdentifier).where(
        PatientIdentifier.system.in_(systems), PatientIdentifier.value.in_(values)
    )
    if not _is_sqlite(session):
        stmt = stmt.with_for_update()
    existing_idents = session.execute(stmt).scalars().all()
    pair_to_patient: dict[tuple[str, str], Patient] = {}
    for ident in existing_idents:
        patient = _locked(session, Patient, ident.patient_id)
        pair_to_patient[(ident.system, ident.value)] = patient  # type: ignore[assignment]

    for entry in patient_entries:
        matched: Patient | None = None
        for pair in entry_pairs[entry.index]:
            patient = pair_to_patient.get(pair)
            if patient is not None:
                if matched is not None and patient.id != matched.id:
                    raise unprocessable(
                        f"Bundle.entry[{entry.index}]: identifiers resolve to different "
                        f"existing patients; conditional create is ambiguous."
                    )
                matched = patient

        if matched is None:
            matched = _create_patient(session, entry, entry_pairs[entry.index])
        else:
            entry.action = "existing"
        entry.row_id = matched.id
        entry.version = matched.version
        if entry.full_url:
            result[entry.full_url] = matched
        result[str(entry.index)] = matched
    return result


def _create_patient(
    session: Session, entry: ParsedEntry, pairs: list[tuple[str, str]]
) -> Patient:
    patient = Patient(status="created", version=1, resource=dict(entry.resource))
    try:
        # Nested transaction (SAVEPOINT): a unique-index collision only rolls
        # back this insert, leaving the outer transaction usable.
        with session.begin_nested():
            session.add(patient)
            session.flush()  # assign id
            for system, value in pairs:
                session.add(
                    PatientIdentifier(patient_id=patient.id, system=system, value=value)
                )
    except IntegrityError:
        # A concurrent bundle committed the same identifier first. PostgreSQL
        # makes its row visible now (the speculative insert wait is over), so
        # look it up and reuse exactly that patient.
        systems = [p[0] for p in pairs]
        values = [p[1] for p in pairs]
        winner_ident = (
            session.execute(
                select(PatientIdentifier)
                .where(
                    PatientIdentifier.system.in_(systems),
                    PatientIdentifier.value.in_(values),
                )
            )
            .scalars()
            .first()
        )
        if winner_ident is None:  # pragma: no cover - defensive
            raise conflict("Concurrent patient creation failed; retry the bundle.")
        winner = _locked(session, Patient, winner_ident.patient_id)
        entry.action = "existing"
        entry.row_id = winner.id  # type: ignore[assignment]
        entry.version = winner.version  # type: ignore[assignment]
        return winner  # type: ignore[return-value]

    resource = dict(entry.resource)
    resource["id"] = patient.id
    patient.resource = resource
    entry.action = "created"
    entry.row_id = patient.id
    entry.version = 1
    session.add(
        ResourceVersion(
            resource_type="Patient",
            resource_id=patient.id,
            version=1,
            status="created",
            resource=resource,
        )
    )
    return patient


# --------------------------------------------------------------------------- #
# Subject / observation / report resolution
# --------------------------------------------------------------------------- #

def _patient_for_ref(
    reference: str, patient_rows: dict[str, Patient], session: Session, index: int
) -> Patient:
    if reference.startswith("urn:uuid:"):
        patient = patient_rows.get(reference)
        if patient is None:  # validated earlier, defensive only
            raise unprocessable(f"Bundle.entry[{index}]: unresolved subject '{reference}'.")
        return patient
    pid = reference.split("/", 1)[1]
    patient = session.get(Patient, pid)
    if patient is None:
        raise unprocessable(
            f"Bundle.entry[{index}]: subject references unknown Patient/{pid}."
        )
    return patient


def _resolve_subjects(
    session: Session, entries: list[ParsedEntry], patient_rows: dict[str, Patient]
) -> None:
    for entry in entries:
        if entry.resource_type in ("Observation", "DiagnosticReport"):
            ref = entry.subject_ref
            patient = _patient_for_ref(ref, patient_rows, session, entry.index)
            entry.subject_pid = patient.id


def _resolve_observations(
    session: Session,
    observation_entries: list[ParsedEntry],
    patient_rows: dict[str, Patient],
    all_entries: list[ParsedEntry],
) -> dict[str, Observation]:
    rows: dict[str, Observation] = {}

    for entry in observation_entries:
        if entry.method == "PUT":
            obs = _locked(session, Observation, entry.update_id)
            if obs is None:
                raise not_found(
                    f"Bundle.entry[{entry.index}]: Observation/{entry.update_id} does not exist."
                )
            if entry.if_match != obs.version:
                raise precondition_failed(
                    f"Bundle.entry[{entry.index}]: If-Match version {entry.if_match} is stale; "
                    f"current version is {obs.version}."
                )
            entry.row_id = obs.id
            entry.version = obs.version
            entry.action = "updated"
        else:
            obs = Observation(
                patient_id=entry.subject_pid,
                status="created",
                version=1,
                resource={},
            )
            session.add(obs)
            session.flush()
            entry.row_id = obs.id
            entry.version = 1
            entry.action = "created"
        rows[entry.row_id] = obs
        if entry.full_url:
            rows[entry.full_url] = obs  # type: ignore[assignment]
    return rows


def _observation_for_ref(
    reference: str,
    obs_rows: dict[str, Observation],
    session: Session,
    index: int,
) -> Observation:
    if reference.startswith("urn:uuid:"):
        obs = obs_rows.get(reference)
        if obs is None:
            raise unprocessable(
                f"Bundle.entry[{index}]: unresolved result reference '{reference}'."
            )
        return obs
    oid = reference.split("/", 1)[1]
    obs = _locked(session, Observation, oid)
    if obs is None:
        raise unprocessable(
            f"Bundle.entry[{index}]: result references unknown Observation/{oid}."
        )
    return obs


def _resolve_reports(
    session: Session,
    report_entries: list[ParsedEntry],
    all_entries: list[ParsedEntry],
    obs_rows: dict[str, Observation],
) -> dict[str, DiagnosticReport]:
    rows: dict[str, DiagnosticReport] = {}
    for entry in report_entries:
        if entry.method == "PUT":
            report = _locked(session, DiagnosticReport, entry.update_id)
            if report is None:
                raise not_found(
                    f"Bundle.entry[{entry.index}]: DiagnosticReport/{entry.update_id} does not exist."
                )
            if entry.if_match != report.version:
                raise precondition_failed(
                    f"Bundle.entry[{entry.index}]: If-Match version {entry.if_match} is stale; "
                    f"current version is {report.version}."
                )
            entry.row_id = report.id
            entry.version = report.version
            entry.action = "updated"
        else:
            report = DiagnosticReport(
                patient_id=entry.subject_pid,
                status="created",
                version=1,
                resource={},
            )
            session.add(report)
            session.flush()
            entry.row_id = report.id
            entry.version = 1
            entry.action = "created"

        linked: list[str] = []
        for ref_obj in entry.resource["result"]:
            ref = ref_obj["reference"].strip()
            obs = _observation_for_ref(ref, obs_rows, session, entry.index)
            linked.append(obs.id)
        if len(set(linked)) != len(linked):
            raise bad_request(
                f"Bundle.entry[{entry.index}]: duplicate Observation references in result."
            )
        entry.result_obs_ids = linked
        rows[report.id] = report
        if entry.full_url:
            rows[entry.full_url] = report  # type: ignore[assignment]
    return rows


# --------------------------------------------------------------------------- #
# Patient-alignment rules
# --------------------------------------------------------------------------- #

def _validate_patient_alignment(
    entries: list[ParsedEntry],
    obs_rows: dict[str, Observation],
    report_rows: dict[str, DiagnosticReport],
    session: Session,
) -> None:
    # Effective patient of each observation / report after this bundle applies.
    obs_effective_pid: dict[str, str] = {}
    report_effective_pid: dict[str, str] = {}

    for entry in entries:
        if entry.resource_type == "Observation":
            obs_effective_pid[entry.row_id] = entry.subject_pid  # type: ignore[assignment]
        elif entry.resource_type == "DiagnosticReport":
            report_effective_pid[entry.row_id] = entry.subject_pid  # type: ignore[assignment]

    # Rule 1: every report (created or updated) must share a patient with all
    # observations it links. Lock external observations so a concurrent bundle
    # cannot move one of them between our check and our commit.
    for entry in entries:
        if entry.resource_type != "DiagnosticReport":
            continue
        for oid in entry.result_obs_ids:
            in_bundle = oid in obs_effective_pid
            obs = obs_rows.get(oid)
            if obs is None:
                obs = _locked(session, Observation, oid)
            obs_pid = obs_effective_pid.get(oid, obs.patient_id)
            if obs_pid != entry.subject_pid:
                raise unprocessable(
                    f"Bundle.entry[{entry.index}]: DiagnosticReport/{entry.row_id} and its "
                    f"Observation/{oid} belong to different patients; bundle rejected."
                )
            if not in_bundle and not _is_sqlite(session):
                # The row was read earlier without FOR UPDATE; lock it now.
                session.refresh(obs, with_for_update=True)

    # Rule 2: moving an observation must not strand an existing referencing
    # report under another patient, unless that report is corrected in-bundle.
    for entry in entries:
        if entry.resource_type != "Observation" or entry.method != "PUT":
            continue
        obs = obs_rows[entry.row_id]
        if obs.patient_id == entry.subject_pid:
            continue
        link_stmt = select(ReportLink).where(ReportLink.observation_id == entry.row_id)
        if not _is_sqlite(session):
            link_stmt = link_stmt.with_for_update()
        links = session.execute(link_stmt).scalars().all()
        for link in links:
            report = _locked(session, DiagnosticReport, link.report_id)
            effective_report_pid = report_effective_pid.get(report.id, report.patient_id)
            if effective_report_pid != entry.subject_pid:
                raise unprocessable(
                    f"Bundle.entry[{entry.index}]: moving Observation/{entry.row_id} to "
                    f"Patient/{entry.subject_pid} breaks DiagnosticReport/{report.id}; "
                    f"correct the report in the same bundle or keep the patient."
                )


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #

def _render_resource(entry: ParsedEntry, version: int) -> dict:
    resource = dict(entry.resource)
    resource["id"] = entry.row_id
    meta = dict(resource.get("meta") or {})
    meta["versionId"] = str(version)
    meta["lastUpdated"] = datetime.now(timezone.utc).isoformat()
    resource["meta"] = meta
    return resource


def _apply_observations(
    session: Session,
    observation_entries: list[ParsedEntry],
    obs_rows: dict[str, Observation],
    patient_rows: dict[str, Patient],
    all_entries: list[ParsedEntry],
) -> None:
    for entry in observation_entries:
        obs = obs_rows[entry.row_id]
        resource = _render_resource(entry, entry.version if entry.method == "POST" else entry.if_match + 1)
        resource["subject"] = {"reference": f"Patient/{entry.subject_pid}"}

        if entry.method == "POST":
            obs.resource = resource
            obs.version = 1
            session.add(
                ResourceVersion(
                    resource_type="Observation",
                    resource_id=obs.id,
                    version=1,
                    status="created",
                    resource=resource,
                )
            )
        else:
            new_version = obs.version + 1
            resource["id"] = obs.id
            resource["meta"]["versionId"] = str(new_version)
            obs.resource = resource
            obs.patient_id = entry.subject_pid  # type: ignore[assignment]
            obs.version = new_version
            obs.status = "updated"
            session.add(
                ResourceVersion(
                    resource_type="Observation",
                    resource_id=obs.id,
                    version=new_version,
                    status="updated",
                    resource=resource,
                )
            )
            entry.version = new_version


def _apply_reports(
    session: Session,
    report_entries: list[ParsedEntry],
    report_rows: dict[str, DiagnosticReport],
    obs_rows: dict[str, Observation],
    all_entries: list[ParsedEntry],
) -> None:
    for entry in report_entries:
        report = report_rows[entry.row_id]
        target_version = 1 if entry.method == "POST" else report.version + 1
        resource = _render_resource(entry, target_version)
        resource["subject"] = {"reference": f"Patient/{entry.subject_pid}"}
        resource["result"] = [
            {"reference": f"Observation/{oid}"} for oid in entry.result_obs_ids
        ]

        if entry.method == "PUT":
            report.patient_id = entry.subject_pid  # type: ignore[assignment]
            report.version = target_version
            report.status = "updated"
            # Replace the link set with the submitted result list.
            for link in list(report.links):
                session.delete(link)
            session.flush()
        else:
            report.version = 1

        report.resource = resource
        for oid in entry.result_obs_ids:
            session.add(ReportLink(report_id=report.id, observation_id=oid))

        session.add(
            ResourceVersion(
                resource_type="DiagnosticReport",
                resource_id=report.id,
                version=target_version,
                status=report.status,
                resource=resource,
            )
        )
        entry.version = target_version


# --------------------------------------------------------------------------- #
# Receipt
# --------------------------------------------------------------------------- #

def _canonicalize(value: object) -> object:
    """Recursively sort dict keys so a JSONB round-trip yields byte-stable output."""
    if isinstance(value, dict):
        return {k: _canonicalize(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_canonicalize(v) for v in value]
    return value


def _build_receipt(entries: list[ParsedEntry], idempotency_key: str) -> dict:
    response_entries = []
    resource_map = []
    for entry in sorted(entries, key=lambda e: e.index):
        http_status = "201 Created" if entry.action in ("created",) else "200 OK"
        if entry.resource_type == "Patient" and entry.action == "existing":
            http_status = "200 OK"
        response_entry = {
            "response": {
                "status": http_status,
                "etag": etag(entry.version or 1),
                "location": f"{entry.resource_type}/{entry.row_id}/_history/{entry.version}",
            },
            "resourceMap": {
                "resourceType": entry.resource_type,
                "id": entry.row_id,
                "version": entry.version,
                "action": entry.action,
            },
        }
        if entry.full_url:
            response_entry["fullUrl"] = entry.full_url
        response_entries.append(response_entry)
        resource_map.append(
            {
                "requestIndex": entry.index,
                "method": entry.method,
                "resourceType": entry.resource_type,
                "id": entry.row_id,
                "version": entry.version,
                "action": entry.action,
                "fullUrl": entry.full_url,
            }
        )

    return {
        "resourceType": "Bundle",
        "type": "transaction-response",
        "idempotencyKey": idempotency_key,
        "entry": response_entries,
        "resourceMap": resource_map,
    }
