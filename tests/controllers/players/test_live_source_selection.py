"""
Tests for starting and releasing a live external source on a player.

Selecting a source starts it on the player and leaves the player's queue alone;
selecting anything else, or deselecting, gives the source back and tells the
plugin so an upstream session stops pointing at Music Assistant.
"""

import asyncio
from contextlib import suppress
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError, PlayerCommandFailed
from music_assistant_models.media_items import AudioSource, Track
from music_assistant_models.media_items.provider_mapping import ProviderMapping
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.players import PlayerController
from music_assistant.models.player import Player, PlayerMedia
from music_assistant.models.plugin import PluginProvider

PLAYER_ID = "player_1"
PROVIDER_INSTANCE = "spotify_connect--abc"
SOURCE_URI = "spotify_connect--abc://audio_source/main"


def _source(item_id: str = "main") -> AudioSource:
    return AudioSource(
        item_id=item_id,
        provider=PROVIDER_INSTANCE,
        name="Spotify Connect",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
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
    """The source owner can give its source back and stop playback."""
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    session = controller.get_audio_source_session(PLAYER_ID)
    assert session is not None

    await controller.deselect_source(
        PLAYER_ID,
        provider_instance_id=PROVIDER_INSTANCE,
        source_id="main",
        playback_session_id=session.playback_session_id,
    )

    assert controller.get_audio_source_session(PLAYER_ID) is None
    provider.on_source_released.assert_awaited_once_with("main", PLAYER_ID)
    controller._handle_cmd_stop.assert_awaited_once()


async def test_deselecting_from_another_provider_leaves_the_source_playing() -> None:
    """A provider cannot release or stop a source session it does not own."""
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    session = controller.get_audio_source_session(PLAYER_ID)
    assert session is not None

    await controller.deselect_source(
        PLAYER_ID,
        provider_instance_id="airplay_receiver--xyz",
        source_id="main",
        playback_session_id=session.playback_session_id,
    )

    assert controller.get_audio_source_session(PLAYER_ID) is session
    provider.on_source_released.assert_not_awaited()
    controller._handle_cmd_stop.assert_not_awaited()


async def test_deselecting_without_an_owned_session_does_not_stop_the_player() -> None:
    """A provider cannot stop a player when it owns no source session."""
    controller, provider, _player = _controller(None)

    await controller.deselect_source(
        PLAYER_ID,
        provider_instance_id=PROVIDER_INSTANCE,
        source_id="main",
        playback_session_id="stale-session",
    )

    provider.on_source_released.assert_not_awaited()
    controller._handle_cmd_stop.assert_not_awaited()


async def test_a_provider_release_without_a_playback_session_is_rejected() -> None:
    """Provider cleanup without a captured playback generation is not authoritative."""
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    session = controller.get_audio_source_session(PLAYER_ID)

    await controller.deselect_source(
        PLAYER_ID,
        provider_instance_id=PROVIDER_INSTANCE,
        source_id="main",
    )

    assert controller.get_audio_source_session(PLAYER_ID) is session
    provider.on_source_released.assert_not_awaited()
    controller._handle_cmd_stop.assert_not_awaited()


async def test_an_unexpected_stop_failure_still_releases_the_source() -> None:
    """Source cleanup completes before an unexpected stop error propagates."""
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    session = controller.get_audio_source_session(PLAYER_ID)
    assert session is not None
    controller._handle_cmd_stop.side_effect = OSError("transport failed")

    with pytest.raises(OSError, match="transport failed"):
        await controller.deselect_source(
            PLAYER_ID,
            provider_instance_id=PROVIDER_INSTANCE,
            source_id="main",
            playback_session_id=session.playback_session_id,
        )

    assert controller.get_audio_source_session(PLAYER_ID) is None
    provider.on_source_released.assert_awaited_once_with("main", PLAYER_ID)


async def test_a_replacement_source_during_release_is_not_stopped() -> None:
    """A source taking over during release remains active and playing."""
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    original_session = controller.get_audio_source_session(PLAYER_ID)
    assert original_session is not None
    replacement_instance = "airplay_receiver--xyz"
    replacement = AudioSource(
        item_id="receiver",
        provider=replacement_instance,
        name="AirPlay",
        provider_mappings={
            ProviderMapping(
                item_id="receiver",
                provider_domain="airplay_receiver",
                provider_instance=replacement_instance,
            )
        },
    )

    async def start_replacement(_source_id: str, _player_id: str) -> None:
        controller._start_audio_source_session(PLAYER_ID, replacement, replacement_instance)

    provider.on_source_released.side_effect = start_replacement

    await controller.deselect_source(
        PLAYER_ID,
        provider_instance_id=PROVIDER_INSTANCE,
        source_id="main",
        playback_session_id=original_session.playback_session_id,
    )

    session = controller.get_audio_source_session(PLAYER_ID)
    assert session is not None
    assert session.source is replacement
    controller._handle_cmd_stop.assert_awaited_once()


async def test_deselecting_another_source_from_the_same_provider_is_rejected() -> None:
    """A provider cannot release a different source session from the one that ended."""
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    original_session = controller.get_audio_source_session(PLAYER_ID)
    assert original_session is not None
    replacement = _source("other")
    session = controller._start_audio_source_session(PLAYER_ID, replacement, PROVIDER_INSTANCE)

    await controller.deselect_source(
        PLAYER_ID,
        provider_instance_id=PROVIDER_INSTANCE,
        source_id="main",
        playback_session_id=original_session.playback_session_id,
    )

    assert controller.get_audio_source_session(PLAYER_ID) is session
    provider.on_source_released.assert_not_awaited()
    controller._handle_cmd_stop.assert_not_awaited()


async def test_deselecting_finishes_before_new_playback_starts() -> None:
    """Release and stop complete before any player playback entry point starts."""
    controller, provider, player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    session = controller.get_audio_source_session(PLAYER_ID)
    assert session is not None
    release_started = asyncio.Event()
    finish_release = asyncio.Event()
    events: list[str] = []

    async def release_source(_source_id: str, _player_id: str) -> None:
        release_started.set()
        await finish_release.wait()

    async def stop_player(_player_id: str) -> None:
        events.append("stop")

    async def play_media(_player_id: str, _media: PlayerMedia) -> None:
        events.append("play")

    async def select_source(_player_id: str, _source_id: str | None) -> None:
        events.append("select")

    async def play(_player_id: str) -> None:
        events.append("play_command")

    async def resume(
        _player_id: str,
        _source_id: str | None,
        _media: PlayerMedia | None,
    ) -> None:
        events.append("resume")

    provider.on_source_released.side_effect = release_source
    controller._handle_cmd_stop.side_effect = stop_player
    controller._handle_play_media = AsyncMock(side_effect=play_media)
    controller._handle_select_source = AsyncMock(side_effect=select_source)
    controller._handle_cmd_play = AsyncMock(side_effect=play)
    controller._handle_cmd_resume = AsyncMock(side_effect=resume)
    player.state.playback_state = PlaybackState.IDLE
    controller.mass.player_queues.get.return_value = None

    release_task = asyncio.create_task(
        controller.deselect_source(
            PLAYER_ID,
            provider_instance_id=PROVIDER_INSTANCE,
            source_id="main",
            playback_session_id=session.playback_session_id,
        )
    )
    await release_started.wait()
    assert events == ["stop"]
    play_task = asyncio.create_task(
        controller.play_media(
            PLAYER_ID,
            PlayerMedia(uri="library://track/1", media_type=MediaType.TRACK),
        )
    )
    select_task = asyncio.create_task(controller.select_source(PLAYER_ID, PLAYER_ID))
    play_command_task = asyncio.create_task(controller.cmd_play(PLAYER_ID))
    resume_task = asyncio.create_task(controller.cmd_resume(PLAYER_ID))
    await asyncio.sleep(0)
    playback_waited = events == ["stop"]

    finish_release.set()
    await asyncio.gather(
        release_task,
        play_task,
        select_task,
        play_command_task,
        resume_task,
    )

    assert playback_waited
    assert events[0] == "stop"
    assert set(events[1:]) == {"play", "play_command", "resume", "select"}


async def test_playback_starting_during_release_callback_is_not_stopped() -> None:
    """Playback bypassing the lock during plugin release sees no later stale stop."""
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    session = controller.get_audio_source_session(PLAYER_ID)
    assert session is not None
    release_started = asyncio.Event()
    finish_release = asyncio.Event()
    events: list[str] = []

    async def release_source(_source_id: str, _player_id: str) -> None:
        release_started.set()
        await finish_release.wait()

    async def stop_player(_player_id: str) -> None:
        events.append("stop")

    async def play_media(_player_id: str, _media: PlayerMedia) -> None:
        events.append("play")

    provider.on_source_released.side_effect = release_source
    controller._handle_cmd_stop.side_effect = stop_player
    controller._handle_play_media = AsyncMock(side_effect=play_media)

    release_task = asyncio.create_task(
        controller.deselect_source(
            PLAYER_ID,
            provider_instance_id=PROVIDER_INSTANCE,
            source_id="main",
            playback_session_id=session.playback_session_id,
        )
    )
    await release_started.wait()
    await controller._handle_play_media(
        PLAYER_ID,
        PlayerMedia(uri="library://track/1", media_type=MediaType.TRACK),
    )
    finish_release.set()
    await release_task

    assert events == ["stop", "play"]


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


async def test_unregistering_a_player_releases_its_source() -> None:
    """
    A player going away hands its source back.

    Otherwise the session outlives the player, the plugin is never told, and an
    upstream session stays pointed at a player that no longer exists.
    """
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    assert controller.get_audio_source_session(PLAYER_ID) is not None

    await controller.unregister(PLAYER_ID)

    assert controller.get_audio_source_session(PLAYER_ID) is None
    provider.on_source_released.assert_awaited_once_with("main", PLAYER_ID)


async def test_refreshing_a_rebuilt_source_reaches_the_session() -> None:
    """A plugin that rebuilds its source has the new capability flags published."""
    controller, _provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    session = controller.get_audio_source_session(PLAYER_ID)
    assert session is not None
    assert session.source.can_seek is False

    rebuilt = AudioSource(
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
        can_seek=True,
    )
    controller.refresh_source(PLAYER_ID, rebuilt)

    assert session.source is rebuilt
    assert session.source.can_seek is True


async def test_refreshing_with_another_providers_source_is_rejected() -> None:
    """A provider cannot publish its object onto a session it does not own."""
    controller, _provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    session = controller.get_audio_source_session(PLAYER_ID)
    assert session is not None
    original = session.source

    controller.refresh_source(
        PLAYER_ID,
        AudioSource(
            item_id="main",
            provider="airplay_receiver--xyz",
            name="AirPlay",
            provider_mappings=set(),
        ),
    )

    assert session.source is original


async def test_swapping_one_source_for_another_hands_the_first_back() -> None:
    """
    A player switching between two live sources tells the first one's plugin.

    Otherwise the displaced plugin keeps an upstream session pointed at a player it
    no longer has, and the session it holds is simply overwritten in silence.
    """
    controller, first_provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)

    other_instance = "airplay_receiver--xyz"
    other_provider = MagicMock(spec=PluginProvider)
    other_provider.instance_id = other_instance
    other_provider.supported_features = {ProviderFeature.AUDIO_SOURCE}
    other_provider.on_source_released = AsyncMock()
    providers = {PROVIDER_INSTANCE: first_provider, other_instance: other_provider}
    controller.mass.get_provider = MagicMock(side_effect=lambda key: providers.get(key))
    other_source = AudioSource(
        item_id="receiver",
        provider=other_instance,
        name="AirPlay",
        provider_mappings={
            ProviderMapping(
                item_id="receiver",
                provider_domain="airplay_receiver",
                provider_instance=other_instance,
            )
        },
    )
    controller.mass.music.get_item_by_uri = AsyncMock(return_value=other_source)

    await controller._handle_select_source(PLAYER_ID, f"{other_instance}://audio_source/receiver")

    first_provider.on_source_released.assert_awaited_once_with("main", PLAYER_ID)
    other_provider.on_source_released.assert_not_awaited()
    session = controller.get_audio_source_session(PLAYER_ID)
    assert session is not None
    assert session.provider_instance_id == other_instance


