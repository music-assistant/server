"""Tests for the Squeezelite player."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import PlayerState as SlimPlayerState
from aioslimproto.models import SlimEvent
from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
    PlayerFeature,
    ProtocolRole,
    RepeatMode,
)
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import (
    CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED,
    CONF_PREFER_WAV_FOR_LIVE_SOURCES,
)
from music_assistant.providers.squeezelite.constants import (
    CONF_ENTRY_DISPLAY,
    CONF_ENTRY_VISUALIZATION,
    CONF_STORED_VOLUME,
    DEFAULT_PLAYER_VOLUME,
)
from music_assistant.providers.squeezelite.player import SqueezelitePlayer, is_protocol_only_device
from tests.common import use_real_create_task

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry


async def test_squeezelite_prefers_wav_for_live_sources_by_default() -> None:
    """Squeezelite players default to the known-compatible low-latency WAV path."""

    async def _no_library_items(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        return
        yield  # pragma: no cover - makes this an async generator

    player = SqueezelitePlayer.__new__(SqueezelitePlayer)
    player.mass = mass = MagicMock()
    mass.music.playlists.iter_library_items = _no_library_items
    mass.music.radio.iter_library_items = _no_library_items

    entries = await player.get_config_entries()
    entry = next(entry for entry in entries if entry.key == CONF_PREFER_WAV_FOR_LIVE_SOURCES)

    assert (
        entry.default_value == CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED.default_value
    )


def _player_with_mocked_mass() -> tuple[SqueezelitePlayer, MagicMock]:
    """Return an uninitialized SqueezelitePlayer alongside its mocked mass instance."""
    player = SqueezelitePlayer.__new__(SqueezelitePlayer)
    player.mass = mass = MagicMock()
    player._protocol_id = "p1"
    player._player = None
    player._provider = MagicMock(get_protocol=lambda pid: mass.players.get_protocol(pid))
    return player, mass


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        (MediaType.RADIO, (64, 1)),
        (MediaType.AUDIO_SOURCE, (64, 1)),
        (MediaType.ANNOUNCEMENT, (64, 1)),
        (MediaType.TRACK, (200, 20)),
    ],
)
async def test_live_streams_start_on_a_smaller_buffer(
    media_type: MediaType, expected: tuple[int, int]
) -> None:
    """Radio, AudioSource and announcement streams start on a smaller buffer, also when repeated."""
    player, mass = _player_with_mocked_mass()
    mass.player_queues.get.return_value = MagicMock(
        repeat_mode=RepeatMode.ONE, shuffle_enabled=False
    )
    player.client = slimplayer = MagicMock()
    slimplayer.play_url = AsyncMock()

    await player._handle_play_url_for_slimplayer(
        url="http://127.0.0.1:8097/stream.flac",
        media=PlayerMedia(uri="fake://x", media_type=media_type, source_id="queue_1"),
    )
    # the repeat-one re-enqueue is scheduled, not awaited here
    mass.call_later.call_args.args[1].close()

    assert slimplayer.play_url.call_count == 2
    for play_call in slimplayer.play_url.call_args_list:
        thresholds = (play_call.kwargs["stream_threshold"], play_call.kwargs["output_threshold"])
        assert thresholds == expected


@pytest.mark.parametrize(
    ("device_model", "expected"),
    [
        # generic squeezelite (software) players are full players
        ("SqueezeLite", False),
        ("SqueezeLite-HA-Addon", False),
        ("SqueezePlay", False),
        ("SqueezeESP32", False),
        # hardware players are full players
        ("Squeezebox Boom", False),
        ("Transporter", False),
        # unknown/absent model info defaults to a full player
        ("", False),
        ("Unknown", False),
        # WiiM/LinkPlay devices use squeezelite as a secondary protocol
        ("WiiM Player", True),
        ("wiim mini", True),
        # LMS bridge tools represent devices that are already players themselves
        ("RaopBridge", True),
        ("CastBridge", True),
        ("UPnPBridge", True),
    ],
)
def test_is_protocol_only_device(device_model: str, expected: bool) -> None:
    """Test protocol-only device detection based on the reported device model."""
    assert is_protocol_only_device(device_model) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8097/single/s1/queue_1/q2/p1.flac", "audio/flac"),
        ("http://127.0.0.1:8097/flow/s1/queue_1/q2/p1.mp3?seek=4", "audio/mpeg"),
    ],
)
async def test_play_url_mime_type_follows_the_url_extension(url: str, expected: str) -> None:
    """The player is told the codec of the stream its url names."""
    player, mass = _player_with_mocked_mass()
    mass.player_queues.get.return_value = None
    player.client = slimplayer = MagicMock()
    slimplayer.play_url = AsyncMock()

    await player._handle_play_url_for_slimplayer(
        url=url,
        media=PlayerMedia(uri="fake://x", media_type=MediaType.TRACK, source_id="queue_1"),
    )

    assert slimplayer.play_url.call_args.kwargs["mime_type"] == expected


async def test_play_media_resets_elapsed_anchor() -> None:
    """A new stream does not carry the previous stream's position."""
    player, mass = _player_with_mocked_mass()
    player.client = MagicMock(elapsed_seconds=0)
    player._provider = MagicMock(mass=mass)
    player._attr_elapsed_time = 3.9
    player._attr_elapsed_time_last_updated = time.time() - 4
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://127.0.0.1:8097/x.flac")

    with (
        patch.object(player, "_handle_play_url_for_slimplayer", new=AsyncMock()),
        patch.object(player, "update_attributes"),
        patch.object(player, "update_state"),
    ):
        await player.play_media(
            PlayerMedia(uri="fake://x", media_type=MediaType.TRACK, source_id="queue_1")
        )

    assert player._attr_elapsed_time == 0
    assert time.time() - player._attr_elapsed_time_last_updated < 1


