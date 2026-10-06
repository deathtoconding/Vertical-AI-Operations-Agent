#!/usr/bin/env python3
"""Generate a software bill of materials (SEC-005).

CycloneDX-shaped JSON, produced from the *installed* environment rather than from a
hand-maintained list — a SBOM that is not generated from what actually runs is a document about
somebody's intentions. Dependencies are keyed by the pinned versions in
``requirements.lock`` where available, so the artefact and the lock file cannot drift apart
silently.

Usage::

    python scripts/generate_sbom.py --output artifacts/sbom.json
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
#: Distribution metadata for inter-package dependencies maps module names to distributions;
#: these are the names we must translate when reading a lock file.
LOCK_FILE = REPO_ROOT / "requirements.lock"


def locked_versions(repo_root: Path = REPO_ROOT) -> dict[str, str]:
    """Parse ``requirements.lock`` (``name==version`` lines) into a mapping."""
    lock = repo_root / LOCK_FILE.name
    versions: dict[str, str] = {}
    if not lock.exists():
        return versions
    for line in lock.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or "==" not in entry:
            continue
        name, _, version = entry.partition("==")
        versions[name.strip().lower()] = version.strip()
    return versions


def build_sbom(project: str, version: str) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    for distribution in sorted(
        importlib.metadata.distributions(), key=lambda d: (d.metadata["Name"] or "").lower()
    ):
        name = distribution.metadata["Name"]
        if not name:
            continue
        resolved = distribution.version
        components.append(
            {
                "type": "library",
                "name": name,
                "version": resolved,
                "purl": f"pkg:pypi/{name.lower().replace('_', '-')}@{resolved}",
                "properties": [
                    {"name": "aiops:installed_version", "value": resolved},
                ],
            }
        )

    locked = locked_versions()
    for component in components:
        pinned = locked.get(component["name"].lower())
        component["properties"].append(
            {
                "name": "aiops:locked",
                "value": "true" if pinned else "false",
            }
        )
        if pinned:
            component["properties"].append({"name": "aiops:locked_version", "value": pinned})

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {"type": "application", "name": project, "version": version},
            "tools": [{"name": "scripts/generate_sbom.py"}],
        },
        "components": components,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "artifacts" / "sbom.json")
    parser.add_argument("--project", default="ai-operations-agent")
    parser.add_argument("--version", default="1.0.0")
    args = parser.parse_args(argv)

    document = build_sbom(args.project, args.version)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    components = len(document["components"])
    locked = sum(
        1
        for component in document["components"]
        if any(p == {"name": "aiops:locked", "value": "true"} for p in component["properties"])
    )
    print(f"wrote {args.output} ({components} components, {locked} present in requirements.lock)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
