"""Tests that Soundcloud parse-failure logging never leaks a raw API payload."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.soundcloud import SoundcloudMusicProvider
from tests.providers.soundcloud.test_drm_tracks import PLAIN_TRANSCODINGS

# a JWT-shaped sentinel: if this ever shows up in caplog.text, a raw payload leaked
SENTINEL = "eyJ0eXAiOiJKV1QifQ.SENTINEL-JWT-MUST-NOT-BE-LOGGED.sig"
PRIVATE_USER = {"id": 1, "username": "PrivatePerson", "permalink": "some-artist"}


def _leaky_track_obj(track_id: int, **overrides: Any) -> dict[str, Any]:
    """Build a track object carrying a sentinel auth token and a private user record."""
    obj: dict[str, Any] = {
        "id": track_id,
        "title": "Some Track",
        "duration": 235818,
        "full_duration": 235771,
        "kind": "track",
        "permalink_url": f"https://soundcloud.com/artist/{track_id}",
        "policy": "MONETIZE",
        "monetization_model": "AD_SUPPORTED",
        "track_authorization": SENTINEL,
        "user": PRIVATE_USER,
        "media": {"transcodings": PLAIN_TRANSCODINGS},
    }
    obj.update(overrides)
    return obj


def _leaky_artist_obj(artist_id: int, **overrides: Any) -> dict[str, Any]:
    """Build a user object carrying a sentinel auth token and a private user record."""
    obj: dict[str, Any] = {
        "id": artist_id,
        "permalink": "some-artist",
        "username": "some-artist",
        "track_authorization": SENTINEL,
        "user": PRIVATE_USER,
    }
    obj.update(overrides)
    return obj


def _leaky_playlist_obj(playlist_id: int, **overrides: Any) -> dict[str, Any]:
    """Build a playlist object carrying a sentinel auth token and a private user record."""
    obj: dict[str, Any] = {
        "id": playlist_id,
        "title": "Some Playlist",
        "track_authorization": SENTINEL,
        "user": PRIVATE_USER,
    }
    obj.update(overrides)
    return obj


async def _call_search(provider: SoundcloudMusicProvider, track_obj: dict[str, Any]) -> str:
    provider._soundcloud.search.return_value = {"collection": [track_obj]}
    search: Any = SoundcloudMusicProvider.search.__wrapped__  # type: ignore[attr-defined]
    await search(provider, "query", [MediaType.TRACK], 10)
    return str(track_obj["id"])


async def _call_subscribed_feed(
    provider: SoundcloudMusicProvider, track_obj: dict[str, Any]
) -> str:
    provider._soundcloud.get_subscribe_feed.return_value = {
        "collection": [{"type": "track", "track": track_obj}]
    }
    get_feed_tracks: Any = (
        SoundcloudMusicProvider._get_subscribed_feed_tracks.__wrapped__  # type: ignore[attr-defined]
    )
    await get_feed_tracks(provider)
    return str(track_obj["id"])


async def _call_get_artist(provider: SoundcloudMusicProvider, artist_obj: dict[str, Any]) -> str:
    provider._soundcloud.get_user_details.return_value = artist_obj
    get_artist: Any = SoundcloudMusicProvider.get_artist.__wrapped__  # type: ignore[attr-defined]
    # get_artist re-raises UnboundLocalError after logging when parsing fails; a pre-existing
    # bug that is out of scope here, tolerated so the logging behaviour can still be asserted
    with contextlib.suppress(UnboundLocalError):
        await get_artist(provider, "42")
    return "42"


async def _call_get_track(provider: SoundcloudMusicProvider, track_obj: dict[str, Any]) -> str:
    provider._soundcloud.get_track_details.return_value = [track_obj]
    get_track: Any = SoundcloudMusicProvider.get_track.__wrapped__  # type: ignore[attr-defined]
    with pytest.raises(MediaNotFoundError):
        await get_track(provider, "7")
    return "7"


async def _call_get_playlist(
    provider: SoundcloudMusicProvider, playlist_obj: dict[str, Any]
) -> str:
    provider._soundcloud.get_playlist_details.return_value = playlist_obj
    get_playlist: Any = SoundcloudMusicProvider.get_playlist.__wrapped__  # type: ignore[attr-defined]
    # same pre-existing UnboundLocalError as get_artist, out of scope
    with contextlib.suppress(UnboundLocalError):
        await get_playlist(provider, "13")
    return "13"


async def _call_get_playlist_tracks(
    provider: SoundcloudMusicProvider, track_obj: dict[str, Any]
) -> str:
    provider._soundcloud.get_playlist_details.return_value = {"id": 13, "tracks": [track_obj]}
    get_playlist_tracks: Any = (
        SoundcloudMusicProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]
    )
    await get_playlist_tracks(provider, "13")
    return str(track_obj["id"])


async def _call_get_artist_toptracks(
    provider: SoundcloudMusicProvider, track_obj: dict[str, Any]
) -> str:
    provider._soundcloud.get_tracks_from_user.return_value = {
        "collection": [{"id": track_obj["id"]}]
    }
    provider._soundcloud.get_track_details.return_value = [track_obj]
    get_toptracks: Any = (
        SoundcloudMusicProvider.get_artist_toptracks.__wrapped__  # type: ignore[attr-defined]
    )
    await get_toptracks(provider, "99")
    return str(track_obj["id"])


async def _call_get_similar_tracks(
    provider: SoundcloudMusicProvider, track_obj: dict[str, Any]
) -> str:
    provider._soundcloud.get_recommended.return_value = {"collection": [{"id": track_obj["id"]}]}
    provider._soundcloud.get_track_details.return_value = [track_obj]
    get_similar: Any = SoundcloudMusicProvider.get_similar_tracks.__wrapped__  # type: ignore[attr-defined]
    await get_similar(provider, "5")
    return str(track_obj["id"])


# (call, payload) pairs, one per entry point that logs a parse failure outside of a sync.
# Each payload carries a sentinel auth token and a private user record, and is missing a
# field required to parse successfully so the call is driven into a genuine failure.
LEAK_TEST_CASES = [
    pytest.param(_call_search, _leaky_track_obj, "permalink_url", id="search"),
    pytest.param(_call_subscribed_feed, _leaky_track_obj, "permalink_url", id="feed"),
    pytest.param(_call_get_artist, _leaky_artist_obj, "permalink", id="get_artist"),
    pytest.param(_call_get_track, _leaky_track_obj, "permalink_url", id="get_track"),
    pytest.param(_call_get_playlist, _leaky_playlist_obj, "title", id="get_playlist"),
    pytest.param(
        _call_get_playlist_tracks, _leaky_track_obj, "permalink_url", id="get_playlist_tracks"
    ),
    pytest.param(
        _call_get_artist_toptracks, _leaky_track_obj, "permalink_url", id="get_artist_toptracks"
    ),
    pytest.param(
        _call_get_similar_tracks, _leaky_track_obj, "permalink_url", id="get_similar_tracks"
    ),
]


@pytest.mark.parametrize(("call", "build_obj", "missing_field"), LEAK_TEST_CASES)
async def test_parse_failure_does_not_leak_raw_payload(
    provider: SoundcloudMusicProvider,
    caplog: pytest.LogCaptureFixture,
    call: Any,
    build_obj: Any,
    missing_field: str,
) -> None:
    """A genuine parse failure logs an identifier and error type, never the raw API payload."""
    obj = build_obj(987654321)
    del obj[missing_field]

    with caplog.at_level(logging.DEBUG):
        item_id = await call(provider, obj)

    assert SENTINEL not in caplog.text
    assert "track_authorization" not in caplog.text
    assert "PrivatePerson" not in caplog.text
    assert "Traceback" not in caplog.text
    assert item_id in caplog.text
    assert "KeyError" in caplog.text


async def test_search_keyerror_is_debuggable(
    provider: SoundcloudMusicProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """A KeyError from a missing field still names the item and the error type."""
    track_obj = _leaky_track_obj(1)
    del track_obj["permalink_url"]
    provider._soundcloud.search.return_value = {"collection": [track_obj]}

    search: Any = SoundcloudMusicProvider.search.__wrapped__  # type: ignore[attr-defined]
    with caplog.at_level(logging.DEBUG):
        result = await search(provider, "query", [MediaType.TRACK], 10)

    assert result.tracks == []
    assert SENTINEL not in caplog.text
    assert "PrivatePerson" not in caplog.text
    assert (
        "Skipping search result track 1 - error details: KeyError: 'permalink_url'" in caplog.text
    )


async def test_get_playlist_tracks_typeerror_is_debuggable(
    provider: SoundcloudMusicProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """A TypeError from a malformed field still names the track and the error type."""
    track_obj = _leaky_track_obj(1, duration=None)
    provider._soundcloud.get_playlist_details.return_value = {"id": 13, "tracks": [track_obj]}

    get_playlist_tracks: Any = (
        SoundcloudMusicProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]
    )
    with caplog.at_level(logging.DEBUG):
        tracks = await get_playlist_tracks(provider, "13")

    assert tracks == []
    assert SENTINEL not in caplog.text
    assert "PrivatePerson" not in caplog.text
    assert "Skipping track 1 in playlist 13 - error details: TypeError" in caplog.text


async def test_get_track_indexerror_reports_argument_id(
    provider: SoundcloudMusicProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """An empty track_details response has no id in the payload, so the log uses the call's id."""
    provider._soundcloud.get_track_details.return_value = []

    get_track: Any = SoundcloudMusicProvider.get_track.__wrapped__  # type: ignore[attr-defined]
    with caplog.at_level(logging.DEBUG), pytest.raises(MediaNotFoundError):
        await get_track(provider, "7")

    assert "Skipping track 7 - error details: IndexError" in caplog.text


async def test_get_artist_invaliddataerror_is_debuggable(
    provider: SoundcloudMusicProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """A user object with no id fails _parse_artist and still names the artist and error type."""
    artist_obj = _leaky_artist_obj(1)
    del artist_obj["id"]
    provider._soundcloud.get_user_details.return_value = artist_obj

    get_artist: Any = SoundcloudMusicProvider.get_artist.__wrapped__  # type: ignore[attr-defined]
    with caplog.at_level(logging.DEBUG), contextlib.suppress(UnboundLocalError):
        await get_artist(provider, "42")

    assert SENTINEL not in caplog.text
    assert "PrivatePerson" not in caplog.text
    # one of our own errors, so its message is reported instead of the exception type name
    assert "Skipping artist 42 - error details: Artist does not have a valid ID" in caplog.text