async def test_play_media_drops_heartbeat_from_before_the_flush() -> None:
    """An old-stream heartbeat that lands before the flush does not survive the restart."""
    player, mass = _player_with_mocked_mass()
    player.client = client = MagicMock(elapsed_seconds=0)
    client.play_url = AsyncMock()
    player._provider = MagicMock(mass=mass)
    mass.player_queues.get.return_value = None

    async def _resolve_with_heartbeat(*_args: object) -> str:
        player._attr_elapsed_time = 3.9
        return "http://127.0.0.1:8097/x.flac"

    mass.streams.resolve_stream_url = _resolve_with_heartbeat

    await player.play_media(
        PlayerMedia(uri="fake://x", media_type=MediaType.TRACK, source_id="queue_1")
    )

    assert player._attr_elapsed_time == 0


def _player_for_volume_restore(
    stored: dict[str, object], legacy_cache: list[object] | None, *, known: bool = True
) -> tuple[SqueezelitePlayer, MagicMock]:
    """
    Return a protocol whose stored config values and legacy cache entry are mocked.

    :param stored: The values stored on the record of protocol ``p1``.
    :param legacy_cache: The legacy cache entry of the protocol.
    :param known: Whether the protocol has a record before its attach creates one.
    """
    player, mass = _player_with_mocked_mass()
    player._provider = MagicMock(mass=mass, instance_id="squeezelite_1")
    player.logger = MagicMock()
    player.client = MagicMock(player_id="p1", muted=False, volume_level=20)
    player.client.stop = AsyncMock()
    player.client.mute = AsyncMock()
    player.client.volume_set = AsyncMock(
        side_effect=lambda level: setattr(player.client, "volume_level", level)
    )

    def _get_protocol_value(protocol_id: str, key: str, default: object = None) -> object:
        return stored.get(key, default) if protocol_id == "p1" else default

    def _set_protocol_value(protocol_id: str, key: str, value: object) -> None:
        # like the config controller: a protocol without a record that is not attached
        if protocol_id != "p1" or not (known or player._player is not None):
            raise KeyError(protocol_id)
        stored[key] = value

    async def _attach(protocol: SqueezelitePlayer) -> None:
        protocol._player = MagicMock(player_id="pl_p1")

    mass.config.get_protocol_value.side_effect = _get_protocol_value
    mass.config.set_protocol_value.side_effect = _set_protocol_value
    mass.cache.get = AsyncMock(return_value=legacy_cache)
    mass.players.register = AsyncMock(side_effect=_attach)
    return player, mass


@pytest.mark.parametrize(
    ("stored", "legacy_cache", "expected_volume"),
    [
        # volume mapped to an AVR: the provider never sends volume commands, so the
        # cache entry expired, but the stored value must still be restored
        ({CONF_STORED_VOLUME: 100}, None, 100),
        ({CONF_STORED_VOLUME: 35}, [False, 80], 35),
        # stored before the volume moved out of the cache
        ({}, [True, 55], 55),
        ({}, None, DEFAULT_PLAYER_VOLUME),
    ],
)
async def test_setup_restores_volume_without_a_valid_cache_entry(
    stored: dict[str, object],
    legacy_cache: list[object] | None,
    expected_volume: int,
) -> None:
    """Reconnecting restores the last volume even after the cache entry expired."""
    player, mass = _player_for_volume_restore(stored, legacy_cache)

    with patch.object(player, "update_attributes"):
        await player.setup()

    client = cast("MagicMock", player.client)
    client.mute.assert_not_called()
    client.volume_set.assert_awaited_once_with(expected_volume)
    mass.players.register.assert_awaited_once_with(player)
    assert stored[CONF_STORED_VOLUME] == expected_volume


