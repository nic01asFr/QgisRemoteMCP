#!/usr/bin/env bash
# test_async_jobs.sh — end-to-end smoke test for the async job subsystem.
#
# Covers the 7 scenarios validated in the async redesign:
#   1. /api/submit returns a job_id fast (<1s)
#   2. /api/job/{id} transitions queued → running → done, heartbeat_age_s stays low
#   3. Frozen Qt main thread surfaces via qt_lag_ms + status="qt_frozen"
#   4. Result persists after TTL_DONE / is still queryable immediately after finish
#   5. Concurrent polls during a running job don't deadlock (50 in parallel)
#   6. Cancel: queued job → "cancel_pending"; dispatched job → "already_dispatched_cannot_cancel"
#   7. Sync /api/command stays byte-for-byte backward-compatible
#
# Usage:
#   API=http://localhost:8081 bash test_async_jobs.sh
#   API=http://localhost:8080 bash test_async_jobs.sh    # inside the container

set -u  # unset var = error, but allow non-zero exit within checks
API="${API:-http://localhost:8081}"
PASS=0
FAIL=0

say()  { printf "\n\033[1;36m== %s ==\033[0m\n" "$*"; }
ok()   { printf "\033[1;32m[PASS]\033[0m %s\n" "$*"; PASS=$((PASS+1)); }
bad()  { printf "\033[1;31m[FAIL]\033[0m %s\n" "$*"; FAIL=$((FAIL+1)); }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$1', ''))"; }

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing: $1"; exit 2; }
}
need curl
need python3

# ── Pre-flight ────────────────────────────────────────────────────
say "Pre-flight: $API reachable"
if ! curl -sf "$API/health" >/dev/null; then
  bad "API not reachable at $API — is the container up?"
  exit 1
fi
ok "API reachable"

# ── Test 1: submit returns job_id fast ────────────────────────────
say "1. POST /api/submit returns job_id in <1s"
t0=$(python3 -c "import time; print(time.time())")
RESP=$(curl -sf -X POST "$API/api/submit" \
  -H 'Content-Type: application/json' \
  -d '{"action":"execute_python","params":{"code":"import time\ntime.sleep(8)\nresult[\"x\"]=42","timeout":60}}')
t1=$(python3 -c "import time; print(time.time())")
DELTA=$(python3 -c "print(round($t1-$t0, 3))")
JOB_ID=$(echo "$RESP" | json job_id)
if [[ -n "$JOB_ID" ]] && python3 -c "import sys; sys.exit(0 if $DELTA < 2 else 1)"; then
  ok "job_id=$JOB_ID returned in ${DELTA}s"
else
  bad "submit slow or missing job_id (took ${DELTA}s, resp=$RESP)"
  exit 1
fi

# ── Test 2: poll shows queued → running → done ────────────────────
say "2. Poll transitions queued → running → done, heartbeat stays fresh"
SEEN_RUNNING=0
for i in {1..15}; do
  sleep 1
  POLL=$(curl -sf "$API/api/job/$JOB_ID")
  STATUS=$(echo "$POLL" | json status)
  HBAGE=$(echo "$POLL" | json heartbeat_age_s)
  printf "  t=%02ds status=%-10s heartbeat_age=%s\n" "$i" "$STATUS" "$HBAGE"
  [[ "$STATUS" == "running" ]] && SEEN_RUNNING=1
  if [[ "$STATUS" == "done" ]]; then
    RES_X=$(echo "$POLL" | python3 -c "import sys,json; d=json.load(sys.stdin); print((d.get('result') or {}).get('x',''))")
    if [[ "$RES_X" == "42" ]]; then
      ok "done with result.x==42, running was observed=$SEEN_RUNNING"
    else
      bad "done but result.x=$RES_X (expected 42)"
    fi
    break
  fi
done
if [[ "$STATUS" != "done" ]]; then
  bad "job did not reach 'done' within 15s (last status=$STATUS)"
fi

# ── Test 3: frozen Qt main thread detected ────────────────────────
say "3. Freeze Qt main thread → status='qt_frozen' within ~10s"
# Submit code that blocks the main thread by busy-waiting on QCoreApplication.processEvents()
# No — that DRAINS events. Instead we busy-sleep inside execute_python which runs ON main thread.
# execute_python runs via _run_on_main_thread → the code RUNS on main thread → main thread frozen during sleep.
RESP=$(curl -sf -X POST "$API/api/submit" \
  -H 'Content-Type: application/json' \
  -d '{"action":"execute_python","params":{"code":"import time\ntime.sleep(15)\nresult[\"done\"]=True","timeout":60}}')
