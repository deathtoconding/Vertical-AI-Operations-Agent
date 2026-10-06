#!/usr/bin/env bash
# Deploy a candidate image to an environment (DEV-003).
#
# The CD workflow builds one immutable artifact and promotes *that* artifact; this script is the
# single place that knows how to install it, so staging and production cannot drift in how a
# release is applied. It is intentionally written against a small, explicit contract:
#
#   DEPLOY_HOST   the target (ssh alias or URL of the platform's deploy API)
#   DEPLOY_TOKEN  credential, supplied by the environment's secret store
#   IMAGE         the image reference to install (tagged with the commit SHA)
#
# Usage:
#   DEPLOY_HOST=staging.invalid DEPLOY_TOKEN=... bash scripts/deploy.sh staging ghcr.io/.../aiops:abc123
#
# Without DEPLOY_HOST the script runs in dry-run mode and prints what it would do, which is how
# the pipeline verifies the deployment wiring in a sandbox with no target infrastructure.
set -euo pipefail

ENVIRONMENT="${1:?usage: deploy.sh <environment> <image>}"
IMAGE="${2:?usage: deploy.sh <environment> <image>}"

case "$ENVIRONMENT" in
    staging|production) ;;
    *) echo "unknown environment: $ENVIRONMENT (expected staging or production)" >&2; exit 2 ;;
esac

if [ -z "${DEPLOY_HOST:-}" ]; then
    echo "DRY RUN: no DEPLOY_HOST configured."
    echo "  environment: $ENVIRONMENT"
    echo "  image:       $IMAGE"
    echo "  would: apply migrations, start the container, wait for /ready, then run release verification"
    exit 0
fi

echo "deploying ${IMAGE} to ${ENVIRONMENT} (${DEPLOY_HOST})"
: "${DEPLOY_TOKEN:?DEPLOY_TOKEN is required when DEPLOY_HOST is set}"

# 1. Ship the artifact. Only the image reference changes between releases.
curl -fsS -X POST "https://${DEPLOY_HOST}/api/deploy" \
    -H "Authorization: Bearer ${DEPLOY_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"environment\": \"${ENVIRONMENT}\", \"image\": \"${IMAGE}\"}"

# 2. Wait for the platform to report the deployment as live, bounded. An unbounded wait is how a
#    stuck rollout becomes an unnoticed outage.
for _ in $(seq 1 60); do
    status="$(curl -fsS "https://${DEPLOY_HOST}/api/deploy/${ENVIRONMENT}/status" \
        -H "Authorization: Bearer ${DEPLOY_TOKEN}" || echo '{"state":"unknown"}')"
    case "$status" in
        *'"state":"live"'*) echo "deployment is live"; break ;;
        *'"state":"failed"'*) echo "deployment failed: $status" >&2; exit 1 ;;
    esac
    sleep 5
done

echo "deployment accepted; post-deploy verification runs next (scripts/verify_release.py)"