async def test_a_first_connect_stores_the_volume_once_attached() -> None:
    """A new device has no record until its attach creates one, so its volume is stored after."""
    stored: dict[str, object] = {}
    player, mass = _player_for_volume_restore(stored, None, known=False)

    with patch.object(player, "update_attributes"):
        await player.setup()

    mass.players.register.assert_awaited_once_with(player)
    assert stored[CONF_STORED_VOLUME] == DEFAULT_PLAYER_VOLUME


async def test_volume_changed_on_the_player_is_stored() -> None:
    """A volume change made on the player itself (e.g. JiveLite) is kept for the next connect."""
    stored: dict[str, object] = {}
    player, mass = _player_for_volume_restore(stored, None)
    with patch.object(player, "update_attributes"), patch.object(player, "update_state"):
        await player.setup()
        mass.config.set_protocol_value.reset_mock()
        event = SlimEvent(type=SlimEventType.PLAYER_UPDATED, player_id="p1")

        cast("MagicMock", player.client).volume_level = 65
        player.handle_slim_event(event)
        assert stored[CONF_STORED_VOLUME] == 65

        player.handle_slim_event(event)
        mass.config.set_protocol_value.assert_called_once()


@pytest.mark.parametrize("entry", [CONF_ENTRY_DISPLAY, CONF_ENTRY_VISUALIZATION])
def test_the_settings_entries_of_a_player_can_be_serialized(entry: ConfigEntry) -> None:
    """The settings page of a player turns its entries into a dict, which a bad default breaks."""
    entry.to_dict()
    if entry.options:
        assert entry.default_value in {option.value for option in entry.options}


@pytest.mark.parametrize(
    ("device_model", "role", "powered_by_client"),
    [("SqueezeLite", ProtocolRole.NATIVE, True), ("WiiM Player", ProtocolRole.AUDIO, False)],
)
async def test_the_role_follows_the_device_model(
    device_model: str, role: ProtocolRole, powered_by_client: bool
) -> None:
    """A device of its own is an audio protocol, powered with the stream; a player has power."""
    provider = MagicMock(instance_id="squeezelite")
    provider.mass.config.get_base_protocol_config.return_value = MagicMock()
    use_real_create_task(provider.mass)
    client = MagicMock(device_model=device_model, max_sample_rate=96000)
    client.stop = AsyncMock()
    client.power = AsyncMock()
    player = SqueezelitePlayer(provider, "aa:bb:cc:dd:ee:ff", client)
    player.update_state = MagicMock()  # type: ignore[misc,method-assign]

    await player.stop()

    assert player.role == role
    assert (PlayerFeature.POWER in player.supported_features) is powered_by_client
    if powered_by_client:
        client.stop.assert_awaited_once()
        client.power.assert_not_awaited()
    else:
        client.power.assert_awaited_once_with(False)
        client.stop.assert_not_awaited()


def _connected_player(device_model: str = "SqueezeLite") -> tuple[SqueezelitePlayer, MagicMock]:
    """Return the protocol of a connected client alongside its mocked mass instance."""
    provider = MagicMock(instance_id="squeezelite")
    mass = provider.mass
    mass.config.get_base_protocol_config.return_value = MagicMock()
    client = MagicMock(device_model=device_model, max_sample_rate=96000, elapsed_seconds=0)
    client.player_id = "aa:bb:cc:dd:ee:ff"
    client.play_url = AsyncMock()
    return SqueezelitePlayer(provider, client.player_id, client), mass


@pytest.mark.parametrize("device_model", ["SqueezeLite", "WiiM Player"])
def test_the_protocol_groups_no_players_itself(device_model: str) -> None:
    """A group of squeezelite players is one of their Sendspin outputs, never of the protocol."""
    player, _mass = _connected_player(device_model)

    assert PlayerFeature.SET_MEMBERS not in player.supported_features
    assert player.can_group_with == set()
    assert player.group_members == []
    assert player.synced_to is None
    assert player.native_grouping_requires_own_stream is False


