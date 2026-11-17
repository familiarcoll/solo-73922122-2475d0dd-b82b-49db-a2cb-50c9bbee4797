"""A rejected bundle must release its idempotency slot so the corrected
payload can be retried with the same accession number."""
from __future__ import annotations

from tests.conftest import make_bundle, patient_entry, report_entry, urn


def test_failed_bundle_releases_idempotency_key(client):
    p_urn = urn()
    p, _ = patient_entry(p_urn, value="RETRY-1")

    bad_bundle = make_bundle([
        p,
        report_entry(urn(), p_urn, ["Observation/does-not-exist"]),
    ])
    first = client.post(
        "/fhir", json=bad_bundle, headers={"Idempotency-Key": "ACC-RETRY"}
    )
    assert first.status_code == 422

    # Same accession key, corrected content: must be accepted, not 409.
    good_bundle = make_bundle([p])
    second = client.post(
        "/fhir", json=good_bundle, headers={"Idempotency-Key": "ACC-RETRY"}
    )
    assert second.status_code == 200, second.text
    assert second.json()["resourceMap"][0]["action"] == "created"