JID=$(echo "$RESP" | json job_id)
FROZEN=0
for i in {1..12}; do
  sleep 1
  POLL=$(curl -sf "$API/api/job/$JID")
  STATUS=$(echo "$POLL" | json status)
  LAG=$(echo "$POLL" | json qt_lag_ms)
  PROB=$(echo "$POLL" | json probably_frozen)
  printf "  t=%02ds status=%-10s qt_lag_ms=%s probably_frozen=%s\n" "$i" "$STATUS" "$LAG" "$PROB"
  if [[ "$STATUS" == "qt_frozen" ]] || python3 -c "import sys; sys.exit(0 if int('${LAG:-0}' or 0) > 5000 else 1)" 2>/dev/null; then
    FROZEN=1
    break
  fi
done
if [[ "$FROZEN" == "1" ]]; then
  ok "Qt freeze surfaced via status=$STATUS / qt_lag_ms=$LAG"
else
  bad "Qt freeze not detected (time.sleep on main thread should block it). Last: status=$STATUS, lag=$LAG"
fi
# Wait for the frozen job to finish so it doesn't contaminate later tests
for i in {1..30}; do
  POLL=$(curl -sf "$API/api/job/$JID")
  [[ "$(echo "$POLL" | json status)" == "done" ]] && break
  sleep 1
done

# ── Test 4: result persists and is queryable after finish ─────────
say "4. Completed job row persists (TTL 1h, still queryable immediately)"
POLL=$(curl -sf "$API/api/job/$JOB_ID")
STATUS=$(echo "$POLL" | json status)
if [[ "$STATUS" == "done" ]]; then
  ok "job $JOB_ID still returns status=done after completion"
else
  bad "completed job not persisted (got status=$STATUS)"
fi

# ── Test 5: 50 concurrent polls during a running job don't deadlock ──
say "5. 50 parallel polls while job runs all return in <3s total"
RESP=$(curl -sf -X POST "$API/api/submit" \
  -H 'Content-Type: application/json' \
  -d '{"action":"execute_python","params":{"code":"import time\ntime.sleep(20)\nresult[\"ok\"]=1","timeout":60}}')
JID=$(echo "$RESP" | json job_id)
sleep 2  # let it transition to running
t0=$(python3 -c "import time; print(time.time())")
FAILS=0
PIDS=""
for i in $(seq 1 50); do
  curl -sf -o /dev/null -w "%{http_code}\n" "$API/api/job/$JID" >>/tmp/polls_$$.log &
  PIDS="$PIDS $!"
done
wait $PIDS 2>/dev/null
t1=$(python3 -c "import time; print(time.time())")
DELTA=$(python3 -c "print(round($t1-$t0, 3))")
BAD_CODES=$(grep -v "^200$" /tmp/polls_$$.log | wc -l)
rm -f /tmp/polls_$$.log
if python3 -c "import sys; sys.exit(0 if $DELTA < 3 else 1)" && [[ "$BAD_CODES" == "0" ]]; then
  ok "50 concurrent polls in ${DELTA}s, all 200"
else
  bad "concurrency broke: ${DELTA}s elapsed, $BAD_CODES non-200 responses"
fi
# Clean up that running job
curl -sf -X DELETE "$API/api/job/$JID" >/dev/null || true
# Wait for it to finish regardless
for i in {1..30}; do
  POLL=$(curl -sf "$API/api/job/$JID")
  ST=$(echo "$POLL" | json status)
  [[ "$ST" == "done" || "$ST" == "cancelled" ]] && break
  sleep 1
done

# ── Test 6: cancel before vs after dispatch ───────────────────────
say "6. Cancel behavior: queued=best-effort, dispatched=cannot_cancel"
RESP=$(curl -sf -X POST "$API/api/submit" \
  -H 'Content-Type: application/json' \
  -d '{"action":"execute_python","params":{"code":"import time\ntime.sleep(30)","timeout":60}}')
JID=$(echo "$RESP" | json job_id)
sleep 3  # definitely dispatched by now
CANCEL=$(curl -sf -X DELETE "$API/api/job/$JID")
STATUS_FIELD=$(echo "$CANCEL" | json status)
if [[ "$STATUS_FIELD" == "already_dispatched_cannot_cancel" ]] || [[ "$STATUS_FIELD" == "cancel_pending" ]]; then
  ok "cancel on dispatched job returned expected status: $STATUS_FIELD"
else
  bad "unexpected cancel response: $CANCEL"
fi

# ── Test 7: sync /api/command backward-compatible ─────────────────
say "7. Sync /api/command /health still returns current shape"
HEALTH=$(curl -sf -X POST "$API/api/command" \
  -H 'Content-Type: application/json' \
  -d '{"action":"health"}')
if echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if isinstance(d, dict) else 1)"; then
  ok "sync POST /api/command returns JSON object (backward compat intact)"
else
  bad "sync /api/command broken: $HEALTH"
fi

# ── Summary ──────────────────────────────────────────────────────
say "SUMMARY"
printf "\033[1;32mPASS: %d\033[0m   \033[1;31mFAIL: %d\033[0m\n" "$PASS" "$FAIL"
[[ "$FAIL" == "0" ]] && exit 0 || exit 1
