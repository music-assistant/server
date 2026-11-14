"""Tests for the decision whether an installed community provider may load."""

from __future__ import annotations

from dataclasses import replace

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
NEWER = "2.0.0"


def _entry(
    status: IndexStatus = IndexStatus.LISTED,
    *,
    versions: dict[str, str] | None = None,
    blocked_versions: dict[str, str] | None = None,
    latest: str | None = None,
) -> IndexProvider:
    """
    Return a provider of the index.

    :param status: Whether the catalog offers the provider.
    :param versions: The content hash of each version; the installed one when omitted.
    :param blocked_versions: The reason of each blocked version.
    :param latest: The latest version; the first of the versions when omitted.
    """
    versions = {VERSION: HASH} if versions is None else versions
    return IndexProvider(
        status=status,
        repository="someone/repo",
        manifest={},
        added=ADDED,
        reason="Sends the library elsewhere" if status != IndexStatus.LISTED else None,
        reason_url=URL,
        latest=latest or next(iter(versions), None),
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
    # the installed version is VERSION, never failed
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
            _entry(
                versions={VERSION: HASH, NEWER: "bbbb"},
                latest=NEWER,
                blocked_versions={VERSION: "Leaks the password"},
            ),
            True,
            True,
            Decision(Verdict.BLOCKED, "Leaks the password", URL, NEWER),
            id="blocked version, latest on offer",
        ),
        pytest.param(
            HASH,
            _entry(
                versions={VERSION: HASH, NEWER: "bbbb"},
                latest=NEWER,
                blocked_versions={VERSION: "Leaks the password"},
            ),
            True,
            False,
            Decision(Verdict.BLOCKED, "Leaks the password", URL),
            id="blocked version in a copy of another line",
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
            _entry(versions={NEWER: HASH}),
            True,
            True,
            Decision(Verdict.SWITCH, version=NEWER),
            id="version not listed",
        ),
        pytest.param(
            HASH,
            _entry(versions={VERSION: "cccc"}),
            True,
            True,
            Decision(Verdict.SWITCH, version=VERSION),
            id="version listed with other content",
        ),
        pytest.param(
            HASH,
            _entry(versions={NEWER: HASH}, latest="3.0.0"),
            True,
            True,
            Decision(Verdict.NOT_FOR_THIS_VERSION),
            id="version not listed, latest without a version record",
        ),
        pytest.param(
            HASH,
            _entry(IndexStatus.WITHDRAWN, versions={NEWER: HASH}),
            True,
            True,
            Decision(Verdict.NOT_FOR_THIS_VERSION),
            id="version not listed, withdrawn",
        ),
        pytest.param(
            HASH,
            _entry(versions={NEWER: HASH}, blocked_versions={NEWER: "Leaks the password"}),
            True,
            True,
            Decision(Verdict.NOT_FOR_THIS_VERSION),
            id="version not listed, latest blocked",
        ),
        pytest.param(
            "bbbb", _entry(), True, True, Decision(Verdict.REINSTALL), id="folder changed"
        ),
        pytest.param(None, _entry(), True, True, Decision(Verdict.REINSTALL), id="no folder"),
        pytest.param(HASH, _entry(), True, True, Decision(Verdict.LOAD), id="listed"),
        pytest.param(
            HASH,
            _entry(versions={VERSION: HASH, NEWER: "bbbb"}, latest=NEWER),
            True,
            True,
            Decision(Verdict.LOAD, version=NEWER),
            id="update on offer",
        ),
        pytest.param(
            "bbbb",
            _entry(versions={VERSION: HASH, NEWER: "bbbb"}, latest=NEWER),
            True,
            True,
            Decision(Verdict.REINSTALL),
            id="update on offer, folder changed",
        ),
        pytest.param(
            HASH,
            _entry(
                versions={VERSION: HASH, NEWER: "bbbb"},
                latest=NEWER,
                blocked_versions={NEWER: "Leaks the password"},
            ),
            True,
            True,
            Decision(Verdict.LOAD),
            id="update to a blocked version",
        ),
        pytest.param(
            HASH,
            _entry(IndexStatus.WITHDRAWN, versions={VERSION: HASH, NEWER: "bbbb"}, latest=NEWER),
            True,
            True,
            Decision(Verdict.LOAD),
            id="update of a withdrawn provider",
        ),
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


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        pytest.param(
            _entry(versions={NEWER: HASH}),
            Decision(Verdict.NOT_FOR_THIS_VERSION),
            id="version not listed, latest failed",
        ),
        pytest.param(
            _entry(versions={VERSION: HASH, NEWER: "bbbb"}, latest=NEWER),
            Decision(Verdict.LOAD),
            id="latest failed",
        ),
        pytest.param(
            _entry(
                versions={VERSION: HASH, NEWER: "bbbb"},
                latest=NEWER,
                blocked_versions={VERSION: "Leaks the password"},
            ),
            Decision(Verdict.BLOCKED, "Leaks the password", URL),
            id="blocked version, latest failed",
        ),
        pytest.param(
            _entry(versions={VERSION: HASH, NEWER: "bbbb", "3.0.0": "cccc"}, latest="3.0.0"),
            Decision(Verdict.LOAD, version="3.0.0"),
            id="newer than the failed version",
        ),
    ],
)
def test_evaluate_failed_version(entry: IndexProvider, expected: Decision) -> None:
    """A version the server went back from is neither switched to nor offered."""
    installed = replace(INSTALLED, failed_version=NEWER)

    assert evaluate(installed, HASH, entry, has_index=True, own_line=True) == expected
