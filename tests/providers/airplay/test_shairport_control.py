"""Tests for controlling another sender's session on a shairport-sync receiver over MQTT."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.airplay.helpers import convert_airplay_volume
from music_assistant.providers.airplay.shairport_player import (
    AIRPLAY_VOLUME_MIN,
    VOLUME_STEP_DB,
    ShairportSyncPlayer,
    volume_steps,
)
from tests.providers.airplay.test_shairport_player import (
    PLAYER_ID,
    _armed_timer,
    _make_player,
    _start_metadata_session,
)


def _controlled_session(
    version: tuple[int, int] | None = (5, 6),
    stream_type: str | None = "Buffered",
    topic: str | None = "shairport/kitchen",
) -> tuple[ShairportSyncPlayer, MagicMock]:
    """Create a player whose receiver publishes to MQTT, with a foreign session playing."""
    player = _make_player()
    mqtt = MagicMock()
    mqtt.topic_for.return_value = topic
    mqtt.version_for.return_value = version
    mqtt.publish_remote = AsyncMock()
    cast("MagicMock", player.provider).shairport_mqtt = mqtt
    _start_metadata_session(player)
    if stream_type:
        player.on_shairport_metadata({"stream_type": stream_type})
    return player, mqtt


def _answer_with(player: ShairportSyncPlayer, mqtt: MagicMock, update: dict[str, Any]) -> None:
    """Have the receiver report the given update for every command that is published."""

    async def publish_remote(_player_id: str, _command: str) -> None:
        player.on_shairport_metadata(dict(update))

    mqtt.publish_remote.side_effect = publish_remote


def _published(mqtt: MagicMock) -> list[str]:
    """Return the remote commands that were published, in order."""
    return [call.args[1] for call in mqtt.publish_remote.await_args_list]


@pytest.mark.parametrize(
    ("version", "stream_type", "transport", "volume"),
    [
        ((5, 6), "Buffered", True, True),
        ((5, 6), "Realtime", True, True),
        ((5, 6), "Classic", True, False),
        ((4, 3), "Classic", True, True),
        ((4, 3), "Buffered", False, False),
        ((5, 5), "Realtime", False, False),
        (None, "Buffered", True, True),
        ((5, 6), None, True, True),
    ],
)
async def test_control_offered_per_version_and_stream_type(
    version: tuple[int, int] | None, stream_type: str | None, transport: bool, volume: bool
) -> None:
    """Control follows what the receiver's release can pass on to this kind of sender."""
    player, mqtt = _controlled_session(version=version, stream_type=stream_type)
    player.on_shairport_metadata({"volume": 50, "airplay_volume": -15.0})
    _answer_with(player, mqtt, {"volume": 60, "airplay_volume": -12.0})

    source = player.source_list[0]
    assert source.can_play_pause is transport
    assert source.can_next_previous is transport
    assert (PlayerFeature.NEXT_PREVIOUS in player.supported_features) is transport
    if volume:
        await player.volume_set(60)
        assert _published(mqtt) == ["volumeup", "volumeup"]
    else:
        with pytest.raises(PlayerCommandFailed):
            await player.volume_set(60)
        mqtt.publish_remote.assert_not_awaited()


async def test_session_stays_read_only_without_a_topic() -> None:
    """A receiver without a known MQTT topic only shows the session."""
    player, mqtt = _controlled_session(topic=None)

    assert player.source_list[0].can_play_pause is False
    with pytest.raises(PlayerCommandFailed):
        await player.pause()
    mqtt.publish_remote.assert_not_awaited()


@pytest.mark.parametrize(
    ("command", "payload", "answer"),
    [
        (lambda player: player.pause(), "pause", {"paused": True}),
        (lambda player: player.stop(), "stop", {"play_end": True}),
        (lambda player: player.next_track(), "nextitem", {"track_id": 2}),
        (lambda player: player.previous_track(), "previtem", {"title": "Earlier Song"}),
    ],
    ids=["pause", "stop", "next", "previous"],
)
async def test_commands_reach_the_sender(
    command: Callable[[ShairportSyncPlayer], Awaitable[None]],
    payload: str,
    answer: dict[str, Any],
) -> None:
    """Each transport command publishes its remote command and waits for its effect."""
    player, mqtt = _controlled_session()
    player.on_shairport_metadata({"title": "Song", "track_id": 1})
    _answer_with(player, mqtt, answer)

    await command(player)

    mqtt.publish_remote.assert_awaited_once_with(PLAYER_ID, payload)


async def test_play_resumes_a_paused_session() -> None:
    """Play on a paused session publishes play and is confirmed by the resume."""
    player, mqtt = _controlled_session()
    player.on_shairport_metadata({"paused": True})
    _answer_with(player, mqtt, {"resumed": True})

    await player.play()

    assert _published(mqtt) == ["play"]
    assert player.playback_state == PlaybackState.PLAYING


