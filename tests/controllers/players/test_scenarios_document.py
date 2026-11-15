"""Keep the scenarios document of the players controller in step with the scenario tests."""

from __future__ import annotations

import ast
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
DOC = REPO / "music_assistant/controllers/players/SCENARIOS.md"
SUITE_PATH = "tests/scenarios"
SUITE = REPO / SUITE_PATH
# test files in the suite that test the harness rather than a scenario
NOT_SCENARIOS = frozenset({"test_harness.py"})

HEADING = re.compile(r"^### (.*)$", re.MULTILINE)
ENTRY = re.compile(r"([A-Z]+-[1-9]\d*) · \S.*")
TEST = re.compile(r"`((?:tests/[\w/]+/)?test_\w+\.py)::(test_\w+)`")
END_TO_END = re.compile(r"^- End-to-end: \*\*(yes|possible with|no)\*\*(.*)$", re.MULTILINE)


def test_the_scenarios_document_names_every_scenario_test_once() -> None:
    """
    SCENARIOS.md names every scenario test in one entry, and only tests that exist.

    A scenario test is a test function at the top level of a test file in tests/scenarios (the
    harness tests excepted). Each entry of the document has a heading with a unique id, an
    end-to-end line that says what a run needs or why it can not be one, and the tests that
    prove it, as `file::test` within tests/scenarios or with the full path of a test
    elsewhere; no test is named by two entries, however it is written. On a failure, the
    message lists every entry or test that is out of step and what to do about it.
    """
    problems = _problems(DOC.read_text(), SUITE)
    lines = "\n  ".join(problems)
    assert not problems, f"{DOC.name} is out of step with the scenario tests:\n  {lines}"


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("malformed_heading", "is no entry heading"),
        ("id_zero", "is no entry heading"),
        ("external_test_named_twice", "keep it in the one entry"),
        ("suite_test_named_by_its_full_path_too", "keep it in the one entry"),
        ("bare_possible_with", "says 'possible with' and not with what"),
        ("no_without_a_reason", "says 'no' without a reason"),
        ("test_class", "is a test class"),
        ("unnamed_test", "is named by no entry"),
    ],
)
def test_the_check_catches_a_document_out_of_step(tmp_path: Path, case: str, message: str) -> None:
    """
    The check fails, and says what to do, for each way the document can get out of step.

    :param case: How the document or the suite gets out of step.
    :param message: What the check reports.
    """
    doc, suite = _out_of_step(case, DOC.read_text(), tmp_path)

    problems = _problems(doc, suite)

    assert any(message in problem for problem in problems), problems


def _problems(doc: str, suite: Path) -> list[str]:
    """
    Return every way the document is out of step with the scenario suite.

    :param doc: The text of the scenarios document.
    :param suite: The folder of the scenario suite.
    """
    parts = HEADING.split(doc)[1:]
    entries: list[tuple[str, str]] = []
    problems: list[str] = []
    for heading, body in zip(parts[::2], parts[1::2], strict=True):
        if (entry := ENTRY.fullmatch(heading)) is None:
            problems.append(
                f"'### {heading}' is no entry heading: write it as '### CHAPTER-n · the rule', "
                "with n from 1"
            )
            continue
        entries.append((entry.group(1), body))
    problems += [
        f"{entry_id} is the id of {count} entries: give all but one the next free number of "
        "their chapter"
        for entry_id, count in Counter(entry_id for entry_id, _ in entries).items()
        if count > 1
    ]
    # every test by its path in the repository, however an entry writes it
    named: dict[str, list[str]] = defaultdict(list)
    for entry_id, body in entries:
        problems += _end_to_end_problems(entry_id, body)
        if not (tests := TEST.findall(body)):
            problems.append(f"{entry_id} names no test: name the tests that prove it, or drop it")
        for file, name in tests:
            path = REPO / file if "/" in file else suite / file
            if name not in _test_functions(path):
                problems.append(
                    f"{entry_id} names {file}::{name}, which does not exist: name the test as "
                    "it is called now, or drop it"
                )
            named[f"{file if '/' in file else f'{SUITE_PATH}/{file}'}::{name}"].append(entry_id)
    problems += [
        f"{test} is named by {' and '.join(entry_ids)}: keep it in the one entry whose rule it "
        "proves"
        for test, entry_ids in named.items()
        if len(entry_ids) > 1
    ]
    for path in sorted(suite.glob("test_*.py")):
        if path.name in NOT_SCENARIOS:
            continue
        problems += [
            f"{path.name}::{name} is a test class: write its tests as functions at the top level "
            "of the file, as every scenario is"
            for name in sorted(_test_classes(path))
        ]
        problems += [
            f"{path.name}::{name} is named by no entry: add it to the entry whose rule it "
            "proves, or write a new entry"
            for name in sorted(_test_functions(path))
            if f"{SUITE_PATH}/{path.name}::{name}" not in named
        ]
    return problems


