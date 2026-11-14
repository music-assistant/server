"""Tests for the decision whether an installed community provider may load."""

from __future__ import annotations

import pytest

from music_assistant.controllers.community_catalog.gate import Decision, Verdict, evaluate
from music_assistant.controllers.community_catalog.models import (
    IndexProvider,
    IndexStatus,
    IndexVersion,
    InstalledProvider,
)
from tests.controllers.community_catalog.conftest import ADDED, RELEASED

VERSION = "1.0.0"
HASH = "aaaa"
INSTALLED = InstalledProvider(
    version=VERSION, repository="someone/repo", commit="0123abcd", content_hash=HASH
)
URL = "https://example.org/why"


def _entry(
    status: IndexStatus = IndexStatus.LISTED,
    *,
    versions: dict[str, str] | None = None,
    blocked_versions: dict[str, str] | None = None,
) -> IndexProvider:
    """
    Return a provider of the index.

    :param status: Whether the catalog offers the provider.
    :param versions: The content hash of each version; the installed one when omitted.
    :param blocked_versions: The reason of each blocked version.
    """
    versions = {VERSION: HASH} if versions is None else versions
    return IndexProvider(
        status=status,
        repository="someone/repo",
        manifest={},
        added=ADDED,
        reason="Sends the library elsewhere" if status != IndexStatus.LISTED else None,
        reason_url=URL,
        latest=next(iter(versions), None),
        versions={
            version: IndexVersion(
                commit="0123abcd",
                folder="foo",
                content_hash=content_hash,
                requirements=[],
                released=RELEASED,
            )
            for version, content_hash in versions.items()
        },
        blocked_versions=blocked_versions or {},
    )


@pytest.mark.parametrize(
    ("folder_hash", "entry", "has_index", "own_line", "expected"),
    [
        pytest.param(
            HASH, None, False, False, Decision(Verdict.CATALOG_UNREACHABLE), id="no kept copy"
        ),
        pytest.param(
            HASH,
            _entry(IndexStatus.BLOCKED),
            True,
            True,
            Decision(Verdict.BLOCKED, "Sends the library elsewhere", URL),
            id="blocked",
        ),
        pytest.param(
            HASH,
            _entry(blocked_versions={VERSION: "Leaks the password"}),
            True,
            True,
            Decision(Verdict.BLOCKED, "Leaks the password", URL),
            id="blocked version",
        ),
        pytest.param(
            HASH,
            _entry(IndexStatus.BLOCKED),
            True,
            False,
            Decision(Verdict.BLOCKED, "Sends the library elsewhere", URL),
            id="block in a copy of another line",
        ),
        pytest.param(HASH, _entry(), True, False, Decision(Verdict.LOAD), id="other line"),
        pytest.param(
            "bbbb",
            _entry(),
            True,
            False,
            Decision(Verdict.CATALOG_UNREACHABLE),
            id="other line, folder changed",
        ),
        pytest.param(HASH, None, True, True, Decision(Verdict.NOT_LISTED), id="not listed"),
        pytest.param(
            HASH,
            _entry(versions={"2.0.0": HASH}),
            True,
            True,
            Decision(Verdict.NOT_FOR_THIS_VERSION),
            id="version not listed",
        ),
        pytest.param(
            HASH,
            _entry(versions={VERSION: "cccc"}),
            True,
            True,
            Decision(Verdict.NOT_FOR_THIS_VERSION),
            id="version listed with other content",
        ),
        pytest.param(
            "bbbb", _entry(), True, True, Decision(Verdict.REINSTALL), id="folder changed"
        ),
        pytest.param(None, _entry(), True, True, Decision(Verdict.REINSTALL), id="no folder"),
        pytest.param(HASH, _entry(), True, True, Decision(Verdict.LOAD), id="listed"),
        pytest.param(
            HASH, _entry(IndexStatus.WITHDRAWN), True, True, Decision(Verdict.LOAD), id="withdrawn"
        ),
    ],
)
def test_evaluate(
    folder_hash: str | None,
    entry: IndexProvider | None,
    has_index: bool,
    own_line: bool,
    expected: Decision,
) -> None:
    """The first rule that matches decides."""
    decision = evaluate(INSTALLED, folder_hash, entry, has_index=has_index, own_line=own_line)

    assert decision == expected
