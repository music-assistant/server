"""Tests for which source a player is resolved to be playing."""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import partial

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerSource

from music_assistant.models.player import Player
from tests.common import MockProtocol, MockProvider, attach_mock, mock_player_mass

PLAYER_ID = "player_1"


def _create_player() -> tuple[Player, MockProtocol]:
    """Create a player that was playing the Music Assistant queue, and its protocol."""
    provider = MockProvider("test_provider", mass=mock_player_mass())
    player, protocol = attach_mock(provider, "protocol_1", "Player 1", player_id=PLAYER_ID)
    player.set_active_mass_source(PLAYER_ID)
    return player, protocol


def test_a_source_a_trusting_player_lists_itself_takes_over_from_the_ma_queue() -> None:
    """Test a source the player reports as its own is what it is playing, if it is trusted."""
    player, protocol = _create_player()
    protocol._attr_trusts_reported_source = True
    # "YouTube Music" is deliberately a name that is absent from EXTERNAL_SOURCES
    protocol._attr_source_list = [
        PlayerSource(id="YouTube Music", name="YouTube Music", passive=True)
    ]
    protocol._attr_active_source = "YouTube Music"
    protocol._attr_playback_state = PlaybackState.PLAYING

    player.update_state(signal_event=False)

    assert player.active_source == "YouTube Music"


def test_a_listed_device_input_does_not_take_over_when_the_player_is_not_trusted() -> None:
    """Test a source of an untrusted player does not end the remembered MA queue."""
    player, protocol = _create_player()
    # "Wi-Fi" is the transport our own stream arrives on for some devices, which they
    # report as soon as that stream pauses - here right after MA stopped playback
    protocol._attr_source_list = [PlayerSource(id="Wi-Fi", name="Wi-Fi")]
    protocol._attr_active_source = "Wi-Fi"
    protocol._attr_playback_state = PlaybackState.PAUSED

    player.update_state(signal_event=False)

    assert player.active_source == PLAYER_ID


def test_an_unlisted_source_does_not_take_over_from_the_ma_queue() -> None:
    """Test a source nothing knows about is not trusted, since many players misreport."""
    player, protocol = _create_player()
    protocol._attr_source_list = []
    protocol._attr_active_source = "Some Service"
    protocol._attr_playback_state = PlaybackState.PLAYING

    player.update_state(signal_event=False)

    assert player.active_source == PLAYER_ID


def test_a_known_external_source_takes_over_from_the_ma_queue() -> None:
    """Test a source from the explicit list takes the player over."""
    player, protocol = _create_player()
    protocol._attr_source_list = []
    protocol._attr_active_source = "qobuz"
    protocol._attr_playback_state = PlaybackState.PLAYING

    player.update_state(signal_event=False)

    assert player.active_source == "qobuz"


@pytest.mark.parametrize(
    "resumed_state", [PlaybackState.PLAYING, PlaybackState.PAUSED, PlaybackState.IDLE]
)
def test_ma_source_expires_only_if_player_remains_idle(
    resumed_state: PlaybackState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the group queue selected when playback resumes before the idle timeout."""
    player, protocol = _create_player()
    pending: dict[str, Callable[[], None]] = {}
    source_timer = f"set_mass_source_{PLAYER_ID}"
    now = [1000.0]

    def schedule(
        delay: float,
        callback: Callable[..., None],
        *args: object,
        task_id: str | None = None,
        **kwargs: object,
    ) -> None:
        if task_id == source_timer:
            # a timer runs once its delay has passed
            def _fire() -> None:
                now[0] += delay
                callback(*args, **kwargs)

            pending[task_id] = _fire

    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(player.mass, "call_later", schedule)
    monkeypatch.setattr(player.mass, "cancel_timer", lambda task_id: pending.pop(task_id, None))
    player.set_active_mass_source("group_queue")
    protocol._attr_playback_state = PlaybackState.PLAYING
    player.update_state(signal_event=False)
    protocol._attr_playback_state = PlaybackState.IDLE
    player.update_state(signal_event=False)
    protocol._attr_playback_state = resumed_state
    player.update_state(signal_event=False)

    for callback in list(pending.values()):
        callback()

    expected_source = PLAYER_ID if resumed_state == PlaybackState.IDLE else "group_queue"
    assert player.active_source == expected_source


def test_an_early_wake_up_keeps_the_ma_source_until_its_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wake-up that runs before the source is due arms the rest of the wait instead."""
    player, protocol = _create_player()
    armed: list[tuple[float, Callable[[], None]]] = []
    now = [1000.0]

    def schedule(
        delay: float,
        callback: Callable[..., None],
        *args: object,
        task_id: str | None = None,
        **kwargs: object,
    ) -> None:
        if task_id == f"set_mass_source_{PLAYER_ID}":
            armed.append((delay, partial(callback, *args, **kwargs)))

    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(player.mass, "call_later", schedule)
    player.set_active_mass_source("group_queue")
    protocol._attr_playback_state = PlaybackState.PLAYING
    player.update_state(signal_event=False)
    protocol._attr_playback_state = PlaybackState.IDLE
    player.update_state(signal_event=False)

    now[0] += 4.9
    armed[-1][1]()
    assert player.active_source == "group_queue"
    assert armed[-1][0] == pytest.approx(0.1)

    now[0] += 0.1
    armed[-1][1]()
    assert player.active_source == PLAYER_ID
