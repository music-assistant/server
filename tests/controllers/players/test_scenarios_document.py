"""Keep the scenarios document of the players controller in step with the scenario tests."""

from __future__ import annotations

import ast
import re
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).parents[3]
DOC = REPO / "music_assistant/controllers/players/SCENARIOS.md"
SUITE = REPO / "tests/scenarios"
# test files in the suite that test the harness rather than a scenario
NOT_SCENARIOS = frozenset({"test_harness.py"})

ENTRY = re.compile(r"^### ([A-Z]+-[1-9]\d*) · ", re.MULTILINE)
TEST = re.compile(r"`((?:tests/[\w/]+/)?test_\w+\.py)::(test_\w+)`")
END_TO_END = re.compile(r"^- End-to-end: \*\*(yes|possible with|no)\*\*", re.MULTILINE)


def test_the_scenarios_document_names_every_scenario_test_once() -> None:
    """
    SCENARIOS.md names every scenario test in one entry, and only tests that exist.

    A scenario test is a test function in tests/scenarios (the harness tests excepted). Each
    entry of the document has a unique id, an end-to-end line and the tests that prove it, as
    `file::test` within tests/scenarios or with the full path of a test elsewhere. On a failure,
    the message lists every entry or test that is out of step and what to do about it.
    """
    parts = ENTRY.split(DOC.read_text())[1:]
    entries = list(zip(parts[::2], parts[1::2], strict=True))
    problems = [
        f"{entry_id} is the id of {count} entries: give all but one the next free number of "
        "their chapter"
        for entry_id, count in Counter(entry_id for entry_id, _ in entries).items()
        if count > 1
    ]
    named: dict[str, list[str]] = defaultdict(list)
    for entry_id, body in entries:
        if not END_TO_END.search(body):
            problems.append(
                f"{entry_id} has no end-to-end line: add '- End-to-end: **yes**', "
                "'**possible with** ...' or '**no**: ...'"
            )
        if not (tests := TEST.findall(body)):
            problems.append(f"{entry_id} names no test: name the tests that prove it, or drop it")
        for file, name in tests:
            path = REPO / file if "/" in file else SUITE / file
            if name not in _test_functions(path):
                problems.append(
                    f"{entry_id} names {file}::{name}, which does not exist: name the test as "
                    "it is called now, or drop it"
                )
            named[f"{file}::{name}"].append(entry_id)
    for path in sorted(SUITE.glob("test_*.py")):
        if path.name in NOT_SCENARIOS:
            continue
        for name in sorted(_test_functions(path)):
            test = f"{path.name}::{name}"
            if not (entry_ids := named.get(test)):
                problems.append(
                    f"{test} is named by no entry: add it to the entry whose rule it proves, "
                    "or write a new entry"
                )
            elif len(entry_ids) > 1:
                problems.append(
                    f"{test} is named by {' and '.join(entry_ids)}: keep it in the one entry "
                    "whose rule it proves"
                )
    lines = "\n  ".join(problems)
    assert not problems, f"{DOC.name} is out of step with the scenario tests:\n  {lines}"


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
