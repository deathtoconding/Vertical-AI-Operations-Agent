#!/bin/sh
# Container entrypoint (DEV-001): apply the schema profile, run migrations, then exec the server.
#
# Why migrations run here rather than in a separate job: a container that starts against an
# un-migrated database is a container that fails in a confusing way. `alembic upgrade head` is
# idempotent, so replicas racing at boot converge on the same revision.
set -eu

# Optional deployment profile (configs/<env>.yaml). Non-secret defaults only; anything secret
# must arrive as an environment variable at run time.
if [ -n "${AIOPS_CONFIG_FILE:-}" ]; then
    echo "loading deployment profile ${AIOPS_CONFIG_FILE}"
    eval "$(python /app/scripts/load_profile.py --file "${AIOPS_CONFIG_FILE}")"
fi

if [ "${AIOPS_SKIP_MIGRATIONS:-false}" != "true" ]; then
    echo "applying database migrations"
    alembic -c /app/app/persistence/alembic.ini upgrade head
fi

exec "$@"
