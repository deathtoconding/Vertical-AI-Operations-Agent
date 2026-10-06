"""The committed secret rules are tested against the repository they guard (SEC-005).

`configs/gitleaks.toml` is what turns a leaked credential into a red pipeline — and also what
turns a shell *reference* such as ``${DEPLOY_TOKEN:?set me}`` into one, which is how the first
v1.0.0 CI run failed: a rule that reported an assignment it was told to tolerate.

gitleaks itself runs in CI over the whole git history, which this suite cannot reproduce offline.
What it can do — and does here — is re-implement the subset of gitleaks' semantics that the
project's own rules rely on and assert the two properties a ruleset must have:

1. **it detects** — every custom rule still fires on a literal credential, with the credential
   (not the variable name) extracted as the secret;
2. **it stays quiet** — no custom rule reports anything in the tracked tree outside the
   allowlisted paths, references and placeholders included.

The evaluator mirrors ``detect/detect.go`` (keyword gate -> match -> ``gitleaks:allow`` ->
capture group / ``secretGroup`` -> entropy -> global allowlist -> rule allowlist) and
``config/allowlist.go`` (regexes against the secret, the match or the line; stopwords against the
secret). ``commits`` and ``targetRules`` allowlists are deliberately *not* modelled: a test below
fails if the project starts using them, so the evaluator can never silently under-approximate the
ruleset it is validating.
"""

from __future__ import annotations

import json
import math
import pathlib
import re
import subprocess
import tomllib
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
GITLEAKS = REPO_ROOT / "configs" / "gitleaks.toml"

pytestmark = [pytest.mark.story("SEC-005"), pytest.mark.unit]

# Paths the repository never scans for secrets (fixtures, example files, generated output).
SKIPPED_TREES = (
    ".git",
    ".venv",
    ".pgdata",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "artifacts",
    "node_modules",
    "__pycache__",
    "evals/results",
)


def _config() -> dict[str, Any]:
    return tomllib.loads(GITLEAKS.read_text(encoding="utf-8"))


def _rules() -> list[dict[str, Any]]:
    return list(_config()["rules"])


def _allowlists(rule: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if rule.get("allowlist"):
        entries.append(rule["allowlist"])
    entries.extend(rule.get("allowlists", []) or [])
    return entries


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {char: value.count(char) for char in set(value)}
    return -sum((n / len(value)) * math.log2(n / len(value)) for n in counts.values())


def _finding_allowed(rule: dict[str, Any], secret: str, match: str, line: str, path: str) -> bool:
    """`config/allowlist.go` — an OR condition checks regexes and stopwords only."""
    for allow in _allowlists(rule):
        target_by_name = {"secret": secret, "match": match, "line": line}
        target = target_by_name[allow.get("regexTarget", "secret")]
        regex_allowed = any(re.search(pattern, target) for pattern in allow.get("regexes", []))
        stopword = next(
            (word for word in allow.get("stopwords", []) if word.lower() in secret.lower()), None
        )
        if str(allow.get("condition", "or")).lower() == "and":
            checks: list[bool] = []
            if allow.get("paths"):
                checks.append(any(re.search(pattern, path) for pattern in allow["paths"]))
            if allow.get("regexes"):
                checks.append(regex_allowed)
            if allow.get("stopwords"):
                checks.append(stopword is not None)
            if checks and all(checks):
                return True
        elif regex_allowed or stopword is not None:
            return True
    return False


def findings(rule: dict[str, Any], path: str, content: str) -> list[tuple[int, str]]:
    """Findings gitleaks would report for one rule over one file (see the module docstring)."""
    keywords = [keyword.lower() for keyword in rule.get("keywords", [])]
    if keywords and not any(keyword in content.lower() for keyword in keywords):
        return []
    pattern = re.compile(rule["regex"], re.DOTALL)
    reported: list[tuple[int, str]] = []
    for match in pattern.finditer(content):
        line_start = content.rfind("\n", 0, match.start()) + 1
        line_end = content.find("\n", match.end())
        line = content[line_start : line_end if line_end != -1 else len(content)]
        if "gitleaks:allow" in line:
            continue
        secret = match.group(0)
        inner = pattern.search(secret)
        if inner is not None and inner.groups():
            group = rule.get("secretGroup", 0)
            if group:
                if len(inner.groups()) < group:
                    continue
                secret = inner.group(group)
            else:
                secret = next((value for value in inner.groups() if value), secret)
        if rule.get("entropy") and _shannon_entropy(secret) <= float(rule["entropy"]):
            continue
        if _finding_allowed(rule, secret, match.group(0), line, path):
            continue
        reported.append((content.count("\n", 0, match.start()) + 1, secret))
    return reported


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],  # noqa: S607 - git is the documented interface of a checkout
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    paths = []
    for path in result.stdout.split():
        if path.startswith(SKIPPED_TREES):
            continue
        if any(re.search(pattern, path) for pattern in _config()["allowlist"]["paths"]):
            continue
        paths.append(path)
    return paths


