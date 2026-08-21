"""
Tests for the Spotify provider's Connect playback redirect.

In Connect playback mode the provider never streams audio itself: play requests are
redirected into an account-matched Spotify Connect session (the instance bound to the
target player preferred, else the system-wide one). In librespot mode the redirect is
opportunistic: only a same-account session already active on the target player is
driven. Account verification is passive (backend report, else a silent device-list
name match); the Web API assist starts contexts at the right track, with the session's
own play command as fallback.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, QueueOption
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import (
    Album,
    AudioSource,
    Playlist,
    ProviderMapping,
    SourceQueueCapabilities,
    Track,
)

from music_assistant.providers.spotify.constants import (
    BACKEND_CONNECT,
    BACKEND_LIBRESPOT,
    LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX,
)
from music_assistant.providers.spotify.provider import SpotifyProvider
from music_assistant.providers.spotify_connect import SpotifyConnectProvider

INSTANCE_ID = "spotify--test"
USER_ID = "user1"
TARGET_PLAYER = "player1"


def _delegate_source(instance_id: str) -> AudioSource:
    """Build the plugin's queue-capable session AudioSource."""
    return AudioSource(
        item_id="main",
        provider=instance_id,
        name="Spotify Connect",
        provider_mappings={
            ProviderMapping(
                item_id="main",
                provider_domain="spotify_connect",
                provider_instance=instance_id,
            )
        },
        queue_capabilities=SourceQueueCapabilities(
            provider_domain="spotify",
            playable_media_types=[
                MediaType.TRACK,
                MediaType.ALBUM,
                MediaType.PLAYLIST,
                MediaType.ARTIST,
            ],
            enqueueable_media_types=[MediaType.TRACK],
        ),
    )


def _plugin(
    instance_id: str = "spotify_connect--a",
    *,
    bound_player: str | None = None,
    publish_name: str = "Music Assistant",
    account_id: str | None = None,
) -> MagicMock:
    """Build a Spotify Connect plugin mock exposing the redirect surface."""
    plugin = MagicMock(spec=SpotifyConnectProvider)
    plugin.available = True
    plugin.instance_id = instance_id
    plugin.configured_player_id = bound_player
    plugin.publish_name = publish_name
    plugin.session_active = True
    plugin.active_player_id = None
    plugin.audio_source = _delegate_source(instance_id)
    plugin.verified_account_id = account_id
    plugin.get_backend_account_id = AsyncMock(return_value=None)
    plugin.set_verified_account_id = Mock(
        side_effect=lambda value: setattr(plugin, "verified_account_id", value)
    )
    plugin.prepare_redirect = AsyncMock()
    plugin.play_media_on_source = AsyncMock()
    plugin.enqueue_on_source = AsyncMock()
    return plugin


def _provider(
    mode: str = BACKEND_CONNECT, plugins: list[MagicMock] | None = None
) -> SpotifyProvider:
    """Return a SpotifyProvider (bypassing __init__) in the given playback mode."""
    prov = object.__new__(SpotifyProvider)
    prov.config = MagicMock(instance_id=INSTANCE_ID)
    prov.manifest = MagicMock(domain="spotify")
    prov.logger = MagicMock()
    prov._sp_user = {"id": USER_ID, "display_name": "User One"}
    prov.get_setup_value = Mock(return_value=mode)  # type: ignore[method-assign]
    mass = MagicMock()
    mass.providers = list(plugins or [])
    plugin_map = {plugin.instance_id: plugin for plugin in plugins or []}
    mass.get_provider = Mock(
        side_effect=lambda instance, *_args, **_kwargs: plugin_map.get(instance)
    )
    prov.mass = mass
    return prov


def _track(item_id: str, instance_id: str = INSTANCE_ID) -> Track:
    """Build a track with a Spotify provider mapping."""
    return Track(
        item_id=item_id,
        provider=instance_id,
        name=item_id,
        provider_mappings={
            ProviderMapping(
                item_id=item_id, provider_domain="spotify", provider_instance=instance_id
            )
        },
    )


def _album(item_id: str = "alb1") -> Album:
    """Build an album with a Spotify provider mapping."""
    return Album(
        item_id=item_id,
        provider=INSTANCE_ID,
        name=item_id,
        provider_mappings={
            ProviderMapping(
                item_id=item_id, provider_domain="spotify", provider_instance=INSTANCE_ID
            )
        },
    )


async def test_connect_delegate_prefers_target_player_bound_instance() -> None:
    """A same-account instance bound to the target player beats the system-wide one."""
    system_wide = _plugin("spotify_connect--sys", account_id=USER_ID)
    bound = _plugin(
        "spotify_connect--bnd",
        bound_player=TARGET_PLAYER,
        publish_name="Living Room",
        account_id=USER_ID,
    )
    prov = _provider(plugins=[system_wide, bound])

    delegate = await prov.get_playback_delegate(TARGET_PLAYER)

    assert delegate is bound.audio_source


