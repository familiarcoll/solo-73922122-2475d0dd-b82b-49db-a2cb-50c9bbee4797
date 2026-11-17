"""End-to-end functional tests of the ingest API on PostgreSQL."""
from __future__ import annotations

import concurrent.futures

import pytest

from tests.conftest import (
    make_bundle,
    observation_entry,
    patient_entry,
    report_entry,
    urn,
)


def _submit(client, key, bundle, expect_code=200):
    resp = client.post("/fhir", json=bundle, headers={"Idempotency-Key": key})
    assert resp.status_code == expect_code, resp.text
    return resp.json()


def test_health_and_root(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").json()["docs"] == "/docs"


def test_happy_path_forward_references_and_receipt(client):
    p_urn, o_urn, r_urn = urn(), urn(), urn()
    p_entry, (system, value) = patient_entry(p_urn)
    o_entry = observation_entry(o_urn, p_urn)
    r_entry = report_entry(r_urn, p_urn, [o_urn])
    # Deliberately out of order: report -> observation -> patient.
    bundle = make_bundle([r_entry, o_entry, p_entry])

    receipt = _submit(client, "ACC-001", bundle)
    assert receipt["type"] == "transaction-response"
    assert receipt["idempotencyKey"] == "ACC-001"
    assert len(receipt["resourceMap"]) == 3
    by_index = {item["requestIndex"]: item for item in receipt["resourceMap"]}
    assert by_index[2]["resourceType"] == "Patient"
    assert by_index[2]["action"] == "created"
    assert by_index[1]["resourceType"] == "Observation"
    assert by_index[0]["resourceType"] == "DiagnosticReport"
    assert all(item["id"] for item in receipt["resourceMap"])

    # Patient read + per-patient listings
    pid = by_index[2]["id"]
    oid = by_index[1]["id"]
    rid = by_index[0]["id"]
    summary = client.get(f"/patients/{pid}").json()
    assert summary["counts"] == {"diagnosticReports": 1, "observations": 1}
    obs_list = client.get(f"/patients/{pid}/observations").json()
    assert obs_list["total"] == 1 and obs_list["entry"][0]["id"] == oid
    rep_list = client.get(f"/patients/{pid}/diagnostic-reports").json()
    assert rep_list["total"] == 1
    assert rep_list["entry"][0]["observationIds"] == [oid]

    report = client.get(f"/diagnostic-reports/{rid}").json()
    assert report["version"] == 1
    assert report["etag"] == 'W/"1"'
    assert report["resource"]["result"] == [{"reference": f"Observation/{oid}"}]

    # Identifier lookup
    found = client.get("/patients", params={"identifier": f"{system}|{value}"}).json()
    assert found["id"] == pid


def test_patient_conditional_create_reuse_same_identifier(client):
    p_urn1, p_urn2 = urn(), urn()
    p1, (system, value) = patient_entry(p_urn1, value="DUP-1")
    p2, _ = patient_entry(p_urn2, value="DUP-1")
    r1 = _submit(client, "K1", make_bundle([p1]))
    r2 = _submit(client, "K2", make_bundle([p2]))
    id1 = r1["resourceMap"][0]["id"]
    assert r2["resourceMap"][0]["action"] == "existing"
    assert r2["resourceMap"][0]["id"] == id1
    # Only one patient row exists.
    assert client.get("/patients", params={"identifier": f"{system}|{value}"}).json()["id"] == id1


def test_duplicate_identifier_inside_bundle_rejected(client):
    p_urn = urn()
    p, _ = patient_entry(p_urn, value="DUP-2")
    p2, _ = patient_entry(urn(), value="DUP-2")
    resp = client.post(
        "/fhir", json=make_bundle([p, p2]), headers={"Idempotency-Key": "K3"}
    )
    assert resp.status_code == 400
    assert "duplicate patient identifier" in resp.json()["issue"][0]["diagnostics"]


def test_idempotency_retry_same_content_replays_receipt(client):
    p_urn = urn()
    p, _ = patient_entry(p_urn, value="IDEM-1")
    bundle = make_bundle([p])
    first_raw = client.post(
        "/fhir", json=bundle, headers={"Idempotency-Key": "ACC-IDEM"}
    ).content
    second_raw = client.post(
        "/fhir", json=bundle, headers={"Idempotency-Key": "ACC-IDEM"}
    ).content
    assert first_raw == second_raw  # byte-stable replay
    first = __import__("json").loads(first_raw)
    # Semantically identical content with different whitespace/key order still
    # counts as the same submission because hashing uses canonical JSON.
    import json as _json

    reordered = _json.loads(_json.dumps(bundle, indent=4, sort_keys=True))
    third = _submit(client, "ACC-IDEM", reordered)
    assert third["resourceMap"] == first["resourceMap"]


def test_idempotency_different_content_rejected(client):
    p, _ = patient_entry(urn(), value="IDEM-2A")
    p2, _ = patient_entry(urn(), value="IDEM-2B")
    _submit(client, "ACC-CONFLICT", make_bundle([p]))
    resp = client.post(
        "/fhir", json=make_bundle([p2]), headers={"Idempotency-Key": "ACC-CONFLICT"}
    )
    assert resp.status_code == 409
    assert "different content" in resp.json()["issue"][0]["diagnostics"]
    # Second patient must not have been created.
    missing = client.get("/patients", params={"identifier": "http://hospital/mrn|IDEM-2B"})
    assert missing.status_code == 404


def test_missing_idempotency_key_rejected(client):
    p, _ = patient_entry(urn())
    resp = client.post("/fhir", json=make_bundle([p]))
    assert resp.status_code == 400


def test_unsupported_resource_type_rejected(client):
    bundle = make_bundle([
        {
            "resource": {"resourceType": "Encounter", "status": "finished"},
            "request": {"method": "POST", "url": "Encounter"},
        }
    ])
    resp = client.post("/fhir", json=bundle, headers={"Idempotency-Key": "KX"})
    assert resp.status_code == 400


def test_too_many_entries_rejected(client):
    entries = [patient_entry(urn())[0] for _ in range(21)]
    resp = client.post(
        "/fhir", json=make_bundle(entries), headers={"Idempotency-Key": "KBIG"}
    )
    assert resp.status_code == 400
    assert "limit is 20" in resp.json()["issue"][0]["diagnostics"]


def test_unresolvable_reference_whole_bundle_rejected_and_atomic(client):
    p_urn, o_urn, ghost_urn = urn(), urn(), urn()
    p, _ = patient_entry(p_urn, value="ATOM-1")
    o = observation_entry(o_urn, p_urn)
    r = report_entry(urn(), p_urn, [ghost_urn])
    resp = client.post(
        "/fhir", json=make_bundle([p, o, r]), headers={"Idempotency-Key": "KATOM"}
    )
    assert resp.status_code == 422
    # Nothing from the failed bundle remains.
    assert client.get("/patients", params={"identifier": "http://hospital/mrn|ATOM-1"}).status_code == 404


def test_unknown_external_observation_reference_rejected(client):
    p_urn = urn()
    p, _ = patient_entry(p_urn, value="ATOM-2")
    r = report_entry(urn(), p_urn, ["Observation/does-not-exist"])
    resp = client.post(
        "/fhir", json=make_bundle([p, r]), headers={"Idempotency-Key": "KATOM2"}
    )
    assert resp.status_code == 422
    assert "unknown Observation" in resp.json()["issue"][0]["diagnostics"]
    assert client.get("/patients", params={"identifier": "http://hospital/mrn|ATOM-2"}).status_code == 404


def test_observation_update_if_match_and_version_history(client):
    p_urn, o_urn = urn(), urn()
    p, _ = patient_entry(p_urn, value="UPD-1")
    o = observation_entry(o_urn, p_urn, value=5.0)
    receipt = _submit(client, "KUPD1", make_bundle([p, o]))
    pid = receipt["resourceMap"][0]["id"]
    oid = receipt["resourceMap"][1]["id"]

    # Stale If-Match (version 99) must fail.
    stale = {
        "resource": {
            "resourceType": "Observation",
            "id": oid,
            "status": "final",
            "code": {"coding": [{"code": "GLU"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "valueQuantity": {"value": 6.1, "unit": "mmol/L"},
        },
        "request": {"method": "PUT", "url": f"Observation/{oid}", "ifMatch": 'W/"99"'},
    }
    resp = client.post("/fhir", json=make_bundle([stale]), headers={"Idempotency-Key": "KUPD2"})
    assert resp.status_code == 412
    assert "stale" in resp.json()["issue"][0]["diagnostics"]

    # Correct If-Match updates and bumps the version.
    ok = dict(stale)
    ok["request"] = {"method": "PUT", "url": f"Observation/{oid}", "ifMatch": 'W/"1"'}
    receipt2 = _submit(client, "KUPD3", make_bundle([ok]))
    assert receipt2["resourceMap"][0]["action"] == "updated"
    assert receipt2["resourceMap"][0]["version"] == 2

    current = client.get(f"/observations/{oid}").json()
    assert current["version"] == 2 and current["resource"]["valueQuantity"]["value"] == 6.1
    assert current["etag"] == 'W/"2"'

    history = client.get(f"/observations/{oid}/_history").json()
    assert [e["version"] for e in history["entry"]] == [1, 2]
    assert history["entry"][0]["resource"]["valueQuantity"]["value"] == 5.0


def test_update_unknown_resource_404(client):
    p_urn = urn()
    p, _ = patient_entry(p_urn, value="UPD-404")
    put = {
        "resource": {
            "resourceType": "Observation",
            "id": "missing",
            "status": "final",
            "code": {"coding": [{"code": "X"}]},
            "subject": {"reference": p_urn},
        },
        "request": {"method": "PUT", "url": "Observation/missing", "ifMatch": 'W/"1"'},
    }
    resp = client.post("/fhir", json=make_bundle([p, put]), headers={"Idempotency-Key": "K404"})
    assert resp.status_code == 404


def test_report_observation_patient_mismatch_rejected(client):
    p1_urn, p2_urn = urn(), urn()
    p1, _ = patient_entry(p1_urn, value="MIX-A")
    p2, _ = patient_entry(p2_urn, value="MIX-B")
    # Observation belongs to patient 1...
    o = observation_entry(urn(), p1_urn)
    # ...report claims patient 2 but references it.
    r = report_entry(urn(), p2_urn, [o["fullUrl"]])
    resp = client.post(
        "/fhir", json=make_bundle([p1, p2, o, r]),
        headers={"Idempotency-Key": "KMIX"},
    )
    assert resp.status_code == 422
    assert "different patients" in resp.json()["issue"][0]["diagnostics"]
    # Both patients absent: whole bundle rolled back.
    assert client.get("/patients", params={"identifier": "http://hospital/mrn|MIX-A"}).status_code == 404


def test_move_observation_without_correcting_report_rejected(client):
    p1_urn, p2_urn = urn(), urn()
    p1, _ = patient_entry(p1_urn, value="MV-A")
    p2, _ = patient_entry(p2_urn, value="MV-B")
    o = observation_entry(urn(), p1_urn)
    r = report_entry(urn(), p1_urn, [o["fullUrl"]])
    first = _submit(client, "KMV1", make_bundle([p1, p2, o, r]))
    oid = next(m["id"] for m in first["resourceMap"] if m["resourceType"] == "Observation")

    # Move observation to patient 2 while the report still belongs to patient 1.
    move = {
        "resource": {
            "resourceType": "Observation",
            "id": oid,
            "status": "final",
            "code": {"coding": [{"code": "GLU"}]},
            "subject": {"reference": p2_urn},
            "valueQuantity": {"value": 9.9, "unit": "mmol/L"},
        },
        "request": {"method": "PUT", "url": f"Observation/{oid}", "ifMatch": 'W/"1"'},
    }
    resp = client.post(
        "/fhir", json=make_bundle([p1, p2, move]), headers={"Idempotency-Key": "KMV2"}
    )
    assert resp.status_code == 422
    assert "breaks DiagnosticReport" in resp.json()["issue"][0]["diagnostics"]
    # Observation stays with original patient, version unchanged.
    unchanged = client.get(f"/observations/{oid}").json()
    assert unchanged["version"] == 1
    assert unchanged["resource"]["subject"]["reference"].startswith("Patient/")


def test_move_observation_with_corrected_report_in_same_bundle(client):
    p1_urn, p2_urn = urn(), urn()
    p1, _ = patient_entry(p1_urn, value="MVC-A")
    p2, _ = patient_entry(p2_urn, value="MVC-B")
    o = observation_entry(urn(), p1_urn)
    r = report_entry(urn(), p1_urn, [o["fullUrl"]])
    first = _submit(client, "KMVC1", make_bundle([p1, p2, o, r]))
    ids = {m["resourceType"]: m["id"] for m in first["resourceMap"]}
    oid, rid = ids["Observation"], ids["DiagnosticReport"]

    move_obs = {
        "resource": {
            "resourceType": "Observation",
            "id": oid,
            "status": "final",
            "code": {"coding": [{"code": "GLU"}]},
            "subject": {"reference": p2_urn},
            "valueQuantity": {"value": 9.9, "unit": "mmol/L"},
        },
        "request": {"method": "PUT", "url": f"Observation/{oid}", "ifMatch": 'W/"1"'},
    }
    move_report = {
        "resource": {
            "resourceType": "DiagnosticReport",
            "id": rid,
            "status": "final",
            "code": {"coding": [{"code": "24323-8"}]},
            "subject": {"reference": p2_urn},
            "result": [{"reference": f"Observation/{oid}"}],
        },
        "request": {"method": "PUT", "url": f"DiagnosticReport/{rid}", "ifMatch": 'W/"1"'},
    }
    receipt = _submit(client, "KMVC2", make_bundle([p1, p2, move_obs, move_report]))
    touched = {m["action"] for m in receipt["resourceMap"] if m["method"] == "PUT"}
    assert touched == {"updated"}

    # Observation now belongs to patient 2.
    p2_id = client.get("/patients", params={"identifier": "http://hospital/mrn|MVC-B"}).json()["id"]
    assert client.get(f"/observations/{oid}").json()["resource"]["subject"] == {
        "reference": f"Patient/{p2_id}"
    }
    p1_id = client.get("/patients", params={"identifier": "http://hospital/mrn|MVC-A"}).json()["id"]
    assert client.get(f"/patients/{p2_id}/observations").json()["total"] == 1
    assert client.get(f"/patients/{p1_id}/observations").json()["total"] == 0
    history = client.get(f"/diagnostic-reports/{rid}/_history").json()
    assert [e["version"] for e in history["entry"]] == [1, 2]


def test_concurrent_identical_patient_creates_one_row(client):
    # Skip where row locking semantics do not apply.
    from app import database

    if database.engine.dialect.name == "sqlite":
        pytest.skip("concurrency test requires PostgreSQL")

    system, value = "http://hospital/mrn", "RACE-1"

    def one(i: int):
        p_urn = urn()
        p, _ = patient_entry(p_urn, value=value, system=system)
        return client.post(
            "/fhir", json=make_bundle([p]), headers={"Idempotency-Key": f"RACE-{i}"}
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(one, range(8)))
    ids = {r.json()["resourceMap"][0]["id"] for r in responses}
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert len(ids) == 1


def test_concurrent_updates_serialize_via_if_match(client):
    from app import database

    if database.engine.dialect.name == "sqlite":
        pytest.skip("concurrency test requires PostgreSQL")

    p_urn, o_urn = urn(), urn()
    p, _ = patient_entry(p_urn, value="RACE-UPD")
    o = observation_entry(o_urn, p_urn)
    receipt = _submit(client, "RACE-U1", make_bundle([p, o]))
    pid = receipt["resourceMap"][0]["id"]
    oid = receipt["resourceMap"][1]["id"]

    def update(_):
        body = {
            "resource": {
                "resourceType": "Observation",
                "id": oid,
                "status": "final",
                "code": {"coding": [{"code": "GLU"}]},
                "subject": {"reference": f"Patient/{pid}"},
                "valueQuantity": {"value": 7.0, "unit": "mmol/L"},
            },
            "request": {"method": "PUT", "url": f"Observation/{oid}", "ifMatch": 'W/"1"'},
        }
        return client.post(
            "/fhir", json=make_bundle([body]), headers={"Idempotency-Key": urn()}
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(update, range(6)))
    ok = [r for r in results if r.status_code == 200]
    failed = [r for r in results if r.status_code == 412]
    assert len(ok) == 1 and len(failed) == 5
    assert client.get(f"/observations/{oid}").json()["version"] == 2


def test_unrelated_patient_still_commits_after_failed_bundle(client):
    # First establish a valid patient in the store.
    p_urn = urn()
    p, _ = patient_entry(p_urn, value="UNREL-OK")
    _submit(client, "KUNREL1", make_bundle([p]))

    # Then a broken bundle referencing a ghost observation.
    p2, _ = patient_entry(urn(), value="UNREL-GHOST")
    ghost = report_entry(urn(), p2["fullUrl"], ["Observation/nope"])
    bad = client.post(
        "/fhir", json=make_bundle([p2, ghost]), headers={"Idempotency-Key": "KUNREL2"}
    )
    assert bad.status_code == 422

    # The earlier patient remains readable and a new valid bundle still works.
    assert client.get("/patients", params={"identifier": "http://hospital/mrn|UNREL-OK"}).status_code == 200
    pid = client.get("/patients", params={"identifier": "http://hospital/mrn|UNREL-OK"}).json()["id"]
    o = observation_entry(urn(), f"Patient/{pid}")
    ok = client.post("/fhir", json=make_bundle([o]), headers={"Idempotency-Key": "KUNREL3"})
    assert ok.status_code == 200


def test_forward_reference_to_external_absolute_id(client):
    p_urn, o_urn = urn(), urn()
    p, (system, value) = patient_entry(p_urn, value="ABS-1")
    first = _submit(client, "KABS1", make_bundle([p, observation_entry(o_urn, p_urn)]))
    pid = first["resourceMap"][0]["id"]
    oid = first["resourceMap"][1]["id"]

    # New report references the already-stored observation by absolute id.
    r = report_entry(urn(), f"Patient/{pid}", [f"Observation/{oid}"])
    second = _submit(client, "KABS2", make_bundle([r]))
    rid = second["resourceMap"][0]["id"]
    assert client.get(f"/diagnostic-reports/{rid}").json()["observationIds"] == [oid]


def test_external_patient_reference_works(client):
    p_urn = urn()
    p, _ = patient_entry(p_urn, value="EXT-P")
    first = _submit(client, "KEXT1", make_bundle([p]))
    pid = first["resourceMap"][0]["id"]
    o = observation_entry(urn(), f"Patient/{pid}")
    second = _submit(client, "KEXT2", make_bundle([o]))
    oid = second["resourceMap"][0]["id"]
    assert client.get(f"/observations/{oid}").json()["resource"]["subject"] == {
        "reference": f"Patient/{pid}"
    }


def test_post_with_ifmatch_rejected(client):
    p_urn = urn()
    p, _ = patient_entry(p_urn, value="BADIF")
    p["request"] = {"method": "POST", "url": "Patient", "ifMatch": 'W/"1"'}
    resp = client.post("/fhir", json=make_bundle([p]), headers={"Idempotency-Key": "KBADIF"})
    assert resp.status_code == 400


def test_patient_without_identifier_rejected(client):
    bundle = make_bundle([
        {
            "resource": {"resourceType": "Patient"},
            "request": {"method": "POST", "url": "Patient"},
        }
    ])
    resp = client.post("/fhir", json=bundle, headers={"Idempotency-Key": "KPNOID"})
    assert resp.status_code == 400
