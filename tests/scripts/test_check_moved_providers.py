"""Tests for the check that a provider leaves the server only for the community catalog."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from music_assistant.controllers.community_catalog import index
from scripts import check_moved_providers as check
from scripts.check_moved_providers import (
    MOVED_FILE,
    LookupFailedError,
    NotFoundError,
    lines_for,
    offline_problems,
    online_problems,
    parse_moved,
)

BASE_SHA = "0" * 40
TAGS = ["2.10.12", "2.11.0b8", "2.11.0b9", "2.11.0.dev2026111503", "2.9.4"]
REMOVED = [("removed", "music_assistant/providers/old_music/manifest.json")]


class FakeCatalog:
    """Answers the requests of the check like the community catalog and the server repository."""

    def __init__(self, indexes: dict[str, dict[str, object]], moving: list[str]) -> None:
        """
        Initialize the answers.

        :param indexes: The providers of each index by server line; a missing line answers 404.
        :param moving: The domains the registry lists as moving.
        """
        self.indexes = indexes
        self.moving = moving
        self.base_source = "MOVED_PROVIDER_DOMAINS = frozenset()\n"
        self.urls: list[str] = []

    def fetch(self, url: str) -> bytes:
        """Answer a request."""
        self.urls.append(url)
        if url == check.RESERVED_URL:
            return json.dumps({"moving": self.moving}).encode()
        if url == check.SOURCE_URL.format(sha=BASE_SHA, path=MOVED_FILE):
            return self.base_source.encode()
        line = url.rsplit("/", 1)[-1].removesuffix(".json")
        if line not in self.indexes:
            raise NotFoundError(url)
        return json.dumps({"providers": self.indexes[line]}).encode()


def _listed(version: str | None = "1.0.0", status: str = "listed") -> dict[str, object]:
    """Return an index entry."""
    return {"status": status, "latest": version}


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> FakeCatalog:
    """Return a catalog where old_music is offered on every line dev reaches."""
    fake = FakeCatalog(
        {line: {"old_music": _listed()} for line in ("nightly", "beta", "2.11")}, ["old_music"]
    )
    monkeypatch.setattr(check, "_fetch", fake.fetch)
    monkeypatch.setattr(check, "_server_tags", lambda: TAGS)
    return fake


def test_parse_moved() -> None:
    """The moved list is read from the source, also when it is empty or annotated."""
    assert parse_moved("MOVED_PROVIDER_DOMAINS: Final[frozenset[str]] = frozenset()\n") == set()
    assert parse_moved('MOVED_PROVIDER_DOMAINS = frozenset({"a_one", "b_two"})\n') == {
        "a_one",
        "b_two",
    }
    assert parse_moved(Path(MOVED_FILE).read_text(encoding="utf-8")) is not None
    with pytest.raises(ValueError, match="is not a frozenset of domains"):
        parse_moved("MOVED_PROVIDER_DOMAINS = set()\n")


def test_a_moved_provider_must_not_keep_its_folder(tmp_path: Path) -> None:
    """A domain in the moved list whose folder still exists is reported."""
    (tmp_path / MOVED_FILE).parent.mkdir(parents=True)
    (tmp_path / MOVED_FILE).write_text(
        'MOVED_PROVIDER_DOMAINS = frozenset({"old_music", "gone"})\n'
    )
    (tmp_path / "music_assistant/providers/old_music").mkdir(parents=True)

    assert offline_problems(tmp_path) == [
        "old_music: is in MOVED_PROVIDER_DOMAINS but music_assistant/providers/old_music still exists"
    ]


def test_the_server_itself_passes_offline() -> None:
    """No moved provider of this repository keeps its folder."""
    assert offline_problems() == []


def test_nothing_is_asked_without_a_removal_or_a_change_of_the_list(catalog: FakeCatalog) -> None:
    """An ordinary pull request does not reach the network."""
    changes = [
        ("modified", "music_assistant/providers/old_music/__init__.py"),
        ("modified", "music_assistant/providers/old_music/manifest.json"),
    ]

    assert online_problems(changes, "dev", BASE_SHA, frozenset()) == []
    assert catalog.urls == []


def test_a_move_the_catalog_offers_everywhere_passes(catalog: FakeCatalog) -> None:
    """A removed provider in the moved list passes when every line offers a passed version."""
    changes = [*REMOVED, ("modified", MOVED_FILE)]

    assert online_problems(changes, "dev", BASE_SHA, frozenset({"old_music"})) == []
    assert [url.rsplit("/", 1)[-1] for url in catalog.urls[1:]] == [
        "nightly.json",
        "beta.json",
        "2.11.json",
    ]


@pytest.mark.parametrize(
    ("line", "entry", "problem"),
    [
        ("beta", None, "old_music: the community catalog index of beta offers no version"),
        ("nightly", _listed(None), "old_music: the community catalog index of nightly offers no"),
        (
            "2.11",
            _listed(status="blocked"),
            "old_music: the community catalog index of 2.11 offers",
        ),
    ],
)
def test_a_move_a_line_does_not_offer_fails(
    catalog: FakeCatalog, line: str, entry: dict[str, object] | None, problem: str
) -> None:
    """A line without the provider, without a passed version or with it blocked fails."""
    if entry is None:
        del catalog.indexes[line]["old_music"]
    else:
        catalog.indexes[line]["old_music"] = entry

    [found] = online_problems(REMOVED, "dev", BASE_SHA, frozenset({"old_music"}))

    assert found.startswith(problem)


def test_a_line_without_an_index_fails(catalog: FakeCatalog) -> None:
    """The coming stable line needs an index of its own before a provider moves."""
    del catalog.indexes["2.11"]

    assert online_problems(REMOVED, "dev", BASE_SHA, frozenset({"old_music"})) == [
        "the community catalog has no index for the server line 2.11 yet"
    ]


def test_a_domain_added_to_the_list_is_checked(catalog: FakeCatalog) -> None:
    """Adding a domain to the moved list is checked like removing its folder."""
    catalog.indexes["beta"] = {}

    problems = online_problems(
        [("modified", MOVED_FILE)], "dev", BASE_SHA, frozenset({"old_music"})
    )

    assert problems == [
        "old_music: the community catalog index of beta offers no version that passed"
    ]


def test_a_domain_already_moved_is_not_checked_again(catalog: FakeCatalog) -> None:
    """Only the domains a pull request adds to the moved list are checked."""
    catalog.base_source = 'MOVED_PROVIDER_DOMAINS = frozenset({"old_music"})\n'
    catalog.indexes["beta"] = {}

    changes = [("modified", MOVED_FILE)]

    assert online_problems(changes, "dev", BASE_SHA, frozenset({"old_music"})) == []


@pytest.mark.usefixtures("catalog")
def test_a_removal_the_catalog_expects_to_move_needs_the_list() -> None:
    """A removed provider the registry lists as moving must be in the moved list."""
    assert online_problems(REMOVED, "dev", BASE_SHA, frozenset()) == [
        "old_music: the community catalog takes it over; add it to MOVED_PROVIDER_DOMAINS"
    ]


def test_a_provider_that_is_dropped_passes(catalog: FakeCatalog) -> None:
    """A provider removed for good, which the registry does not expect, needs no index."""
    catalog.moving = []

    assert online_problems(REMOVED, "dev", BASE_SHA, frozenset()) == []
    assert catalog.urls == [check.RESERVED_URL]


@pytest.mark.usefixtures("catalog")
def test_the_lines_a_branch_reaches() -> None:
    """Dev reaches nightly, beta and the line of the newest beta; stable its newest line."""
    assert lines_for("dev") == ["nightly", "beta", "2.11"]
    assert lines_for("stable") == ["2.10"]


def test_a_failed_lookup_fails_the_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the community catalog does not answer, the check fails rather than passes."""

    def unreachable(url: str) -> bytes:
        raise LookupFailedError(url)

    monkeypatch.setattr(check, "_fetch", unreachable)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO("removed\tmusic_assistant/providers/x/manifest.json\n")
    )

    assert check.main(["--base-branch", "dev", "--base-sha", BASE_SHA]) == 1
    assert "::error::could not ask the community catalog" in capsys.readouterr().out


def test_the_index_is_the_one_the_server_reads() -> None:
    """The check asks the same index as the server."""
    assert check.INDEX_URL == index.INDEX_URL
