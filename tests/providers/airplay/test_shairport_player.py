"""Tests for the sessions other senders run on a shairport-sync receiver."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from ipaddress import ip_address
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import MediaNotFoundError, PlayerCommandFailed

from music_assistant.constants import EXTERNAL_PAUSE_IDLE_TIMEOUT
from music_assistant.providers.airplay.constants import (
    AIRPLAY_DISCOVERY_TYPE,
    EXTERNAL_STATE_GRACE_S,
    RECEIVER_SESSION_ACTIVE_BIT,
    SHAIRPORT_SYNC_SESSION_BIT,
)
from music_assistant.providers.airplay.shairport_player import ShairportSyncPlayer

PLAYER_ID = "apaabbccddeeff"
OWN_DACP_ID = "0123456789ABCDEF"
FOREIGN_DACP_ID = 0xFEDCBA9876543210


def _service_info(flags: int) -> MagicMock:
    """Create an _airplay service-info mock that advertises the given status flags."""
    info = MagicMock()
    info.type = AIRPLAY_DISCOVERY_TYPE
    info.name = f"Kitchen.{AIRPLAY_DISCOVERY_TYPE}"
    info.decoded_properties = {"model": "ShairportSync", "flags": hex(flags)}
    info.properties = {
        key.encode(): value.encode() for key, value in info.decoded_properties.items()
    }
    info.ip_addresses_by_version.return_value = [ip_address("192.168.1.20")]
    return info


def _make_player(flags: int = 0) -> ShairportSyncPlayer:
    """Create a shairport-sync player with mocked provider state and no MQTT broker."""
    provider = MagicMock()
    provider.dacp_id = OWN_DACP_ID
    provider.logger = logging.getLogger("test.airplay.shairport")
    provider.shairport_mqtt = None
    provider.bridge_manager.is_streaming.return_value = False
    provider.mass.config.get_raw_player_config_value.side_effect = (
        lambda _player_id, _key, default=None: default
    )
    config = MagicMock()
    config.get_value.side_effect = lambda _key, default=None: default
    provider.mass.config.get_base_player_config.return_value = config
    provider.mass.player_queues.get.return_value = None
    provider.mass.players.get_player.return_value = None
    return ShairportSyncPlayer(
        provider=provider,
        player_id=PLAYER_ID,
        raop_discovery_info=None,
        airplay_discovery_info=_service_info(flags),
        address="192.168.1.20",
        display_name="Kitchen",
        manufacturer="AirPlay",
        model="ShairportSync",
        initial_volume=30,
    )


def _set_flags(player: ShairportSyncPlayer, flags: int) -> None:
    """Deliver an mDNS update carrying new status flags."""
    player.set_discovery_info(_service_info(flags), "Kitchen")


def _start_metadata_session(player: ShairportSyncPlayer) -> None:
    """Have a foreign sender connect and start playing, as metadata reports it."""
    player.on_shairport_metadata({"dacp_id": FOREIGN_DACP_ID})
    player.on_shairport_metadata({"sender_name": "Kyle's iPhone"})
    player.on_shairport_metadata({"play_begin": True})


def _armed_timer(player: ShairportSyncPlayer, task_id: str) -> tuple[float, Callable[[], None]]:
    """Return the delay and callback of the last timer armed under a task id."""
    for call in reversed(cast("MagicMock", player.mass.call_later).call_args_list):
        if call.kwargs.get("task_id") == task_id:
            return call.args[0], call.args[1]
    msg = f"no timer armed for {task_id}"
    raise AssertionError(msg)


def _after_grace_period() -> AbstractContextManager[MagicMock]:
    """Move the monotonic clock past the grace period that follows our own stream."""
    return patch("time.monotonic", return_value=time.monotonic() + EXTERNAL_STATE_GRACE_S)


@pytest.mark.parametrize("bit", [RECEIVER_SESSION_ACTIVE_BIT, SHAIRPORT_SYNC_SESSION_BIT])
def test_session_flag_starts_and_ends_external_session(bit: int) -> None:
    """Both session bits present a connected sender as the active airplay source."""
    player = _make_player()
    player.evaluate_session_flag()

    _set_flags(player, 0x4 | bit)

    assert player.active_source == "airplay"
    assert player.playback_state == PlaybackState.PLAYING
    assert player.current_media is None
    assert player.source_list[0].passive is True
    assert player.source_list[0].can_play_pause is False

    _set_flags(player, 0x4)

    assert player.active_source is None
    assert player.playback_state == PlaybackState.IDLE  # type: ignore[unreachable]


def test_other_status_bits_start_no_session() -> None:
    """Status bits other than the session bits never start a session."""
    player = _make_player()
    player.evaluate_session_flag()

    _set_flags(player, 0x4 | 0x400)

    assert player.active_source is None


def test_session_already_running_at_registration_is_found() -> None:
    """The evaluation right after registration presents a sender that already holds the receiver."""
    player = _make_player(flags=RECEIVER_SESSION_ACTIVE_BIT)

    player.evaluate_session_flag()

    assert player.active_source == "airplay"


def test_flag_while_streaming_is_checked_again_after_the_grace_period() -> None:
    """A flag raised during our own stream only starts a session once the grace period is over."""
    player = _make_player()
    player.evaluate_session_flag()
    player.stream = MagicMock(running=True)

    _set_flags(player, RECEIVER_SESSION_ACTIVE_BIT)

    assert player.active_source is None

    # our stream ends, and the sender that took the receiver over keeps the flag up
    player.stream = None
    player.record_stream_end()
    delay, recheck = _armed_timer(player, f"airplay_external_recheck_{PLAYER_ID}")
    assert delay == pytest.approx(EXTERNAL_STATE_GRACE_S, abs=1)
    with _after_grace_period():
        recheck()

    assert player.active_source == "airplay"


def test_flag_recheck_does_nothing_once_the_flag_cleared() -> None:
    """The check at the end of the grace period starts nothing for a flag that went down."""
    player = _make_player()
    player.evaluate_session_flag()
    player.stream = MagicMock(running=True)
    _set_flags(player, RECEIVER_SESSION_ACTIVE_BIT)
    player.stream = None
    player.record_stream_end()
    _, recheck = _armed_timer(player, f"airplay_external_recheck_{PLAYER_ID}")
    _set_flags(player, 0)

    with _after_grace_period():
        recheck()

    assert player.active_source is None


def test_own_stream_reclaims_the_receiver() -> None:
    """Our own stream reporting state clears the external session."""
    player = _make_player()
    player.evaluate_session_flag()
    _set_flags(player, RECEIVER_SESSION_ACTIVE_BIT)
    stream = MagicMock(running=True)
    player.stream = stream

    player.set_state_from_stream(state=PlaybackState.PLAYING, elapsed_time=0, stream=stream)

    assert player.active_source is None
    assert player.playback_state == PlaybackState.PLAYING


def test_metadata_session_shows_sender_track_and_volume() -> None:
    """Metadata of a foreign sender becomes the current media of the airplay source."""
    player = _make_player()
    _start_metadata_session(player)

    player.on_shairport_metadata({"metadata_start": True})
    player.on_shairport_metadata(
        {"title": "Song", "artist": "Artist", "album": "Album", "duration": 200, "track_id": 7}
    )
    player.on_shairport_metadata({"elapsed_time": 42})
    player.on_shairport_metadata({"volume": 70, "airplay_volume": -9.0})

    assert player.active_source == "airplay"
    assert player.source_list[0].name == "Kyle's iPhone"
    media = player.current_media
    assert media is not None
    assert (media.title, media.artist, media.album, media.duration) == (
        "Song",
        "Artist",
        "Album",
        200,
    )
    assert media.uri == f"airplay://{PLAYER_ID}/7"
    assert media.source_id == "airplay"
    assert player.elapsed_time == pytest.approx(42, abs=1)
    assert player.volume_level == 70


def test_new_track_replaces_the_previous_media() -> None:
    """A new track id drops the fields of the previous track instead of mixing them in."""
    player = _make_player()
    _start_metadata_session(player)
    player.on_shairport_metadata({"title": "First", "album": "Old Album", "track_id": 1})

    player.on_shairport_metadata({"title": "Second", "track_id": 2})

    media = player.current_media
    assert media is not None
    assert media.title == "Second"
    assert media.album is None


def test_paused_and_resumed_follow_the_sender() -> None:
    """Pause and resume events move the session between paused and playing."""
    player = _make_player()
    _start_metadata_session(player)

    player.on_shairport_metadata({"paused": True})
    assert player.playback_state == PlaybackState.PAUSED

    player.on_shairport_metadata({"resumed": True})
    assert player.playback_state == PlaybackState.PLAYING  # type: ignore[comparison-overlap]


def test_flush_only_pauses_when_nothing_follows() -> None:
    """A flush pauses after a short delay, which new progress of the next track cancels."""
    player = _make_player()
    _start_metadata_session(player)
    flush_timer = f"airplay_external_flush_{PLAYER_ID}"

    player.on_shairport_metadata({"flush": True})
    player.on_shairport_metadata({"elapsed_time": 0})

    cast("MagicMock", player.mass.cancel_timer).assert_called_with(flush_timer)
    assert player.playback_state == PlaybackState.PLAYING

    player.on_shairport_metadata({"flush": True})
    _, pause_after_flush = _armed_timer(player, flush_timer)
    pause_after_flush()

    assert player.playback_state == PlaybackState.PAUSED  # type: ignore[comparison-overlap]


def test_play_end_pauses_while_the_flag_holds_the_session() -> None:
    """With a session flag on the receiver, the end of playback leaves the session paused."""
    player = _make_player()
    player.evaluate_session_flag()
    _set_flags(player, RECEIVER_SESSION_ACTIVE_BIT)
    player.on_shairport_metadata({"dacp_id": FOREIGN_DACP_ID})

    player.on_shairport_metadata({"play_end": True})

    assert player.active_source == "airplay"
    assert player.playback_state == PlaybackState.PAUSED


@pytest.mark.parametrize("event", ["play_end", "active_end"])
def test_end_events_end_the_session_without_a_flag(event: str) -> None:
    """A receiver without a session flag ends the session with its play or active end."""
    player = _make_player()
    _start_metadata_session(player)

    player.on_shairport_metadata({event: True})

    assert player.active_source is None
    assert player.playback_state == PlaybackState.IDLE


def test_paused_session_ends_after_the_pause_timeout() -> None:
    """A paused session ends after the timeout, and the unchanged flag does not revive it."""
    player = _make_player()
    player.evaluate_session_flag()
    _set_flags(player, RECEIVER_SESSION_ACTIVE_BIT)
    player.on_shairport_metadata({"dacp_id": FOREIGN_DACP_ID})
    player.on_shairport_metadata({"paused": True})

    with patch("time.time", return_value=time.time() + EXTERNAL_PAUSE_IDLE_TIMEOUT + 1):
        player.update_state()

    assert player.active_source is None
    assert player.playback_state == PlaybackState.IDLE

    # an unrelated TXT update (e.g. a group id change) with the flag still set
    _set_flags(player, RECEIVER_SESSION_ACTIVE_BIT)

    assert player.active_source is None


def test_own_sender_metadata_is_ignored_after_our_stream() -> None:
    """Metadata of our own sender never becomes an external session, also after the grace period."""
    player = _make_player()

    player.on_shairport_metadata({"dacp_id": int(OWN_DACP_ID, 16)})
    player.on_shairport_metadata({"play_begin": True})

    assert player.active_source is None


def test_foreign_sender_is_followed_inside_the_grace_period() -> None:
    """A sender that connects right after our stream ended is presented straight away."""
    player = _make_player()
    player.record_stream_end()
    assert player.external_state_blocked is True

    _start_metadata_session(player)

    assert player.active_source == "airplay"


def test_unknown_sender_is_ignored_inside_the_grace_period() -> None:
    """Without a sender id, metadata inside the grace period is taken for our own stream."""
    player = _make_player()
    player.record_stream_end()

    player.on_shairport_metadata({"play_begin": True})

    assert player.active_source is None


def test_sender_identity_is_kept_while_streaming() -> None:
    """The sender id is recorded while we stream, so its later metadata is attributed right."""
    player = _make_player()
    player.stream = MagicMock(running=True)
    player.on_shairport_metadata({"dacp_id": FOREIGN_DACP_ID})
    player.on_shairport_metadata({"play_begin": True})
    assert player.active_source is None

    player.stream = None
    player.record_stream_end()
    player.on_shairport_metadata({"play_begin": True})

    assert player.active_source == "airplay"


def test_volume_level_follows_our_stream_while_it_runs() -> None:
    """The sender's volume is only shown while we do not stream, so our stream keeps our level."""
    player = _make_player()
    _start_metadata_session(player)
    player.on_shairport_metadata({"volume": 70, "airplay_volume": -9.0})
    assert player.volume_level == 70

    player.stream = MagicMock(running=True)

    assert player.volume_level == 30


