"""
CI check: verify a pull request title and description are fit to be release-noted.

Release notes are generated verbatim from pull request titles, so a conventional-commit subject
has to be rewritten by hand at release time. The template is not paperwork either: its sections
carry the change description reviewers read, the "Types of changes" box the release-notes label
is derived from, and a checklist recording the author's own verification. A body that replaced
the template (by hand or by an AI agent) drops all of that silently.

Both are reported together, as one list, so the author gets a single message.

Reads the body from stdin and the title from ``--title``.

Usage:
    gh pr view 1234 --json title,body --jq .body |
        uv run -m scripts.check_pr_metadata --title "$(gh pr view 1234 --json title --jq .title)"
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ruff: noqa: T201

DEFAULT_TEMPLATE = ".github/PULL_REQUEST_TEMPLATE.md"

# Checklist items every pull request must tick. The remaining template items are conditional
# (companion model/frontend pull requests, documentation) and can never be required. Each entry
# must match exactly one template item — tests/scripts/test_check_pr_metadata.py asserts that,
# so renaming an item in the template surfaces there instead of failing every pull request.
REQUIRED_CHECKLIST_ITEMS = (
    "The code change is tested and works locally",
    "`pre-commit run --all-files` passes",
    "`pytest` passes",
    "AI Policy",
)

CONVENTIONAL_TYPES = (
    "build",
    "chore",
    "ci",
    "deps",
    "docs",
    "feat",
    "feature",
    "fix",
    "perf",
    "refactor",
    "revert",
    "style",
    "test",
    "tests",
)
# A conventional-commit subject: an optional scope and "!" between the type and the colon.
CONVENTIONAL_PREFIX_RE = re.compile(
    rf"^\s*(?:{'|'.join(CONVENTIONAL_TYPES)})(\([^)]*\))?!?\s*:",
    re.IGNORECASE,
)

HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(?P<text>.+?)\s*#*\s*$")
TASK_ITEM_RE = re.compile(r"^\s*[-*]\s*\[(?P<tick>[ xX])\]\s*(?P<text>.*?)\s*$")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def template_headings(template: str) -> list[str]:
    """Return the template's heading lines, in template order."""
    return [line.strip() for line in template.splitlines() if HEADING_RE.match(line)]


def checklist_items(template: str) -> list[str]:
    """Return the task list item texts under the template's "Checklist" heading."""
    items: list[str] = []
    in_checklist = False
    for line in _strip_comments(template).splitlines():
        if heading := HEADING_RE.match(line):
            in_checklist = _normalize(heading["text"]) == "checklist"
            continue
        if in_checklist and (item := TASK_ITEM_RE.match(line)):
            items.append(item["text"])
    return items


def check_title(title: str) -> list[str]:
    """Return the problems found in a pull request title, empty when it passes."""
    if not title.strip():
        return ["The pull request title is empty."]
    if not (prefix := CONVENTIONAL_PREFIX_RE.match(title)):
        return []
    return [
        f"Title starts with the conventional-commit prefix `{prefix.group().strip()}` — write the "
        'title as the user-facing change instead, for example "Fix Sonos hanging after reconnect".'
    ]


def check_description(body: str, template: str) -> list[str]:
    """
    Return the template problems found in a pull request body, empty when it passes.

    :param body: The pull request description.
    :param template: Contents of the pull request template to check against.
    """
    if not body.strip():
        return ["The pull request description is empty."]

    problems = [
        f"Missing template section: {heading}"
        for heading in template_headings(template)
        if not _has_heading(body, heading)
    ]
    items = checklist_items(template)
    problems.extend(
        f"Checklist item not ticked: {_label(required, items)}"
        for required in REQUIRED_CHECKLIST_ITEMS
        if not _is_ticked(body, required)
    )
    return problems


def check_pull_request(title: str, body: str, template: str) -> list[str]:
    """
    Return every title and description problem as one list, empty when the pull request passes.

    :param title: The pull request title.
    :param body: The pull request description.
    :param template: Contents of the pull request template to check against.
    """
    return check_title(title) + check_description(body, template)


def main(argv: list[str] | None = None) -> int:
    """Check the title and the body on stdin, and return the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default="", help="The pull request title.")
    parser.add_argument(
        "--template", type=Path, default=Path(DEFAULT_TEMPLATE), help="Template to check against."
    )
    args = parser.parse_args(argv)
    template = args.template.read_text(encoding="utf-8")

    if not (problems := check_pull_request(args.title, sys.stdin.read(), template)):
        print("The title and description are good to go.")
        return 0

    print("The title or description of this pull request needs a fix:\n")
    for problem in problems:
        print(f"- {problem}")
    print(
        "\nRelease notes are generated from the title, and the template carries what reviewers "
        "need, so please edit them before this is reviewed."
    )
    summary = "; ".join(problems)
    print(
        f"::error title=Pull request title or description needs a fix::{summary}", file=sys.stderr
    )
    return 1


def _normalize(text: str) -> str:
    """Return text stripped of backticks and casing differences for tolerant matching."""
    return re.sub(r"\s+", " ", text.replace("`", "")).strip().casefold()


def _strip_comments(text: str) -> str:
    """Return text without HTML comments, so commented-out lines never count as present."""
    return HTML_COMMENT_RE.sub("", text)


def _has_heading(body: str, heading: str) -> bool:
    """Return whether the body carries the given template heading, at any heading level."""
    match = HEADING_RE.match(heading)
    wanted = _normalize(match["text"] if match else heading)
    return any(
        _normalize(match["text"]) == wanted
        for line in _strip_comments(body).splitlines()
        if (match := HEADING_RE.match(line))
    )


def _is_ticked(body: str, required: str) -> bool:
    """Return whether the body's task list ticks the item matching a required text."""
    wanted = _normalize(required)
    return any(
        item["tick"] in "xX" and wanted in _normalize(item["text"])
        for line in _strip_comments(body).splitlines()
        if (item := TASK_ITEM_RE.match(line))
    )


def _label(required: str, items: list[str]) -> str:
    """Return the template's own wording for a required item, falling back to its key text."""
    wanted = _normalize(required)
    return next((item for item in items if wanted in _normalize(item)), required)


if __name__ == "__main__":
    sys.exit(main())