def _end_to_end_problems(entry_id: str, body: str) -> list[str]:
    """
    Return what is wrong with the end-to-end line of an entry.

    :param entry_id: The id of the entry.
    :param body: The text of the entry below its heading.
    """
    if (line := END_TO_END.search(body)) is None:
        return [
            f"{entry_id} has no end-to-end line: add '- End-to-end: **yes**', "
            "'**possible with** ...' or '**no**: ...'"
        ]
    mark, rest = line.groups()
    if mark == "possible with" and not re.match(r" \S", rest):
        return [f"{entry_id} says 'possible with' and not with what: name what a run needs"]
    if mark == "no" and not re.match(r": \S", rest):
        return [f"{entry_id} says 'no' without a reason: add ': ' and why a run can not be one"]
    return []


def _test_functions(path: Path) -> set[str]:
    """
    Return the names of the test functions at the top level of a test file.

    :param path: The test file; one that does not exist has none.
    """
    if not path.is_file():
        return set()
    return {
        node.name
        for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    }


def _test_classes(path: Path) -> set[str]:
    """
    Return the names of the classes at the top level of a test file that pytest collects.

    :param path: The test file.
    """
    return {
        node.name
        for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test")
    }


def _out_of_step(case: str, doc: str, tmp_path: Path) -> tuple[str, Path]:
    """
    Return the document and the scenario suite, put out of step in one way.

    :param case: The way, as named by test_the_check_catches_a_document_out_of_step.
    :param doc: The text of the document.
    :param tmp_path: A folder for a copy of the suite.
    """
    entry = _first(r"^### ([A-Z]+)-1 · ", doc)
    match case:
        case "malformed_heading":
            return doc.replace(entry.group(0), f"### {entry.group(1)}-two · ", 1), SUITE
        case "id_zero":
            new = f"### {entry.group(1)}-0 · A rule\n\n{entry.group(0)}"
            return doc.replace(entry.group(0), new, 1), SUITE
        case "external_test_named_twice":
            test = _first(r"`(tests/[\w/]+\.py::test_\w+)`", doc).group(1)
            return doc + _entry_naming(test), SUITE
        case "suite_test_named_by_its_full_path_too":
            test = _first(r"`(test_\w+\.py::test_\w+)`", doc).group(1)
            return doc + _entry_naming(f"{SUITE_PATH}/{test}"), SUITE
        case "bare_possible_with":
            line = _first(r"^- End-to-end: \*\*possible with\*\*.*$", doc).group(0)
            return doc.replace(line, "- End-to-end: **possible with**", 1), SUITE
        case "no_without_a_reason":
            line = _first(r"^- End-to-end: \*\*no\*\*.*$", doc).group(0)
            return doc.replace(line, "- End-to-end: **no**.", 1), SUITE
    suite = tmp_path / "scenarios"
    shutil.copytree(SUITE, suite)
    new_test = (
        "class TestNewScenario:\n    def test_it(self) -> None:\n        pass\n"
        if case == "test_class"
        else "async def test_new_scenario() -> None:\n    pass\n"
    )
    (suite / "test_new.py").write_text(new_test)
    return doc, suite


def _first(pattern: str, text: str) -> re.Match[str]:
    """
    Return the first match of a pattern in the document, which a case of the check relies on.

    :param pattern: The pattern, matched per line.
    :param text: The text of the document.
    """
    match = re.search(pattern, text, re.MULTILINE)
    assert match is not None, f"the document has nothing that matches {pattern!r} any more"
    return match


def _entry_naming(test: str) -> str:
    """
    Return an entry, to add at the end of the document, that names one test.

    :param test: The test as an entry names it.
    """
    return f"\n### OTHER-1 · A rule\n\n- End-to-end: **no**: a reason.\n- Proven by: `{test}`\n"
