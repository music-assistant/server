"""
Tests for routing player commands to whatever is producing the audio.

A player command applies to what the player is actually playing: a live external
source handles it in its own session, and Music Assistant's queue handles it for
its own items. This replaces the queue-delegation tests, which covered the same
forwarding while a live source was a queue item.
"""

import time
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import (
    PlaybackState,
    PlayerFeature,
    ProviderFeature,
    RepeatMode,
    SourceControl,
)
from music_assistant_models.errors import (
    InvalidCommand,
    PlayerCommandFailed,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import AudioSource
from music_assistant_models.media_items.provider_mapping import ProviderMapping
from music_assistant_models.player import PlayerSoundMode, PlayerSource

from music_assistant.controllers.player_queues.constants import SKIP_END_MARGIN
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.audio_sources import AudioSourceSession
from music_assistant.models.player import PlayerMedia
from music_assistant.models.plugin import PluginProvider
from tests.common import MockProtocol, MockProvider, attach_mock

PLAYER_ID = "player_1"
PROVIDER_INSTANCE = "spotify_connect--abc"
# a source the player's own device runs, so Music Assistant has no session for it
NATIVE_SOURCE_ID = "spotify"


def _source(
    *,
    can_play_pause: bool = False,
    can_seek: bool = False,
    can_next_previous: bool = False,
) -> AudioSource:
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
        can_play_pause=can_play_pause,
        can_seek=can_seek,
        can_next_previous=can_next_previous,
    )


