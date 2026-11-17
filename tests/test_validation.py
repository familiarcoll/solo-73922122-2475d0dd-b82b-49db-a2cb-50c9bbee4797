"""Validation edge cases for Bundle shape and request semantics."""
from __future__ import annotations

from tests.conftest import make_bundle, observation_entry, patient_entry, report_entry, urn


def _post(client, key, bundle):
    return client.post("/fhir", json=bundle, headers={"Idempotency-Key": key})


def test_not_a_bundle(client):
    resp = _post(client, "V1", {"resourceType": "Patient"})
    assert resp.status_code == 400


def test_wrong_bundle_type(client):
    p, _ = patient_entry(urn())
    bundle = make_bundle([p])
    bundle["type"] = "collection"
    assert _post(client, "V2", bundle).status_code == 400


def test_empty_entry(client):
    assert _post(client, "V3", {"resourceType": "Bundle", "type": "transaction", "entry": []}).status_code == 400


def test_bad_fullurl_shape(client):
    p, _ = patient_entry(urn())
    p["fullUrl"] = "https://example.com/Patient/123"
    assert _post(client, "V4", make_bundle([p])).status_code == 400


def test_duplicate_fullurl(client):
    shared = urn()
    p1, _ = patient_entry(shared, value="U1")
    p2, _ = patient_entry(shared, value="U2")
    assert _post(client, "V5", make_bundle([p1, p2])).status_code == 400


def test_url_resource_type_mismatch(client):
    p, _ = patient_entry(urn())
    p["request"] = {"method": "POST", "url": "Observation"}
    assert _post(client, "V6", make_bundle([p])).status_code == 400


def test_put_id_conflict_between_body_and_url(client):
    p_urn = urn()
    p, _ = patient_entry(p_urn, value="PUTID")
    receipt = _post(client, "V7A", make_bundle([p, observation_entry(urn(), p_urn)])).json()
    oid = receipt["resourceMap"][1]["id"]
    body = {
        "resource": {
            "resourceType": "Observation",
            "id": "different-id",
            "status": "final",
            "code": {"coding": [{"code": "x"}]},
            "subject": {"reference": p_urn},
        },
        "request": {"method": "PUT", "url": f"Observation/{oid}", "ifMatch": 'W/"1"'},
    }
    assert _post(client, "V7B", make_bundle([body])).status_code == 400


def test_put_without_ifmatch(client):
    body = {
        "resource": {"resourceType": "Observation", "id": "x", "status": "final"},
        "request": {"method": "PUT", "url": "Observation/x"},
    }
    assert _post(client, "V8", make_bundle([body])).status_code == 400


def test_malformed_ifmatch(client):
    body = {
        "resource": {"resourceType": "Observation", "id": "x", "status": "final"},
        "request": {"method": "PUT", "url": "Observation/x", "ifMatch": "not-an-etag"},
    }
    assert _post(client, "V9", make_bundle([body])).status_code == 400


def test_patient_put_not_allowed(client):
    body = {
        "resource": {"resourceType": "Patient", "id": "p1",
                     "identifier": [{"system": "s", "value": "v"}]},
        "request": {"method": "PUT", "url": "Patient/p1", "ifMatch": 'W/"1"'},
    }
    assert _post(client, "V10", make_bundle([body])).status_code == 400


def test_report_without_result(client):
    p_urn = urn()
    p, _ = patient_entry(p_urn, value="NOR")
    r = report_entry(urn(), p_urn, [])
    r["resource"].pop("result")
    assert _post(client, "V11", make_bundle([p, r])).status_code == 400


def test_exactly_twenty_entries_accepted(client):
    entries = []
    p_urn = urn()
    p, _ = patient_entry(p_urn, value="MAX20")
    entries.append(p)
    for i in range(19):
        entries.append(observation_entry(urn(), p_urn, code=f"C{i}"))
    resp = _post(client, "V12", make_bundle(entries))
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["resourceMap"]) == 20


def test_subject_wrong_reference_type(client):
    p_urn, o_urn = urn(), urn()
    p, _ = patient_entry(p_urn, value="WRONGTYPE")
    o = observation_entry(o_urn, p_urn)
    # Observation subject pointing at an Observation-type urn reference.
    o["resource"]["subject"] = {"reference": o_urn}
    # Shape is valid, but the referenced bundle entry has the wrong type, so it
    # fails during reference resolution: the whole bundle is rejected (422).
    assert _post(client, "V13", make_bundle([p, o])).status_code == 422


def test_invalid_json_body(client):
    resp = client.post(
        "/fhir", content=b"{not-json", headers={"Idempotency-Key": "V14",
                                                "Content-Type": "application/json"}
    )
    assert resp.status_code == 400
