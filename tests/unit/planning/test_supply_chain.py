"""Supply-chain and pipeline contract (DEV-002, SEC-005).

The pipelines are code, so they are tested like code: the workflows must parse, every job must
be pinned and least-privileged, every command they run must exist in the repository, and the
dependency lock must actually correspond to the dependencies we declare. A CI file that calls a
deleted script is a green build that tests nothing.

None of these tests need the network: they validate the *repository's* contract with its own
tooling. (`pip-audit`, Trivy and CodeQL need a runner and databases; those run in CI.)
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import tomllib
from typing import Any

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
LOCK = REPO_ROOT / "requirements.lock"
PRE_COMMIT = REPO_ROOT / ".pre-commit-config.yaml"
GITLEAKS = REPO_ROOT / "configs" / "gitleaks.toml"

pytestmark = [pytest.mark.story("DEV-002"), pytest.mark.story("SEC-005"), pytest.mark.unit]

WORKFLOW_FILES = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def load_workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def steps_of(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in document["jobs"].values() for step in job.get("steps", [])]


def run_commands(document: dict[str, Any]) -> list[str]:
    return [str(step["run"]) for step in steps_of(document) if "run" in step]


@pytest.fixture(scope="module")
def pyproject() -> dict[str, Any]:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Workflow shape and pinning
# --------------------------------------------------------------------------- #


def test_the_expected_pipelines_exist_and_parse() -> None:
    names = {path.name for path in WORKFLOW_FILES}
    assert names == {"ci.yml", "security.yml", "ai-evaluation.yml", "cd.yml"}
    for path in WORKFLOW_FILES:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        # PyYAML parses the bare `on:` key as boolean True (YAML 1.1), so accept both.
        assert "on" in document or True in document, f"{path.name} must declare triggers"
        assert document.get("jobs"), f"{path.name} must declare jobs"


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda item: item.name)
def test_every_job_is_scheduled_and_bounded(path: pathlib.Path) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job_id, job in document["jobs"].items():
        assert job.get("runs-on") or job.get("uses"), f"{path.name}:{job_id} has no runner"
        if "uses" not in job:
            assert job.get("steps"), f"{path.name}:{job_id} has no steps"
        assert job.get("timeout-minutes"), f"{path.name}:{job_id} must bound its runtime"


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda item: item.name)
def test_third_party_actions_are_pinned_to_a_reference(path: pathlib.Path) -> None:
    """`uses: owner/action` with no ref is a supply-chain hole (SEC-005)."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for step in steps_of(document):
        reference = step.get("uses")
        if not reference or reference.startswith("./"):
            continue
        assert "@" in reference, f"{path.name}: {reference} is not pinned"
        _, _, ref = reference.partition("@")
        assert ref and not ref.startswith(("${{", "main", "master")), (
            f"{path.name}: {reference} must pin a version tag, not a branch"
        )


def test_pipelines_declare_least_privilege_permissions() -> None:
    """A token that can write everywhere is the cheapest way to lose a repository."""
    for name in ("ci.yml", "security.yml", "ai-evaluation.yml", "cd.yml"):
        document = load_workflow(name)
        top = document.get("permissions") or {}
        assert top.get("contents") == "read", f"{name} must read contents, and nothing more"
        assert set(top) <= {"contents", "packages"}, f"{name} declares an unrelated scope"
    for name in ("ci.yml", "security.yml", "ai-evaluation.yml", "cd.yml"):
        for job_id, job in load_workflow(name)["jobs"].items():
            assert job.get("permissions"), f"{name}:{job_id} must declare its own permissions"


def test_write_permissions_are_confined_to_the_job_that_needs_them() -> None:
    cd = load_workflow("cd.yml")
    writers = {
        job_id
        for job_id, job in cd["jobs"].items()
        if any(value == "write" for value in (job.get("permissions") or {}).values())
    }
    assert writers == {"build"}, "only the image-publishing job may write"
    assert cd["jobs"]["build"]["permissions"]["packages"] == "write"

    security = load_workflow("security.yml")
    sarif_writers = {
        job_id
        for job_id, job in security["jobs"].items()
        if job["permissions"].get("security-events") == "write"
    }
    assert sarif_writers == {"container"}, "only the SARIF upload may write security events"


# --------------------------------------------------------------------------- #
# The pipelines run the repository's real checks
# --------------------------------------------------------------------------- #

