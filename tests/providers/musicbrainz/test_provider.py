"""Tests for the MusicBrainz provider."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.errors import RateLimited

from music_assistant.constants import VARIOUS_ARTISTS_MBID
from music_assistant.providers.musicbrainz.provider import MusicbrainzProvider

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _provider(
    response: Any, release_group_response: Any = None
) -> tuple[MusicbrainzProvider, AsyncMock]:
    """
    Return a MusicbrainzProvider whose API client answers with the given response.

    :param response: Answer to every request but the release group lookup.
    :param release_group_response: Answer to the release group lookup.
    """
    with patch.object(MusicbrainzProvider, "__init__", lambda *_a, **_kw: None):
        provider = MusicbrainzProvider.__new__(MusicbrainzProvider)

    async def _answer(endpoint: str, **_kwargs: Any) -> Any:
        return release_group_response if endpoint == "release-group" else response

    get_data = AsyncMock(side_effect=_answer)
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


def _credit(name: str, artist_id: str) -> dict[str, Any]:
    """Return one artist credit of a release."""
    return {"name": name, "artist": {"id": artist_id, "name": name, "sort-name": name}}


def _release(
    date: str,
    *,
    title: str = "A Night at the Opera",
    primary_type: str = "Album",
    secondary_types: list[str] | None = None,
    status: str = "Official",
    credit: dict[str, Any] | None = None,
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
    if credit is not None:
        release["artist-credit"] = [credit]
    return release


def _search_result(*recordings: dict[str, Any]) -> dict[str, Any]:
    """Return a recording search response holding the given recordings."""
    return {"count": len(recordings), "recordings": list(recordings)}


def _recording(
    *releases: dict[str, Any],
    title: str = "Bohemian Rhapsody",
    artist: str = "Queen",
    artist_id: str = "artist-1",
    first_release: str | None = None,
) -> dict[str, Any]:
    """Return one searched recording credited to the given artist."""
    recording: dict[str, Any] = {
        "id": f"recording-{title}-{releases[0]['date'] if releases else 'none'}",
        "title": title,
        "artist-credit": [{"artist": {"id": artist_id, "name": artist, "sort-name": artist}}],
        "releases": list(releases),
    }
    if first_release is not None:
        recording["first-release-date"] = first_release
    return recording


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
    assert get_data.await_args_list[0].args == ("recording",)
    assert get_data.await_args_list[0].kwargs == {
        "query": '"Bohemian Rhapsody" AND artist:"Queen"',
        "limit": "100",
    }


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


async def test_release_year_by_track_name_ignores_a_various_artists_release() -> None:
    """Never date a song by a hits compilation that carries no Compilation type."""
    provider, _ = _provider(
        _search_result(
            _recording(
                # the compilation predates the studio album, so the credit filter has to
                # hold on its own for the studio year to win
                _release(
                    "1968-10-26",
                    title="Hits of the 60s",
                    credit=_credit("Various Artists", VARIOUS_ARTISTS_MBID),
                ),
                _release("1975-11-21", credit=_credit("Queen", "artist-1")),
            )
        )
    )

    assert await provider.get_release_year_by_track_name("Queen", "Bohemian Rhapsody") == 1975


async def test_release_year_by_track_name_identifies_various_artists_by_id() -> None:
    """
    Recognise the Various Artists entity by its id rather than by its name.

    MusicBrainz localizes the name it credits that entity under, and unrelated artists
    are named after it.
    """
    provider, _ = _provider(
        _search_result(
            _recording(
                _release(
                    "1968-10-26",
                    title="Artisti Vari Compilation",
                    credit=_credit("Artisti Vari", VARIOUS_ARTISTS_MBID),
                ),
                _release(
                    "1975-11-21",
                    credit=_credit("Various Artist", "artist-named-like-various"),
                ),
            )
        )
    )

    assert await provider.get_release_year_by_track_name("Queen", "Bohemian Rhapsody") == 1975


async def test_release_group_by_track_name_drops_a_various_artists_release() -> None:
    """Offer no artwork candidate when a song is only listed on a hits compilation."""
    provider, _ = _provider(
        _search_result(
            _recording(
                _release(
                    "1981-10-26",
                    title="Hits of the 80s",
                    credit=_credit("Various Artists", VARIOUS_ARTISTS_MBID),
                )
            )
        )
    )

    result = await provider.get_release_group_by_track_name("Queen", "Bohemian Rhapsody")

    assert result is not None
    artist, release_groups = result
    assert artist.name == "Queen"
    assert release_groups == []


async def test_release_year_by_track_name_ignores_an_undated_release_group() -> None:
    """Date a song by the oldest release group that has a date, not by an undated one."""
    provider, _ = _provider(
        _search_result(
            _recording(_release("", title="Unknown Pressing")),
            _recording(_release("1975-11-21")),
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


def _release_groups(*groups: tuple[str, str]) -> dict[str, Any]:
    """
    Return a release group search response holding the given release groups.

    :param groups: Release groups as (id, first release date) pairs.
    """
    return {
        "count": len(groups),
        "release-groups": [
            {"id": group_id, "title": group_id, "first-release-date": date}
            for group_id, date in groups
        ],
    }


async def test_release_year_by_track_name_prefers_the_release_group_first_release() -> None:
    """Date a much reissued song by its album's first release, not by the reissue found."""
    provider, _ = _provider(
        _search_result(_recording(_release("2021-11-12"))),
        _release_groups(("rg-A Night at the Opera-Album", "1975-11-21")),
    )

    assert await provider.get_release_year_by_track_name("Queen", "Bohemian Rhapsody") == 1975


