"""Tests for the Music Assistant Auracast player lifecycle."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerUnavailableError

from music_assistant.models.player import PlayerMedia
from music_assistant.providers.auracast.broadcaster import (
    CONTROLLER_STACK_SETUP_KEY,
    AuracastBroadcaster,
)
from music_assistant.providers.auracast.player import (
    PCM_FORMAT,
    SILENCE,
    AuracastPlayer,
    _apply_volume,
)

PLAYER_MODULE = "music_assistant.providers.auracast.player"


def create_player() -> AuracastPlayer:
    """Build a player with a minimal provider and mass stub."""
    mass = MagicMock()
    mass.config.get_base_player_config.return_value = MagicMock()
    mass.config.get_raw_player_config_value.return_value = 100
    mass.streams.audio.get_player_output_plan.return_value = MagicMock(filter_params=[])
    mass.create_task = lambda coro: asyncio.create_task(coro)
    provider = MagicMock()
    provider.mass = mass
    provider.logger = logging.getLogger("test.auracast")
    provider.instance_id = "auracast_test"
    provider.config.get_value.side_effect = lambda key, default=None: (
        "/dev/test" if key == "serial_port" else default
    )
    provider.get_setup_value.side_effect = lambda _key, default=None: default
    return AuracastPlayer(provider, "auracast_test")


def stub_broadcaster(player: Any) -> None:
    """Replace the player's broadcaster I/O with mocks."""

    async def start() -> None:
        player._broadcaster._big = object()

    async def stop() -> None:
        player._broadcaster._big = None

    player._broadcaster.start = AsyncMock(side_effect=start)
    player._broadcaster.stop = AsyncMock(side_effect=stop)
    player._broadcaster.send_pcm = AsyncMock()
    player._broadcaster.drain = AsyncMock()
    player.update_state = MagicMock()


async def pcm_source(*chunks: bytes) -> AsyncGenerator[bytes]:
    """Yield the given PCM chunks."""
    for chunk in chunks:
        yield chunk


async def wait_for_idle(player: Any) -> None:
    """Wait until a naturally completed stream leaves the player idle."""
    for _ in range(200):
        if player._stream_task is None:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Player never released the broadcast")


@pytest.mark.asyncio
async def test_play_media_keeps_broadcast_active_after_stream_eof() -> None:
    """Forward decoded PCM and keep the broadcast alive with silence after EOF."""
    player = cast("Any", create_player())
    media = MagicMock(spec=PlayerMedia)
    chunk = bytes(480 * 2 * 2)
    player.mass.streams.get_stream.return_value = pcm_source(chunk)
    stub_broadcaster(player)
    player._broadcaster.controller_stack = (
        "Bluetooth Core 5.2; company ID 0x0057; HCI revision 0x1234; "
        "LMP version 11; firmware revision 0x5678"
    )

    await player.play_media(media)
    assert player.playback_state == PlaybackState.PLAYING
    await wait_for_idle(player)

    player.mass.streams.get_stream.assert_called_once_with(media, PCM_FORMAT, player.player_id)
    player._broadcaster.start.assert_awaited_once()
    assert player._broadcaster.send_pcm.await_args_list[0].args == (chunk,)
    player._broadcaster.drain.assert_awaited_once()
    player._broadcaster.stop.assert_not_awaited()
    assert player._broadcaster.running
    assert player._silence_task is not None
    assert player.current_media is None
    assert player.playback_state == PlaybackState.IDLE
    assert player.device_info.manufacturer == "Bluetooth SIG"
    assert player.device_info.model == (
        "Host stack: Bumble; Controller stack: Bluetooth Core 5.2; company ID 0x0057; "
        "HCI revision 0x1234; LMP version 11; firmware revision 0x5678"
    )
    await player.stop()
    player._broadcaster.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_broadcast_starts_empty_broadcast_and_sends_silence() -> None:
    """Start an idle broadcast before any media has played."""
    player = cast("Any", create_player())
    stub_broadcaster(player)

    await player.start_broadcast()
    await asyncio.sleep(0)

    player._broadcaster.start.assert_awaited_once()
    player._broadcaster.send_pcm.assert_awaited_with(SILENCE)
    assert player._broadcaster.running
    assert player._silence_task is not None
    assert player.playback_state == PlaybackState.IDLE
    await player.stop()


@pytest.mark.asyncio
async def test_play_media_reuses_broadcast_started_while_idle() -> None:
    """Reuse the startup broadcast instead of reopening the controller on playback."""
    player = cast("Any", create_player())
    media = MagicMock(spec=PlayerMedia)
    player.mass.streams.get_stream.return_value = pcm_source()
    stub_broadcaster(player)

    await player.start_broadcast()
    await player.play_media(media)
    await wait_for_idle(player)

    player._broadcaster.start.assert_awaited_once()
    player._broadcaster.stop.assert_not_awaited()
    await player.stop()


