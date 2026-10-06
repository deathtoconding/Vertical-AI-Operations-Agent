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

# The application and its pinned dependencies are installed into a virtual environment rather
# than a `--prefix` directory. `--prefix=/install` writes packages where the interpreter does not
# look: nothing adds `/install/lib/python3.11/site-packages` to `sys.path`, so the image built
# cleanly and then died at boot with `ModuleNotFoundError: No module named 'alembic'` — the
# container gate could never pass. A venv is a real site directory for its interpreter, so
# imports, `.pth` files and console scripts behave exactly as they do in development.
RUN python -m pip install --upgrade pip \
    && python -m pip wheel --wheel-dir /wheels . \
    && python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-index --find-links /wheels \
        /wheels/ai_operations_agent-*.whl

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
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH="/app" \
    AIOPS_ENV=production

# Runtime libraries only: libpq for psycopg, curl for the healthcheck.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 aiops \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin aiops

COPY --from=builder /opt/venv /opt/venv
COPY --chown=10001:10001 app /app/app
COPY --chown=10001:10001 prompts /app/prompts
COPY --chown=10001:10001 configs /app/configs
COPY --chown=10001:10001 scripts/entrypoint.sh /app/scripts/entrypoint.sh
COPY --chown=10001:10001 app/persistence/alembic.ini /app/app/persistence/alembic.ini

# No package manager in the released image: the interpreter, the virtual environment and every
# wheel are final at build time. `pip`, `setuptools` and the copies of `wheel`/`jaraco.context`
# vendored inside setuptools are bootstrapping tools this project neither pins nor ships — and
# nine HIGH/CRITICAL advisories in the release gate came from exactly those distributions, none
# of which a dependency change here could patch. Removing them is the fix (SEC-001, DEV-001).
RUN set -eux; \
    for prefix in /usr/local /opt/venv; do \
        rm -rf \
            "$prefix/lib/python3.11/site-packages/pip" \
            "$prefix"/lib/python3.11/site-packages/pip-*.dist-info \
            "$prefix/lib/python3.11/site-packages/setuptools" \
            "$prefix"/lib/python3.11/site-packages/setuptools-*.dist-info \
            "$prefix/lib/python3.11/site-packages/wheel" \
            "$prefix"/lib/python3.11/site-packages/wheel-*.dist-info \
            "$prefix/lib/python3.11/site-packages/pkg_resources" \
            "$prefix/lib/python3.11/site-packages/_distutils_hack" \
            "$prefix/lib/python3.11/site-packages/distutils-precedence.pth" \
            "$prefix/bin/pip" "$prefix/bin/pip3" "$prefix/bin/pip3.11"; \
    done

WORKDIR /app
USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# The entrypoint applies migrations, then execs the server (PID 1 receives SIGTERM).
ENTRYPOINT ["/bin/sh", "/app/scripts/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