async def test_artwork_is_served_while_current() -> None:
    """Artwork bytes of the sender are served under an id that changes with the image."""
    player = _make_player()
    _start_metadata_session(player)
    # the image proxy URL is the artwork path, which ends in the artwork id
    cast("MagicMock", player.mass.metadata.get_image_url).side_effect = lambda image, **_kwargs: (
        image.path
    )

    def served_artwork_id() -> str:
        assert player.current_media is not None
        assert player.current_media.image_url is not None
        return player.current_media.image_url.rsplit("/", 1)[-1]

    player.on_shairport_metadata({"artwork": b"first image"})
    first_id = served_artwork_id()
    player.on_shairport_metadata({"artwork": b"first image"})
    assert served_artwork_id() == first_id

    player.on_shairport_metadata({"artwork": b"second image"})

    assert served_artwork_id() != first_id
    assert await player.async_get_external_artwork(served_artwork_id()) == b"second image"
    with pytest.raises(MediaNotFoundError):
        await player.async_get_external_artwork(first_id)


@pytest.mark.parametrize(
    "command",
    [
        lambda player: player.stop(),
        lambda player: player.volume_set(50),
        lambda player: player.volume_mute(True),
    ],
    ids=["stop", "volume_set", "volume_mute"],
)
async def test_commands_fail_on_a_read_only_session(
    command: Callable[[ShairportSyncPlayer], Awaitable[None]],
) -> None:
    """Stop, volume and mute cannot be carried out on a session without control."""
    player = _make_player()
    _start_metadata_session(player)

    with pytest.raises(PlayerCommandFailed):
        await command(player)


async def test_volume_still_reaches_our_stream_without_a_session() -> None:
    """Without an external session the volume still goes to our own stream."""
    player = _make_player()
    stream = MagicMock(running=True)
    stream.send_cli_command = AsyncMock(return_value=True)
    player.stream = stream

    await player.volume_set(55)

    stream.send_cli_command.assert_awaited_once_with("VOLUME=55")