def _controller(source: AudioSource | None) -> tuple[Any, MagicMock]:
    """Build a controller with (or without) a live source on PLAYER_ID."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "INFO"
    controller = PlayerController(mass)
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = PROVIDER_INSTANCE
    provider.supported_features = {ProviderFeature.AUDIO_SOURCE}
    provider.on_source_control = AsyncMock()
    mass.get_provider.return_value = provider
    if source is not None:
        controller._source_sessions[PLAYER_ID] = AudioSourceSession(
            player_id=PLAYER_ID,
            source=source,
            provider_instance_id=PROVIDER_INSTANCE,
        )
    player = MagicMock()
    player.player_id = PLAYER_ID
    player.name = "Player 1"
    player.available = True
    player.offline_by_power_control = False
    player.synced_to = None
    player.active_group = None
    controller.get_player = MagicMock(return_value=player)  # type: ignore[method-assign]
    # the command decorator looks the player up in the registry, not via get_player
    controller._players[PLAYER_ID] = player
    return controller, provider


async def test_a_capable_source_takes_the_command() -> None:
    """The source is asked to seek within its own session."""
    controller, provider = _controller(_source(can_seek=True))

    handled = await controller._forward_to_external_source(
        controller.get_player(PLAYER_ID), SourceControl.SEEK, 42
    )

    assert handled is True
    provider.on_source_control.assert_awaited_once_with("main", SourceControl.SEEK, 42)


async def test_a_source_that_cannot_do_it_refuses_rather_than_forwarding() -> None:
    """A client is told no instead of being left waiting on a command nothing handles."""
    controller, provider = _controller(_source(can_seek=False))

    with pytest.raises(PlayerCommandFailed, match="does not support this action"):
        await controller._forward_to_external_source(
            controller.get_player(PLAYER_ID), SourceControl.SEEK, 42
        )

    provider.on_source_control.assert_not_awaited()


@pytest.mark.parametrize(
    ("action", "flag"),
    [
        (SourceControl.PLAY, "can_play_pause"),
        (SourceControl.PAUSE, "can_play_pause"),
        (SourceControl.NEXT, "can_next_previous"),
        (SourceControl.PREVIOUS, "can_next_previous"),
        (SourceControl.SEEK, "can_seek"),
    ],
)
async def test_each_transport_action_is_gated_on_its_own_flag(
    action: SourceControl, flag: str
) -> None:
    """Every transport action is gated by the capability that describes it."""
    controller, _provider = _controller(_source(**{flag: True}))
    assert await controller._forward_to_external_source(controller.get_player(PLAYER_ID), action)

    controller, _provider = _controller(_source(**{flag: False}))
    with pytest.raises(PlayerCommandFailed):
        await controller._forward_to_external_source(controller.get_player(PLAYER_ID), action)


@pytest.mark.parametrize("action", [SourceControl.SHUFFLE, SourceControl.REPEAT])
async def test_ordering_is_not_gated_by_a_capability_flag(action: SourceControl) -> None:
    """
    Shuffle and repeat go to the session unconditionally.

    Only the session knows whether its content can be reordered, and it refuses in
    its own words — a flag here would second-guess it.
    """
    controller, provider = _controller(_source())

    assert await controller._forward_to_external_source(
        controller.get_player(PLAYER_ID), action, True
    )

    provider.on_source_control.assert_awaited_once_with("main", action, True)


async def test_nothing_is_forwarded_when_no_source_is_playing() -> None:
    """With no live source the caller is told to look elsewhere, not refused."""
    controller, provider = _controller(None)

    handled = await controller._forward_to_external_source(
        controller.get_player(PLAYER_ID), SourceControl.SEEK, 42
    )

    assert handled is False
    provider.on_source_control.assert_not_awaited()


async def test_a_gone_provider_is_not_forwarded_to() -> None:
    """A session whose plugin has unloaded forwards nothing rather than raising."""
    controller, _provider = _controller(_source(can_seek=True))
    controller.mass.get_provider.return_value = None

    assert (
        await controller._forward_to_external_source(
            controller.get_player(PLAYER_ID), SourceControl.SEEK, 42
        )
        is False
    )


async def test_a_group_member_is_playing_its_groups_source() -> None:
    """A member hearing its group's audio resolves to the group's source, not its own."""
    controller, provider = _controller(None)
    group_source = _source(can_seek=True)
    controller._source_sessions["group_1"] = AudioSourceSession(
        player_id="group_1",
        source=group_source,
        provider_instance_id=PROVIDER_INSTANCE,
    )
    member = MagicMock()
    member.player_id = "member_1"
    member.name = "Member"
    member.synced_to = None
    member.active_group = "group_1"
    group = MagicMock()
    group.player_id = "group_1"
    group.synced_to = None
    group.active_group = None
    controller.get_player = MagicMock(
        side_effect=lambda pid, *_a, **_k: {"member_1": member, "group_1": group}.get(pid)
    )

    assert await controller._forward_to_external_source(member, SourceControl.SEEK, 7)

    provider.on_source_control.assert_awaited_once_with("main", SourceControl.SEEK, 7)


async def test_shuffle_falls_through_to_the_queue_when_no_source_is_playing() -> None:
    """Without a live source, shuffle is the Music Assistant queue's business."""
    controller, provider = _controller(None)
    controller._get_player_with_redirect = MagicMock(return_value=controller.get_player(PLAYER_ID))
    queue = MagicMock()
    queue.queue_id = PLAYER_ID
    controller.get_active_queue = MagicMock(return_value=queue)
    controller.mass.player_queues.set_shuffle = AsyncMock()

    await controller.cmd_shuffle(PLAYER_ID, shuffle_enabled=True)

    controller.mass.player_queues.set_shuffle.assert_awaited_once_with(PLAYER_ID, True)
    provider.on_source_control.assert_not_awaited()


async def test_repeat_reaches_the_live_source_before_the_queue() -> None:
    """A live source is what is playing, so it gets the command and the queue does not."""
    controller, provider = _controller(_source())
    controller._get_player_with_redirect = MagicMock(return_value=controller.get_player(PLAYER_ID))
    controller.get_active_queue = MagicMock(return_value=MagicMock())
    controller.mass.player_queues.set_repeat = AsyncMock()

    await controller.cmd_repeat(PLAYER_ID, RepeatMode.ALL)

    provider.on_source_control.assert_awaited_once_with(
        "main", SourceControl.REPEAT, RepeatMode.ALL
    )
    controller.mass.player_queues.set_repeat.assert_not_awaited()