CONCRETE_PATH = re.compile(r"(?:python|bash)\s+(scripts/[\w./-]+\.(?:py|sh))")


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda item: item.name)
def test_commands_reference_files_that_exist(path: pathlib.Path) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for command in run_commands(document):
        for match in CONCRETE_PATH.finditer(command):
            target = REPO_ROOT / match.group(1)
            assert target.exists(), f"{path.name} runs {match.group(1)}, which does not exist"


def test_ci_runs_every_blocking_stage_in_order() -> None:
    ci = load_workflow("ci.yml")
    commands = " \n".join(run_commands(ci))
    for needle, description in (
        ("check_repo_hygiene.sh", "pre-commit hygiene"),
        ("ruff format --check", "format check"),
        ("ruff check", "lint"),
        ("mypy", "type checking"),
        ("tests/unit/planning", "plan contract"),
        ("pytest tests/unit", "unit tests"),
        ("pytest tests/integration", "integration tests (real PostgreSQL)"),
        ("pytest tests/security", "security tests"),
        ("pytest tests/e2e", "end-to-end tests"),
        ("--cov=app", "coverage"),
        ("docker build", "image build"),
    ):
        assert needle in commands, f"ci.yml does not run {description}"
    assert "postgres:" in yaml.safe_dump(ci), "integration tests need a real database service"
    assert "continue-on-error" not in yaml.safe_dump(ci), "no stage may be allowed to fail"


def test_the_local_mirror_of_ci_matches_the_workflow() -> None:
    """`make ci` is what developers run; it must not be a weaker pipeline than CI."""
    local = (REPO_ROOT / "scripts" / "ci.sh").read_text(encoding="utf-8")
    # The eval regression gate lives in the evaluation workflow and in CD (against the deployed
    # candidate); everything else is split between ci.yml and that workflow.
    pipelines = "\n".join(
        yaml.safe_dump(load_workflow(name)) for name in ("ci.yml", "ai-evaluation.yml", "cd.yml")
    )
    for needle in (
        "check_repo_hygiene.sh",
        "format --check",
        "check ",
        "mypy",
        "tests/unit/planning",
        "tests/unit -q",
        "tests/integration -q",
        "tests/security -q",
        "tests/e2e -q",
        "--check-regression",
    ):
        assert needle in local, f"scripts/ci.sh omits {needle!r}"
        assert needle in pipelines, f"no workflow runs {needle!r}"


def test_the_release_pipeline_gates_production_on_verification() -> None:
    cd = load_workflow("cd.yml")
    dumped = yaml.safe_dump(cd)
    assert "environment" in dumped, "the production job must use a protected environment"
    assert cd["jobs"]["production"].get("environment", {}).get("name") == "production"
    for needle in ("scripts/deploy.sh", "scripts/verify_release.py", "scripts/rollback_drill.py"):
        assert needle in dumped, f"cd.yml must run {needle}"
    assert "needs:" in dumped, "jobs must be ordered by dependencies, not by chance"


def test_the_evaluation_pipeline_blocks_on_regression() -> None:
    document = load_workflow("ai-evaluation.yml")
    commands = " \n".join(run_commands(document))
    assert "evals/runner.py" in commands
    assert "evals/regression.py" in commands, "the report comparison must run in CI too"
    assert "--baseline evals/baselines/baseline.json" in commands, (
        "the gate must compare against the stored baseline"
    )
    assert "tests/unit/eval" in commands
    triggers = document.get("on") or document.get(True)
    assert triggers, "the eval workflow must declare triggers"
    # The same gate must run against the *deployed candidate* before production.
    cd = yaml.safe_dump(load_workflow("cd.yml"))
    assert "--check-regression" in cd, "staging must gate on the eval regression suite"


def test_the_security_pipeline_runs_every_declared_control() -> None:
    document = load_workflow("security.yml")
    commands = " \n".join(run_commands(document))
    dumped = yaml.safe_dump(document)
    assert "check_pins.py" in commands, "pins are verified before any audit"
    assert "pip-audit" in commands
    assert "bandit" in commands and "check_bandit.py" in commands
    assert "trivy" in dumped and "codeql" in dumped.lower()
    assert "generate_sbom.py" in commands


# --------------------------------------------------------------------------- #
# Secrets: scanning and pinning
# --------------------------------------------------------------------------- #


