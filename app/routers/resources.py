"""Read endpoints: per-patient resources, single reads and version history."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.database import get_session
from app.errors import bad_request
from app.fhir_utils import etag
from app.services import queries

router = APIRouter(tags=["resources"])


def _etag_headers(version: int) -> dict[str, str]:
    return {"ETag": etag(version)}


# --- Patient-scoped listing ------------------------------------------------ #

@router.get("/patients/{patient_id}")
def get_patient(patient_id: str, response: Response, session: Session = Depends(get_session)):
    view = queries.patient_summary(session, patient_id)
    response.headers.update(_etag_headers(view["version"]))
    return view


@router.get("/patients/{patient_id}/observations")
def get_patient_observations(
    patient_id: str, session: Session = Depends(get_session)
):
    items = queries.list_observations(session, patient_id)
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "patientId": patient_id,
        "total": len(items),
        "entry": [
            {"resource": item["resource"], "id": item["id"], "version": item["version"]}
            for item in items
        ],
    }


@router.get("/patients/{patient_id}/diagnostic-reports")
def get_patient_reports(
    patient_id: str, session: Session = Depends(get_session)
):
    items = queries.list_reports(session, patient_id)
    enriched = []
    for item in items:
        obs_ids = queries.report_observation_ids(session, item["id"])
        enriched.append(
            {
                "resource": item["resource"],
                "id": item["id"],
                "version": item["version"],
                "observationIds": obs_ids,
            }
        )
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "patientId": patient_id,
        "total": len(enriched),
        "entry": enriched,
    }


# --- Identifier lookup (helps callers resolve accession -> patient) ------- #

@router.get("/patients")
def find_patient(
    identifier: str = Query(
        ...,
        description="FHIR token identifier, format 'system|value' (system optional).",
    ),
    session: Session = Depends(get_session),
):
    if "|" in identifier:
        system, value = identifier.split("|", 1)
    else:
        system, value = "", identifier
    if not value:
        raise bad_request("identifier value is required.")
    patient = queries.find_patient_by_identifier(session, system, value)
    if patient is None:
        from app.errors import not_found

        raise not_found(f"No patient found for identifier '{identifier}'.")
    return queries.patient_summary(session, patient.id)


# --- Single resource reads ------------------------------------------------- #

@router.get("/observations/{observation_id}")
def get_observation(
    observation_id: str, response: Response, session: Session = Depends(get_session)
):
    view = queries.read_resource(session, "Observation", observation_id)
    response.headers.update(_etag_headers(view["version"]))
    return view


@router.get("/diagnostic-reports/{report_id}")
def get_report(
    report_id: str, response: Response, session: Session = Depends(get_session)
):
    view = queries.read_resource(session, "DiagnosticReport", report_id)
    obs_ids = queries.report_observation_ids(session, report_id)
    response.headers.update(_etag_headers(view["version"]))
    return {**view, "observationIds": obs_ids}


# --- Version history ------------------------------------------------------- #

@router.get("/observations/{observation_id}/_history")
def observation_history(
    observation_id: str, session: Session = Depends(get_session)
):
    versions = queries.history(session, "Observation", observation_id)
    return {
        "resourceType": "Bundle",
        "type": "history",
        "resourceId": observation_id,
        "total": len(versions),
        "entry": versions,
    }


@router.get("/diagnostic-reports/{report_id}/_history")
def report_history(report_id: str, session: Session = Depends(get_session)):
    versions = queries.history(session, "DiagnosticReport", report_id)
    return {
        "resourceType": "Bundle",
        "type": "history",
        "resourceId": report_id,
        "total": len(versions),
        "entry": versions,
    }


@router.get("/patients/{patient_id}/_history")
def patient_history(patient_id: str, session: Session = Depends(get_session)):
    versions = queries.history(session, "Patient", patient_id)
    return {
        "resourceType": "Bundle",
        "type": "history",
        "resourceId": patient_id,
        "total": len(versions),
        "entry": versions,
    }
