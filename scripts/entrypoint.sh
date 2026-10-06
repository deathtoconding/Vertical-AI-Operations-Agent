#!/bin/sh
# DEV-001 — container entrypoint: apply migrations, then hand over to the server.
#
# A container that starts the API before the schema exists "works" until the first request
# touches a table, which is exactly the failure mode a release gate is supposed to catch. So the
# entrypoint is: wait for the database (bounded), migrate, then `exec` the server so that PID 1
# is uvicorn and receives SIGTERM directly (graceful shutdown, no orphaned processes).
#
# Failure is loud and immediate: if migrations cannot be applied the container exits non-zero and
# the rollout stops, rather than serving traffic against a schema it does not understand.
set -eu

MAX_ATTEMPTS="${AIOPS_DB_WAIT_ATTEMPTS:-30}"
SLEEP_SECONDS="${AIOPS_DB_WAIT_INTERVAL:-2}"

log() { printf '[entrypoint] %s\n' "$1"; }

if [ -z "${AIOPS_DATABASE_URL:-}" ]; then
    log "AIOPS_DATABASE_URL is not set; refusing to start (no implicit default in a container)"
    exit 2
fi

log "applying database migrations (alembic upgrade head)"
attempt=1
while :; do
    if alembic -c /app/app/persistence/alembic.ini upgrade head; then
        log "migrations applied"
        break
    fi
    if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
        log "migrations still failing after ${attempt} attempts; giving up"
        exit 1
    fi
    log "database not ready (attempt ${attempt}/${MAX_ATTEMPTS}); retrying in ${SLEEP_SECONDS}s"
    attempt=$((attempt + 1))
    sleep "$SLEEP_SECONDS"
done

log "starting: $*"
exec "$@"
