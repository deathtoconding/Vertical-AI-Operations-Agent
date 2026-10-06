"""Prompt loading (SRE-002 / OPS-041).

Prompts are versioned artefacts on disk, not strings embedded in code:

* a prompt change shows up as a reviewable diff;
* the version is recorded on every diagnosis, so an eval regression can be attributed to a
  specific prompt revision;
* a missing prompt file is a startup-time (not runtime) failure.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

PROMPTS_ROOT = Path(__file__).resolve().parents[2] / "prompts"

REQUIRED_PROMPTS = ("investigation", "planning")


class PromptNotFound(FileNotFoundError):
    """Raised when a required prompt artefact is missing from the deployment."""


@cache
def load_prompt(name: str) -> str:
    """Load ``prompts/<name>/system.md``.

    Raises:
        PromptNotFound: when the artefact is missing — better to fail loudly than to run with
            a silently truncated instruction set.
    """
    path = PROMPTS_ROOT / name / "system.md"
    if not path.exists():
        raise PromptNotFound(f"prompt artefact missing: {path}")
    return path.read_text(encoding="utf-8")


def prompt_version(name: str = "investigation") -> str:
    """Version = first 12 hex characters of the prompt's content hash.

    Derived rather than declared, so it cannot be forgotten or lie about which text ran.
    """
    import hashlib

    return hashlib.sha256(load_prompt(name).encode()).hexdigest()[:12]


def available_prompts() -> list[str]:
    return sorted(path.parent.name for path in PROMPTS_ROOT.glob("*/system.md") if path.parent.name)


__all__ = [
    "PROMPTS_ROOT",
    "REQUIRED_PROMPTS",
    "PromptNotFound",
    "available_prompts",
    "load_prompt",
    "prompt_version",
]