async def test_command_without_effect_is_not_checked() -> None:
    """Play on a session that already plays is sent without waiting for a change."""
    player, mqtt = _controlled_session()

    with patch("music_assistant.providers.airplay.shairport_player.COMMAND_CONFIRM_TIMEOUT", 0):
        await player.play()

    assert _published(mqtt) == ["play"]
    assert player.source_list[0].can_play_pause is True


async def test_unanswered_command_makes_the_session_read_only() -> None:
    """A command the sender ignores fails, and the session stays read-only until it ends."""
    player, mqtt = _controlled_session()

    with (
        patch("music_assistant.providers.airplay.shairport_player.COMMAND_CONFIRM_TIMEOUT", 0.01),
        pytest.raises(PlayerCommandFailed),
    ):
        await player.pause()

    assert player.source_list[0].can_play_pause is False
    assert PlayerFeature.NEXT_PREVIOUS not in player.supported_features
    with pytest.raises(PlayerCommandFailed):
        await player.pause()
    assert _published(mqtt) == ["pause"]

    # the next session starts out controllable again
    player.on_shairport_metadata({"play_end": True})
    player.on_shairport_metadata({"play_begin": True})

    assert player.source_list[0].can_play_pause is True


@pytest.mark.parametrize("topic", [None, "shairport/kitchen"], ids=["read_only", "ignored"])
async def test_stop_does_not_fail_on_a_session_that_cannot_be_stopped(topic: str | None) -> None:
    """A power off stops the player first, so a session we cannot stop must not fail it."""
    player, mqtt = _controlled_session(topic=topic)

    with patch("music_assistant.providers.airplay.shairport_player.COMMAND_CONFIRM_TIMEOUT", 0.01):
        await player.stop()

    assert _published(mqtt) == ([] if topic is None else ["stop"])
    assert player.active_source == "airplay"
    assert player.source_list[0].can_play_pause is False


@pytest.mark.parametrize("seconds_ago", [0, 40], ids=["reported", "playing_since"])
async def test_previous_that_restarts_the_track_is_confirmed(seconds_ago: int) -> None:
    """Previous past the first seconds restarts the track, which confirms it as well."""
    player, mqtt = _controlled_session()
    player.on_shairport_metadata({"title": "Song", "track_id": 1})
    player.on_shairport_metadata({"elapsed_time": 40 - seconds_ago, "duration": 240})
    player._attr_elapsed_time_last_updated = time.time() - seconds_ago
    _answer_with(player, mqtt, {"flush": True, "elapsed_time": 0, "duration": 240})

    with patch("music_assistant.providers.airplay.shairport_player.COMMAND_CONFIRM_TIMEOUT", 0.5):
        await player.previous_track()

    assert player.source_list[0].can_next_previous is True


async def test_volume_change_while_steps_echo_starts_from_the_target() -> None:
    """A volume change while earlier steps still echo back neither repeats them nor bounces."""
    player, mqtt = _controlled_session()
    player.on_shairport_metadata({"volume": 100, "airplay_volume": 0.0})
    receiver_db = [0.0]
    shown_levels: list[int | None] = []
    loop = asyncio.get_running_loop()

    async def publish_remote(_player_id: str, command: str) -> None:
        # the receiver applies the queued steps one after another and reports each of them
        def report_step() -> None:
            receiver_db[0] += VOLUME_STEP_DB if command == "volumeup" else -VOLUME_STEP_DB
            player.on_shairport_metadata(
                {"volume": convert_airplay_volume(receiver_db[0]), "airplay_volume": receiver_db[0]}
            )
            shown_levels.append(player.volume_level)

        loop.call_later(0.01 * len(_published(mqtt)), report_step)

    mqtt.publish_remote.side_effect = publish_remote

    await player.volume_set(50)
    await player.volume_set(50)
    await asyncio.sleep(0.01 * len(_published(mqtt)) + 0.05)

    assert _published(mqtt) == ["volumedown"] * 8
    assert receiver_db[0] == -15.0
    assert shown_levels == [convert_airplay_volume(-15.0)] * 8


async def test_volume_report_is_shown_once_missing_step_reports_time_out() -> None:
    """When fewer steps come back than were sent, the receiver's next report is shown."""
    player, mqtt = _controlled_session()
    player.on_shairport_metadata({"volume": 70, "airplay_volume": -9.0})

    async def publish_remote(_player_id: str, _command: str) -> None:
        if len(_published(mqtt)) == 1:
            player.on_shairport_metadata({"volume": 66, "airplay_volume": -10.875})

    mqtt.publish_remote.side_effect = publish_remote
    with patch("music_assistant.providers.airplay.shairport_player.COMMAND_CONFIRM_TIMEOUT", 0.05):
        await player.volume_set(50)
        await asyncio.sleep(0.1)

    player.on_shairport_metadata({"volume": 60, "airplay_volume": -12.0})

    assert player.volume_level == 60