async def test_play_media_plays_the_stream_of_the_player_itself() -> None:
    """A player on its own gets its own stream, in its own codec, started once it is buffered."""
    player, mass = _connected_player()
    mass.player_queues.get.return_value = None
    mass.streams.resolve_stream_url = AsyncMock(
        return_value="http://127.0.0.1:8097/single/s1/queue_1/q2/aa:bb:cc:dd:ee:ff.flac"
    )
    media = PlayerMedia(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        title="Track",
        source_id="queue_1",
        queue_item_id="q2",
        duration=198,
    )

    await player.play_media(media)

    mass.streams.resolve_stream_url.assert_awaited_once_with("aa:bb:cc:dd:ee:ff", media)
    cast("MagicMock", player.client).play_url.assert_awaited_once_with(
        url="http://127.0.0.1:8097/single/s1/queue_1/q2/aa:bb:cc:dd:ee:ff.flac",
        mime_type="audio/flac",
        metadata={
            "item_id": "library://track/1",
            "title": "Track",
            "album": None,
            "artist": None,
            "image_url": None,
            "duration": 198,
            "source_id": "queue_1",
            "queue_item_id": "q2",
        },
        enqueue=False,
        send_flush=True,
        autostart=False,
        stream_threshold=200,
        output_threshold=20,
    )


# ---------------------------------------------------------------- Sendspin bridge


def _player_with_bridge(*, owns_stream: bool = True) -> tuple[SqueezelitePlayer, MagicMock]:
    """Return a player whose Sendspin bridge has, or does not have, its stream on the player."""
    player, _mass = _player_with_mocked_mass()
    player.client = MagicMock(player_id="p1")
    player.sendspin_bridge = bridge = MagicMock(owns_stream=owns_stream)
    return player, bridge


@pytest.mark.parametrize("owns_stream", [True, False])
async def test_a_stream_of_the_bridge_is_started_by_the_bridge(owns_stream: bool) -> None:
    """The bridge starts its stream at an instant of its timeline, the player would start it now."""
    player, _bridge = _player_with_bridge(owns_stream=owns_stream)
    use_real_create_task(cast("MagicMock", player.mass))
    client = cast("MagicMock", player.client)
    client.jiffies = 1234
    client.unpause_at = AsyncMock()
    event = SlimEvent(type=SlimEventType.PLAYER_BUFFER_READY, player_id="p1")

    player.handle_slim_event(event)
    await asyncio.sleep(0)

    if owns_stream:
        client.unpause_at.assert_not_awaited()
    else:
        client.unpause_at.assert_awaited_once_with(1234)


@pytest.mark.parametrize("owns_stream", [True, False])
def test_the_position_of_a_bridge_stream_is_not_that_of_the_protocol(owns_stream: bool) -> None:
    """The elapsed time of the protocol does not follow a bridge stream."""
    player, _bridge = _player_with_bridge(owns_stream=owns_stream)
    cast("MagicMock", player.client).elapsed_seconds = 42.0
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 0
    event = SlimEvent(type=SlimEventType.PLAYER_HEARTBEAT, player_id="p1")

    with patch.object(player, "update_state"):
        player.handle_slim_event(event)

    assert player._attr_elapsed_time == (0 if owns_stream else 42.0)


@pytest.mark.parametrize("owns_stream", [True, False])
def test_a_bridge_stream_is_no_media_of_the_protocol(owns_stream: bool) -> None:
    """The protocol reports that it plays, but not what: that is what Sendspin shows."""
    provider = MagicMock(instance_id="squeezelite")
    provider.mass.config.get_base_protocol_config.return_value = MagicMock()
    client = MagicMock(
        device_model="SqueezeLite",
        max_sample_rate=96000,
        state=SlimPlayerState.PLAYING,
        volume_level=20,
        muted=False,
    )
    client.current_media.metadata = {"item_id": "library://track/1", "title": "Before"}
    player = SqueezelitePlayer(provider, "aa:bb:cc:dd:ee:ff", client)
    player.sendspin_bridge = MagicMock(owns_stream=owns_stream)

    player.update_attributes()

    assert player.playback_state == PlaybackState.PLAYING
    if owns_stream:
        assert player.current_media is None
    else:
        assert player.current_media is not None
        assert player.current_media.title == "Before"


def test_a_track_change_of_the_player_goes_to_its_bridge() -> None:
    """The stream of a bridge plays on from track to track: the bridge shows what plays now."""
    player, bridge = _player_with_bridge()

    player.on_player_media_updated()

    bridge.update_now_playing.assert_called_once_with()
    # without a bridge there is nothing to tell
    player.sendspin_bridge = None
    player.on_player_media_updated()