async def test_connect_delegate_skips_instances_bound_to_other_players() -> None:
    """An instance bound to another player belongs to that player and is never used."""
    other = _plugin(
        "spotify_connect--oth", bound_player="player2", publish_name="Kitchen", account_id=USER_ID
    )
    prov = _provider(plugins=[other])

    assert await prov.get_playback_delegate(TARGET_PLAYER) is None


async def test_connect_delegate_requires_account_match() -> None:
    """Sessions verified to another account are not redirect targets."""
    plugin = _plugin(account_id="someone_else")
    prov = _provider(plugins=[plugin])

    assert await prov.get_playback_delegate(TARGET_PLAYER) is None


async def test_account_verification_uses_the_backend_report() -> None:
    """A backend that reports its paired account short-circuits verification."""
    plugin = _plugin()
    plugin.get_backend_account_id = AsyncMock(return_value=USER_ID)
    prov = _provider(plugins=[plugin])

    delegate = await prov.get_playback_delegate(TARGET_PLAYER)

    assert delegate is plugin.audio_source
    plugin.set_verified_account_id.assert_called_once_with(USER_ID)


async def test_account_verification_by_device_list_name_match() -> None:
    """A single device-list name match verifies the session for this account."""
    plugin = _plugin(publish_name="Music Assistant")
    prov = _provider(plugins=[plugin])
    prov._get_player_data = AsyncMock(  # type: ignore[method-assign]
        return_value={"devices": [{"id": "dev1", "name": "Music Assistant"}]}
    )

    delegate = await prov.get_playback_delegate(TARGET_PLAYER)

    assert delegate is plugin.audio_source
    plugin.set_verified_account_id.assert_called_once_with(USER_ID)


async def test_account_verification_fails_when_device_absent() -> None:
    """A daemon that is not in this account's device list stays unverified."""
    plugin = _plugin()
    prov = _provider(plugins=[plugin])
    prov._get_player_data = AsyncMock(return_value={"devices": []})  # type: ignore[method-assign]

    assert await prov.get_playback_delegate(TARGET_PLAYER) is None
    plugin.set_verified_account_id.assert_not_called()


async def test_librespot_mode_redirects_only_into_the_active_source() -> None:
    """Librespot mode drives a same-account session already active on the target player."""
    plugin = _plugin(account_id=USER_ID)
    plugin.active_player_id = TARGET_PLAYER
    prov = _provider(mode=BACKEND_LIBRESPOT, plugins=[plugin])
    queue = MagicMock()
    queue.current_item.media_item = plugin.audio_source
    prov.mass.player_queues.get = Mock(return_value=queue)  # type: ignore[method-assign]

    assert await prov.get_playback_delegate(TARGET_PLAYER) is plugin.audio_source

    # a stale leftover source whose live session moved to another player is not
    # hijacked back; librespot streams normally
    plugin.active_player_id = "player2"
    assert await prov.get_playback_delegate(TARGET_PLAYER) is None

    # a plain track as current item: no session to drive, librespot streams normally
    plugin.active_player_id = TARGET_PLAYER
    queue.current_item.media_item = _track("t1")
    assert await prov.get_playback_delegate(TARGET_PLAYER) is None


async def test_librespot_mode_ignores_an_ended_session() -> None:
    """A leftover source of an ended session does not hijack the play request."""
    plugin = _plugin(account_id=USER_ID)
    plugin.session_active = False
    prov = _provider(mode=BACKEND_LIBRESPOT, plugins=[plugin])
    queue = MagicMock()
    queue.current_item.media_item = plugin.audio_source
    prov.mass.player_queues.get = Mock(return_value=queue)  # type: ignore[method-assign]

    assert await prov.get_playback_delegate(TARGET_PLAYER) is None


async def test_enqueue_family_coerces_to_session_enqueue() -> None:
    """ADD/NEXT/REPLACE_NEXT all become add-to-queue on the session's active queue."""
    plugin = _plugin(account_id=USER_ID)
    plugin.active_player_id = TARGET_PLAYER
    prov = _provider(plugins=[plugin])

    await prov.play_on_delegate(
        plugin.audio_source,
        [_track("t1"), _track("t2")],
        QueueOption.NEXT,
        TARGET_PLAYER,
    )

    plugin.enqueue_on_source.assert_awaited_once_with(["spotify:track:t1", "spotify:track:t2"])
    plugin.prepare_redirect.assert_not_awaited()


async def test_enqueue_family_requires_the_session_active_on_the_target() -> None:
    """Adding to the session queue needs the session playing on that player."""
    plugin = _plugin(account_id=USER_ID)
    plugin.active_player_id = "player2"
    prov = _provider(plugins=[plugin])

    with pytest.raises(AudioError):
        await prov.play_on_delegate(
            plugin.audio_source, [_track("t1")], QueueOption.ADD, TARGET_PLAYER
        )