# --------------------------------------------------------------------------- #
# The rules detect credentials
# --------------------------------------------------------------------------- #


def credential_shaped(*parts: str) -> str:
    """Assemble a credential-shaped sample without embedding one in the source.

    The samples below must look like credentials to the rules under test and must not look like
    one to the scanners that guard this repository — the same convention, for the same reason, as
    ``tests/unit/core/test_logging.py``: ``scripts/scan_secrets.sh`` fails the build on a
    credential-shaped literal in the working tree, and GitHub push protection blocks the push.
    """
    return "".join(parts)


TOKEN_BINDING = credential_shaped("hK7Qz2W", "m9Rt4Yp6", "Nc8Ld0Bv3Ax5") + ":operator:"
JIRA_TOKEN = credential_shaped("ATATT3xFfGF0", "aBcDeFgHiJk", "LmNoPqRsTuVwXyZ012345")
LLM_KEY = credential_shaped("sk-proj-", "AbCdEfGhIj", "KlMnOpQrStUvWx")
DEPLOY_VALUE = credential_shaped("hK7Qz2W", "m9Rt4Yp6", "Nc8Ld0Bv3Ax5")
SLACK_WEBHOOK = credential_shaped(
    "https://hooks.slack.com/services/", "T00000000/", "B00000000/", "AbCdEfGhIjKlMnOpQrStUvWx"
)

LITERAL_CREDENTIALS = [
    ("aiops-api-token-registry", f'AIOPS_API_TOKENS="{TOKEN_BINDING}:alice"', TOKEN_BINDING),
    ("aiops-jira-api-token", f"AIOPS_JIRA_API_TOKEN={JIRA_TOKEN}", JIRA_TOKEN),
    ("aiops-llm-api-key", f"AIOPS_LLM_API_KEY={LLM_KEY}", LLM_KEY),
    ("aiops-deploy-credential", f"DEPLOY_TOKEN={DEPLOY_VALUE}", DEPLOY_VALUE),
    ("aiops-slack-webhook", SLACK_WEBHOOK, SLACK_WEBHOOK),
]


def test_every_custom_rule_still_fires_on_a_literal_credential() -> None:
    """A rule that matches nothing is decoration — and must extract the *value* as the secret."""
    rules = {rule["id"]: rule for rule in _rules()}
    for rule_id, sample, expected in LITERAL_CREDENTIALS:
        rule = rules[rule_id]
        found = findings(rule, "scripts/example.sh", sample)
        assert found, f"{rule_id} no longer detects {sample!r}"
        assert [secret for _, secret in found] == [expected], (
            f"{rule_id} must report the credential, not the variable name: got {found!r}"
        )


# --------------------------------------------------------------------------- #
# The rules stay quiet on the repository's own shapes
# --------------------------------------------------------------------------- #

REFERENCES_AND_PLACEHOLDERS = [
    # The line that failed the first v1.0.0 CI run: a shell conditional expansion, not a value.
    '    : "${DEPLOY_TOKEN:?DEPLOY_TOKEN is required when DEPLOY_HOST is set}"',
    # Compose and workflow files pass credentials through from the secret store.
    "      AIOPS_API_TOKENS: ${AIOPS_API_TOKENS:?provide token:role:actor bindings}",
    "      AIOPS_DATABASE_URL: postgresql+psycopg://aiops:${POSTGRES_PASSWORD:?set it}@postgres",
    "      AIOPS_LLM_API_KEY: ${AIOPS_LLM_API_KEY:-}",
    # Documented placeholders and short non-secrets.
    "AIOPS_LLM_API_KEY=your-key-here-placeholder",
    "AIOPS_JIRA_API_TOKEN=placeholder-token-value",
    "DEPLOY_TOKEN=changeme-in-the-secret-store",
    "GRAFANA_PASSWORD=example-password-value",
    "          POSTGRES_PASSWORD: aiops",
]


