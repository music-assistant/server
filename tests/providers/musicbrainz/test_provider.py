"""Tests for the MusicBrainz provider."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.providers.musicbrainz.provider import MusicbrainzProvider

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _provider(response: Any) -> tuple[MusicbrainzProvider, AsyncMock]:
    """Return a MusicbrainzProvider whose API client answers with the given response."""
    with patch.object(MusicbrainzProvider, "__init__", lambda *_a, **_kw: None):
        provider = MusicbrainzProvider.__new__(MusicbrainzProvider)
    get_data = AsyncMock(return_value=response)
    api_client = MagicMock()
    api_client.get_data = get_data
    provider._api_client = api_client
    return provider, get_data


def _recordings(*first_release_dates: str | None) -> dict[str, Any]:
    """Return an isrc lookup response with a recording per given release date."""
    return {
        "isrc": "GBAYE8600477",
        "recordings": [
            {"id": f"stub-{i}", "title": "stub"}
            if date is None
            else {"id": f"stub-{i}", "title": "stub", "first-release-date": date}
            for i, date in enumerate(first_release_dates)
        ],
    }


# ---------------------------------------------------------------------------
# get_release_year_by_isrc
# ---------------------------------------------------------------------------


async def test_release_year_parses_all_date_precisions() -> None:
    """Accept a year, year-month or full date as the first release date."""
    for release_date in ("1986", "1986-06", "1986-06-23"):
        provider, _ = _provider(_recordings(release_date))
        assert await provider.get_release_year_by_isrc("GBAYE8600477") == 1986


async def test_release_year_uses_bare_isrc_lookup() -> None:
    """Look the recording up on the isrc resource without inc parameters."""
    provider, get_data = _provider(_recordings("1986"))

    await provider.get_release_year_by_isrc("GB-AYE-86-00477")

    get_data.assert_awaited_once_with("isrc/GBAYE8600477")


async def test_release_year_returns_earliest_of_multiple_recordings() -> None:
    """Date the song by the oldest recording the ISRC covers."""
    provider, _ = _provider(_recordings("2009-05-01", "1986-06", "1994"))
    assert await provider.get_release_year_by_isrc("GBAYE8600477") == 1986


async def test_release_year_is_none_without_a_usable_date() -> None:
    """Return None when MusicBrainz has no parseable first release date."""
    for response in (
        None,
        {"isrc": "GBAYE8600477"},
        {"isrc": "GBAYE8600477", "recordings": []},
        _recordings(None),
        _recordings("????-06"),
    ):
        provider, _ = _provider(response)
        assert await provider.get_release_year_by_isrc("GBAYE8600477") is None


async def test_release_year_rejects_a_malformed_isrc() -> None:
    """Never put an ISRC that cannot be part of a URL path in the request."""
    provider, get_data = _provider(_recordings("1986"))

    assert await provider.get_release_year_by_isrc("../artist/1") is None
    get_data.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_recordings_by_isrc
# ---------------------------------------------------------------------------


_YELLOW_SUBMARINE = {
    "isrc": "GBAYE0601498",
    "recordings": [
        {
            "id": "b2181aae-5cba-496c-bb0c-b4cc0109ebf8",
            "title": "Yellow Submarine",
            "length": 160000,
            "first-release-date": "1966-08-05",
            "disambiguation": "original stereo studio mix",
            "video": False,
        }
    ],
}


async def test_recordings_by_isrc_parses_a_realistic_payload() -> None:
    """Parse id, title and first-release-date from a real isrc lookup response."""
    provider, _ = _provider(_YELLOW_SUBMARINE)

    recordings = await provider.get_recordings_by_isrc("GBAYE0601498")

    assert len(recordings) == 1
    recording = recordings[0]
    assert recording.id == "b2181aae-5cba-496c-bb0c-b4cc0109ebf8"
    assert recording.title == "Yellow Submarine"
    assert recording.first_release_date == "1966-08-05"


async def test_recordings_by_isrc_returns_all_recordings() -> None:
    """Return every recording an ISRC covers, not just the first."""
    provider, _ = _provider(_recordings("2009-05-01", "1986-06", "1994"))

    recordings = await provider.get_recordings_by_isrc("GBAYE8600477")

    assert len(recordings) == 3
    assert [r.first_release_date for r in recordings] == ["2009-05-01", "1986-06", "1994"]


async def test_recordings_by_isrc_skips_a_malformed_entry() -> None:
    """Skip a recording missing a required field while keeping its valid siblings."""
    response = {
        "isrc": "GBAYE8600477",
        "recordings": [
            {"title": "no id here"},
            {"id": "good-1", "title": "stub", "first-release-date": "1986"},
        ],
    }
    provider, _ = _provider(response)

    recordings = await provider.get_recordings_by_isrc("GBAYE8600477")

    assert len(recordings) == 1
    assert recordings[0].id == "good-1"


async def test_recordings_by_isrc_is_empty_without_usable_data() -> None:
    """Return an empty list for every shape of "MusicBrainz has nothing" response."""
    for response in (
        None,
        {"isrc": "GBAYE8600477"},
        {"isrc": "GBAYE8600477", "recordings": []},
    ):
        provider, _ = _provider(response)
        assert await provider.get_recordings_by_isrc("GBAYE8600477") == []


async def test_recordings_by_isrc_rejects_a_malformed_isrc() -> None:
    """Never put an ISRC that cannot be part of a URL path in the request."""
    provider, get_data = _provider(_YELLOW_SUBMARINE)

    assert await provider.get_recordings_by_isrc("../artist/1") == []
    get_data.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_release_year_by_track_name
# ---------------------------------------------------------------------------


def _release(
    date: str,
    *,
    title: str = "A Night at the Opera",
    primary_type: str = "Album",
    secondary_types: list[str] | None = None,
    status: str = "Official",
) -> dict[str, Any]:
    """Return one release of a searched recording."""
    release: dict[str, Any] = {
        "id": f"release-{date}",
        "title": title,
        "date": date,
        "status": status,
        "release-group": {
            "id": f"rg-{title}-{primary_type}",
            "title": title,
            "primary-type": primary_type,
        },
    }
    if secondary_types:
        release["release-group"]["secondary-types"] = secondary_types
    return release


def _search_result(*recordings: dict[str, Any]) -> dict[str, Any]:
    """Return a recording search response holding the given recordings."""
    return {"count": len(recordings), "recordings": list(recordings)}


def _recording(
    *releases: dict[str, Any],
    title: str = "Bohemian Rhapsody",
    artist: str = "Queen",
) -> dict[str, Any]:
    """Return one searched recording credited to the given artist."""
    return {
        "id": f"recording-{title}-{releases[0]['date'] if releases else 'none'}",
        "title": title,
        "artist-credit": [{"artist": {"id": "artist-1", "name": artist, "sort-name": artist}}],
        "releases": list(releases),
    }


async def test_release_year_by_track_name_returns_the_earliest_studio_release() -> None:
    """Date a song by the oldest studio album any matching recording appeared on."""
    provider, get_data = _provider(
        _search_result(
            _recording(_release("2011-05-16", title="The Platinum Collection")),
            _recording(_release("1992-08-25", title="Classic Queen")),
            _recording(_release("1975-11-21")),
        )
    )

    assert await provider.get_release_year_by_track_name("Queen", "Bohemian Rhapsody") == 1975
    get_data.assert_awaited_once_with(
        "recording",
        query='"Bohemian Rhapsody" AND artist:"Queen"',
        limit="100",
    )


async def test_release_year_by_track_name_ignores_untrustworthy_releases() -> None:
    """Never date a song by a compilation, a live album, a bootleg or an unrelated single."""
    provider, _ = _provider(
        _search_result(
            _recording(
                # every untrusted release predates the studio album, so each filter has to
                # hold on its own for the studio year to win
                _release("1968-10-26", title="Greatest Hits", secondary_types=["Compilation"]),
                _release("1969-06-22", title="Live Killers", secondary_types=["Live"]),
                _release("1970-01-01", title="Some Other Song", primary_type="Single"),
                _release("1971-03-01", title="Bootleg Tape", status="Bootleg"),
                _release("1972-05-05", title="A Tribute", primary_type="Other"),
                _release("1975-11-21"),
            )
        )
    )

    assert await provider.get_release_year_by_track_name("Queen", "Bohemian Rhapsody") == 1975


async def test_release_year_by_track_name_accepts_a_single_named_after_the_song() -> None:
    """Date a song by its own single when no studio album carries it."""
    provider, _ = _provider(
        _search_result(
            _recording(_release("1975-10-31", title="Bohemian Rhapsody", primary_type="Single"))
        )
    )

    assert await provider.get_release_year_by_track_name("Queen", "Bohemian Rhapsody") == 1975


async def test_release_year_by_track_name_is_none_without_a_confident_match() -> None:
    """Return no year at all rather than guessing from a name that does not match."""
    for response in (
        None,
        {"count": 0, "recordings": []},
        _search_result(_recording(_release("1975-11-21"), artist="Not Queen")),
        _search_result(_recording(_release("1975-11-21"), title="Another Song")),
        _search_result(_recording()),
        _search_result(_recording(_release(""))),
    ):
        provider, _ = _provider(response)
        assert await provider.get_release_year_by_track_name("Queen", "Bohemian Rhapsody") is None


async def test_release_group_by_track_name_returns_the_artist_and_oldest_groups_first() -> None:
    """Hand out the matched artist and their release groups, oldest release first."""
    provider, _ = _provider(
        _search_result(
            _recording(_release("2011-05-16", title="The Platinum Collection")),
            _recording(_release("1975-11-21")),
        )
    )

    result = await provider.get_release_group_by_track_name("Queen", "Bohemian Rhapsody")

    assert result is not None
    artist, release_groups = result
    assert artist.name == "Queen"
    assert [group.title for group in release_groups] == [
        "A Night at the Opera",
        "The Platinum Collection",
    ]


async def test_release_group_by_track_name_returns_the_artist_without_release_groups() -> None:
    """Still identify the artist when no matched recording carries a usable release group."""
    provider, _ = _provider(
        _search_result(_recording(_release("1981-10-26", secondary_types=["Compilation"])))
    )

    result = await provider.get_release_group_by_track_name("Queen", "Bohemian Rhapsody")

    assert result is not None
    artist, release_groups = result
    assert artist.name == "Queen"
    assert release_groups == []


async def test_release_year_by_track_name_escapes_lucene_specials() -> None:
    """Escape characters that would otherwise change the meaning of the search query."""
    provider, get_data = _provider(_search_result())

    await provider.get_release_year_by_track_name("AC/DC", "T.N.T. (live!)")

    get_data.assert_awaited_once_with(
        "recording",
        query='"T.N.T. \\(live\\!\\)" AND artist:"AC\\/DC"',
        limit="100",
    )
