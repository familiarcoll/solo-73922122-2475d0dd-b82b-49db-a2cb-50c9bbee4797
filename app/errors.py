"""FHIR OperationOutcome error type and HTTP mapping."""
from __future__ import annotations

from fastapi import status


class FhirError(Exception):
    """An error rendered as a FHIR R4 OperationOutcome JSON body."""

    def __init__(
        self,
        status_code: int,
        issue_type: str,
        diagnostics: str,
        severity: str = "error",
        code: str | None = None,
    ):
        super().__init__(diagnostics)
        self.status_code = status_code
        self.issue_type = issue_type
        self.code = code or _default_code(status_code)
        self.severity = severity
        self.diagnostics = diagnostics

    def to_outcome(self) -> dict:
        return {
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": self.severity,
                    "code": self.code,
                    "details": {"text": self.issue_type},
                    "diagnostics": self.diagnostics,
                }
            ],
        }


def _default_code(status_code: int) -> str:
    mapping = {
        status.HTTP_400_BAD_REQUEST: "invalid",
        status.HTTP_404_NOT_FOUND: "not-found",
        status.HTTP_409_CONFLICT: "conflict",
        status.HTTP_412_PRECONDITION_FAILED: "conflict",
        422: "unprocessable",
        status.HTTP_500_INTERNAL_SERVER_ERROR: "exception",
    }
    return mapping.get(status_code, "exception")


# Common constructors -------------------------------------------------------

def bad_request(diagnostics: str) -> "FhirError":
    return FhirError(status.HTTP_400_BAD_REQUEST, "invalid", diagnostics)


def not_found(diagnostics: str) -> "FhirError":
    return FhirError(status.HTTP_404_NOT_FOUND, "not-found", diagnostics)


def conflict(diagnostics: str) -> "FhirError":
    return FhirError(status.HTTP_409_CONFLICT, "conflict", diagnostics)


def precondition_failed(diagnostics: str) -> "FhirError":
    return FhirError(status.HTTP_412_PRECONDITION_FAILED, "conflict", diagnostics)


def unprocessable(diagnostics: str) -> "FhirError":
    return FhirError(422, "unprocessable", diagnostics)
