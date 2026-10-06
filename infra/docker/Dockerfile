# DEV-001 — multi-stage build producing a slim, non-root runtime image.
#
# Stage 1 resolves and builds wheels against build tooling that never ships; stage 2 installs
# only the wheels. The result has no compiler, no package manager cache and no test code, so
# the attack surface of the image is the application and its pinned runtime dependencies.
#
# It runs as UID 10001 with a healthcheck, and it does not contain a single secret: every
# credential arrives as an environment variable at run time (rule 6).

# --------------------------------------------------------------------------- #
# Stage 1 — build
# --------------------------------------------------------------------------- #
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
COPY scripts ./scripts
COPY prompts ./prompts

RUN python -m pip install --upgrade pip \
    && python -m pip wheel --wheel-dir /wheels . \
    && python -m pip install --prefix=/install --no-deps /wheels/*.whl

# --------------------------------------------------------------------------- #
# Stage 2 — runtime
# --------------------------------------------------------------------------- #
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="ai-operations-agent" \
      org.opencontainers.image.description="Vertical AI Operations Agent — policy-governed SaaS operations engineer" \
      org.opencontainers.image.source="https://github.com/deathtoconding/Vertical-AI-Operations-Agent" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/install/bin:${PATH}" \
    PYTHONPATH="/app" \
    AIOPS_ENV=production

# Runtime libraries only: libpq for psycopg, curl for the healthcheck.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 aiops \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin aiops

COPY --from=builder /install /install
COPY --chown=10001:10001 app /app/app
COPY --chown=10001:10001 prompts /app/prompts
COPY --chown=10001:10001 configs /app/configs
COPY --chown=10001:10001 scripts/entrypoint.sh /app/scripts/entrypoint.sh
COPY --chown=10001:10001 app/persistence/alembic.ini /app/app/persistence/alembic.ini

WORKDIR /app
USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# The entrypoint applies migrations, then execs the server (PID 1 receives SIGTERM).
ENTRYPOINT ["/bin/sh", "/app/scripts/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
