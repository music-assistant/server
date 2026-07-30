"""Tests for the pull request title and description checks."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from scripts.check_pr_metadata import (
    REQUIRED_CHECKLIST_ITEMS,
    check_description,
    check_pull_request,
    check_title,
    checklist_items,
    main,
    template_headings,
)

TEMPLATE = """# What does this implement/fix?

<!-- Quick description and explanation of changes. -->

**Related issue (if applicable):**

- related issue <link to issue>

## Types of changes

<!-- Tick exactly one box. -->

- [ ] Bugfix (non-breaking change which fixes an issue) — `bugfix`
- [ ] CI / workflow change — `ci`

## Checklist

- [ ] The code change is tested and works locally.
- [ ] `pre-commit run --all-files` passes.
- [ ] `pytest` passes, and tests have been added/updated under `tests/` where applicable.
- [ ] For changes to shared models, the companion PR in `music-assistant/models` is linked.
- [ ] For changes affecting the UI, the companion PR in `music-assistant/frontend` is linked.
- [ ] I have read and complied with the project's [AI Policy](https://example.org) for any \
AI-assisted contributions.
- [ ] I have raised a PR against the documentation repository targeting the main or beta branch \
as appropriate.
"""

COMPLETE_BODY = """# What does this implement/fix?

Fixes a thing that was broken.

**Related issue (if applicable):**

- n/a

## Types of changes

- [x] Bugfix (non-breaking change which fixes an issue) — `bugfix`
- [ ] CI / workflow change — `ci`

## Checklist

