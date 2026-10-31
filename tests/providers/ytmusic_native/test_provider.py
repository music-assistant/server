"""Test the native YouTube Music provider's stream, playlist and session handling."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import LoginFailed, SetupFailedError, UnplayableMediaError

from music_assistant.providers.ytmusic_native import YoutubeMusicNativeProvider, _premium_served
from music_assistant.providers.ytmusic_native.cipher import CipherError, PlayerCipher
from music_assistant.providers.ytmusic_native.constants import (
    CONF_HIGH_QUALITY,
    PLAYLIST_ID_DELIMITER,
    PREMIUM_ITAGS,
    PREMIUM_RECHECK_INTERVAL,
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


def _free_player() -> dict[str, Any]:
    """Return the player response a free account gets: no Premium formats."""
    player = _player()
    formats = player["streamingData"]["adaptiveFormats"]
    player["streamingData"]["adaptiveFormats"] = [
        fmt for fmt in formats if fmt["itag"] not in PREMIUM_ITAGS
    ]
    return player


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


async def test_loading_leaves_the_session_cookies_alone() -> None:
    """
    The provider never rotates the session, like the desktop app.

    RotateCookies starts a new chain of session timestamps. Anything else holding the
    same cookie then sends an outdated one, and YouTube signs the session out.
    """
    prov = _provider()
    setup = {"cookie": COOKIE, "keep_session_alive": True}  # as stored by older setups
    session = MagicMock(closed=False, close=AsyncMock())
    response = MagicMock(status=200, cookies={})
    session.post.return_value.__aenter__ = AsyncMock(return_value=response)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.object(prov, "get_setup_value", side_effect=lambda key, *_: setup.get(key)),
        patch("music_assistant.providers.ytmusic_native.mini_racer_available", return_value=True),
        patch(
            "music_assistant.providers.ytmusic_native.create_clientsession", return_value=session
        ),
        patch.object(InnerTube, "bootstrap", AsyncMock()),
        patch.object(prov, "_fetch_account_name", AsyncMock(return_value="me")),
        patch.object(prov, "_require_premium", AsyncMock()),
        patch.object(prov.mass, "create_task") as create_task,
    ):
        await prov.handle_async_init()

    session.post.assert_not_called()
    create_task.assert_not_called()
    assert prov._innertube.cookie == COOKIE


@pytest.mark.parametrize(
    ("player", "premium"),
    [
        (_player(), True),
        (_free_player(), False),
        (_player(status="UNPLAYABLE"), None),
        ({"playabilityStatus": {"status": "OK"}}, None),
    ],
)
def test_premium_is_read_from_the_served_formats(
    player: dict[str, Any], premium: bool | None
) -> None:
    """Only Premium accounts are served itag 141; no formats at all says nothing."""
    assert _premium_served(player) is premium


async def test_setup_refuses_an_account_without_premium() -> None:
    """A free account hears ads in YouTube Music; this provider plays none, so it needs Premium."""
    prov = _provider()
    prov._check_premium = AsyncMock(return_value=False)  # type: ignore[method-assign]

    with pytest.raises(LoginFailed) as err:
        await prov._require_premium()
    assert err.value.translation_key == "premium_required"


async def test_setup_fails_when_premium_can_not_be_checked() -> None:
    """An inconclusive check never lets the provider load."""
    prov = _provider()
    prov._check_premium = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with pytest.raises(SetupFailedError):
        await prov._require_premium()


async def test_playback_stops_once_premium_is_gone() -> None:
    """A day without Premium formats triggers a check, and a lapsed subscription unloads."""
    prov = _provider()
    resolve = _with_player(prov, _free_player())
    prov._check_premium = AsyncMock(return_value=False)  # type: ignore[method-assign]
    prov._premium_verified_at = time.monotonic() - PREMIUM_RECHECK_INTERVAL - 1

    with (
        patch.object(prov, "unload_with_error") as unload,
        pytest.raises(LoginFailed),
    ):
        await prov.get_stream_details("vid", MediaType.TRACK)
    unload.assert_called_once()
    resolve.assert_not_called()


async def test_tracks_with_premium_formats_need_no_extra_check() -> None:
    """Premium formats on the played track confirm the subscription without another call."""
    prov = _provider()
    _with_player(prov, _player())
    prov._check_premium = AsyncMock()  # type: ignore[method-assign]
    prov._premium_verified_at = time.monotonic() - PREMIUM_RECHECK_INTERVAL - 1

    await prov.get_stream_details("vid", MediaType.TRACK)

    prov._check_premium.assert_not_called()
    assert time.monotonic() - prov._premium_verified_at < 60


def _continuation_pages(count: int) -> AsyncMock:
    """Return a call stub serving continuation pages "1".."count-1" of one item each."""

    async def call(_endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        page = int(body["continuation"])
        return {"item": page, "next": str(page + 1) if page + 1 < count else None}

    return AsyncMock(side_effect=call)


async def test_paging_reads_a_long_library_to_the_end() -> None:
    """A library sync drops what is not returned, so a list of 150 pages is read in full."""
    prov = _provider()
    prov._innertube = MagicMock(call=_continuation_pages(150))

    items = await prov._continued(
        [{"item": 0}], "1", lambda response: [{"item": response["item"]}], lambda r: r["next"]
    )

    assert [item["item"] for item in items] == list(range(150))


async def test_paging_stops_on_a_repeated_token() -> None:
    """A token YouTube hands out again would loop forever; it ends the list with a warning."""
    prov = _provider()
    call = AsyncMock(return_value={"item": 1, "next": "same"})
    prov._innertube = MagicMock(call=call)

    with patch.object(prov, "logger") as logger:
        items = await prov._continued(
            [], "same", lambda response: [{"item": response["item"]}], lambda r: r["next"]
        )

    assert items == [{"item": 1}]
    call.assert_awaited_once()
    logger.warning.assert_called_once()


async def test_a_player_build_that_fails_to_load_is_not_downloaded_again() -> None:
    """Every play would otherwise fetch the ~3 MB base.js and seed V8 again."""
    prov = _provider()
    base_js = AsyncMock(return_value=("base.js source", 20700))
    player = AsyncMock()
    prov._innertube = MagicMock(
        player_id="newbuild", bootstrap=AsyncMock(), base_js=base_js, player=player
    )
    prov._cipher = PlayerCipher(logging.getLogger(__name__))
    prov._player_lock = asyncio.Lock()

    with patch.object(prov._cipher, "_seed", side_effect=CipherError("swap helper not found")):
        for _ in range(3):
            with pytest.raises(UnplayableMediaError):
                await prov._player("vid")

    base_js.assert_awaited_once()
    player.assert_not_called()


def test_episodes_get_their_release_date() -> None:
    """The date text YouTube shows becomes the episode's release date."""
    prov = _provider()
    podcast = prov._parse_podcast({"podcast_id": "PLshow", "title": "Show"})

    episode = prov._parse_episode(
        {"video_id": "ep1", "title": "Episode", "date": "Nov 2, 2025"}, podcast, position=1
    )

    assert episode.metadata.release_date == datetime(2025, 11, 2, tzinfo=UTC)
    assert episode.item_id == "PLshow|ep1"
