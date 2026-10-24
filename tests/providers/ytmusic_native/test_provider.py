"""Test the native YouTube Music provider's stream, playlist and session handling."""

import logging
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import LoginFailed, UnplayableMediaError

from music_assistant.providers.ytmusic_native import YoutubeMusicNativeProvider
from music_assistant.providers.ytmusic_native.constants import (
    CONF_HIGH_QUALITY,
    PLAYLIST_ID_DELIMITER,
)
from music_assistant.providers.ytmusic_native.innertube import InnerTube

COOKIE = "SAPISID=abc; __Secure-3PAPISID=abc"


def _provider(high_quality: bool = True) -> YoutubeMusicNativeProvider:
    """Return a provider with mocked dependencies."""
    manifest = MagicMock()
    manifest.domain = "ytmusic_native"
    config = MagicMock()
    config.instance_id = "ytmusic_native--test"
    config.name = "YTM"
    values = {CONF_HIGH_QUALITY: high_quality}
    config.get_value.side_effect = lambda key, *_: values.get(key, "GLOBAL")
    return YoutubeMusicNativeProvider(AsyncMock(), manifest, config)


def _player(status: str = "OK") -> dict[str, Any]:
    """Return a web player response with standard and Premium audio formats."""
    return {
        "playabilityStatus": {"status": status, "reason": "Video unavailable"},
        "streamingData": {
            "adaptiveFormats": [
                {
                    "itag": 140,
                    "mimeType": 'audio/mp4; codecs="mp4a.40.2"',
                    "bitrate": 130000,
                    "url": "u140",
                },
                {
                    "itag": 251,
                    "mimeType": 'audio/webm; codecs="opus"',
                    "bitrate": 150000,
                    "url": "u251",
                },
                {
                    "itag": 141,
                    "mimeType": 'audio/mp4; codecs="mp4a.40.2"',
                    "bitrate": 260000,
                    "url": "u141",
                },
                {
                    "itag": 137,
                    "mimeType": 'video/mp4; codecs="avc1"',
                    "bitrate": 4000000,
                    "url": "u137",
                },
            ]
        },
        "playbackTracking": {"videostatsPlaybackUrl": {"baseUrl": "https://example/ping"}},
    }


def _with_player(prov: YoutubeMusicNativeProvider, player: dict[str, Any]) -> AsyncMock:
    """Stub the web player and the URL solver; return the solver mock."""
    prov._player = AsyncMock(return_value=player)  # type: ignore[method-assign]
    prov._innertube = MagicMock()
    resolve = AsyncMock(side_effect=lambda fmt, _ok: f"https://media/{fmt['itag']}?expire=0")
    prov._cipher = MagicMock(resolve=resolve)
    return resolve


@pytest.mark.parametrize(("high_quality", "itag"), [(True, 141), (False, 251)])
async def test_stream_format_follows_the_quality_setting(high_quality: bool, itag: int) -> None:
    """Premium itag 141 when allowed; otherwise the best format that is not a Premium one."""
    prov = _provider(high_quality)
    resolve = _with_player(prov, _player())

    details = await prov.get_stream_details("vid", MediaType.TRACK)

    assert resolve.call_args.args[0]["itag"] == itag
    assert details.path == f"https://media/{itag}?expire=0"
    assert details.data == {"tracking_url": "https://example/ping"}


async def test_unplayable_video_reports_youtubes_reason() -> None:
    """A refused playback surfaces YouTube's reason instead of a solver error."""
    prov = _provider()
    resolve = _with_player(prov, _player(status="UNPLAYABLE"))

    with pytest.raises(UnplayableMediaError, match="Video unavailable"):
        await prov.get_stream_details("vid", MediaType.TRACK)
    resolve.assert_not_called()


def _entries() -> list[dict[str, Any]]:
    return [{"video_id": f"v{i}", "set_video_id": f"s{i}", "kind": "track"} for i in range(1, 5)]


@pytest.mark.parametrize(
    ("from_position", "to_position", "successor"),
    [(1, 3, "s4"), (4, 1, "s1"), (2, 4, None)],
)
async def test_move_puts_the_entry_before_its_new_successor(
    from_position: int, to_position: int, successor: str | None
) -> None:
    """YouTube moves an entry in front of another one, or to the end without one."""
    prov = _provider()
    prov._playlist_items = AsyncMock(return_value=_entries())  # type: ignore[method-assign]
    prov._edit_playlist = AsyncMock()  # type: ignore[method-assign]

    await prov.move_playlist_track("PLx", from_position, to_position)

    action = prov._edit_playlist.call_args.args[1][0]
    assert action["setVideoId"] == f"s{from_position}"
    assert action.get("movedSetVideoIdSuccessor") == successor


async def test_remove_uses_one_based_positions_and_set_video_ids() -> None:
    """Positions are the 1-based ones the provider handed out; entries go by setVideoId."""
    prov = _provider()
    prov._playlist_items = AsyncMock(return_value=_entries())  # type: ignore[method-assign]
    prov._edit_playlist = AsyncMock()  # type: ignore[method-assign]

    await prov.remove_playlist_tracks("PLx", (2, 4))

    actions = prov._edit_playlist.call_args.args[1]
    assert [(a["removedVideoId"], a["setVideoId"]) for a in actions] == [("v2", "s2"), ("v4", "s4")]


def test_personal_playlists_get_an_id_per_instance() -> None:
    """Liked Music has the same id on every account, so it is suffixed per instance."""
    prov = _provider()

    liked = prov._parse_playlist({"playlist_id": "LM", "title": "Liked Music"})
    public = prov._parse_playlist({"playlist_id": "PLabc", "title": "Public"})

    assert liked.item_id == f"LM{PLAYLIST_ID_DELIMITER}ytmusic_native--test"
    assert prov._raw_playlist_id(liked.item_id) == "LM"
    assert public.item_id == "PLabc"


async def test_logged_out_answer_raises_login_failed() -> None:
    """A dead cookie gets a normal answer marked logged_in=0, which must not pass as data."""
    response = MagicMock(status=200)
    response.json = AsyncMock(
        return_value={
            "responseContext": {
                "serviceTrackingParams": [
                    {"service": "GFEEDBACK", "params": [{"key": "logged_in", "value": "0"}]}
                ]
            },
            "contents": {},
        }
    )
    session = MagicMock()
    session.post.return_value.__aenter__ = AsyncMock(return_value=response)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    innertube = InnerTube(session, COOKIE, logging.getLogger(__name__), "ytmusic_native")
    innertube._bootstrapped_at = time.time()

    with pytest.raises(LoginFailed):
        await innertube.call("browse", {"browseId": "FEmusic_home"})