def _skip_controller(
    source: AudioSource, elapsed: float | None, duration: int | None = None
) -> tuple[Any, MagicMock]:
    """Build a controller whose player plays the live source at the given position."""
    controller, provider = _controller(source)
    player = controller.get_player(PLAYER_ID)
    player.corrected_elapsed_time = elapsed
    player.current_media = (
        None if duration is None else PlayerMedia(uri="spotify:track:1", duration=duration)
    )
    return controller, provider


async def test_a_skip_reaches_the_live_source_as_a_seek_from_where_it_plays() -> None:
    """A live source is what is playing, so it seeks and the queue does not skip."""
    controller, provider = _skip_controller(_source(can_seek=True), elapsed=30.6, duration=200)
    controller.get_active_queue = MagicMock(return_value=MagicMock())
    controller.mass.player_queues.skip = AsyncMock()

    await controller.cmd_skip(PLAYER_ID, 15)

    provider.on_source_control.assert_awaited_once_with("main", SourceControl.SEEK, 45)
    controller.mass.player_queues.skip.assert_not_awaited()


async def test_a_skip_back_past_the_start_lands_on_the_start() -> None:
    """Skipping back further than the source has played seeks to its start."""
    controller, provider = _skip_controller(_source(can_seek=True), elapsed=5, duration=200)

    await controller.cmd_skip(PLAYER_ID, -10)

    provider.on_source_control.assert_awaited_once_with("main", SourceControl.SEEK, 0)


async def test_a_skip_forward_past_the_end_stops_short_of_it() -> None:
    """A skip never lands on the end of media with a known duration, so it ends on its own."""
    controller, provider = _skip_controller(_source(can_seek=True), elapsed=190, duration=200)

    await controller.cmd_skip(PLAYER_ID, 30)

    provider.on_source_control.assert_awaited_once_with(
        "main", SourceControl.SEEK, 200 - SKIP_END_MARGIN
    )


async def test_a_skip_in_media_of_unknown_length_is_not_held_back() -> None:
    """Without a duration there is no end to stay clear of."""
    controller, provider = _skip_controller(_source(can_seek=True), elapsed=190)

    await controller.cmd_skip(PLAYER_ID, 30)

    provider.on_source_control.assert_awaited_once_with("main", SourceControl.SEEK, 220)


async def test_a_skip_without_a_known_position_is_refused() -> None:
    """A skip has nowhere to count from while the position is unknown, so nothing seeks."""
    controller, provider = _skip_controller(_source(can_seek=True), elapsed=None, duration=200)

    with pytest.raises(PlayerCommandFailed) as error:
        await controller.cmd_skip(PLAYER_ID, 30)

    assert error.value.translation_key == "source_control_unsupported"
    provider.on_source_control.assert_not_awaited()


async def test_a_skip_in_a_source_that_cannot_seek_is_refused_like_a_seek() -> None:
    """A source that does not seek refuses a skip in the same words as a seek."""
    controller, provider = _skip_controller(_source(can_seek=False), elapsed=30, duration=200)

    with pytest.raises(PlayerCommandFailed, match="does not support this action") as error:
        await controller.cmd_skip(PLAYER_ID, 15)

    assert error.value.translation_key == "source_control_unsupported"
    provider.on_source_control.assert_not_awaited()


async def test_a_source_with_no_control_surface_refuses_cleanly() -> None:
    """
    A source that implements no controls at all is a refusal, not a server error.

    vban_receiver has no on_source_control, so the call reaches the base
    implementation and raises NotImplementedError. Ordering is not gated here, so
    that path is reachable and a caller should get a refusal it can render.
    """
    controller, provider = _controller(_source())
    provider.on_source_control = AsyncMock(side_effect=NotImplementedError)

    with pytest.raises(PlayerCommandFailed, match="can not be controlled"):
        await controller._forward_to_external_source(
            controller.get_player(PLAYER_ID), SourceControl.SHUFFLE, True
        )