@pytest.mark.asyncio
async def test_play_media_applies_player_output_filters() -> None:
    """Route the PCM stream through ffmpeg when DSP or EQ filters are configured."""
    player = cast("Any", create_player())
    media = MagicMock(spec=PlayerMedia)
    chunk = bytes(480 * 2 * 2)
    source = pcm_source(chunk)
    player.mass.streams.get_stream.return_value = source
    player.mass.streams.audio.get_player_output_plan.return_value = MagicMock(
        filter_params=["volume=-3dB"]
    )
    stub_broadcaster(player)

    with patch(f"{PLAYER_MODULE}.get_ffmpeg_stream", return_value=pcm_source(chunk)) as ffmpeg:
        await player.play_media(media)
        await wait_for_idle(player)

    assert ffmpeg.call_args.kwargs["audio_input"] is source
    assert ffmpeg.call_args.kwargs["filter_params"] == ["volume=-3dB"]
    assert player._broadcaster.send_pcm.await_args_list[0].args == (chunk,)
    await player.stop()


@pytest.mark.asyncio
async def test_pause_takes_effect_between_slices_of_one_chunk() -> None:
    """Send silence for the remaining slices of a chunk once pause was requested."""
    player = cast("Any", create_player())
    media = MagicMock(spec=PlayerMedia)
    slice_bytes = 480 * 2 * 2
    chunk = b"\x01\x00" * (slice_bytes // 2 * 3)
    player.mass.streams.get_stream.return_value = pcm_source(chunk)
    stub_broadcaster(player)
    sent: list[bytes] = []

    async def send_pcm(pcm: bytes) -> None:
        sent.append(pcm)
        if len(sent) == 1:
            await player.pause()
        elif len(sent) == 3:
            await player.play()

    player._broadcaster.send_pcm.side_effect = send_pcm

    await player.play_media(media)
    await wait_for_idle(player)

    audio = b"\x01\x00" * (slice_bytes // 2)
    silence = bytes(slice_bytes)
    assert sent[0] == audio
    assert sent[1:3] == [silence, silence]
    assert sent[3:] == [audio, audio]
    await player.stop()


@pytest.mark.asyncio
async def test_elapsed_time_tracks_streamed_audio_and_survives_pause() -> None:
    """Report cumulative playback time and freeze it, without paused silence, while paused."""
    player = cast("Any", create_player())
    media = MagicMock(spec=PlayerMedia)
    player.mass.streams.get_stream.return_value = pcm_source(bytes(48_000 * 4))
    stub_broadcaster(player)
    paused_elapsed: list[float] = []

    async def send_pcm(_pcm: bytes) -> None:
        if player._streamed_seconds >= 0.5 and not paused_elapsed:
            await player.pause()
            paused_elapsed.append(player.elapsed_time)
            await player.play()

    player._broadcaster.send_pcm.side_effect = send_pcm

    with patch.object(AuracastBroadcaster, "queued_seconds", PropertyMock(return_value=0.25)):
        await player.play_media(media)
        assert player.elapsed_time == 0.0
        assert player.elapsed_time_last_updated is not None
        await wait_for_idle(player)

    assert paused_elapsed[0] == pytest.approx(0.25, abs=0.02)
    await player.stop()


@pytest.mark.asyncio
async def test_failed_controller_start_marks_player_unavailable() -> None:
    """Report an unavailable player and the cause when the controller cannot be opened."""
    player = cast("Any", create_player())
    media = MagicMock(spec=PlayerMedia)
    stub_broadcaster(player)
    player._broadcaster.start.side_effect = OSError("could not open /dev/test")

    with pytest.raises(PlayerUnavailableError, match="could not open /dev/test"):
        await player.play_media(media)

    assert not player.available
    assert player._stream_task is None
    assert player.needs_poll


@pytest.mark.asyncio
async def test_failed_idle_broadcast_start_marks_player_unavailable() -> None:
    """Keep provider startup successful when the controller is temporarily unavailable."""
    player = cast("Any", create_player())
    stub_broadcaster(player)
    player._broadcaster.start.side_effect = OSError("could not open /dev/test")

    await player.start_broadcast()

    assert not player.available
    assert player.needs_poll
    assert player._silence_task is None


@pytest.mark.asyncio
async def test_poll_restores_availability_when_serial_port_returns() -> None:
    """Make the player available again once its serial port exists."""
    player = cast("Any", create_player())
    stub_broadcaster(player)
    player._attr_available = False

    with patch(f"{PLAYER_MODULE}.os.path.exists", return_value=False):
        await player.poll()
    assert not player.available

    with patch(f"{PLAYER_MODULE}.os.path.exists", return_value=True):
        await player.poll()
    assert player.available
    assert player.needs_poll
    await player.stop()


@pytest.mark.asyncio
async def test_poll_tears_down_and_restarts_broadcast_after_controller_hotplug() -> None:
    """Close the broadcast on unplug and recreate it when the controller returns."""
    player = cast("Any", create_player())
    stub_broadcaster(player)
    await player.start_broadcast()
    assert player.available
    assert player._broadcaster.running

    with patch(f"{PLAYER_MODULE}.os.path.exists", return_value=False):
        await player.poll()
    assert not player.available
    assert not player._broadcaster.running
    assert player._silence_task is None

    with patch(f"{PLAYER_MODULE}.os.path.exists", return_value=True):
        await player.poll()
    assert player.available
    assert player._broadcaster.running
    assert player._broadcaster.start.await_count == 2
    await player.stop()


@pytest.mark.asyncio
async def test_new_playback_reuses_broadcast_after_stopping_old_stream() -> None:
    """Join the old audio stream and reuse its active broadcast for the next track."""
    player = cast("Any", create_player())
    media = MagicMock(spec=PlayerMedia)
    stub_broadcaster(player)
    events: list[str] = []
    streaming = asyncio.Event()

    async def endless_source() -> AsyncGenerator[bytes]:
        try:
            streaming.set()
            await asyncio.Event().wait()
            yield b""
        finally:
            events.append("source closed")

    original_start = player._broadcaster.start.side_effect

    async def start() -> None:
        await original_start()
        events.append("broadcaster started")

    player._broadcaster.start.side_effect = start
    player.mass.streams.get_stream.side_effect = [endless_source(), pcm_source()]

    await player.play_media(media)
    await streaming.wait()
    events.clear()
    await player.play_media(media)
    await wait_for_idle(player)

    assert events == ["source closed"]
    assert player._broadcaster.start.await_count == 1
    player._broadcaster.stop.assert_not_awaited()
    await player.stop()


@pytest.mark.asyncio
async def test_stop_cancels_stream_without_draining() -> None:
    """Stop promptly instead of draining queued audio on an explicit stop."""
    player = cast("Any", create_player())
    media = MagicMock(spec=PlayerMedia)
    stub_broadcaster(player)
    streaming = asyncio.Event()

    async def endless_source() -> AsyncGenerator[bytes]:
        streaming.set()
        await asyncio.Event().wait()
        yield b""

    player.mass.streams.get_stream.return_value = endless_source()

    await player.play_media(media)
    await streaming.wait()
    await player.stop()

    player._broadcaster.drain.assert_not_awaited()
    player._broadcaster.stop.assert_awaited_once()
    assert player._stream_task is None
    assert player._silence_task is None
    assert player.playback_state == PlaybackState.IDLE


@pytest.mark.asyncio
async def test_volume_set_is_supported_and_scales_little_endian_pcm() -> None:
    """Expose the UI volume control and attenuate PCM before LC3 encoding."""
    player = cast("Any", create_player())
    player.update_state = MagicMock()
    pcm = b"\xff\x7f\x00\x80"

    assert PlayerFeature.VOLUME_SET in player.supported_features
    assert _apply_volume(pcm, 50) == b"\x00\x40\x00\xc0"
    assert _apply_volume(pcm, 0) == bytes(4)
    assert _apply_volume(pcm, 100) == pcm

    # The web UI's volume command updates the attenuation applied to subsequent frames.
    await player.volume_set(35)
    assert player.volume_level == 35
    player.mass.config.set_raw_player_config_value.assert_called_once_with(
        player.player_id, "auracast_stored_volume", 35
    )
    player.update_state.assert_called_once()


def test_player_restores_persisted_volume() -> None:
    """Restore the previously selected software volume after player reload."""
    player = create_player()
    cast("Any", player.mass.config.get_raw_player_config_value).return_value = 42

    restored_player = AuracastPlayer(cast("Any", player.provider), player.player_id)

    assert restored_player.volume_level == 42


def test_player_shows_controller_details_from_setup_probe() -> None:
    """Show the HCI identity discovered during setup before broadcasting starts."""
    player = create_player()
    cast("Any", player.provider).get_setup_value.side_effect = lambda key, default=None: (
        "Bluetooth Core 5.4; company ID 0x0059" if key == CONTROLLER_STACK_SETUP_KEY else default
    )

    player_with_details = AuracastPlayer(cast("Any", player.provider), player.player_id)

    assert player_with_details.device_info.model == (
        "Host stack: Bumble; Controller stack: Bluetooth Core 5.4; company ID 0x0059"
    )