async def test_release_year_by_track_name_keeps_a_close_release_date() -> None:
    """Keep the found release when the album barely predates it, as a single ahead of it would."""
    provider, _ = _provider(
        _search_result(_recording(_release("1975-11-21"))),
        _release_groups(("rg-A Night at the Opera-Album", "1974-10-31")),
    )

    assert await provider.get_release_year_by_track_name("Queen", "Bohemian Rhapsody") == 1975


async def test_release_year_by_track_name_corrects_only_beyond_the_threshold() -> None:
    """Correct the year only once the album predates the found release by enough years."""
    for first_release_year, expected in ((1970, 1975), (1969, 1969)):
        provider, _ = _provider(
            _search_result(_recording(_release("1975-11-21"))),
            _release_groups(("rg-A Night at the Opera-Album", f"{first_release_year}-10-31")),
        )

        assert (
            await provider.get_release_year_by_track_name("Queen", "Bohemian Rhapsody") == expected
        )


async def test_release_year_by_track_name_keeps_the_found_release_when_the_lookup_fails() -> None:
    """Never lose the year the search already supplied when the release group lookup fails."""
    search_result = _search_result(_recording(_release("1975-11-21")))
    provider, get_data = _provider(search_result)

    async def _answer(endpoint: str, **_kwargs: Any) -> Any:
        if endpoint == "release-group":
            raise RateLimited("rate limited")
        return search_result

    get_data.side_effect = _answer

    assert await provider.get_release_year_by_track_name("Queen", "Bohemian Rhapsody") == 1975


async def test_release_year_by_track_name_looks_up_every_release_group_at_once() -> None:
    """Resolve all release groups of a song with a single, escaped, request."""
    provider, get_data = _provider(
        _search_result(
            _recording(_release("2011-05-16", title="The Platinum Collection")),
            _recording(_release("1975-11-21")),
        ),
        _release_groups(("rg-A Night at the Opera-Album", "1975-11-21")),
    )

    await provider.get_release_year_by_track_name("Queen", "Bohemian Rhapsody")

    # one release group lookup, however many groups the search turned up
    assert [call.args for call in get_data.await_args_list] == [("recording",), ("release-group",)]
    assert get_data.await_args_list[1].kwargs["query"] == (
        r"rgid:(rg\-A Night at the Opera\-Album OR rg\-The Platinum Collection\-Album)"
    )


async def test_release_year_by_track_name_falls_back_to_the_found_release() -> None:
    """Keep the found release when MusicBrainz does not know when the album first came out."""
    for release_group_response in (None, _release_groups(), {"release-groups": [{"id": "rg-x"}]}):
        provider, _ = _provider(
            _search_result(_recording(_release("1975-11-21"))), release_group_response
        )

        assert await provider.get_release_year_by_track_name("Queen", "Bohemian Rhapsody") == 1975


async def test_release_year_by_track_name_dates_an_undated_release_group() -> None:
    """Date a song whose found releases carry no date at all by its album's first release."""
    provider, _ = _provider(
        _search_result(_recording(_release(""))),
        _release_groups(("rg-A Night at the Opera-Album", "1975-11-21")),
    )

    assert await provider.get_release_year_by_track_name("Queen", "Bohemian Rhapsody") == 1975


async def test_release_group_by_track_name_costs_a_single_request() -> None:
    """Never spend the release group lookup on callers that only want the release groups."""
    provider, get_data = _provider(
        _search_result(_recording(_release("1975-11-21"))),
        _release_groups(("rg-A Night at the Opera-Album", "1975-11-21")),
    )

    await provider.get_release_group_by_track_name("Queen", "Bohemian Rhapsody")

    assert [call.args for call in get_data.await_args_list] == [("recording",)]


async def test_release_group_by_track_name_returns_the_artist_and_oldest_groups_first() -> None:
    """Hand out the artist of the oldest recording, and their release groups oldest first."""
    provider, _ = _provider(
        _search_result(
            _recording(
                _release("2011-05-16", title="The Platinum Collection"),
                first_release="2011-05-16",
                artist_id="artist-reissue",
            ),
            _recording(
                _release("1975-11-21"),
                first_release="1975-11-21",
                artist_id="artist-original",
            ),
        )
    )

    result = await provider.get_release_group_by_track_name("Queen", "Bohemian Rhapsody")

    assert result is not None
    artist, release_groups = result
    # MusicBrainz can hold several artist entries under one name, and the oldest recording
    # is the one that identifies the original
    assert artist.id == "artist-original"
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
