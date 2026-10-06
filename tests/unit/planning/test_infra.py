"""Container and local-stack contract (DEV-001).

Docker is not available in the CI sandbox, so this is a *static* validation of the artefacts
that make the container runnable and safe. Static tests are worth having here because the
failure modes they catch are exactly the ones that only appear in a built image:

* a runtime stage that still contains a compiler or a package manager;
* a container that runs as root, or starts the API before the schema exists;
* a secret baked into the image instead of injected at run time;
* a compose file that mounts a file nobody committed (the stack then fails at `up`, not in CI).

The declared entrypoint is executed too (with `alembic` replaced by a fake on PATH), so the
migration-then-exec contract is tested as a *program*, not only as text.
"""

from __future__ import annotations

import os
import pathlib
import re
import stat
import subprocess
import tomllib
from typing import Any

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "infra" / "docker" / "Dockerfile"
COMPOSE = REPO_ROOT / "infra" / "compose" / "docker-compose.yml"
ENTRYPOINT = REPO_ROOT / "scripts" / "entrypoint.sh"
SECRETS_DIR = REPO_ROOT / "infra" / "compose" / "secrets"
GRAFANA = REPO_ROOT / "infra" / "deployment" / "grafana"

pytestmark = [pytest.mark.story("DEV-001"), pytest.mark.unit]


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _directives(text: str) -> list[str]:
    """Logical Dockerfile instructions (continuations joined, comments dropped)."""
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


# --------------------------------------------------------------------------- #
# Image build
# --------------------------------------------------------------------------- #


def test_the_image_is_multi_stage(dockerfile: str) -> None:
    stages = [line for line in _directives(dockerfile) if line.upper().startswith("FROM ")]
    assert len(stages) >= 2, "a single-stage build ships build tooling to production"
    assert any(" AS builder" in line for line in stages)
    assert any(" AS runtime" in line for line in stages)


def test_the_runtime_stage_has_no_build_tooling(dockerfile: str) -> None:
    """The final stage is what ships: it must not install compilers or dev packages."""
    runtime = dockerfile.split(" AS runtime", 1)[1]
    assert "build-essential" not in runtime
    assert "apt-get install" in runtime and "--no-install-recommends" in runtime
    assert "rm -rf /var/lib/apt/lists/*" in runtime


def test_base_images_are_pinned_to_a_minor_version(dockerfile: str) -> None:
    for line in _directives(dockerfile):
        if line.upper().startswith("FROM "):
            image = line.split()[1]
            assert image != "latest" and ":" in image, image
            tag = image.rsplit(":", 1)[1]
            assert tag[0].isdigit(), f"{image} must be pinned to a version, not a floating tag"


def test_the_container_runs_as_a_non_root_user(dockerfile: str) -> None:
    assert re.search(r"^USER\s+10001:10001\s*$", dockerfile, re.MULTILINE), "USER must be set"
    assert "useradd" in dockerfile and "--uid 10001" in dockerfile
    assert "USER root" not in dockerfile


def test_the_image_declares_a_healthcheck_against_liveness(dockerfile: str) -> None:
    healthcheck = re.search(r"^HEALTHCHECK.*?(?=\n[A-Z]|\Z)", dockerfile, re.MULTILINE | re.DOTALL)
    assert healthcheck, "a container with no healthcheck is opaque to the platform"
    text = healthcheck.group(0)
    assert "/health" in text, "readiness/liveness must be probed at the documented endpoint"
    assert "start-period" in text and "retries" in text


def test_there_are_no_secrets_in_the_image(dockerfile: str) -> None:
    """Image layers are readable by anyone who can pull the image (rule 6)."""
    offenders = [
        line
        for line in _directives(dockerfile)
        if re.search(
            r"(?i)(password|secret|api[_-]?key|token)\s*=\s*[^$\s]",  # no placeholder/expansion
            line,
        )
    ]
    assert not offenders, f"credential-shaped ENV/ARG in the image: {offenders}"


# --------------------------------------------------------------------------- #
# Entrypoint: migrate, then exec
# --------------------------------------------------------------------------- #


def test_entrypoint_is_executable() -> None:
    assert ENTRYPOINT.exists()
    mode = ENTRYPOINT.stat().st_mode
    assert mode & stat.S_IXUSR, "an entrypoint that is not executable fails at container start"


def test_entrypoint_migrates_before_starting_the_server() -> None:
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert "alembic" in text and "upgrade head" in text, "schema must be migrated on start"
    assert text.index("alembic") < text.index("exec "), "migrations must precede the server"
    assert 'exec "$@"' in text, "the server must receive PID 1 (signal handling)"
    assert text.startswith("#!/bin/sh") and "set -eu" in text