def test_gitleaks_job_uses_the_committed_configuration() -> None:
    document = load_workflow("security.yml")
    dumped = yaml.safe_dump(document)
    assert "gitleaks" in dumped
    assert "GITLEAKS_CONFIG: configs/gitleaks.toml" in dumped
    assert GITLEAKS.exists(), "the configuration the workflow points at must be committed"


def test_gitleaks_configuration_extends_the_defaults_and_scopes_its_allowlists() -> None:
    config = tomllib.loads(GITLEAKS.read_text(encoding="utf-8"))
    assert config["extend"]["useDefault"] is True, "provider rules must stay enabled"
    rules = {rule["id"] for rule in config["rules"]}
    assert rules, "the project's own secret shapes must be covered"
    assert any("allowlist" in rule for rule in config["rules"]), (
        "placeholders must be tolerated per rule, not by switching rules off"
    )
    allowlist = config["allowlist"]
    assert allowlist.get("paths"), "allowlists must be path-scoped"
    assert allowlist.get("regexes"), "allowlists must be value-scoped"
    assert allowlist["paths"] != ["."], "an allowlist that matches everything scans nothing"


def test_the_offline_scanner_and_the_makefile_agree() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "scripts/scan_secrets.sh" in makefile
    assert "pip-audit" in makefile and "bandit" in makefile
    assert "tests/security" in makefile, "the security suite must have a make target"


def test_pre_commit_mirrors_the_pinned_toolchain() -> None:
    config = yaml.safe_load(PRE_COMMIT.read_text(encoding="utf-8"))
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ruff_pin = next(
        item.split("==")[1]
        for item in project["project"]["optional-dependencies"]["dev"]
        if item.startswith("ruff==")
    )
    hooks = {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}
    assert "ruff" in hooks and "ruff-format" in hooks
    ruff_repo = next(repo for repo in config["repos"] if "ruff-pre-commit" in repo["repo"])
    assert ruff_repo["rev"] == f"v{ruff_pin}", "the hook and the pinned ruff must not drift"
    assert "gitleaks" in hooks
    assert "--config=configs/gitleaks.toml" in " ".join(hooks["gitleaks"].get("args", []))
    for hook in hooks.values():
        entry = str(hook.get("entry", ""))
        for match in re.finditer(r"scripts/[\w./-]+", entry):
            assert (REPO_ROOT / match.group(0)).exists(), f"{match.group(0)} is missing"


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


def test_the_lockfile_pins_the_full_resolution(pyproject: dict[str, Any]) -> None:
    lines = [
        line.strip()
        for line in LOCK.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, "a lockfile with no entries is not a lockfile"
    for line in lines:
        assert "==" in line, f"{line!r} is not an exact pin"
        assert not any(operator in line for operator in (">=", "<=", "~=", "!=", ">", "<")), line
    assert not any(line.startswith("-e") or "git+" in line for line in lines)


def test_every_direct_dependency_is_locked(pyproject: dict[str, Any]) -> None:
    locked = {
        line.split("==", 1)[0].strip().lower().replace("_", "-")
        for line in LOCK.read_text(encoding="utf-8").splitlines()
        if "==" in line
    }
    declared = [
        *pyproject["project"]["dependencies"],
        *pyproject["project"]["optional-dependencies"]["dev"],
    ]
    for requirement in declared:
        name = re.split(r"[<>=!\[; ]", requirement, maxsplit=1)[0].strip().lower().replace("_", "-")
        assert name in locked, f"{name} is declared but not locked"


def test_direct_dependencies_are_exactly_pinned_in_pyproject(pyproject: dict[str, Any]) -> None:
    declared = [
        *pyproject["project"]["dependencies"],
        *pyproject["project"]["optional-dependencies"]["dev"],
    ]
    unpinned = [item for item in declared if "==" not in item]
    assert not unpinned, f"unpinned direct dependencies: {unpinned}"


def test_check_pins_script_agrees_with_the_repository() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/home/user/.venv/bin/python", str(REPO_ROOT / "scripts" / "check_pins.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_dependabot_covers_every_ecosystem_without_automerge() -> None:
    config = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    ecosystems = {entry["package-ecosystem"] for entry in config["updates"]}
    assert {"pip", "docker", "github-actions"} <= ecosystems
    dumped = yaml.safe_dump(config)
    assert "automerge" not in dumped, "a dependency change is a code change: a human merges it"
    for entry in config["updates"]:
        assert entry.get("commit-message", {}).get("prefix") in {"build", "ci"}, entry