async def test_an_unknown_repeat_mode_is_refused_before_it_reaches_a_source() -> None:
    """
    UNKNOWN is what a source reports when it cannot say, not a mode to set.

    Forwarding it asks a plugin to apply a non-mode: soloist raises a bare ValueError
    on it, and other providers would silently accept and do nothing.
    """
    controller, provider = _controller(_source())
    controller._get_player_with_redirect = MagicMock(return_value=controller.get_player(PLAYER_ID))

    with pytest.raises(InvalidCommand, match="unknown repeat mode"):
        await controller.cmd_repeat(PLAYER_ID, RepeatMode.UNKNOWN)

    provider.on_source_control.assert_not_awaited()


class _NativeSourceProtocol(MockProtocol):
    """The protocol of a device that runs a source of its own and orders that source itself."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.shuffle_calls: list[bool] = []
        self.repeat_calls: list[RepeatMode] = []
        self.seek_calls: list[int] = []

    async def seek(self, position: int) -> None:
        self.seek_calls.append(position)

    async def set_shuffle(self, shuffle_enabled: bool) -> None:
        self.shuffle_calls.append(shuffle_enabled)

    async def set_repeat(self, repeat_mode: RepeatMode) -> None:
        self.repeat_calls.append(repeat_mode)


def _native_source_controller(
    *,
    can_shuffle: bool = False,
    can_repeat: bool = False,
    active_source: str = NATIVE_SOURCE_ID,
    queue: MagicMock | None = None,
) -> tuple[PlayerController, _NativeSourceProtocol]:
    """
    Build a controller whose player is playing a source its own device runs.

    Returns the controller and the protocol of the device.
    """
    mass = MagicMock()
    mass.closing = False
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    mass.config.get = MagicMock(return_value=[])
    mass.signal_event = MagicMock()
    controller = PlayerController(mass)
    mass.players = controller
    mass.player_queues = MagicMock()
    mass.player_queues.get = MagicMock(return_value=queue)
    provider = MockProvider("test_provider", instance_id="test", mass=mass)
    player, protocol = attach_mock(
        provider,
        "device_1",
        "Player 1",
        player_id=PLAYER_ID,
        protocol_class=_NativeSourceProtocol,
    )
    protocol._attr_source_list = [
        PlayerSource(
            id=NATIVE_SOURCE_ID,
            name="Spotify",
            can_shuffle=can_shuffle,
            can_repeat=can_repeat,
        )
    ]
    protocol._attr_active_source = active_source
    # a device only counts as playing its own source while it is not idle
    protocol._attr_playback_state = PlaybackState.PLAYING
    player.update_state(signal_event=False)
    return controller, cast("_NativeSourceProtocol", protocol)


async def test_a_device_native_source_orders_its_own_content() -> None:
    """A source the device runs itself has no session, so the player is asked directly."""
    controller, protocol = _native_source_controller(can_shuffle=True, can_repeat=True)

    await controller.cmd_shuffle(PLAYER_ID, shuffle_enabled=True)
    await controller.cmd_repeat(PLAYER_ID, RepeatMode.ONE)

    assert protocol.shuffle_calls == [True]
    assert protocol.repeat_calls == [RepeatMode.ONE]


@pytest.mark.parametrize(
    ("command", "kwargs"),
    [("cmd_shuffle", {"shuffle_enabled": True}), ("cmd_repeat", {"repeat_mode": RepeatMode.ALL})],
)
async def test_a_device_native_source_without_the_capability_is_refused(
    command: str, kwargs: dict[str, Any]
) -> None:
    """A source that does not claim to order its own content is told no, not left waiting."""
    controller, protocol = _native_source_controller()

    with pytest.raises(PlayerCommandFailed, match="unavailable for this source") as error:
        await getattr(controller, command)(PLAYER_ID, **kwargs)

    assert error.value.translation_key == "source_control_unsupported"
    assert error.value.translation_args == ["Player 1"]
    assert not protocol.shuffle_calls
    assert not protocol.repeat_calls


async def test_a_command_aimed_at_a_source_that_stopped_is_refused() -> None:
    """
    Naming the source keeps a command off whatever took the player since.

    A client builds its shuffle control against the source it is showing. If that
    source ends before the click, Music Assistant's queue takes the player back -
    and the setting would surprise the user whenever that queue next resumes.
    """
    queue = MagicMock()
    queue.queue_id = PLAYER_ID
    controller, protocol = _native_source_controller(
        can_shuffle=True, active_source=PLAYER_ID, queue=queue
    )
    controller.mass.player_queues.set_shuffle = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(PlayerCommandFailed, match="no longer playing") as error:
        await controller.cmd_shuffle(PLAYER_ID, shuffle_enabled=True, source_id=NATIVE_SOURCE_ID)

    assert error.value.translation_key == "source_no_longer_playing"
    assert error.value.translation_args == ["Player 1"]
    controller.mass.player_queues.set_shuffle.assert_not_awaited()
    assert not protocol.shuffle_calls


async def test_a_source_the_device_does_not_have_is_refused_by_its_name() -> None:
    """Selecting a source that is not in the list of the device says which one it lacks."""
    controller, protocol = _native_source_controller()
    protocol._attr_supported_features.add(PlayerFeature.SELECT_SOURCE)
    # an idle device has nothing to stop before the source changes
    protocol._attr_playback_state = PlaybackState.IDLE
    controller.get_player(PLAYER_ID).update_state(signal_event=False)  # type: ignore[union-attr]

    with pytest.raises(PlayerCommandFailed, match="invalid source") as error:
        await controller.select_source(PLAYER_ID, "HDMI 2")

    assert error.value.translation_key == "source_not_found"
    assert error.value.translation_args == ["Player 1", "HDMI 2"]


async def test_a_sound_mode_the_device_does_not_have_is_refused_by_its_name() -> None:
    """Selecting a sound mode that is not in the list of the device says which one it lacks."""
    controller, protocol = _native_source_controller()
    protocol._attr_supported_features.add(PlayerFeature.SELECT_SOUND_MODE)
    protocol._attr_sound_mode_list = [PlayerSoundMode(id="music", name="Music")]
    controller.get_player(PLAYER_ID).update_state(signal_event=False)  # type: ignore[union-attr]

    with pytest.raises(PlayerCommandFailed, match="invalid sound_mode") as error:
        await controller.select_sound_mode(PLAYER_ID, "movie")

    assert error.value.translation_key == "sound_mode_not_found"
    assert error.value.translation_args == ["Player 1", "movie"]


async def test_a_device_that_can_not_seek_says_so() -> None:
    """A seek in a source that allows it, on a device that can not seek, names the device."""
    controller, protocol = _native_source_controller()
    protocol._attr_source_list = [PlayerSource(id=NATIVE_SOURCE_ID, name="Spotify", can_seek=True)]
    controller.get_player(PLAYER_ID).update_state(signal_event=False)  # type: ignore[union-attr]

    with pytest.raises(UnsupportedFeaturedException) as error:
        await controller.cmd_seek(PLAYER_ID, 10)

    assert error.value.translation_key == "player_cannot_seek"
    assert error.value.translation_args == ["Player 1"]


async def test_a_skip_in_a_device_native_source_seeks_the_device_from_where_it_plays() -> None:
    """A source the device runs itself has no session, so the device seeks."""
    controller, protocol = _native_source_controller()
    protocol._attr_source_list = [PlayerSource(id=NATIVE_SOURCE_ID, name="Spotify", can_seek=True)]
    protocol._attr_supported_features.add(PlayerFeature.SEEK)
    protocol._attr_current_media = PlayerMedia(uri="spotify:track:1", duration=200)
    protocol._attr_elapsed_time = 30
    protocol._attr_elapsed_time_last_updated = time.time()
    controller.get_player(PLAYER_ID).update_state(signal_event=False)  # type: ignore[union-attr]

    await controller.cmd_skip(PLAYER_ID, 15)

    assert protocol.seek_calls == [45]


@pytest.mark.parametrize(
    ("command", "args", "translation_key"),
    [
        ("cmd_next_track", (), "player_cannot_skip_tracks"),
        ("cmd_previous_track", (), "player_cannot_skip_tracks"),
        ("select_source", ("HDMI 2",), "player_no_sources"),
        ("select_sound_mode", ("movie",), "player_no_sound_modes"),
    ],
)
async def test_a_command_the_device_has_no_ability_for_says_what_it_lacks(
    command: str, args: tuple[Any, ...], translation_key: str
) -> None:
    """A device without the ability a command needs is refused with the ability it lacks."""
    controller, protocol = _native_source_controller()
    # an idle device without sources of its own: nothing to stop and nothing to choose from
    protocol._attr_source_list = []
    protocol._attr_playback_state = PlaybackState.IDLE
    controller.get_player(PLAYER_ID).update_state(signal_event=False)  # type: ignore[union-attr]

    with pytest.raises(UnsupportedFeaturedException) as error:
        await getattr(controller, command)(PLAYER_ID, *args)

    assert error.value.translation_key == translation_key
    assert error.value.translation_args == ["Player 1"]


async def test_a_command_aimed_at_the_queue_still_reaches_it() -> None:
    """Naming the source it is showing does not stand in a client's way."""
    queue = MagicMock()
    queue.queue_id = PLAYER_ID
    controller, _player = _native_source_controller(active_source=PLAYER_ID, queue=queue)
    controller.mass.player_queues.set_shuffle = AsyncMock()  # type: ignore[method-assign]

    await controller.cmd_shuffle(PLAYER_ID, shuffle_enabled=True, source_id=PLAYER_ID)

    controller.mass.player_queues.set_shuffle.assert_awaited_once_with(PLAYER_ID, True)


