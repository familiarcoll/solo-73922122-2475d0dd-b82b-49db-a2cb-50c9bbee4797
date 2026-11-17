"""Bundle submission endpoint."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_session
from app.errors import bad_request
from app.services.bundle import process_bundle

router = APIRouter(tags=["bundle"])


async def _request_body(request: Request) -> bytes:
    # Async dependency: runs on the event loop where the ASGI receive channel
    # lives, so the synchronous threadpool endpoint below still gets the body.
    return await request.body()


@router.post("/fhir")
def submit_transaction_bundle(
    raw_body: bytes = Depends(_request_body),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
):
    """Accept a FHIR R4 transaction Bundle (Patient/Observation/DiagnosticReport).

    Defined as a synchronous ``def`` endpoint so FastAPI executes it in the
    shared threadpool: a slow bundle (or a retried request blocked on 409) does
    not stall the event loop, and reads plus unrelated patients' submissions keep
    flowing on both API instances.

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
        bundle = json.loads(raw_body)
    except Exception:
        raise bad_request("Request body is not valid JSON.")
    receipt = process_bundle(session, bundle, idempotency_key.strip())
    return JSONResponse(receipt, media_type="application/fhir+json")