async def test_play_uses_the_web_api_assist_with_context_and_offset() -> None:
    """A single-container play goes through the Web API with context and start offset."""
    plugin = _plugin(account_id=USER_ID)
    prov = _provider(plugins=[plugin])
    prov._get_player_data = AsyncMock(  # type: ignore[method-assign]
        return_value={"devices": [{"id": "dev1", "name": "Music Assistant"}]}
    )
    put_calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def _put(endpoint: str, data: Any = None, **kwargs: Any) -> None:
        put_calls.append((endpoint, data, kwargs))

    prov._put_data = AsyncMock(side_effect=_put)  # type: ignore[method-assign]
    start = _track("t2")

    await prov.play_on_delegate(
        plugin.audio_source,
        [_track("t1"), start],
        QueueOption.PLAY,
        TARGET_PLAYER,
        context=_album("alb1"),
        start_item=start,
    )

    plugin.prepare_redirect.assert_awaited_once_with(TARGET_PLAYER)
    endpoint, body, kwargs = put_calls[0]
    assert endpoint == "me/player/play"
    assert body == {"context_uri": "spotify:album:alb1", "offset": {"uri": "spotify:track:t2"}}
    assert kwargs == {"device_id": "dev1"}
    plugin.play_media_on_source.assert_not_awaited()


async def test_play_falls_back_to_the_session_control_plane() -> None:
    """A rejected Web API play falls back to the session's own play command."""
    plugin = _plugin(account_id=USER_ID)
    prov = _provider(plugins=[plugin])
    prov._get_player_data = AsyncMock(  # type: ignore[method-assign]
        return_value={"devices": [{"id": "dev1", "name": "Music Assistant"}]}
    )
    prov._put_data = AsyncMock(side_effect=RuntimeError("restricted"))  # type: ignore[method-assign]

    await prov.play_on_delegate(
        plugin.audio_source,
        [_track("t1"), _track("t2")],
        QueueOption.PLAY,
        TARGET_PLAYER,
    )

    plugin.play_media_on_source.assert_awaited_once_with(
        ["spotify:track:t1", "spotify:track:t2"], context_uri=None, start_uri=None
    )


async def test_play_clears_stale_verification_when_the_device_vanished() -> None:
    """A device gone from the account's list forces re-verification next time."""
    plugin = _plugin(account_id=USER_ID)
    prov = _provider(plugins=[plugin])
    prov._get_player_data = AsyncMock(return_value={"devices": []})  # type: ignore[method-assign]

    await prov.play_on_delegate(
        plugin.audio_source, [_track("t1")], QueueOption.PLAY, TARGET_PLAYER
    )

    plugin.set_verified_account_id.assert_called_once_with(None)
    plugin.play_media_on_source.assert_awaited_once()


def test_liked_songs_pseudo_playlist_is_no_spotify_context() -> None:
    """The Liked Songs pseudo-playlist falls back to the expanded track list."""
    prov = _provider()
    liked_songs_id = f"{LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX}-{INSTANCE_ID}"
    playlist = Playlist(
        item_id=liked_songs_id,
        provider=INSTANCE_ID,
        name="Liked Songs",
        provider_mappings={
            ProviderMapping(
                item_id=liked_songs_id, provider_domain="spotify", provider_instance=INSTANCE_ID
            )
        },
    )

    assert prov._delegate_context(playlist, None, []) == (None, None)


def test_delegate_context_parses_a_start_item_uri_string() -> None:
    """A start item passed as MA uri string resolves to a Spotify track uri."""
    prov = _provider()

    context_uri, start_uri = prov._delegate_context(_album("alb1"), f"{INSTANCE_ID}://track/t5", [])

    assert context_uri == "spotify:album:alb1"
    assert start_uri == "spotify:track:t5"


def test_delegate_context_resolves_a_library_start_item_via_the_batch() -> None:
    """A library start uri resolves through the resolved batch to its Spotify mapping."""
    prov = _provider()
    library_track = Track(
        item_id="12345",
        provider="library",
        name="t",
        provider_mappings={
            ProviderMapping(item_id="t7", provider_domain="spotify", provider_instance=INSTANCE_ID)
        },
    )

    context_uri, start_uri = prov._delegate_context(
        _album("alb1"), "library://track/12345", [library_track]
    )

    assert context_uri == "spotify:album:alb1"
    assert start_uri == "spotify:track:t7"


def test_delegate_context_ignores_an_unresolvable_start_item_uri() -> None:
    """A foreign start uri matching nothing in the batch yields no start offset."""
    prov = _provider()

    context_uri, start_uri = prov._delegate_context(_album("alb1"), "library://track/12345", [])

    assert context_uri == "spotify:album:alb1"
    assert start_uri is None


def test_track_uris_prefer_this_instances_mapping() -> None:
    """The own instance's mapping wins over another Spotify instance's mapping."""
    prov = _provider()
    track = Track(
        item_id="lib1",
        provider="library",
        name="t",
        provider_mappings={
            ProviderMapping(
                item_id="other", provider_domain="spotify", provider_instance="spotify--other"
            ),
            ProviderMapping(
                item_id="own", provider_domain="spotify", provider_instance=INSTANCE_ID
            ),
            ProviderMapping(
                item_id="tidal1", provider_domain="tidal", provider_instance="tidal--a"
            ),
        },
    )

    assert prov._delegate_track_uris([track]) == ["spotify:track:own"]