def test_entrypoint_fails_loudly_when_migrations_cannot_be_applied(tmp_path: pathlib.Path) -> None:
    """A container that serves traffic against an unmigrated schema is worse than a crash."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls.log"
    for name, body in (
        ("alembic", f'#!/bin/sh\necho "alembic $*" >> "{calls}"\nexit 1\n'),
        ("sleep", "#!/bin/sh\nexit 0\n"),
    ):
        script = fake_bin / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AIOPS_DATABASE_URL": "postgresql+psycopg://aiops:aiops@localhost:5432/aiops",
        "AIOPS_DB_WAIT_ATTEMPTS": "3",
        "AIOPS_DB_WAIT_INTERVAL": "0",
    }
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/bin/sh", str(ENTRYPOINT), "uvicorn", "app.main:app"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode != 0, "migration failure must not fall through to the server"
    assert "giving up" in result.stdout
    assert calls.read_text(encoding="utf-8").count("upgrade head") == 3, "retries must be bounded"
    assert "uvicorn" not in result.stdout.split("[entrypoint] starting", 1)[-1]


def test_entrypoint_refuses_to_start_without_a_database_url() -> None:
    env = {k: v for k, v in os.environ.items() if k != "AIOPS_DATABASE_URL"}
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/bin/sh", str(ENTRYPOINT), "uvicorn", "app.main:app"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2
    assert "AIOPS_DATABASE_URL" in result.stdout + result.stderr


def test_entrypoint_starts_the_server_when_migrations_succeed(tmp_path: pathlib.Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in (
        ("alembic", "#!/bin/sh\necho applied\nexit 0\n"),
        ("sleep", "#!/bin/sh\nexit 0\n"),
    ):
        script = fake_bin / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
    marker = tmp_path / "started"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AIOPS_DATABASE_URL": "postgresql+psycopg://aiops:aiops@localhost:5432/aiops",
    }
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/bin/sh", str(ENTRYPOINT), "/bin/sh", "-c", f"touch {marker}"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert marker.exists(), "the entrypoint must exec the command it was given"


# --------------------------------------------------------------------------- #
# Compose stack
# --------------------------------------------------------------------------- #


def test_compose_defines_the_documented_services(compose: dict[str, Any]) -> None:
    assert set(compose["services"]) == {"postgres", "app", "prometheus", "grafana"}
    assert compose["name"] == "aiops"


def test_the_application_waits_for_a_healthy_database(compose: dict[str, Any]) -> None:
    app = compose["services"]["app"]
    assert app["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "healthcheck" in compose["services"]["postgres"]
    assert app["restart"] == "unless-stopped"


def test_compose_passes_configuration_as_environment_not_literals(compose: dict[str, Any]) -> None:
    """No credential may be committed: values come from the shell or a secret file."""
    app_env = compose["services"]["app"]["environment"]
    for key in ("AIOPS_API_TOKENS", "AIOPS_DATABASE_URL"):
        assert key in app_env
        assert "${" in app_env[key], f"{key} must be injected, not baked in"
        assert ":?" in app_env[key], f"{key} must fail fast when unset"
    serialised = yaml.safe_dump(compose)
    assert not re.search(r"(?i)(password|token|secret)\s*:\s*[A-Za-z0-9_\-]{12,}", serialised)


def test_the_database_secret_comes_from_a_file(compose: dict[str, Any]) -> None:
    postgres = compose["services"]["postgres"]
    assert postgres["environment"]["POSTGRES_PASSWORD_FILE"] == "/run/secrets/postgres_password"
    assert "postgres_password" in compose["secrets"]
    assert "POSTGRES_PASSWORD" not in postgres["environment"]


def test_the_real_secret_files_are_git_ignored_and_placeholders_are_committed() -> None:
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "infra/compose/secrets/*" in ignore
    assert "!infra/compose/secrets/*.example" in ignore
    for name in ("postgres_password.example", "grafana_password.example"):
        assert (SECRETS_DIR / name).exists(), f"{name} documents what to create"
    assert not (SECRETS_DIR / "postgres_password").exists()


def test_compose_mounts_only_files_that_exist(compose: dict[str, Any]) -> None:
    """A bind mount of a missing path silently creates a directory and breaks the stack."""
    for service in compose["services"].values():
        for volume in service.get("volumes", []):
            source = str(volume).split(":", 1)[0]
            if source.startswith("."):
                resolved = (COMPOSE.parent / source).resolve()
                assert resolved.exists(), f"compose mounts {source}, which is not committed"
    assert (GRAFANA / "dashboard.json").exists()
    assert (GRAFANA / "provisioning" / "datasources" / "prometheus.yml").exists()
    assert (GRAFANA / "provisioning" / "dashboards" / "aiops.yml").exists()


def test_grafana_provisioning_points_at_the_compose_service() -> None:
    datasources = yaml.safe_load(
        (GRAFANA / "provisioning" / "datasources" / "prometheus.yml").read_text(encoding="utf-8")
    )
    urls = [item["url"] for item in datasources["datasources"]]
    assert "http://prometheus:9090" in urls, "the datasource must use the compose service name"

    provider = yaml.safe_load(
        (GRAFANA / "provisioning" / "dashboards" / "aiops.yml").read_text(encoding="utf-8")
    )
    assert provider["providers"][0]["options"]["path"] == "/var/lib/grafana/dashboards"


def test_prometheus_scrapes_the_application_metrics_endpoint() -> None:
    config = yaml.safe_load(
        (REPO_ROOT / "infra" / "deployment" / "prometheus" / "prometheus.yml").read_text(
            encoding="utf-8"
        )
    )
    targets = [
        job["static_configs"][0]["targets"]
        for job in config["scrape_configs"]
        if "app" in job["job_name"] or "aiops" in job["job_name"]
    ]
    assert targets, "the app must be scraped, or no SLO rule can fire"
    assert any("app:8000" in target for group in targets for target in group)
    rules = (REPO_ROOT / "infra" / "deployment" / "prometheus" / "rules.yml").read_text(
        encoding="utf-8"
    )
    assert "aiops_" in rules, "alert rules must reference real metric families"


def test_the_lockfile_and_project_metadata_agree_on_the_runtime() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires = project["project"]["requires-python"]
    assert "3.11" in requires
    assert "python:3.11" in DOCKERFILE.read_text(encoding="utf-8")
