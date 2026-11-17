#!/usr/bin/env bash
# End-to-end demonstration of the POST /fhir retry & recovery guarantees.
#
# Runs against the two-instance Docker Compose deployment by default:
#   docker compose up --build
#   ./scripts/demo_retry_recovery.sh
#
# Scenarios:
#   1. Identical retry  -> original receipt replayed (same resource ids).
#   2. Different payload on the same accession key -> 409, no duplicate data.
#   3. Live slow request -> a parallel retry on the OTHER instance gets a prompt
#      409 and the bundle is applied exactly once.
#   4. Crashed pending marker (older than the 60s stale threshold) -> a retry
#      recovers the slot exactly once; restart-safe because state is in the DB.
#
# Override targets with API1 / API2 / STALE_SECONDS / HEARTBEAT_SECONDS.
set -euo pipefail

API1="${API1:-http://localhost:8001}"
API2="${API2:-http://localhost:8002}"
STALE_SECONDS="${STALE_SECONDS:-60}"
TS="$(date +%s)"

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
post() { # post BASE KEY JSON
  curl -sS -o /tmp/demo_resp.json -w '%{http_code}' -X POST "$1/fhir" \
    -H "Idempotency-Key: $2" -H "Content-Type: application/json" -d "$3"
}
patient_bundle() { # patient_bundle MRN
  cat <<JSON
{"resourceType":"Bundle","type":"transaction","entry":[{"fullUrl":"urn:uuid:11111111-1111-4111-8111-111111111111","resource":{"resourceType":"Patient","identifier":[{"system":"http://hospital/mrn","value":"$1"}],"name":[{"family":"Demo"}]},"request":{"method":"POST","url":"Patient"}}]}
JSON
}
map_id() { python3 -c "import json;print(json.load(open('/tmp/demo_resp.json'))['resourceMap'][0]['id'])"; }

say "0) health of both API instances"
curl -fsS "$API1/health" && echo " <- api1"
curl -fsS "$API2/health" && echo " <- api2"

say "1) identical retry replays the first receipt"
KEY="DEMO-REPLAY-$TS"
B="$(patient_bundle "MRN-REPLAY-$TS")"
code=$(post "$API1" "$KEY" "$B"); echo "first  POST api1 -> $code"
FIRST_ID="$(map_id)"
code=$(post "$API2" "$KEY" "$B"); echo "retry  POST api2 -> $code (same key, same body)"
SECOND_ID="$(map_id)"
[ "$FIRST_ID" = "$SECOND_ID" ] && echo "OK: same patient id $FIRST_ID, applied once"

say "2) different content on the same accession key is rejected"
code=$(post "$API1" "$KEY" "$(patient_bundle "MRN-DIFFERENT-$TS")")
echo "conflict POST -> $code"
[ "$code" = "409" ] && echo "OK: 409 OperationOutcome:" && python3 -m json.tool /tmp/demo_resp.json

say "3) live slow request: parallel retry gets a prompt 409, no duplicate"
SLOW_KEY="DEMO-LIVE-$TS"
SLOW_B="$(patient_bundle "MRN-LIVE-$TS")"
# api1 holds the request in-flight for 3s (DEMO_PROCESSING_DELAY_SECONDS, see
# docker-compose.yml); the retry on api2 must be rejected immediately while the
# first request is still alive and refreshing its heartbeat.
post "$API1" "$SLOW_KEY" "$SLOW_B" >/tmp/demo_first.code &
FIRST_PID=$!
sleep 0.5
started="$(date +%s.%N)"
code=$(post "$API2" "$SLOW_KEY" "$SLOW_B")
elapsed="$(python3 -c "print(f'{$(date +%s.%N) - $started:.2f}s')")"
echo "parallel retry on api2 -> $code after $elapsed (prompt, no wait for api1)"
[ "$code" = "409" ] && echo "OK: retry rejected while request is live"
wait "$FIRST_PID"
echo "original request -> $(cat /tmp/demo_first.code)"
code=$(post "$API2" "$SLOW_KEY" "$SLOW_B")
echo "post-completion retry -> $code (replays receipt, same id)"

say "4) crash recovery after the ${STALE_SECONDS}s stale threshold"
CRASH_KEY="DEMO-CRASH-$TS"
# Insert an orphaned pending marker and backdate it, simulating a process that
# registered the request and died before completing.
docker compose exec -T db psql -U research -d researchdb -v ON_ERROR_STOP=1 <<SQL
INSERT INTO idempotency_records
  (key, content_hash, state, created_at, updated_at, owner_id, fence_version, heartbeat_at)
VALUES
  ('$CRASH_KEY', 'orphan-hash', 'pending',
   now() - interval '${STALE_SECONDS} seconds', now() - interval '${STALE_SECONDS} seconds',
   'dead-process', 1, now() - interval '${STALE_SECONDS} seconds');
SQL
echo "orphaned pending marker inserted (owner dead-process, age ${STALE_SECONDS}s)"
code=$(post "$API2" "$CRASH_KEY" "$(patient_bundle "MRN-CRASH-$TS")")
echo "recovery POST api2 -> $code"
[ "$code" = "200" ] && echo "OK: slot recovered and bundle applied exactly once"
# A second recovery attempt must find the completed row, not apply it again.
code=$(post "$API1" "$CRASH_KEY" "$(patient_bundle "MRN-CRASH-$TS")")
echo "second POST api1 -> $code (replays the recovered receipt)"
docker compose exec -T db psql -U research -d researchdb -c \
  "SELECT key, state, fence_version, owner_id IS NULL AS owner_cleared FROM idempotency_records WHERE key='$CRASH_KEY';"

say "DEMO OK"