async def test_reselecting_the_same_source_does_not_hand_it_back() -> None:
    """A player reconnecting to the source it already has keeps its session."""
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    first = controller.get_audio_source_session(PLAYER_ID)
    assert first is not None
    first_playback_session_id = first.playback_session_id

    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)

    provider.on_source_released.assert_not_awaited()
    assert controller.get_audio_source_session(PLAYER_ID) is first
    assert first.playback_session_id != first_playback_session_id


async def test_a_stale_release_cannot_end_a_reselected_source() -> None:
    """A delayed cleanup cannot release a new selection of the same source."""
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    session = controller.get_audio_source_session(PLAYER_ID)
    assert session is not None
    stale_playback_session_id = session.playback_session_id

    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    await controller.deselect_source(
        PLAYER_ID,
        provider_instance_id=PROVIDER_INSTANCE,
        source_id="main",
        playback_session_id=stale_playback_session_id,
    )

    assert controller.get_audio_source_session(PLAYER_ID) is session
    provider.on_source_released.assert_not_awaited()
    controller._handle_cmd_stop.assert_not_awaited()


async def test_a_source_that_fails_to_start_is_not_left_on_the_player() -> None:
    """
    A failed play command rolls the session back.

    A session left behind would have the player publish a source that never started,
    and its queue stays inactive with nothing playing it — unreachable from play.
    """
    controller, provider, _player = _controller(_source())
    controller._handle_play_media = AsyncMock(side_effect=PlayerCommandFailed("no route"))

    with pytest.raises(PlayerCommandFailed):
        await controller._handle_select_source(PLAYER_ID, SOURCE_URI)

    assert controller.get_audio_source_session(PLAYER_ID) is None
    provider.on_source_released.assert_awaited_once_with("main", PLAYER_ID)


