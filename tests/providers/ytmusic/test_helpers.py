"""Test the ytmusicapi call wrapper's handling of a signed-out session."""

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
import ytmusicapi
from music_assistant_models.errors import LoginFailed, MediaNotFoundError, RetriesExhausted
from ytmusicapi import LikeStatus
from ytmusicapi.exceptions import YTMusicServerError
from ytmusicapi.models.content.enums import PlaylistSortOrder

from music_assistant.providers.ytmusic import helpers

# A trimmed but realistic nav() KeyError: YouTube answered with its signed-out
# page instead of an auth error, so the renderer lookup fails deep in that payload.
SIGNED_OUT_PAYLOAD_ERROR = KeyError(
    "Unable to find 'twoColumnBrowseResultsRenderer' using path "
    "['contents', 'twoColumnBrowseResultsRenderer', 'tabs', 0] on "
    "{'singleColumnBrowseResultsRenderer': {'tabs': [{'tabRenderer': {'content': "
    "{'sectionListRenderer': {'contents': [{'itemSectionRenderer': {'contents': "
    "[{'buttonRenderer': {'text': {'runs': [{'text': 'Sign in'}]}, "
    "'navigationEndpoint': {'clickTrackingParams': '...', "
    "'signInEndpoint': {'hack': True}}}}]}}]}}}}]}}, "
    "exception: 'twoColumnBrowseResultsRenderer'"
)


def _patch_get_home(error: Exception) -> Any:
    """Patch ytmusicapi.YTMusic so get_home() raises the given error."""
    mock_ytm = MagicMock()
    mock_ytm.get_home.side_effect = error
    return patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm)


async def test_signed_out_payload_is_translated_to_login_failed() -> None:
    """A KeyError carrying the signed-out page must surface as LoginFailed."""
    with _patch_get_home(SIGNED_OUT_PAYLOAD_ERROR), pytest.raises(LoginFailed):
        await helpers.get_home(headers={})


async def test_unrelated_key_error_still_propagates() -> None:
    """A KeyError unrelated to a signed-out session must propagate as-is."""
    with _patch_get_home(KeyError("some_other_key")), pytest.raises(KeyError):
        await helpers.get_home(headers={})


async def test_get_artist_does_not_hide_signed_out_behind_its_fallback() -> None:
    """get_artist's channel fallback must not turn a signed-out session into an artist."""
    mock_ytm = MagicMock()
    mock_ytm.get_artist.side_effect = SIGNED_OUT_PAYLOAD_ERROR
    mock_ytm.get_user.side_effect = SIGNED_OUT_PAYLOAD_ERROR
    with (
        patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm),
        pytest.raises(LoginFailed),
    ):
        await helpers.get_artist(prov_artist_id="UC123", headers={})


async def test_get_artist_fallback_still_returns_unknown() -> None:
    """An artist that is neither a channel nor a user still falls back to Unknown."""
    mock_ytm = MagicMock()
    mock_ytm.get_artist.side_effect = KeyError("header")
    mock_ytm.get_user.side_effect = KeyError("name")
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm):
        artist = await helpers.get_artist(prov_artist_id="UC123", headers={})
    assert artist == {"channelId": "UC123", "name": "Unknown"}


async def test_get_podcast_rejected_id_raises_media_not_found() -> None:
    """A podcast id YouTube rejects with HTTP 400 must surface as MediaNotFoundError."""
    mock_ytm = MagicMock()
    mock_ytm.get_podcast.side_effect = YTMusicServerError(
        "Server returned HTTP 400: Bad Request.\nRequest contains an invalid argument."
    )
    with (
        patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm),
        pytest.raises(MediaNotFoundError),
    ):
        await helpers.get_podcast(prov_podcast_id="SE", headers={})


async def test_get_podcast_server_failure_still_propagates() -> None:
    """A YouTube server failure on get_podcast must not be reported as not found."""
    mock_ytm = MagicMock()
    mock_ytm.get_podcast.side_effect = YTMusicServerError(
        "Server returned HTTP 503: Service Unavailable.\nThe service is currently unavailable."
    )
    with (
        patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm),
        pytest.raises(YTMusicServerError),
    ):
        await helpers.get_podcast(prov_podcast_id="PL123", headers={})


async def test_search_is_anonymous() -> None:
    """search() must not authenticate, so lookups stay out of the user's search history."""
    mock_ytm = MagicMock()
    mock_ytm.search.return_value = []
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm) as mock_ytmusic:
        await helpers.search(query="test")
    mock_ytmusic.assert_called_once_with(language="en")


async def test_get_album_passes_auth_headers_and_user() -> None:
    """get_album must authenticate its YTMusic client so tracks resolve with real ids."""
    mock_ytm = MagicMock()
    mock_ytm.get_album.return_value = {}
    headers = {"cookie": "abc"}
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm) as mock_ytmusic:
        await helpers.get_album(headers=headers, prov_album_id="album", user="123")
    mock_ytmusic.assert_called_once_with(auth=headers, language="en", user="123")


