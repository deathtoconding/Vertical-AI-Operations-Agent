"""Repository hygiene: the layout the code imports must be the layout git ships.

Story: OPS-070 (release validation). This module exists because of a defect that no unit test could
have caught: ``.gitignore`` contained an unanchored ``logs/`` pattern, intended for runtime log
output at the repository root. It also matched ``app/integrations/logs/`` — a source package the
application imports and the backlog declares as an artefact of OPS-023. The module therefore
existed in the working tree and was absent from every clone and container build, where it fails at
import time (and ``mypy`` reports it as a missing module rather than as a missing file).

The lesson is mechanical: an ignore rule is part of the build contract, because anything git does
not carry does not ship. These tests check the rules rather than trusting review.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.story("OPS-070"), pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Directory names that may legitimately be ignored at any depth: they are caches, virtualenvs or
#: build output for a language runtime, and no first-party source package is ever named this.
SAFE_UNANCHORED_DIRECTORIES = frozenset(
    {
        "__pycache__",
        ".eggs",
        ".idea",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        ".vscode",
        "node_modules",
        "venv",
    }
)

#: Top-level source trees: a file here is imported, tested or executed, so git must carry it.
SOURCE_TREES = ("app", "tests", "evals", "scripts")


def _ignore_patterns() -> list[str]:
    return [
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and not line.lstrip().startswith("!")
    ]


def test_no_unanchored_directory_pattern_can_swallow_a_source_package() -> None:
    """``logs/`` matches ``app/integrations/logs/`` too — runtime paths must be anchored.

    A pattern that only ends in ``/`` (no leading ``/``, no earlier directory segment) matches that
    directory name at *every* depth. That is correct for ``__pycache__/`` and wrong for anything a
    package could be called, which is why the runtime list is kept short and explicit.
    """
    offenders: list[str] = []
    for pattern in _ignore_patterns():
        if not pattern.endswith("/"):
            continue
        name = pattern.rstrip("/")
        if "/" in name:  # anchored or a path — it cannot match an arbitrary depth
            continue
        if any(char in name for char in "*?["):
            # A glob matches a *shape* of name (``*.egg-info/``), not an arbitrary one, so it
            # cannot swallow a package that happens to be called ``logs``. The empirical check in
            # the next test covers the globs anyway: git is asked about the real tree.
            continue
        if name in SAFE_UNANCHORED_DIRECTORIES:
            continue
        offenders.append(pattern)

    assert not offenders, (
        "these .gitignore entries match a directory at any depth and would ignore a source "
        f"package with the same name: {offenders}. Anchor them with a leading slash "
        "(e.g. `/logs/`) if they are runtime output."
    )


def test_the_ignored_paths_do_not_include_source_trees() -> None:
    """Belt and braces: ask git itself what it would ignore under the source trees.

    ``git check-ignore`` is the authority — it applies the real rules, including nested
    ``.gitignore`` files — so this catches patterns that the structural check above does not
    anticipate.
    """
    git = shutil.which("git")
    if git is None or not (REPO_ROOT / ".git").exists():  # pragma: no cover - CI has both
        pytest.skip("git is unavailable, so ignored paths cannot be queried")

    candidates = [
        path
        for tree in SOURCE_TREES
        for path in (REPO_ROOT / tree).rglob("*.py")
        if "__pycache__" not in path.parts
    ]
    assert candidates, "no Python sources found under the source trees"

    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, repo-local paths
        [git, "check-ignore", "--stdin"],
        cwd=REPO_ROOT,
        input="\n".join(str(path.relative_to(REPO_ROOT)) for path in candidates),
        capture_output=True,
        text=True,
        check=False,
    )

    ignored = [line for line in result.stdout.splitlines() if line.strip()]
    assert not ignored, f"git would not carry these source files: {ignored}"


def test_the_documented_log_provider_module_is_importable() -> None:
    """The artefact OPS-023 declares must exist at the path the documentation names.

    ``docs/planning/domain.md`` and the backlog both point at
    ``app/integrations/logs/provider.py``; a missing file there breaks the import in production and
    makes the delivery-completeness gate fail. This is the specific regression, pinned.
    """
    module = REPO_ROOT / "app" / "integrations" / "logs" / "provider.py"
    assert module.exists(), f"{module} is missing: the log store provider is not shipped"

    from app.integrations.logs.provider import HttpLogsProvider, SandboxLogsProvider

    assert HttpLogsProvider.__name__ == "HttpLogsProvider"
    assert SandboxLogsProvider.__name__ == "SandboxLogsProvider"


#: Documented provider modules (``docs/planning/domain.md``, backlog OPS-020…OPS-023). Each system
#: gets a stable module so callers import a system name rather than a transport detail.
DOCUMENTED_PROVIDER_MODULES = (
    "app.integrations.github.client",
    "app.integrations.jira.client",
    "app.integrations.slack.client",
    "app.integrations.metrics.provider",
    "app.integrations.logs.provider",
    "app.integrations.payments.client",
)


def test_every_integration_provider_is_reachable_from_its_documented_path() -> None:
    """A documented provider path that exists but does not import is the same defect as a missing
    file, one step later — so each one is imported, not merely stat-ed."""
    import importlib
    import importlib.util

    missing = [
        name for name in DOCUMENTED_PROVIDER_MODULES if importlib.util.find_spec(name) is None
    ]
    assert not missing, f"documented integration modules are not importable: {missing}"

    for name in DOCUMENTED_PROVIDER_MODULES:
        module = importlib.import_module(name)
        assert module is not None
