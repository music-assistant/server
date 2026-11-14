"""Tests for the Squeezelite player."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import PlayerState as SlimPlayerState
from aioslimproto.models import SlimEvent
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import (
    CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED,
    CONF_OUTPUT_CODEC,
    CONF_PREFER_WAV_FOR_LIVE_SOURCES,
)
from music_assistant.providers.squeezelite.constants import (
    CONF_STORED_VOLUME,
    DEFAULT_PLAYER_VOLUME,
)
from music_assistant.providers.squeezelite.player import SqueezelitePlayer, is_protocol_only_device
from tests.common import use_real_create_task


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


def _member_player(*, prefer_wav_for_live_sources: bool) -> MagicMock:
    """Return a mocked member player with the given effective WAV preference."""
    member = MagicMock()
    member.config.get_value.side_effect = lambda key, default=None: (
        prefer_wav_for_live_sources if key == CONF_PREFER_WAV_FOR_LIVE_SOURCES else default
    )
    return member


def _player_with_mocked_mass() -> tuple[SqueezelitePlayer, MagicMock]:
    """Return an uninitialized SqueezelitePlayer alongside its mocked mass instance."""
    player = SqueezelitePlayer.__new__(SqueezelitePlayer)
    player.mass = mass = MagicMock()
    return player, mass


def test_group_audio_source_uses_wav_when_member_prefers_it() -> None:
    """A sync group member that opted into low-latency WAV gets WAV for AudioSource media."""
    player, mass = _player_with_mocked_mass()
    mass.players.get_player.return_value = _member_player(prefer_wav_for_live_sources=True)

    codec = player._get_member_output_codec(
        "member_1", PlayerMedia(uri="fake://x", media_type=MediaType.AUDIO_SOURCE)
    )

    assert codec == "wav"
    mass.config.get_raw_player_config_value.assert_not_called()


def test_group_audio_source_falls_back_to_output_codec_when_disabled() -> None:
    """A sync group member that disabled the WAV preference uses its configured output codec."""
    player, mass = _player_with_mocked_mass()
    mass.players.get_player.return_value = _member_player(prefer_wav_for_live_sources=False)
    mass.config.get_raw_player_config_value.return_value = "aac"

    codec = player._get_member_output_codec(
        "member_1", PlayerMedia(uri="fake://x", media_type=MediaType.AUDIO_SOURCE)
    )

    assert codec == "aac"
    mass.config.get_raw_player_config_value.assert_called_once_with(
        "member_1", CONF_OUTPUT_CODEC, "flac"
    )


def test_group_regular_track_ignores_wav_preference() -> None:
    """Regular track playback in a sync group always uses the member's configured codec."""
    player, mass = _player_with_mocked_mass()
    mass.config.get_raw_player_config_value.return_value = "flac"

    codec = player._get_member_output_codec(
        "member_1", PlayerMedia(uri="fake://x", media_type=MediaType.TRACK)
    )

    assert codec == "flac"
    mass.players.get_player.assert_not_called()


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


async def test_regroup_supersedes_pending_resume() -> None:
    """A member removed and re-added while the leader is busy restarts the leader once."""
    player, mass = _player_with_mocked_mass()
    player._player_id = "leader"
    player._attr_group_members = ["leader", "member"]
    player._attr_playback_state = PlaybackState.PLAYING
    use_real_create_task(mass)
    member = MagicMock()
    member.player_id = "member"
    member.state = SlimPlayerState.STOPPED
    mass.players.get_player.return_value = member
    # the playback lock an announcement holds on the leader while it plays
    leader_busy = asyncio.Event()
    resumed: list[str] = []

    async def _cmd_resume(player_id: str) -> None:
        await leader_busy.wait()
        resumed.append(player_id)

    mass.players.cmd_resume = _cmd_resume

    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(SqueezelitePlayer, "state", new_callable=PropertyMock),
        patch.object(player, "_get_sync_clients", return_value=[member]),
        patch.object(player, "update_state"),
    ):
        await player.set_members(player_ids_to_remove=["member"])
        await player.set_members(player_ids_to_add=["member"])
        leader_busy.set()
        await asyncio.gather(*mass._tracked_tasks.values(), return_exceptions=True)

    assert resumed == ["leader"]