async def test_an_announcement_does_not_take_the_source_off_the_player() -> None:
    """
    An announcement interrupts the player without ending the source session.

    The player is handed straight back afterwards, and a released source cannot be
    re-selected: its plugin has let go of the upstream session by then.
    """
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    session = controller.get_audio_source_session(PLAYER_ID)
    controller._handle_play_media = PlayerController._handle_play_media.__get__(controller)
    controller.get_player = MagicMock(return_value=_player)
    _player.play_media = AsyncMock()

    with suppress(Exception):
        await controller._handle_play_media(
            PLAYER_ID,
            PlayerMedia(uri="http://x/announce.mp3", media_type=MediaType.ANNOUNCEMENT),
        )

    assert controller.get_audio_source_session(PLAYER_ID) is session
    provider.on_source_released.assert_not_awaited()


async def test_ordinary_media_does_take_the_source_off_the_player() -> None:
    """Anything that is not transient ends the session, so the source is handed back."""
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    controller._handle_play_media = PlayerController._handle_play_media.__get__(controller)
    controller.get_player = MagicMock(return_value=_player)
    _player.play_media = AsyncMock()

    with suppress(Exception):
        await controller._handle_play_media(
            PLAYER_ID,
            PlayerMedia(uri="library://track/1", media_type=MediaType.TRACK),
        )

    assert controller.get_audio_source_session(PLAYER_ID) is None
    provider.on_source_released.assert_awaited_once_with("main", PLAYER_ID)


