"""Read-side queries: resources by patient, single reads and version history."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import not_found
from app.fhir_utils import etag
from app.models import (
    DiagnosticReport,
    Observation,
    Patient,
    PatientIdentifier,
    ReportLink,
    ResourceVersion,
)

_MODEL_BY_TYPE = {
    "Patient": Patient,
    "Observation": Observation,
    "DiagnosticReport": DiagnosticReport,
}


def _patient_or_404(session: Session, patient_id: str) -> Patient:
    patient = session.get(Patient, patient_id)
    if patient is None:
        raise not_found(f"Patient/{patient_id} does not exist.")
    return patient


def find_patient_by_identifier(
    session: Session, system: str, value: str
) -> Patient | None:
    ident = (
        session.execute(
            select(PatientIdentifier).where(
                PatientIdentifier.system == system, PatientIdentifier.value == value
            )
        )
        .scalars()
        .first()
    )
    if ident is None:
        return None
    return session.get(Patient, ident.patient_id)


def patient_summary(session: Session, patient_id: str) -> dict:
    patient = _patient_or_404(session, patient_id)
    report_count = session.execute(
        select(DiagnosticReport.id).where(DiagnosticReport.patient_id == patient_id)
    ).all()
    obs_count = session.execute(
        select(Observation.id).where(Observation.patient_id == patient_id)
    ).all()
    return {
        "resourceType": "Patient",
        "id": patient.id,
        "version": patient.version,
        "resource": patient.resource,
        "counts": {
            "diagnosticReports": len(report_count),
            "observations": len(obs_count),
        },
    }


def list_observations(session: Session, patient_id: str) -> list[dict]:
    _patient_or_404(session, patient_id)
    rows = session.execute(
        select(Observation)
        .where(Observation.patient_id == patient_id)
        .order_by(Observation.created_at)
    ).scalars().all()
    return [_current_view(row, "Observation") for row in rows]


def list_reports(session: Session, patient_id: str) -> list[dict]:
    _patient_or_404(session, patient_id)
    rows = session.execute(
        select(DiagnosticReport)
        .where(DiagnosticReport.patient_id == patient_id)
        .order_by(DiagnosticReport.created_at)
    ).scalars().all()
    return [_current_view(row, "DiagnosticReport") for row in rows]


def _current_view(row: object, resource_type: str) -> dict:
    return {
        "resourceType": resource_type,
        "id": row.id,  # type: ignore[attr-defined]
        "version": row.version,  # type: ignore[attr-defined]
        "etag": etag(row.version),  # type: ignore[attr-defined]
        "resource": row.resource,  # type: ignore[attr-defined]
    }


def read_resource(session: Session, resource_type: str, resource_id: str) -> dict:
    model = _MODEL_BY_TYPE[resource_type]
    row = session.get(model, resource_id)
    if row is None:
        raise not_found(f"{resource_type}/{resource_id} does not exist.")
    return _current_view(row, resource_type)


def history(
    session: Session, resource_type: str, resource_id: str
) -> list[dict]:
    if resource_type not in _MODEL_BY_TYPE:
        raise not_found(f"Unknown resource type '{resource_type}'.")
    rows = session.execute(
        select(ResourceVersion)
        .where(
            ResourceVersion.resource_type == resource_type,
            ResourceVersion.resource_id == resource_id,
        )
        .order_by(ResourceVersion.version)
    ).scalars().all()
    if not rows:
        raise not_found(f"{resource_type}/{resource_id} has no history (resource missing).")
    return [
        {
            "version": row.version,
            "status": row.status,
            "etag": etag(row.version),
            "createdAt": row.created_at.isoformat() if row.created_at else None,
            "resource": row.resource,
        }
        for row in rows
    ]


def report_observation_ids(session: Session, report_id: str) -> list[str]:
    return list(
        session.execute(
            select(ReportLink.observation_id).where(ReportLink.report_id == report_id)
        )
        .scalars()
        .all()
    )
