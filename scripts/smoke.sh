#!/usr/bin/env bash
# End-to-end smoke test against a running instance (default http://localhost:8000).
# Usage: BASE_URL=http://localhost:8000 ./scripts/smoke.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
KEY="SMOKE-$(date +%s)"
PATIENT_URN="urn:uuid:11111111-1111-4111-8111-111111111111"
OBS_URN="urn:uuid:22222222-2222-4222-8222-222222222222"
REPORT_URN="urn:uuid:33333333-3333-4333-8333-333333333333"

BUNDLE=$(cat <<JSON
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {"fullUrl": "$REPORT_URN",
     "resource": {"resourceType": "DiagnosticReport", "status": "final",
       "code": {"coding": [{"code": "24323-8"}]},
       "subject": {"reference": "$PATIENT_URN"},
       "result": [{"reference": "$OBS_URN"}]},
     "request": {"method": "POST", "url": "DiagnosticReport"}},
    {"fullUrl": "$OBS_URN",
     "resource": {"resourceType": "Observation", "status": "final",
       "code": {"coding": [{"code": "2345-7"}]},
       "subject": {"reference": "$PATIENT_URN"},
       "valueQuantity": {"value": 5.5, "unit": "mmol/L"}},
     "request": {"method": "POST", "url": "Observation"}},
    {"fullUrl": "$PATIENT_URN",
     "resource": {"resourceType": "Patient",
       "identifier": [{"system": "http://hospital/mrn", "value": "$KEY"}]},
     "request": {"method": "POST", "url": "Patient"}}
  ]
}
JSON
)

echo "1) submit out-of-order bundle"
RECEIPT=$(curl -fsS -X POST "$BASE_URL/fhir" -H "Idempotency-Key: $KEY" \
  -H "Content-Type: application/json" -d "$BUNDLE")
echo "$RECEIPT" | python3 -m json.tool | head -20

echo "2) identical retry must return the same ids"
REPLAY=$(curl -fsS -X POST "$BASE_URL/fhir" -H "Idempotency-Key: $KEY" \
  -H "Content-Type: application/json" -d "$BUNDLE")
canon() { python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['resourceMap'], sort_keys=True))"; }
diff <(echo "$RECEIPT" | canon) <(echo "$REPLAY" | canon) && echo "   receipts identical (canonical comparison)"

PID=$(echo "$RECEIPT" | python3 -c "import json,sys;print(next(m['id'] for m in json.load(sys.stdin)['resourceMap'] if m['resourceType']=='Patient'))")
echo "3) read patient $PID"
curl -fsS "$BASE_URL/patients/$PID" | python3 -m json.tool | head -12
echo "SMOKE OK"
