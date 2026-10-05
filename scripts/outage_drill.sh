#!/usr/bin/env bash
# pass() always succeeds, so "check && pass || fail" is a safe if-then-else here.
# shellcheck disable=SC2015
# =============================================================================
# Simulated SAP outage drill for the Docker sandbox.
#
# Cuts the middleware's network link to SAP (the factory side keeps running),
# verifies that records are cached locally and the daemon stays healthy,
# restarts the middleware mid-outage to prove the cache survives, restores the
# link and verifies that every cached record is delivered.
#
# Usage:  ./scripts/outage_drill.sh [outage_seconds]      (default 90)
# Exit code 0 = drill passed, 1 = a check failed (the link is restored anyway).
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OUTAGE_SECONDS="${1:-90}"
RECOVERY_TIMEOUT="${RECOVERY_TIMEOUT:-300}"
PROJECT="$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')"
SAP_NETWORK="${PROJECT}_sap"
STATS_URL="http://127.0.0.1:8080/_mock/stats"
FAILED=0
LINK_CUT=0

log()  { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
pass() { printf '  PASS  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; FAILED=1; }

middleware_id() { docker compose ps -q middleware; }

status_field() {
  docker compose exec -T middleware python /app/main.py status 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"
}

received() {
  curl -fsS "$STATS_URL" | python3 -c 'import json,sys; print(json.load(sys.stdin)["received"])'
}

health() {
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$(middleware_id)"
}

restore_link() {
  if [ "$LINK_CUT" -eq 1 ]; then
    log "Restoring the SAP network link"
    docker network connect "$SAP_NETWORK" "$(middleware_id)" 2>/dev/null || true
    LINK_CUT=0
  fi
}
trap restore_link EXIT

# ----------------------------------------------------------------- preflight
log "Preflight"
if [ -z "$(middleware_id)" ]; then
  echo "The sandbox is not running. Start it with: docker compose up -d --build" >&2
  exit 1
fi
[ "$(health)" = "healthy" ] && pass "middleware is healthy" || fail "middleware is $(health) before the drill"
OUTBOX_BEFORE="$(status_field outbox_records)"
RECEIVED_BEFORE="$(received)"
echo "  outbox_records=$OUTBOX_BEFORE  sap_received=$RECEIVED_BEFORE"
[ "$OUTBOX_BEFORE" -eq 0 ] && pass "outbox is empty" || fail "outbox already holds $OUTBOX_BEFORE records"

# -------------------------------------------------------------------- outage
log "Cutting the middleware's link to SAP for ${OUTAGE_SECONDS}s (database link and simulator keep running)"
docker network disconnect "$SAP_NETWORK" "$(middleware_id)"
LINK_CUT=1

HALF=$(( OUTAGE_SECONDS / 2 ))
sleep "$HALF"

OUTBOX_MID="$(status_field outbox_records)"
echo "  outbox_records=$OUTBOX_MID after ${HALF}s"
[ "$OUTBOX_MID" -gt 0 ] && pass "new records are cached locally" || fail "nothing was cached during the outage"

log "Restarting the middleware during the outage (cache must survive)"
docker compose restart middleware > /dev/null
# Make sure the link is still cut after the restart.
docker network disconnect "$SAP_NETWORK" "$(middleware_id)" 2>/dev/null || true
OUTBOX_AFTER_RESTART="$(status_field outbox_records)"
[ "$OUTBOX_AFTER_RESTART" -ge "$OUTBOX_MID" ] \
  && pass "cache survived the restart ($OUTBOX_AFTER_RESTART records)" \
  || fail "cache shrank across the restart ($OUTBOX_MID -> $OUTBOX_AFTER_RESTART)"

sleep $(( OUTAGE_SECONDS - HALF ))

STATE="$(docker inspect --format '{{.State.Status}}' "$(middleware_id)")"
[ "$STATE" = "running" ] && pass "middleware kept running during the outage" || fail "middleware is $STATE"
OUTBOX_PEAK="$(status_field outbox_records)"
echo "  outbox_records=$OUTBOX_PEAK at the end of the outage"
docker compose logs --since "${OUTAGE_SECONDS}s" middleware 2>/dev/null \
  | grep -q "Circuit 'sap' opened" && pass "circuit breaker opened" || fail "circuit breaker did not open"

# ------------------------------------------------------------------ recovery
restore_link
log "Waiting up to ${RECOVERY_TIMEOUT}s for the outbox to drain"
DEADLINE=$(( $(date +%s) + RECOVERY_TIMEOUT ))
while :; do
  OUTBOX_NOW="$(status_field outbox_records)"
  if [ "$OUTBOX_NOW" -eq 0 ]; then
    break
  fi
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    break
  fi
  echo "  outbox_records=$OUTBOX_NOW, waiting"
  sleep 10
done

RECEIVED_AFTER="$(received)"
DELIVERED=$(( RECEIVED_AFTER - RECEIVED_BEFORE ))
[ "$OUTBOX_NOW" -eq 0 ] && pass "outbox drained" || fail "outbox still holds $OUTBOX_NOW records"
[ "$DELIVERED" -ge "$OUTBOX_PEAK" ] \
  && pass "SAP received $DELIVERED new records (at least the $OUTBOX_PEAK that were cached)" \
  || fail "SAP received only $DELIVERED records, $OUTBOX_PEAK were cached"
HEALTH_DEADLINE=$(( $(date +%s) + 150 ))
while [ "$(health)" != "healthy" ] && [ "$(date +%s)" -lt "$HEALTH_DEADLINE" ]; do
  sleep 5
done
[ "$(health)" = "healthy" ] && pass "middleware is healthy after recovery" || fail "middleware is $(health) after recovery"

log "Result"
if [ "$FAILED" -eq 0 ]; then
  echo "  DRILL PASSED"
else
  echo "  DRILL FAILED: see the FAIL lines above and 'docker compose logs middleware'"
fi
exit "$FAILED"
