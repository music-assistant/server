"""Tests for the state the DLNA player reads from its device."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from async_upnp_client.profiles.dlna import TransportState
from music_assistant_models.enums import PlaybackState, PlayerFeature

from music_assistant.providers.dlna.player import DLNAPlayer
from tests.common import MockProvider

REPORTED_AT = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
STALE_POSITION = 120.0
STALE_REPORTED_AT = 1000.0


def _mock_device(**device_state: Any) -> MagicMock:
    """
    Return a mocked device that fully reports its state.

    :param device_state: Attributes to override on the device.
    """
    device = MagicMock()
    device.profile_device.available = True
    device.name = "Living Room Renderer"
    device.volume_level = 0.5
    device.is_volume_muted = False
    device.transport_state = TransportState.PLAYING
    device.current_track_uri = "http://192.168.1.10/stream.mp3"
    device.media_title = "Test Title"
    device.media_artist = "Test Artist"
    device.media_album_name = "Test Album"
    device.media_image_url = "http://192.168.1.10/cover.jpg"
    device.media_duration = 240
    device.media_position = 42
    device.media_position_updated_at = REPORTED_AT
    for name, value in device_state.items():
        setattr(device, name, value)
    return device


def _player(device: MagicMock) -> DLNAPlayer:
    """
    Return a player for the given device that already knows a position.

    :param device: The mocked device to attach to the player.
    """
    provider = MockProvider("dlna", instance_id="dlna_test")
    provider.mass.streams.base_url = "http://192.168.1.2:8097"

    player = DLNAPlayer(
        provider,  # type: ignore[arg-type]
        "uuid:dlna-player",
        "http://192.168.1.10/description.xml",
        device=device,
    )
    player._attr_elapsed_time = STALE_POSITION
    player._attr_elapsed_time_last_updated = STALE_REPORTED_AT
    return player


async def _updated_player(**device_state: Any) -> DLNAPlayer:
    """
    Run a state update on a player that already knows a position, and return it.

    :param device_state: Attributes to override on the fully reporting mocked device.
    """
    player = _player(_mock_device(**device_state))
    await player.set_dynamic_attributes()
    return player


async def test_zero_position_replaces_the_previous_one() -> None:
    """A device restarting a track reports 0, which must not be read as 'unknown'."""
    player = await _updated_player(media_position=0)

    assert player.elapsed_time == 0.0
    assert player.elapsed_time_last_updated == REPORTED_AT.timestamp()


async def test_known_position_is_applied() -> None:
    """A position reported by the device is adopted together with its timestamp."""
    player = await _updated_player(media_position=42)

    assert player.elapsed_time == 42.0
    assert player.elapsed_time_last_updated == REPORTED_AT.timestamp()


async def test_missing_position_keeps_the_previous_one() -> None:
    """A device that reports no position at all leaves the known position alone."""
    player = await _updated_player(media_position=None)

    assert player.elapsed_time == STALE_POSITION
    assert player.elapsed_time_last_updated == STALE_REPORTED_AT


async def test_position_from_before_a_resume_is_not_extrapolated() -> None:
    """
    A position reported from before a resume is anchored at the resume.

    A device does not re-stamp a position that did not change, so the timestamp it
    reports right after a resume still dates from before the pause.
    """
    paused_at = datetime.now(UTC) - timedelta(minutes=10)
    device = _mock_device(
        transport_state=TransportState.PAUSED_PLAYBACK,
        media_position=200,
        media_position_updated_at=paused_at,
    )
    player = _player(device)
    await player.set_dynamic_attributes()

    device.transport_state = TransportState.PLAYING
    resumed_at = time.time()
    await player.set_dynamic_attributes()

    assert player.elapsed_time == 200.0
    assert player.elapsed_time_last_updated is not None
    assert player.elapsed_time_last_updated >= resumed_at


async def test_position_of_a_player_found_while_playing_keeps_its_own_anchor() -> None:
    """Without having seen playback start, the timestamp the device reports is all there is."""
    player = await _updated_player(media_position=200, media_position_updated_at=REPORTED_AT)

    assert player.elapsed_time == 200.0
    assert player.elapsed_time_last_updated == REPORTED_AT.timestamp()


async def test_optimistic_playing_state_does_not_hide_the_start_of_playback() -> None:
    """
    Playback starting is tracked from what the device reports, not from what MA assumes.

    A play command marks the player as playing before the device confirms it, which must
    not be mistaken for the player having been playing all along.
    """
    device = _mock_device(
        transport_state=TransportState.STOPPED,
        media_position=200,
        media_position_updated_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    player = _player(device)
    await player.set_dynamic_attributes()

    player._attr_playback_state = PlaybackState.PLAYING
    device.transport_state = TransportState.PLAYING
    started_at = time.time()
    await player.set_dynamic_attributes()

    assert player.elapsed_time_last_updated is not None
    assert player.elapsed_time_last_updated >= started_at


async def test_transport_state_event_polls_before_reading_the_position() -> None:
    """A player that starts playing reports the position of the track it just started."""
    device = _mock_device(
        transport_state=TransportState.STOPPED,
        media_position=200,
        media_position_updated_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    async def _async_update(**_kwargs: Any) -> None:
        """Answer the poll a round trip later with the position of the new track."""
        await asyncio.sleep(0)
        device.transport_state = TransportState.PLAYING
        device.media_position = 0
        device.media_position_updated_at = datetime.now(UTC)

    device.async_update = _async_update

    player = _player(device)
    tasks: list[asyncio.Task[Any]] = []

    def _create_task(target: Coroutine[Any, Any, Any], **_kwargs: Any) -> asyncio.Task[Any]:
        # eager, like the real helper: the stale read happened because the update task
        # ran up to its first suspension before the poll task got its answer
        task: asyncio.Task[Any] = asyncio.Task(
            target, loop=asyncio.get_running_loop(), eager_start=True
        )
        tasks.append(task)
        return task

    player.mass.create_task = _create_task  # type: ignore[assignment]

    service = MagicMock()
    service.service_id = "urn:upnp-org:serviceId:AVTransport"
    state_variable = MagicMock()
    state_variable.name = "TransportState"
    state_variable.value = TransportState.PLAYING

    started_at = time.time()
    player._handle_event(service, [state_variable])
    await asyncio.gather(*tasks)

    assert player.elapsed_time == 0.0
    assert player.elapsed_time_last_updated is not None
    assert player.elapsed_time_last_updated >= started_at


@pytest.mark.parametrize(("reported", "expected"), [(0.5, 50), (0.0, 0), (1.0, 100)])
async def test_volume_level_is_scaled_to_percent(reported: float, expected: int) -> None:
    """The device reports volume as a 0..1 fraction, the player as a percentage."""
    player = await _updated_player(volume_level=reported)

    assert player.volume_level == expected


async def test_unknown_volume_level_stays_unknown() -> None:
    """A device that does not report its volume must not read as volume 0."""
    player = await _updated_player(volume_level=None)

    assert player.volume_level is None


@pytest.mark.parametrize("reported", [True, False])
async def test_mute_state_is_applied(reported: bool) -> None:
    """A mute state reported by the device is adopted as-is."""
    player = await _updated_player(is_volume_muted=reported)

    assert player.volume_muted is reported


async def test_unknown_mute_state_stays_unknown() -> None:
    """A device that does not report its mute state must not read as unmuted."""
    player = await _updated_player(is_volume_muted=None)

    assert player.volume_muted is None


@pytest.mark.parametrize("capable", [True, False])
async def test_spotify_connect_is_exposed_as_controllable_source(capable: bool) -> None:
    """A Spotify Connect session on the device offers the transport controls the device has."""
    player = await _updated_player(
        current_track_uri="spotify:track:4uLU6hMCjMI75M1A2tKUQC",
        has_pause=True,
        has_next=capable,
        has_previous=capable,
        has_seek_rel_time=capable,
        can_seek_rel_time=capable,
    )

    assert player.active_source == "spotify"
    assert len(player.source_list) == 1
    source = player.source_list[0]
    assert source.id == "spotify"
    assert source.passive
    assert source.can_play_pause
    assert source.can_next_previous is capable
    assert source.can_seek is capable


async def test_spotify_seek_follows_live_transport_actions() -> None:
    """Seek isn't offered while the device leaves Seek out of its current transport actions."""
    player = await _updated_player(
        current_track_uri="spotify:track:4uLU6hMCjMI75M1A2tKUQC",
        has_seek_rel_time=True,
        can_seek_rel_time=False,
    )

    assert player.source_list[0].can_seek is False