async def test_a_plugin_unloading_releases_the_sources_it_owns() -> None:
    """
    A plugin going away takes its sources off the players playing them.

    A session outliving its provider leaves the player naming a source that can no
    longer be streamed, and the queue behind it stays inactive.
    """
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    assert controller.get_audio_source_session(PLAYER_ID) is not None

    await controller.release_provider_sources(PROVIDER_INSTANCE)

    assert controller.get_audio_source_session(PLAYER_ID) is None
    provider.on_source_released.assert_awaited_once_with("main", PLAYER_ID)


async def test_another_plugin_unloading_leaves_the_session_alone() -> None:
    """Only the sources of the plugin that is going away are given back."""
    controller, provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    session = controller.get_audio_source_session(PLAYER_ID)

    await controller.release_provider_sources("some_other_provider--1")

    assert controller.get_audio_source_session(PLAYER_ID) is session
    provider.on_source_released.assert_not_awaited()


async def test_the_reported_media_can_be_handed_back_to_the_player() -> None:
    """
    What the player reports it is playing carries the session token.

    The announcement restore hands this object straight back to the player, and the
    stream url cannot be resolved without the token the session is keyed on.
    """
    controller, _provider, _player = _controller(_source())
    await controller._handle_select_source(PLAYER_ID, SOURCE_URI)
    session = controller.get_audio_source_session(PLAYER_ID)
    assert session is not None

    media = controller._handle_play_media.await_args.args[1]
    reported = Player._Player__audio_source_media(_player, session)  # type: ignore[attr-defined]

    assert reported.queue_session_id == session.playback_session_id
    assert reported.source_id == media.source_id