async def test_a_stream_of_the_protocol_takes_the_player_back_from_its_bridge() -> None:
    """Before the protocol plays on its player, the bridge that plays on it lets go."""
    player, bridge = _player_with_bridge()
    mass = cast("MagicMock", player.mass)
    player._provider = MagicMock(mass=mass)
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://127.0.0.1:8097/x.flac")
    steps = MagicMock()
    steps.attach_mock(bridge.stop_streaming, "bridge")
    play_url = AsyncMock()
    steps.attach_mock(play_url, "play_url")

    with (
        patch.object(player, "_handle_play_url_for_slimplayer", new=play_url),
        patch.object(player, "update_state"),
    ):
        await player.play_media(
            PlayerMedia(uri="fake://x", media_type=MediaType.TRACK, source_id="queue_1")
        )

    assert [step[0] for step in steps.mock_calls] == ["bridge", "play_url"]


async def test_a_stop_of_the_protocol_ends_the_stream_of_its_bridge() -> None:
    """The bridge gives the player back and leaves its session, then the player is stopped."""
    provider = MagicMock(instance_id="squeezelite")
    provider.mass.config.get_base_protocol_config.return_value = MagicMock()
    use_real_create_task(provider.mass)
    client = MagicMock(device_model="SqueezeLite", max_sample_rate=96000)
    client.stop = AsyncMock()
    player = SqueezelitePlayer(provider, "aa:bb:cc:dd:ee:ff", client)
    player.update_state = MagicMock()  # type: ignore[misc,method-assign]
    player.sendspin_bridge = bridge = MagicMock()
    steps = MagicMock()
    steps.attach_mock(bridge.stop_streaming, "bridge")
    steps.attach_mock(client.stop, "client")

    await player.stop()

    assert steps.mock_calls == [call.bridge(), call.client()]


def test_a_stream_that_takes_the_player_over_ends_the_position_of_the_protocol() -> None:
    """The protocol reports no position of its own under a stream that takes the player over."""
    player, _mass = _player_with_mocked_mass()
    player._attr_elapsed_time = 80.0
    player._attr_elapsed_time_last_updated = time.time() - 80

    player.release_native_playback()

    assert player._attr_elapsed_time == 0
    assert time.time() - player._attr_elapsed_time_last_updated < 1


@pytest.mark.parametrize(
    ("target", "queue_of"), [("living_room", "living_room"), (None, "kitchen")]
)
async def test_buttons_act_on_the_player_that_leads_the_sendspin_group(
    target: str | None, queue_of: str
) -> None:
    """While the player plays for its bridge, next on it skips for its Sendspin group."""
    player, bridge = _player_with_bridge()
    mass = cast("MagicMock", player.mass)
    player._player = MagicMock(player_id="kitchen")
    player.logger = MagicMock()
    bridge.transport_command_target.return_value = target
    mass.player_queues.next = AsyncMock()
    queue = mass.player_queues.get_active_queue.return_value
    event = SlimEvent(type=SlimEventType.PLAYER_BTN_EVENT, player_id="p1", data="button fwd")

    await player._handle_player_cli_event(event)

    mass.player_queues.get_active_queue.assert_called_once_with(queue_of)
    mass.player_queues.next.assert_awaited_once_with(queue.queue_id)


async def test_the_bridge_is_evaluated_when_the_config_of_the_protocol_is_loaded() -> None:
    """Every attach and config change brings the Sendspin bridge in line."""
    player, mass = _player_with_mocked_mass()
    use_real_create_task(mass)

    with (
        patch.object(player, "_set_preset_items", new=AsyncMock()),
        patch.object(player, "_set_display", new=AsyncMock()),
        patch(
            "music_assistant.providers.squeezelite.player.evaluate_bridge_of", new=AsyncMock()
        ) as evaluate,
    ):
        await player.on_config_updated()
        await asyncio.sleep(0)

    evaluate.assert_awaited_once_with(mass, "p1")


async def test_a_protocol_that_is_removed_takes_its_bridge_client_along() -> None:
    """The Sendspin protocol of the bridge is removed with the protocol it rides on."""
    player, _mass = _player_with_mocked_mass()
    player._provider = MagicMock()
    player._provider.bridge_manager.remove_bridge = remove_bridge = AsyncMock()

    await player.on_remove()

    remove_bridge.assert_awaited_once_with("p1", permanent=True)
