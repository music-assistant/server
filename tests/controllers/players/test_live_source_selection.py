"""
Tests for starting and releasing a live external source on a player.

Selecting a source starts it on the player and leaves the player's queue alone;
selecting anything else, or deselecting, gives the source back and tells the
plugin so an upstream session stops pointing at Music Assistant.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioSource, Track
from music_assistant_models.media_items.provider_mapping import ProviderMapping
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.players import PlayerController
from music_assistant.models.plugin import PluginProvider

PLAYER_ID = "player_1"
PROVIDER_INSTANCE = "spotify_connect--abc"
SOURCE_URI = "spotify_connect--abc://audio_source/main"


def _source() -> AudioSource:
    return AudioSource(
        item_id="main",
        provider=PROVIDER_INSTANCE,
        name="Spotify Connect",
        provider_mappings={
            ProviderMapping(
                item_id="main",
                provider_domain="spotify_connect",
                provider_instance=PROVIDER_INSTANCE,
            )
        },
    )


def _controller(resolved: Any = None) -> tuple[Any, MagicMock, MagicMock]:
    """Build a controller whose music lookup returns ``resolved`` for any uri."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "INFO"
    controller = PlayerController(mass)
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = PROVIDER_INSTANCE
    provider.supported_features = {ProviderFeature.AUDIO_SOURCE}
    provider.on_source_released = AsyncMock()
    mass.get_provider.return_value = provider
    if isinstance(resolved, Exception):
        mass.music.get_item_by_uri = AsyncMock(side_effect=resolved)
    else:
        mass.music.get_item_by_uri = AsyncMock(return_value=resolved)
    player = MagicMock()
    player.player_id = PLAYER_ID
    player.display_name = "Player 1"
    player.available = True
    player.state.active_source = None
    player.state.active_group = None
    player.state.synced_to = None
    player.protocol_parent_id = None
    controller._players[PLAYER_ID] = player
    controller.get_player = MagicMock(return_value=player)  # type: ignore[method-assign]
    controller._handle_play_media = AsyncMock()  # type: ignore[method-assign]
    controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]
    controller.trigger_player_update = MagicMock()  # type: ignore[method-assign]
    return controller, provider, player


async def test_selecting_a_source_starts_it_and_names_it_on_the_player() -> None:
    """The source becomes a session on the player, and playback is started for it."""
    source = _source()
    controller, _provider, _player = _controller(source)

    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)

    session = controller.get_audio_source_session(PLAYER_ID)
    assert session is not None
    assert session.source is source
    assert session.provider_instance_id == PROVIDER_INSTANCE
    controller._handle_play_media.assert_awaited_once()
    media = controller._handle_play_media.await_args.args[1]
    assert media.media_type is MediaType.AUDIO_SOURCE
    # the session's owner, which its stream url is keyed on
    assert media.source_id == PLAYER_ID
    assert media.queue_session_id == session.playback_session_id
    # no queue item: this is what tells the stream layer it is not queue content
    assert media.queue_item_id is None


async def test_selecting_a_source_does_not_touch_the_queue() -> None:
    """The queue is not cleared, replaced or loaded — it just stops being the active source."""
    controller, _provider, _player = _controller(_source())

    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)

    controller.mass.player_queues.load.assert_not_called()
    controller.mass.player_queues.clear.assert_not_called()


async def test_selecting_another_source_releases_the_first() -> None:
    """A player plays one source at a time, and the one it leaves is handed back."""
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    controller.mass.music.get_item_by_uri = AsyncMock(side_effect=MediaNotFoundError("nope"))
    controller.mass.player_queues.get = MagicMock(return_value=MagicMock())

    await controller._handle_select_source(PLAYER_ID, PLAYER_ID)

    assert controller.get_audio_source_session(PLAYER_ID) is None
    provider.on_source_released.assert_awaited_once_with("main", PLAYER_ID)


async def test_deselecting_releases_the_source_and_stops_the_player() -> None:
    """An explicit deselect gives the source back and stops playback."""
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)

    await controller.deselect_source(PLAYER_ID)

    assert controller.get_audio_source_session(PLAYER_ID) is None
    provider.on_source_released.assert_awaited_once_with("main", PLAYER_ID)
    controller._handle_cmd_stop.assert_awaited_once()


async def test_releasing_a_player_with_nothing_playing_is_a_no_op() -> None:
    """A release for a player holding no source tells no plugin anything."""
    controller, provider, _player = _controller(None)

    await controller._release_audio_source(PLAYER_ID)

    provider.on_source_released.assert_not_awaited()


async def test_a_plugin_that_raises_does_not_block_the_player_moving_on() -> None:
    """The session is dropped even when the owning plugin fails to let go."""
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    provider.on_source_released.side_effect = OSError("daemon gone")

    await controller._release_audio_source(PLAYER_ID)

    assert controller.get_audio_source_session(PLAYER_ID) is None


async def test_a_non_source_uri_is_not_treated_as_one() -> None:
    """A uri resolving to ordinary media is left to the rest of the source handling."""
    controller, _provider, _player = _controller(
        Track(
            item_id="t1",
            provider="test",
            name="A track",
            artists=UniqueList(),
            provider_mappings={
                ProviderMapping(item_id="t1", provider_domain="test", provider_instance="test")
            },
        )
    )

    assert await controller._resolve_audio_source_uri("library://track/t1") is None


async def test_a_source_string_that_is_not_a_uri_is_not_resolved() -> None:
    """A player-native source id is not a uri, so the music lookup is never attempted."""
    controller, _provider, _player = _controller(_source())

    assert await controller._resolve_audio_source_uri("line-in") is None
    controller.mass.music.get_item_by_uri.assert_not_awaited()


async def test_an_unresolvable_uri_is_not_treated_as_a_source() -> None:
    """A uri the music controller cannot resolve falls through rather than raising."""
    controller, _provider, _player = _controller(MediaNotFoundError("gone"))

    assert await controller._resolve_audio_source_uri(SOURCE_URI) is None


async def test_a_source_whose_provider_dropped_the_feature_is_not_started() -> None:
    """A provider that no longer exposes audio sources cannot be selected from."""
    controller, provider, _player = _controller(_source())
    provider.supported_features = set()

    assert await controller._resolve_audio_source_uri(SOURCE_URI) is None