- [x] The code change is tested and works locally.
- [x] `pre-commit run --all-files` passes.
- [x] `pytest` passes, and tests have been added/updated under `tests/` where applicable.
- [ ] For changes to shared models, the companion PR in `music-assistant/models` is linked.
- [ ] For changes affecting the UI, the companion PR in `music-assistant/frontend` is linked.
- [x] I have read and complied with the project's [AI Policy](https://example.org) for any \
AI-assisted contributions.
- [ ] I have raised a PR against the documentation repository targeting the main or beta branch \
as appropriate.
"""


def test_template_headings() -> None:
    """Every heading level is collected, in template order."""
    assert template_headings(TEMPLATE) == [
        "# What does this implement/fix?",
        "## Types of changes",
        "## Checklist",
    ]


def test_checklist_items_stop_at_the_checklist_section() -> None:
    """Only the boxes under "## Checklist" count; the "Types of changes" boxes are excluded."""
    items = checklist_items(TEMPLATE)
    assert len(items) == 7
    assert items[0] == "The code change is tested and works locally."
    assert not any("Bugfix" in item for item in items)


def test_complete_body_passes() -> None:
    """A filled-in template with all required boxes ticked reports no problems."""
    assert check_description(COMPLETE_BODY, TEMPLATE) == []


def test_prose_body_reports_every_missing_heading() -> None:
    """A body that replaced the template is reported per missing section."""
    problems = check_description("Just some prose about the change.\n", TEMPLATE)
    report = "\n".join(problems)
    assert "# What does this implement/fix?" in report
    assert "## Types of changes" in report
    assert "## Checklist" in report


def test_single_missing_heading_is_reported() -> None:
    """Dropping one section fails even when the rest of the template is intact."""
    body = COMPLETE_BODY.replace("## Types of changes", "## Kinds of changes")
    problems = check_description(body, TEMPLATE)
    assert len(problems) == 1
    assert "## Types of changes" in problems[0]


def test_heading_matching_ignores_case_and_spacing() -> None:
    """Headings match on normalised text, so casing and extra spacing are accepted."""
    body = COMPLETE_BODY.replace("## Checklist", "##   CHECKLIST")
    assert check_description(body, TEMPLATE) == []


def test_extra_headings_are_allowed() -> None:
    """Authors may add their own sections."""
    assert check_description(f"{COMPLETE_BODY}\n## Changes\n\n- did a thing\n", TEMPLATE) == []


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ("The code change is tested and works locally.", "tested and works locally"),
        ("`pre-commit run --all-files` passes.", "pre-commit run --all-files"),
        ("`pytest` passes, and tests", "pytest` passes"),
        ("I have read and complied with the project's", "AI Policy"),
    ],
)
def test_each_required_item_must_be_ticked(item: str, expected: str) -> None:
    """Unticking any required checklist item fails, and the report names that item."""
    body = COMPLETE_BODY.replace(f"- [x] {item}", f"- [ ] {item}")
    assert body != COMPLETE_BODY
    problems = check_description(body, TEMPLATE)
    assert len(problems) == 1
    assert expected.replace("`", "") in problems[0].replace("`", "")


def test_conditional_items_may_stay_unticked() -> None:
    """The models/frontend/docs items are conditional and are never required."""
    assert check_description(COMPLETE_BODY, TEMPLATE) == []
    assert "companion PR" not in "".join(REQUIRED_CHECKLIST_ITEMS)
    assert "documentation repository" not in "".join(REQUIRED_CHECKLIST_ITEMS)


def test_uppercase_tick_is_accepted() -> None:
    """GitHub renders "[X]" as ticked, so the check accepts it too."""
    assert check_description(COMPLETE_BODY.replace("- [x]", "- [X]"), TEMPLATE) == []


def test_asterisk_bullets_are_accepted() -> None:
    """Markdown allows "*" bullets for task list items."""
    assert check_description(COMPLETE_BODY.replace("- [x]", "* [x]"), TEMPLATE) == []


def test_ticked_item_inside_a_comment_does_not_count() -> None:
    """HTML comments are stripped before ticks are read, so a commented box is still missing."""
    body = COMPLETE_BODY.replace(
        "- [x] `pre-commit run --all-files` passes.",
        "<!-- - [x] `pre-commit run --all-files` passes. -->",
    )
    problems = check_description(body, TEMPLATE)
    assert len(problems) == 1
    assert "pre-commit" in problems[0]


def test_empty_body_is_reported() -> None:
    """A missing description is reported as a single, clear problem."""
    assert check_description("", TEMPLATE) == ["The pull request description is empty."]
    assert check_description("   \n\n", TEMPLATE) == ["The pull request description is empty."]


def test_required_items_exist_in_the_real_template() -> None:
    """
    Guards against template drift.

    Each required item must match exactly one checklist item in the repository's own template,
    so renaming an item in the template surfaces here instead of failing every pull request.
    """
    template = Path(".github/PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    items = checklist_items(template)
    assert items
    for required in REQUIRED_CHECKLIST_ITEMS:
        matched = [item for item in items if required.casefold() in item.casefold()]
        assert len(matched) == 1, f"{required!r} matched {len(matched)} template items"


@pytest.mark.parametrize(
    "title",
    [
        "Fix Sonos hanging after reconnect",
        "Apple Music: signing in a second account no longer breaks the first",
        'Revert "Play the track you selected when shuffle is on"',
        "Don't show the dashboard keepalive as active playback on cast players",
        "⬆️ Update music-assistant-models to 1.1.175",
        "Bump docker/login-action from 4.5.1 to 4.5.2",
        "Add tests for Open Subsonic provider",
    ],
)
def test_user_facing_titles_pass(title: str) -> None:
    """Titles that read as the change itself are accepted, colons and all."""
    assert check_title(title) == []


@pytest.mark.parametrize(
    ("title", "prefix"),
    [
        ("fix: sonos reconnect", "fix:"),
        ("Fix: Sonos reconnect", "Fix:"),
        ("FIX:uppercase no space", "FIX:"),
        ("feat(airplay): add thing", "feat(airplay):"),
        ("chore(deps): bump docker/login-action", "chore(deps):"),
        ("refactor!: drop legacy api", "refactor!:"),
        ("docs : spaced colon", "docs :"),
        ("tests: add coverage", "tests:"),
        ("deps: bump x", "deps:"),
    ],
)
def test_conventional_commit_titles_are_rejected(title: str, prefix: str) -> None:
    """The report names the offending prefix so the author knows what to drop."""
    problems = check_title(title)
    assert len(problems) == 1
    assert f"`{prefix}`" in problems[0]


def test_empty_title_is_reported() -> None:
    """A missing title is reported as a single, clear problem."""
    assert check_title("  ") == ["The pull request title is empty."]


def test_title_and_description_problems_are_reported_together() -> None:
    """
    Both checks feed one list.

    The workflow turns that list into a single comment, so a pull request with a bad title and a
    replaced description must not produce two separate reports.
    """
    problems = check_pull_request("fix: a thing", "just prose", TEMPLATE)
    assert "conventional-commit prefix" in problems[0]
    assert any("Missing template section" in problem for problem in problems)
    assert len(problems) == len(check_title("fix: a thing")) + len(
        check_description("just prose", TEMPLATE)
    )


def test_main_passes_on_a_good_title_and_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI exits 0 and stays quiet about problems when both are complete."""
    template_file = tmp_path / "PULL_REQUEST_TEMPLATE.md"
    template_file.write_text(TEMPLATE, encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(COMPLETE_BODY))

    argv = ["--template", str(template_file), "--title", "Fix Sonos hanging after reconnect"]
    assert main(argv) == 0
    assert "::error" not in capsys.readouterr().out


def test_main_reports_problems_as_markdown_and_annotations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI exits 1, prints one markdown report on stdout and an annotation on stderr."""
    template_file = tmp_path / "PULL_REQUEST_TEMPLATE.md"
    template_file.write_text(TEMPLATE, encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO("no template here"))

    argv = ["--template", str(template_file), "--title", "fix: sonos reconnect"]
    assert main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out.lstrip().startswith("The title or description")
    assert "`fix:`" in captured.out
    assert "## Types of changes" in captured.out
    assert "::error" in captured.err
