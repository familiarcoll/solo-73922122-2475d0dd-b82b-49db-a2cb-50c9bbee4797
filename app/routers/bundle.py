"""Bundle submission endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_session
from app.errors import bad_request
from app.services.bundle import process_bundle

router = APIRouter(tags=["bundle"])


@router.post("/fhir")
async def submit_transaction_bundle(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
):
    """Accept a FHIR R4 transaction Bundle (Patient/Observation/DiagnosticReport).

    The ``Idempotency-Key`` header (the laboratory accession/order number) makes
    retries safe: an identical body replays the original receipt, a different
    body is rejected with 409. Replayed receipts are canonicalized so retries
    return a byte-stable response even though they are reloaded from JSONB.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise bad_request(
            "Header 'Idempotency-Key' (accession/order number) is required."
        )
    try:
        bundle = await request.json()
    except Exception:
        raise bad_request("Request body is not valid JSON.")
    receipt = process_bundle(session, bundle, idempotency_key.strip())
    return JSONResponse(receipt, media_type="application/fhir+json")
