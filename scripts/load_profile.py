#!/usr/bin/env python3
"""Load a deployment profile and print shell ``export`` statements (DEV-001).

Profiles under ``configs/`` hold **non-secret** defaults for an environment (log format,
integration mode, proxy trust, timeouts). Secrets are never read from a file in the repository —
they arrive as environment variables at run time (AI coding rule 6), which is why
:func:`load_profile` refuses to emit anything that looks like a credential.

Usage::

    eval "$(python scripts/load_profile.py --file configs/production.yaml)"
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Keys whose presence in a profile is a mistake, not a configuration.
SECRET_HINTS = re.compile(r"(token|secret|password|api[_-]?key|credential|webhook)", re.IGNORECASE)


class ProfileError(ValueError):
    """Raised when a deployment profile is missing, malformed, or contains a secret."""


def load_profile(path: Path) -> dict[str, str]:
    """Return ``AIOPS_*`` environment variables declared by a profile file."""
    target = path if path.is_absolute() else REPO_ROOT / path
    if not target.exists():
        raise ProfileError(f"profile not found: {target}")
    raw: Any = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ProfileError(f"{target} must contain a mapping at the top level")

    values: dict[str, Any] = raw.get("environment", raw)
    if not isinstance(values, dict):
        raise ProfileError(f"{target}: 'environment' must be a mapping")

    environment: dict[str, str] = {}
    for key, value in values.items():
        name = str(key)
        if SECRET_HINTS.search(name):
            raise ProfileError(
                f"{target}: '{name}' looks like a secret — credentials belong in the environment, "
                "never in a committed profile"
            )
        env_name = name if name.startswith("AIOPS_") else f"AIOPS_{name.upper()}"
        environment[env_name] = _render(value)
    return environment


def _render(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return ",".join(_render(item) for item in value)
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True, help="profile file to load")
    parser.add_argument("--format", choices=["shell", "json"], default="shell")
    args = parser.parse_args(argv)

    try:
        environment = load_profile(args.file)
    except ProfileError as exc:
        print(f"PROFILE INVALID: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        json.dump(environment, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    for name, value in sorted(environment.items()):
        sys.stdout.write(f"export {name}={shlex.quote(value)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