async def test_set_members_removes_several_members_at_once() -> None:
    """All members passed in one removal call leave the group and are stopped."""
    player, _mass = _player_with_mocked_mass()
    player._player_id = "leader"
    player._attr_group_members = ["leader", "member_1", "member_2", "member_3"]
    player.client = MagicMock(player_id="leader")
    members = {
        member_id: MagicMock(player_id=member_id, state=SlimPlayerState.PLAYING, stop=AsyncMock())
        for member_id in ("member_1", "member_2", "member_3")
    }
    player._provider = MagicMock()
    player._provider.slimproto.get_player.side_effect = members.get

    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(SqueezelitePlayer, "state", new_callable=PropertyMock),
        patch.object(player, "update_state"),
    ):
        await player.set_members(player_ids_to_remove=["member_1", "member_2"])

    assert player.group_members == ["leader", "member_3"]
    members["member_1"].stop.assert_awaited_once()
    members["member_2"].stop.assert_awaited_once()
    members["member_3"].stop.assert_not_called()


async def test_play_media_resets_elapsed_anchor() -> None:
    """A new stream does not carry the previous stream's position."""
    player, mass = _player_with_mocked_mass()
    player.multi_client_stream = None
    player.client = MagicMock(elapsed_seconds=0)
    player._provider = MagicMock(mass=mass)
    player._attr_elapsed_time = 3.9
    player._attr_elapsed_time_last_updated = time.time() - 4
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://127.0.0.1:8097/x.flac")

    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(
            SqueezelitePlayer, "group_members", new_callable=PropertyMock, return_value=[]
        ),
        patch.object(SqueezelitePlayer, "player_id", new_callable=PropertyMock, return_value="p1"),
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
    player.multi_client_stream = None
    player._extra_data = {}
    player.client = client = MagicMock(elapsed_seconds=0)
    client.play_url = AsyncMock()
    player._provider = MagicMock(mass=mass)
    mass.player_queues.get.return_value = None

    async def _resolve_with_heartbeat(*_args: object) -> str:
        player._attr_elapsed_time = 3.9
        return "http://127.0.0.1:8097/x.flac"

    mass.streams.resolve_stream_url = _resolve_with_heartbeat

    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(
            SqueezelitePlayer, "group_members", new_callable=PropertyMock, return_value=[]
        ),
        patch.object(SqueezelitePlayer, "player_id", new_callable=PropertyMock, return_value="p1"),
    ):
        await player.play_media(
            PlayerMedia(uri="fake://x", media_type=MediaType.TRACK, source_id="queue_1")
        )

    assert player._attr_elapsed_time == 0


def _player_for_volume_restore(
    stored: dict[str, object], legacy_cache: list[object] | None
) -> tuple[SqueezelitePlayer, MagicMock]:
    """Return a player whose stored config values and legacy cache entry are mocked."""
    player, mass = _player_with_mocked_mass()
    player._player_id = "p1"
    player._provider = MagicMock(mass=mass, instance_id="squeezelite_1")
    player.logger = MagicMock()
    player.client = MagicMock(player_id="p1", muted=False, volume_level=20)
    player.client.stop = AsyncMock()
    player.client.mute = AsyncMock()
    player.client.volume_set = AsyncMock(
        side_effect=lambda level: setattr(player.client, "volume_level", level)
    )
    mass.config.get_raw_player_config_value.side_effect = lambda _player_id, key, default=None: (
        stored.get(key, default)
    )
    mass.config.set_raw_player_config_value.side_effect = lambda _player_id, key, value: (
        stored.__setitem__(key, value)
    )
    mass.cache.get = AsyncMock(return_value=legacy_cache)
    mass.players.register_or_update = AsyncMock()
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

    player.client.mute.assert_not_called()
    player.client.volume_set.assert_awaited_once_with(expected_volume)
    mass.players.register_or_update.assert_awaited_once_with(player)
    assert stored[CONF_STORED_VOLUME] == expected_volume


async def test_volume_changed_on_the_player_is_stored() -> None:
    """A volume change made on the player itself (e.g. JiveLite) is kept for the next connect."""
    stored: dict[str, object] = {}
    player, mass = _player_for_volume_restore(stored, None)
    with patch.object(player, "update_attributes"), patch.object(player, "update_state"):
        await player.setup()
        mass.config.set_raw_player_config_value.reset_mock()
        event = SlimEvent(type=SlimEventType.PLAYER_UPDATED, player_id="p1")

        player.client.volume_level = 65
        player.handle_slim_event(event)
        assert stored[CONF_STORED_VOLUME] == 65

        player.handle_slim_event(event)
        mass.config.set_raw_player_config_value.assert_called_once()
