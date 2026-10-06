"""CI report annotators: test failures from JUnit XML, scanner findings from SARIF (DEV-002).

Story: DEV-002 (CI pipeline + gates). The pipeline is only as trustworthy as the reports it
leaves behind: a step that fails with a bare exit code sends a reader hunting through logs, and
the only thing worse than no signal is a signal nobody can act on. Both reporters turn a machine
written report — pytest's JUnit XML, a scanner's SARIF — into annotations on the check run,
which is where a reviewer is already looking.
"""

from __future__ import annotations

import importlib.util
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.story("DEV-002")

REPO_ROOT = Path(__file__).resolve().parents[3]


def load_script(name: str) -> Any:
    """Import ``scripts/<name>.py`` by path, the way the other planning tests do."""
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def junit(path: Path, *, failures: list[dict[str, str]], skipped: int = 0, passed: int = 0) -> Path:
    """A JUnit report shaped like pytest's: one suite, plain test cases."""
    suite = ElementTree.Element(
        "testsuite", {"name": "pytest", "tests": str(passed + len(failures) + skipped)}
    )
    for index in range(passed):
        ElementTree.SubElement(
            suite, "testcase", {"classname": "tests.unit.test_ok", "name": f"ok{index}"}
        )
    for failure in failures:
        case = ElementTree.SubElement(
            suite,
            "testcase",
            {"classname": failure["classname"], "name": failure["name"]},
        )
        node = ElementTree.SubElement(
            case, failure.get("kind", "failure"), {"message": failure["message"]}
        )
        node.text = failure.get("traceback", "")
    for index in range(skipped):
        case = ElementTree.SubElement(
            suite, "testcase", {"classname": "tests.unit.test_skip", "name": f"skip{index}"}
        )
        ElementTree.SubElement(case, "skipped")
    document = ElementTree.ElementTree(suite)
    document.write(path, encoding="utf-8", xml_declaration=True)
    return path


def test_a_failing_test_is_annotated_with_its_location_and_message(tmp_path: Path) -> None:
    module = load_script("report_test_failures")
    report = junit(
        tmp_path / "junit-planning.xml",
        failures=[
            {
                "classname": "tests.unit.planning.test_backlog",
                "name": "test_the_board_is_sprinted",
                "message": "assert 23 == 24",
                "traceback": (
                    "____________________ test_the_board_is_sprinted ____________________\n\n"
                    "    def test_the_board_is_sprinted() -> None:\n"
                    ">       assert points == 24\n"
                    "E       assert 23 == 24\n\n"
                    "tests/unit/planning/test_backlog.py:118: AssertionError\n"
                ),
            }
        ],
    )

    assert module.main([str(report), "--label", "ci"]) == 1
    root = ElementTree.parse(report).getroot()  # noqa: S314 - a fixture this test just wrote
    annotation = module.annotate(module.failures_in(root, "x")[0], "ci")

    assert annotation.startswith(
        "::error file=tests/unit/planning/test_backlog.py,line=118,"
        "title=test_the_board_is_sprinted::"
    )
    assert "assert 23 == 24" in annotation
    assert "ci: test_the_board_is_sprinted" in annotation
    # A path that has to be escaped in a *property*: a comma would end `file=`, and a `%` would
    # start an escape sequence. Both come from pytest's own output often enough to matter.
    escaped = module.annotate(
        module.Failure(
            test="test_odd",
            path="tests/unit/planning/test_odd,comma.py",
            line=7,
            message="100% of cases failed",
        ),
        "ci",
    )
    assert "file=tests/unit/planning/test_odd%2Ccomma.py" in escaped
    assert "100%25 of cases failed" in escaped


def test_the_report_is_silent_when_the_suite_is_green(tmp_path: Path) -> None:
    """No findings means no annotations, and — crucially — a zero exit code."""
    module = load_script("report_test_failures")
    report = junit(tmp_path / "junit-unit.xml", failures=[], skipped=2, passed=5)
    assert module.main([str(report), "--label", "ci"]) == 0
    # A missing report is not a failure: the scanner/test step owns that verdict, and a
    # reporting step that reds the job would hide the real cause.
    assert module.main([str(tmp_path / "absent.xml")]) == 0


def test_a_collection_error_is_annotated_even_without_a_traceback(tmp_path: Path) -> None:
    """pytest reports import failures as ``<error>``; the classname still names the file."""
    module = load_script("report_test_failures")
    report = junit(
        tmp_path / "junit-integration.xml",
        failures=[
            {
                "classname": "tests.integration.test_api",
                "name": "tests.integration.test_api",
                "message": "ImportError: cannot import name 'ledger'",
                "kind": "error",
            }
        ],
    )
    root = ElementTree.parse(report).getroot()  # noqa: S314 - our own fixture
    failure = module.failures_in(root, "junit-integration.xml")[0]

    assert failure.path == "tests/integration/test_api.py"
    assert failure.line is None
    assert "ledger" in failure.message
    assert module.main([str(report)]) == 1