async def test_spotify_source_is_dropped_when_the_session_ends() -> None:
    """Once the device plays something else, the Spotify source is no longer listed."""
    device = _mock_device(current_track_uri="spotify:track:4uLU6hMCjMI75M1A2tKUQC")
    player = _player(device)
    await player.set_dynamic_attributes()
    device.current_track_uri = "http://192.168.1.2:8097/flow/stream.flac"
    await player.set_dynamic_attributes()

    assert player.active_source is None
    assert player.source_list == []


async def test_transport_features_follow_device_capabilities() -> None:
    """Next/previous and seek are advertised only when the device has those actions."""
    capable = _player(_mock_device(has_next=True, has_previous=True, has_seek_rel_time=True))
    capable.set_static_attributes()
    limited = _player(_mock_device(has_next=True, has_previous=False, has_seek_rel_time=False))
    limited.set_static_attributes()

    assert {PlayerFeature.NEXT_PREVIOUS, PlayerFeature.SEEK} <= capable.supported_features
    assert PlayerFeature.NEXT_PREVIOUS not in limited.supported_features
    assert PlayerFeature.SEEK not in limited.supported_features


async def test_seek_sends_relative_time() -> None:
    """Seeking sends the position to the device as a relative time."""
    device = _mock_device(async_seek_rel_time=AsyncMock())
    player = _player(device)

    await player.seek(83)

    device.async_seek_rel_time.assert_awaited_once_with(timedelta(seconds=83))


@pytest.mark.parametrize(
    ("command", "action_name"), [("next_track", "Next"), ("previous_track", "Previous")]
)
async def test_skip_is_sent_despite_stale_transport_actions(command: str, action_name: str) -> None:
    """A device whose CurrentTransportActions omits Next/Previous still gets the raw action."""
    action = MagicMock(async_call=AsyncMock())
    device = _mock_device(
        can_next=False, can_previous=False, async_next=AsyncMock(), async_previous=AsyncMock()
    )
    device._action = MagicMock(
        side_effect=lambda _service, name: action if name == action_name else None
    )
    player = _player(device)

    await getattr(player, command)()

    device._action.assert_any_call("AVT", action_name)
    action.async_call.assert_awaited_once_with(InstanceID=0)