async def test_pause_of_a_classic_sender_is_confirmed_by_its_flush() -> None:
    """A Classic sender pauses with a flush alone, which confirms the pause once nothing follows."""
    player, mqtt = _controlled_session(stream_type="Classic")

    async def publish_remote(_player_id: str, _command: str) -> None:
        player.on_shairport_metadata({"flush": True})
        _, pause_after_flush = _armed_timer(player, f"airplay_external_flush_{PLAYER_ID}")
        asyncio.get_running_loop().call_soon(pause_after_flush)

    mqtt.publish_remote.side_effect = publish_remote
    with patch("music_assistant.providers.airplay.shairport_player.COMMAND_CONFIRM_TIMEOUT", 0.5):
        await player.pause()

    assert player.playback_state == PlaybackState.PAUSED
    assert player.source_list[0].can_play_pause is True


async def test_skip_without_track_ids_is_confirmed_by_the_next_title() -> None:
    """A sender that sends no track ids confirms a skip with the metadata block of the next track."""
    player, mqtt = _controlled_session(stream_type="Classic")
    player.on_shairport_metadata({"metadata_start": True})
    player.on_shairport_metadata({"title": "First"})

    async def publish_remote(_player_id: str, _command: str) -> None:
        player.on_shairport_metadata({"metadata_start": True})
        player.on_shairport_metadata({"title": "Second"})

    mqtt.publish_remote.side_effect = publish_remote
    with patch("music_assistant.providers.airplay.shairport_player.COMMAND_CONFIRM_TIMEOUT", 0.5):
        await player.next_track()

    media = player.current_media
    assert media is not None
    assert media.title == "Second"
    assert player.source_list[0].can_next_previous is True


async def test_volume_steps_towards_the_requested_level() -> None:
    """Setting the volume publishes the steps and shows the level they reach right away."""
    player, mqtt = _controlled_session()
    player.on_shairport_metadata({"volume": 70, "airplay_volume": -9.0})
    shown_levels: list[int | None] = []

    async def publish_remote(_player_id: str, _command: str) -> None:
        shown_levels.append(player.volume_level)
        player.on_shairport_metadata({"volume": 45, "airplay_volume": -16.5})

    mqtt.publish_remote.side_effect = publish_remote

    await player.volume_set(50)

    # four steps of 1.875 dB from -9 dB pass the requested -15 dB and land on -16.5 dB
    assert _published(mqtt) == ["volumedown"] * 4
    assert shown_levels[0] == 45


async def test_muted_session_steps_up_from_the_lowest_volume() -> None:
    """A muted sender is stepped up from -30 dB, as shairport-sync leaves mute there first."""
    player, mqtt = _controlled_session()
    player.on_shairport_metadata({"volume": 0, "airplay_volume": -144.0})
    _answer_with(player, mqtt, {"volume": 6, "airplay_volume": -28.125})

    await player.volume_set(10)

    assert _published(mqtt) == ["volumeup", "volumeup"]


@pytest.mark.parametrize(
    ("current", "volume_level", "expected"),
    [
        (-15.0, 50, 0),
        (-15.0, 60, 2),
        (-15.0, 40, -2),
        (-30.0, 100, 16),
        (-30.0, 0, 0),
        (-29.0, 0, 0),
        (-27.0, 0, -1),
        (0.0, 0, -16),
    ],
)
def test_volume_steps(current: float, volume_level: int, expected: int) -> None:
    """Steps round away from zero but never step below -30 dB, where a step means mute."""
    assert volume_steps(current, volume_level) == expected


@pytest.mark.parametrize("current", [-30.0, -27.5, -15.0, -9.0, -3.3, 0.0])
def test_volume_steps_never_stop_short(current: float) -> None:
    """The reached volume lies at or past the requested level, so volume limits hold at once."""
    for volume_level in range(101):
        target = AIRPLAY_VOLUME_MIN - AIRPLAY_VOLUME_MIN * volume_level / 100
        reached = current + volume_steps(current, volume_level) * VOLUME_STEP_DB
        assert reached >= AIRPLAY_VOLUME_MIN - 1e-9
        if target > current:
            assert reached >= target - 1e-9
        elif target < current:
            assert reached <= target + 1e-9 or reached < AIRPLAY_VOLUME_MIN + VOLUME_STEP_DB