def test_the_rules_do_not_fire_on_references_placeholders_or_short_values() -> None:
    rules = {rule["id"]: rule for rule in _rules()}
    for line in REFERENCES_AND_PLACEHOLDERS:
        for rule_id, rule in rules.items():
            found = findings(rule, "infra/compose/docker-compose.yml", line)
            assert not found, f"{rule_id} must not report the non-secret line {line!r}"


def test_the_tracked_tree_is_free_of_custom_rule_findings() -> None:
    """The end-to-end check: no custom rule reports anything outside the allowlisted paths.

    This is the offline mirror of the gitleaks job, and the check that would have caught the
    ``${DEPLOY_TOKEN:?...}`` false positive before it turned CI red.
    """
    findings_by_rule: list[str] = []
    for path in tracked_files():
        try:
            content = (REPO_ROOT / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for rule in _rules():
            for line_no, secret in findings(rule, path, content):
                findings_by_rule.append(f"{rule['id']} {path}:{line_no} ({secret[:12]}…)")
    assert not findings_by_rule, "custom secret rules report non-secrets:\n  " + "\n  ".join(
        findings_by_rule
    )


def test_the_evaluator_covers_every_feature_the_ruleset_uses() -> None:
    """If a rule grows a feature this evaluator does not model, the test must fail, not drift."""
    unmodelled = {"commits", "targetRules", "condition", "path", "skipReport"}
    for rule in _rules():
        assert not (unmodelled & set(rule)), (
            f"{rule['id']} uses a feature the offline evaluator does not model — teach the "
            "evaluator, then assert on it"
        )
        for allow in _allowlists(rule):
            assert not (unmodelled & set(allow)), (
                f"{rule['id']}'s allowlist uses {sorted(unmodelled & set(allow))}, which the "
                "offline evaluator does not model"
            )
            assert allow.get("regexes") or allow.get("stopwords") or allow.get("paths"), (
                f"{rule['id']}'s allowlist matches nothing"
            )


# --------------------------------------------------------------------------- #
# How a finding is reported
# --------------------------------------------------------------------------- #

SCRIPT = "report_gitleaks_findings"


def load_script(name: str) -> Any:
    """Import ``scripts/<name>.py`` by path, the way the other planning tests do."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sarif(rule_id: str, path: str, line: int, commit: str, snippet: str) -> pathlib.Path:
    document = {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "gitleaks"}},
                "results": [
                    {
                        "ruleId": rule_id,
                        "message": {"text": "redacted description"},
                        "partialFingerprints": {"commitSha": commit},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": path},
                                    "region": {"startLine": line, "snippet": {"text": snippet}},
                                }
                            }
                        ],
                    }
                ],
            }
        ],
    }
    report = pathlib.Path("/tmp/aiops-test-results.sarif")
    report.write_text(json.dumps(document), encoding="utf-8")
    return report


def test_a_finding_is_annotated_with_its_location_and_fingerprint(tmp_path: pathlib.Path) -> None:
    """The annotation must be actionable (file, line, fingerprint) and must not leak the secret."""
    module = load_script(SCRIPT)
    report = sarif(
        "aiops-deploy-credential",
        "scripts/deploy,prod.sh",
        36,
        "abcdef1234567890fedcba",
        "DEPLOY_TOKEN=do-not-repeat-this-value",
    )
    result = module.results_of(json.loads(report.read_text(encoding="utf-8")))[0]
    annotation = module.annotate(result)

    assert annotation.startswith("::error file=scripts/deploy%2Cprod.sh,line=36::")
    assert "aiops-deploy-credential" in annotation
    assert "abcdef1234567890fedcba:scripts/deploy,prod.sh:aiops-deploy-credential:36" in annotation
    assert "do-not-repeat-this-value" not in annotation, (
        "the annotation must never echo a value the report only redacted"
    )


def test_the_reporter_only_fails_when_there_is_something_to_report() -> None:
    module = load_script(SCRIPT)
    with_findings = sarif("aiops-llm-api-key", "scripts/x.sh", 3, "0" * 40, "redacted")
    assert module.main([str(with_findings)]) == 1

    empty = pathlib.Path("/tmp/aiops-test-empty.sarif")
    empty.write_text(json.dumps({"version": "2.1.0", "runs": [{"results": []}]}), encoding="utf-8")
    assert module.main([str(empty)]) == 0

    # A run whose scan never produced a report must not turn a scanner failure into a *reporting*
    # failure: the scanner step owns that verdict.
    assert module.main(["/tmp/aiops-test-missing.sarif"]) == 0