async def test_get_album_survives_missing_audio_playlist_id() -> None:
    """A present-but-null audioPlaylistId must not reach get_playlist(playlistId=None)."""
    mock_ytm = MagicMock()
    mock_ytm.get_album.return_value = {"audioPlaylistId": None, "tracks": []}
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm):
        album = await helpers.get_album(headers={}, prov_album_id="album")
    assert album == {"audioPlaylistId": None, "tracks": []}
    mock_ytm.get_playlist.assert_not_called()


async def test_get_album_upload_does_not_resolve_audio_playlist_id() -> None:
    """An uploaded album's audioPlaylistId must not be passed to get_playlist."""
    album_id = "FEmusic_library_privately_owned_release_detailb_123"
    mock_ytm = MagicMock()
    mock_ytm.get_library_upload_album.return_value = {
        "audioPlaylistId": "upload_playlist",
        "tracks": [{"title": "track", "videoId": "upload_video"}],
    }
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm):
        album = await helpers.get_album(headers={}, prov_album_id=album_id)
    mock_ytm.get_library_upload_album.assert_called_once_with(browseId=album_id)
    mock_ytm.get_album.assert_not_called()
    mock_ytm.get_playlist.assert_not_called()
    assert album["tracks"] == [{"title": "track", "videoId": "upload_video"}]


async def test_add_playlist_tracks_allows_duplicates() -> None:
    """Playlist writes must opt into duplicate entries."""
    mock_ytm = MagicMock()
    headers = {"cookie": "abc"}
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm):
        await helpers.add_remove_playlist_tracks(
            headers=headers,
            prov_playlist_id="playlist",
            prov_track_ids=["track", "track"],
            add=True,
            user="123",
        )

    mock_ytm.add_playlist_items.assert_called_once_with(
        playlistId="playlist",
        videoIds=["track", "track"],
        duplicates=True,
    )


async def test_rate_track_passes_auth_headers_and_user() -> None:
    """rate_track must rate the song on the authenticated account."""
    mock_ytm = MagicMock()
    headers = {"cookie": "abc"}
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm) as mock_ytmusic:
        await helpers.rate_track(
            headers=headers, prov_track_id="video", rating=LikeStatus.DISLIKE, user="123"
        )
    mock_ytmusic.assert_called_once_with(auth=headers, user="123")
    mock_ytm.rate_song.assert_called_once_with(videoId="video", rating=LikeStatus.DISLIKE)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"playerConfig": {"playbackStartConfig": {"startSeconds": 1007}}}, 1007),
        ({"playerConfig": {"playbackStartConfig": {}}}, 0),
        ({}, 0),
    ],
)
async def test_get_episode_start_seconds_reads_the_player_response(
    response: dict[str, Any], expected: int
) -> None:
    """The resume second comes from the player response, 0 when YouTube sends none."""
    ytm = MagicMock()
    ytm._send_request.return_value = response
    with patch.object(ytmusicapi, "YTMusic", return_value=ytm):
        assert await helpers.get_episode_start_seconds("v1", headers={}) == expected

    assert ytm._send_request.call_args.args[0] == "player"
    assert ytm._send_request.call_args.args[1]["video_id"] == "v1"


@pytest.mark.parametrize(
    ("successor", "placement"),
    [
        ("D", {"movedSetVideoIdSuccessor": "D"}),
        (None, {}),
    ],
)
async def test_move_playlist_entries_orders_manually_then_moves_in_one_request(
    successor: str | None, placement: dict[str, str]
) -> None:
    """The playlist is ordered manually first, then all entries move in one request."""
    mock_ytm = MagicMock()
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm):
        await helpers.move_playlist_entries(
            headers={}, prov_playlist_id="PL1", set_video_ids=["A", "C"], successor=successor
        )

    assert mock_ytm.mock_calls == [
        call.edit_playlist("PL1", sortOrder=PlaylistSortOrder.MANUAL),
        call._send_request(
            "browse/edit_playlist",
            {
                "playlistId": "PL1",
                "actions": [
                    {"action": "ACTION_MOVE_VIDEO_BEFORE", "setVideoId": svid, **placement}
                    for svid in ("A", "C")
                ],
            },
        ),
    ]


@pytest.mark.parametrize(
    "make_call",
    [
        lambda: helpers.get_home(headers={}),
        lambda: helpers.search("query"),
        helpers.get_visitor_id,
    ],
    ids=["account", "search", "visitor_id"],
)
async def test_no_call_reaches_youtube_during_a_rate_limit_cooldown(
    make_call: Callable[[], Awaitable[Any]],
) -> None:
    """All calls share one throttler, so its rate limit cooldown holds back every call."""
    helpers._YTMusicGate.throttler.set_cooldown(3600)
    with patch.object(ytmusicapi, "YTMusic") as mock_ytmusic, pytest.raises(RetriesExhausted):
        await make_call()

    mock_ytmusic.assert_not_called()


async def test_http_429_arms_the_shared_rate_limit_cooldown() -> None:
    """An HTTP 429 from YouTube Music is a rate limit that holds back every later call."""
    error = YTMusicServerError("Server returned HTTP 429: Too Many Requests.\nQuota exceeded.")
    with _patch_get_home(error), pytest.raises(RetriesExhausted):
        await helpers.get_home(headers={})

    assert helpers._YTMusicGate.throttler.cooldown_remaining > 0