async def test_a_command_aimed_at_the_source_still_playing_is_delivered() -> None:
    """The source named is the one playing, so the command goes through as usual."""
    controller, protocol = _native_source_controller(can_repeat=True)

    await controller.cmd_repeat(PLAYER_ID, RepeatMode.ALL, source_id=NATIVE_SOURCE_ID)

    assert protocol.repeat_calls == [RepeatMode.ALL]


async def test_a_command_aimed_at_a_live_source_reaches_its_session() -> None:
    """
    Naming a live source does not stand in the way of reaching its session.

    The target is checked before anything is dispatched, so what a client reads as
    the player's active source has to be the same string the session publishes.
    """
    source = _source()
    controller, provider = _controller(source)
    player = controller.get_player(PLAYER_ID)
    player.active_source = source.uri
    controller._get_player_with_redirect = MagicMock(return_value=player)

    await controller.cmd_shuffle(PLAYER_ID, shuffle_enabled=True, source_id=source.uri)

    provider.on_source_control.assert_awaited_once_with("main", SourceControl.SHUFFLE, True)


async def test_a_command_aimed_at_a_live_source_that_ended_is_refused() -> None:
    """A session that is gone gets nothing, and neither does the queue that took over."""
    controller, provider = _controller(None)
    player = controller.get_player(PLAYER_ID)
    player.active_source = PLAYER_ID
    controller._get_player_with_redirect = MagicMock(return_value=player)
    queue = MagicMock()
    queue.queue_id = PLAYER_ID
    controller.get_active_queue = MagicMock(return_value=queue)
    controller.mass.player_queues.set_shuffle = AsyncMock()

    with pytest.raises(PlayerCommandFailed, match="no longer playing"):
        await controller.cmd_shuffle(
            PLAYER_ID, shuffle_enabled=True, source_id="spotify_connect--abc://audio_source/main"
        )

    provider.on_source_control.assert_not_awaited()
    controller.mass.player_queues.set_shuffle.assert_not_awaited()
