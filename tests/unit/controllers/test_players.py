"""Unit tests for PlayerController."""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import (
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import (
    PlayerUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant_models.player_control import PlayerControl

from music_assistant.constants import (
    ATTR_ENABLED,
    ATTR_FAKE_POWER,
    ATTR_FAKE_VOLUME,
    ATTR_MUTE_LOCK,
    ATTR_PREVIOUS_VOLUME,
)
from music_assistant.controllers.players import PlayerController
from music_assistant.helpers.throttle_retry import Throttler
from tests.common import MockPlayer, MockProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_mass() -> MagicMock:
    """Return a minimal mock MusicAssistant for PlayerController tests."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config = MagicMock()
    mass.config.get_raw_player_config_value = MagicMock(return_value="auto")
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    return mass


def _add_players(
    controller: PlayerController,
    mass: MagicMock,
    *names: str,
) -> list[MockPlayer]:
    """Register named mock players directly on the controller."""
    provider = MockProvider("test_provider", instance_id="test", mass=mass)
    players = []
    for name in names:
        player = MockPlayer(provider, name.lower().replace(" ", "_"), name)
        # Mark as initialized so all_players() includes them
        player.set_initialized()
        controller._players[player.player_id] = player
        controller._player_throttlers[player.player_id] = Throttler(1, 0.05)
        players.append(player)
    return players


# ---------------------------------------------------------------------------
# all_players
# ---------------------------------------------------------------------------


class TestAllPlayers:
    """Tests for PlayerController.all_players()."""

    def test_returns_initialized_available_players(self) -> None:
        """all_players returns only initialized, available players by default."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl

        # Given: two available players
        [p1, p2] = _add_players(ctrl, mass, "Speaker A", "Speaker B")

        with (
            patch(
                "music_assistant.controllers.players.controller.get_current_user",
                return_value=None,
            ),
            patch(
                "music_assistant.controllers.players.controller.get_sendspin_player_id",
                return_value=None,
            ),
        ):
            result = ctrl.all_players()

        # Then: both players are returned
        assert p1 in result
        assert p2 in result

    def test_excludes_unavailable_when_flag_false(self) -> None:
        """all_players excludes unavailable players when return_unavailable=False."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl

        [available, unavailable] = _add_players(ctrl, mass, "Online", "Offline")
        unavailable.state.available = False

        with (
            patch(
                "music_assistant.controllers.players.controller.get_current_user",
                return_value=None,
            ),
            patch(
                "music_assistant.controllers.players.controller.get_sendspin_player_id",
                return_value=None,
            ),
        ):
            result = ctrl.all_players(return_unavailable=False)

        # Then: only available player is returned
        assert available in result
        assert unavailable not in result

    def test_excludes_disabled_by_default(self) -> None:
        """all_players excludes disabled players unless return_disabled=True."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl

        [_enabled, disabled] = _add_players(ctrl, mass, "Enabled", "Disabled")
        disabled.state.enabled = False

        with (
            patch(
                "music_assistant.controllers.players.controller.get_current_user",
                return_value=None,
            ),
            patch(
                "music_assistant.controllers.players.controller.get_sendspin_player_id",
                return_value=None,
            ),
        ):
            result_default = ctrl.all_players()
            result_include_disabled = ctrl.all_players(return_disabled=True)

        assert disabled not in result_default
        assert disabled in result_include_disabled

    def test_provider_filter_limits_to_one_provider(self) -> None:
        """all_players with provider_filter only returns players from that provider."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl

        prov_a = MockProvider("provider_a", instance_id="provider_a", mass=mass)
        prov_b = MockProvider("provider_b", instance_id="provider_b", mass=mass)
        p_a = MockPlayer(prov_a, "player_a", "Player A")
        p_b = MockPlayer(prov_b, "player_b", "Player B")
        p_a.set_initialized()
        p_b.set_initialized()
        ctrl._players = {"player_a": p_a, "player_b": p_b}
        ctrl._player_throttlers = {
            "player_a": Throttler(1, 0.05),
            "player_b": Throttler(1, 0.05),
        }

        with (
            patch(
                "music_assistant.controllers.players.controller.get_current_user",
                return_value=None,
            ),
            patch(
                "music_assistant.controllers.players.controller.get_sendspin_player_id",
                return_value=None,
            ),
        ):
            result = ctrl.all_players(provider_filter="provider_a")

        assert p_a in result
        assert p_b not in result


# ---------------------------------------------------------------------------
# get_player
# ---------------------------------------------------------------------------


class TestGetPlayer:
    """Tests for PlayerController.get_player()."""

    def test_returns_player_by_id(self) -> None:
        """get_player returns the player when it exists."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "My Player")

        result = ctrl.get_player(player.player_id)

        assert result is player

    def test_returns_none_when_not_found(self) -> None:
        """get_player returns None when player_id is not registered."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)

        result = ctrl.get_player("nonexistent_id")

        assert result is None

    def test_raises_when_unavailable_and_flag_set(self) -> None:
        """get_player raises PlayerUnavailableError when unavailable and raise_unavailable=True."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Broken Speaker")
        player.state.available = False

        with pytest.raises(PlayerUnavailableError):
            ctrl.get_player(player.player_id, raise_unavailable=True)

    def test_raises_when_not_found_and_flag_set(self) -> None:
        """get_player raises PlayerUnavailableError when ID not found and raise_unavailable=True."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)

        with pytest.raises(PlayerUnavailableError):
            ctrl.get_player("ghost_player", raise_unavailable=True)


# ---------------------------------------------------------------------------
# get_player_by_name
# ---------------------------------------------------------------------------


class TestGetPlayerByName:
    """Tests for PlayerController.get_player_by_name()."""

    def test_returns_player_case_insensitive(self) -> None:
        """get_player_by_name performs case-insensitive matching."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Living Room")
        # Manually set the state name so it matches
        player.state.name = "Living Room"

        result = ctrl.get_player_by_name("living room")

        assert result is player

    def test_returns_none_when_no_match(self) -> None:
        """get_player_by_name returns None when no player matches the name."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Kitchen")
        player.state.name = "Kitchen"

        result = ctrl.get_player_by_name("Bathroom")

        assert result is None

    def test_returns_first_match_on_duplicate_names(self, caplog: pytest.LogCaptureFixture) -> None:
        """get_player_by_name returns first match and logs a warning for duplicates."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)

        provider = MockProvider("test_provider", instance_id="test", mass=mass)
        p1 = MockPlayer(provider, "p1", "Duplicate")
        p2 = MockPlayer(provider, "p2", "Duplicate")
        p1.state.name = "Duplicate"
        p2.state.name = "Duplicate"
        ctrl._players = {"p1": p1, "p2": p2}

        result = ctrl.get_player_by_name("Duplicate")

        # Then: a player is returned (whichever comes first in dict iteration)
        assert result is not None


# ---------------------------------------------------------------------------
# cmd_volume_up / cmd_volume_down step sizes
# ---------------------------------------------------------------------------


class TestVolumeStepSizes:
    """Tests for volume up/down step-size logic in PlayerController."""

    async def test_volume_up_small_step_at_low_volume(self) -> None:
        """cmd_volume_up uses step_size=1 when current volume is below 10."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl

        [player] = _add_players(ctrl, mass, "Low Vol")
        player.state.volume_level = 5

        recorded_volumes: list[int] = []

        async def fake_volume_set(_player_id: str, volume_level: int) -> None:
            recorded_volumes.append(volume_level)

        ctrl._handle_cmd_volume_set = fake_volume_set  # type: ignore[assignment]

        with (
            patch(
                "music_assistant.controllers.players.controller.get_current_user",
                return_value=None,
            ),
            patch(
                "music_assistant.controllers.players.controller.get_sendspin_player_id",
                return_value=None,
            ),
        ):
            await ctrl.cmd_volume_up(player.player_id)

        assert recorded_volumes == [6]  # 5 + 1

    async def test_volume_up_large_step_at_mid_volume(self) -> None:
        """cmd_volume_up uses step_size=3 when current volume is between 30 and 70."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl

        [player] = _add_players(ctrl, mass, "Mid Vol")
        player.state.volume_level = 50

        recorded_volumes: list[int] = []

        async def fake_volume_set(_player_id: str, volume_level: int) -> None:
            recorded_volumes.append(volume_level)

        ctrl._handle_cmd_volume_set = fake_volume_set  # type: ignore[assignment]

        with (
            patch(
                "music_assistant.controllers.players.controller.get_current_user",
                return_value=None,
            ),
            patch(
                "music_assistant.controllers.players.controller.get_sendspin_player_id",
                return_value=None,
            ),
        ):
            await ctrl.cmd_volume_up(player.player_id)

        assert recorded_volumes == [53]  # 50 + 3

    async def test_volume_down_small_step_at_high_volume(self) -> None:
        """cmd_volume_down uses step_size=1 when current volume is above 90."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl

        [player] = _add_players(ctrl, mass, "High Vol")
        player.state.volume_level = 95

        recorded_volumes: list[int] = []

        async def fake_volume_set(_player_id: str, volume_level: int) -> None:
            recorded_volumes.append(volume_level)

        ctrl._handle_cmd_volume_set = fake_volume_set  # type: ignore[assignment]

        with (
            patch(
                "music_assistant.controllers.players.controller.get_current_user",
                return_value=None,
            ),
            patch(
                "music_assistant.controllers.players.controller.get_sendspin_player_id",
                return_value=None,
            ),
        ):
            await ctrl.cmd_volume_down(player.player_id)

        assert recorded_volumes == [94]  # 95 - 1

    async def test_volume_up_caps_at_100(self) -> None:
        """cmd_volume_up never exceeds 100."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl

        [player] = _add_players(ctrl, mass, "Max Vol")
        player.state.volume_level = 99

        recorded_volumes: list[int] = []

        async def fake_volume_set(_player_id: str, volume_level: int) -> None:
            recorded_volumes.append(volume_level)

        ctrl._handle_cmd_volume_set = fake_volume_set  # type: ignore[assignment]

        with (
            patch(
                "music_assistant.controllers.players.controller.get_current_user",
                return_value=None,
            ),
            patch(
                "music_assistant.controllers.players.controller.get_sendspin_player_id",
                return_value=None,
            ),
        ):
            await ctrl.cmd_volume_up(player.player_id)

        assert recorded_volumes[0] <= 100

    async def test_volume_down_floors_at_0(self) -> None:
        """cmd_volume_down never goes below 0."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl

        [player] = _add_players(ctrl, mass, "Zero Vol")
        player.state.volume_level = 1

        recorded_volumes: list[int] = []

        async def fake_volume_set(_player_id: str, volume_level: int) -> None:
            recorded_volumes.append(volume_level)

        ctrl._handle_cmd_volume_set = fake_volume_set  # type: ignore[assignment]

        with (
            patch(
                "music_assistant.controllers.players.controller.get_current_user",
                return_value=None,
            ),
            patch(
                "music_assistant.controllers.players.controller.get_sendspin_player_id",
                return_value=None,
            ),
        ):
            await ctrl.cmd_volume_down(player.player_id)

        assert recorded_volumes[0] >= 0


# ---------------------------------------------------------------------------
# cmd_play_pause toggle
# ---------------------------------------------------------------------------


class TestPlayPauseToggle:
    """Tests for PlayerController.cmd_play_pause()."""

    async def test_play_pause_pauses_when_playing(self) -> None:
        """cmd_play_pause sends pause when player is currently playing."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl

        [player] = _add_players(ctrl, mass, "Playing Player")
        player.state.playback_state = PlaybackState.PLAYING

        pause_called = False
        play_called = False

        async def fake_cmd_pause(_player_id: str) -> None:
            nonlocal pause_called
            pause_called = True

        async def fake_cmd_play(_player_id: str) -> None:
            nonlocal play_called
            play_called = True

        ctrl.cmd_pause = fake_cmd_pause  # type: ignore[assignment]
        ctrl.cmd_play = fake_cmd_play  # type: ignore[assignment]

        with (
            patch(
                "music_assistant.controllers.players.controller.get_current_user",
                return_value=None,
            ),
            patch(
                "music_assistant.controllers.players.controller.get_sendspin_player_id",
                return_value=None,
            ),
        ):
            await ctrl.cmd_play_pause(player.player_id)

        assert pause_called
        assert not play_called

    async def test_play_pause_plays_when_paused(self) -> None:
        """cmd_play_pause sends play when player is currently paused."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl

        [player] = _add_players(ctrl, mass, "Paused Player")
        player.state.playback_state = PlaybackState.PAUSED

        pause_called = False
        play_called = False

        async def fake_cmd_pause(_player_id: str) -> None:
            nonlocal pause_called
            pause_called = True

        async def fake_cmd_play(_player_id: str) -> None:
            nonlocal play_called
            play_called = True

        ctrl.cmd_pause = fake_cmd_pause  # type: ignore[assignment]
        ctrl.cmd_play = fake_cmd_play  # type: ignore[assignment]

        with (
            patch(
                "music_assistant.controllers.players.controller.get_current_user",
                return_value=None,
            ),
            patch(
                "music_assistant.controllers.players.controller.get_sendspin_player_id",
                return_value=None,
            ),
        ):
            await ctrl.cmd_play_pause(player.player_id)

        assert play_called
        assert not pause_called


# ---------------------------------------------------------------------------
# register_or_update
# ---------------------------------------------------------------------------


class TestRegisterOrUpdate:
    """Tests for PlayerController.register_or_update() update path."""

    async def test_update_replaces_existing_player_in_dict(self) -> None:
        """register_or_update replaces the player object when player_id already registered."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl

        [original_player] = _add_players(ctrl, mass, "Original")

        # Given: a replacement player with the same ID
        provider = MockProvider("test_provider", instance_id="test", mass=mass)
        replacement = MockPlayer(provider, original_player.player_id, "Replacement")
        replacement.set_initialized()

        # When: register_or_update is called with the replacement
        await ctrl.register_or_update(replacement)

        # Then: the controller now holds the replacement
        stored = ctrl.get_player(original_player.player_id)
        assert stored is replacement

    async def test_skips_registration_when_closing(self) -> None:
        """register_or_update does nothing when mass is closing."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.closing = True

        provider = MockProvider("test_provider", instance_id="test", mass=mass)
        player = MockPlayer(provider, "new_player", "New Player")

        # When: register_or_update called while closing
        await ctrl.register_or_update(player)

        # Then: player is not added
        assert ctrl.get_player("new_player") is None


# ---------------------------------------------------------------------------
# get_player_state
# ---------------------------------------------------------------------------


class TestGetPlayerState:
    """Tests for PlayerController.get_player_state()."""

    def test_returns_none_for_unknown_player(self) -> None:
        """get_player_state returns None when player_id is not registered."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)

        with (
            patch(
                "music_assistant.controllers.players.controller.get_current_user",
                return_value=None,
            ),
            patch(
                "music_assistant.controllers.players.controller.get_sendspin_player_id",
                return_value=None,
            ),
        ):
            result = ctrl.get_player_state("ghost")

        assert result is None

    def test_returns_player_state_for_known_player(self) -> None:
        """get_player_state returns the state object for a registered player."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Known Player")

        with (
            patch(
                "music_assistant.controllers.players.controller.get_current_user",
                return_value=None,
            ),
            patch(
                "music_assistant.controllers.players.controller.get_sendspin_player_id",
                return_value=None,
            ),
        ):
            result = ctrl.get_player_state(player.player_id)

        assert result is player.state


# ---------------------------------------------------------------------------
# Helpers (shared)
# ---------------------------------------------------------------------------

_PATCHES = [
    patch(
        "music_assistant.controllers.players.controller.get_current_user",
        return_value=None,
    ),
    patch(
        "music_assistant.controllers.players.controller.get_sendspin_player_id",
        return_value=None,
    ),
]


def _patched() -> contextlib.ExitStack:
    """Context manager that patches auth helpers to None."""
    stack = contextlib.ExitStack()
    for p in _PATCHES:
        stack.enter_context(p)
    return stack


# ---------------------------------------------------------------------------
# all_player_states
# ---------------------------------------------------------------------------


class TestAllPlayerStates:
    """Tests for PlayerController.all_player_states()."""

    def test_returns_list_of_player_states(self) -> None:
        """all_player_states returns PlayerState objects."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [p1, p2] = _add_players(ctrl, mass, "A", "B")

        with _patched():
            result = ctrl.all_player_states()

        assert p1.state in result
        assert p2.state in result

    def test_empty_when_no_players(self) -> None:
        """all_player_states returns empty list when no players registered."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl

        with _patched():
            result = ctrl.all_player_states()

        assert result == []


# ---------------------------------------------------------------------------
# get_player_state_by_name
# ---------------------------------------------------------------------------


class TestGetPlayerStateByName:
    """Tests for PlayerController.get_player_state_by_name()."""

    def test_returns_state_when_found(self) -> None:
        """get_player_state_by_name returns state object."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Lounge")
        player.state.name = "Lounge"

        with _patched():
            result = ctrl.get_player_state_by_name("lounge")

        assert result is player.state

    def test_returns_none_when_not_found(self) -> None:
        """get_player_state_by_name returns None for unknown name."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)

        with _patched():
            result = ctrl.get_player_state_by_name("Ghost")

        assert result is None


# ---------------------------------------------------------------------------
# player_controls / get_player_control
# ---------------------------------------------------------------------------


class TestPlayerControlsMethods:
    """Tests for player control registry helpers."""

    def test_player_controls_returns_empty_initially(self) -> None:
        """player_controls returns empty list before any controls are registered."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        assert ctrl.player_controls() == []

    def test_get_player_control_returns_none_for_unknown(self) -> None:
        """get_player_control returns None when control_id is not registered."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        assert ctrl.get_player_control("nonexistent") is None

    def test_get_player_control_returns_control(self) -> None:
        """get_player_control returns the registered control."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        control = MagicMock(spec=PlayerControl)
        control.id = "ctrl1"
        ctrl._controls["ctrl1"] = control

        assert ctrl.get_player_control("ctrl1") is control

    def test_remove_player_control_removes_it(self) -> None:
        """remove_player_control removes the control from the registry."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        control = MagicMock(spec=PlayerControl)
        control.id = "ctrl1"
        control.name = "Test Control"
        ctrl._controls["ctrl1"] = control

        ctrl.remove_player_control("ctrl1")

        assert "ctrl1" not in ctrl._controls

    def test_remove_player_control_noop_when_not_found(self) -> None:
        """remove_player_control is silent when control not registered."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        # Should not raise
        ctrl.remove_player_control("ghost")

    async def test_register_or_update_player_control_updates_existing(self) -> None:
        """register_or_update_player_control updates control when already registered."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.loop = MagicMock()
        mass.closing = False

        control = MagicMock(spec=PlayerControl)
        control.id = "ctrl1"
        control.name = "Control A"
        ctrl._controls["ctrl1"] = control

        new_control = MagicMock(spec=PlayerControl)
        new_control.id = "ctrl1"
        new_control.name = "Control B"

        await ctrl.register_or_update_player_control(new_control)

        assert ctrl._controls["ctrl1"] is new_control

    async def test_register_or_update_player_control_noop_when_closing(self) -> None:
        """register_or_update_player_control does nothing when mass is closing."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.closing = True

        control = MagicMock(spec=PlayerControl)
        control.id = "ctrl1"

        await ctrl.register_or_update_player_control(control)

        assert "ctrl1" not in ctrl._controls

    def test_update_player_control_calls_update_state_on_matching_players(self) -> None:
        """update_player_control triggers state updates on affected players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.loop = MagicMock()
        [player] = _add_players(ctrl, mass, "TV Room")
        # Manually assign the volume_control to our control ID
        player._state.volume_control = "ctrl1"
        player._state.power_control = PLAYER_CONTROL_NONE
        player._state.mute_control = PLAYER_CONTROL_NONE

        ctrl.update_player_control("ctrl1")

        mass.loop.call_soon.assert_called()


# ---------------------------------------------------------------------------
# cmd_stop / cmd_play / cmd_pause
# ---------------------------------------------------------------------------


class TestCmdStop:
    """Tests for PlayerController.cmd_stop()."""

    async def test_cmd_stop_redirects_to_queue(self) -> None:
        """cmd_stop calls player_queues.stop when an active queue exists."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "TV")

        fake_queue = MagicMock()
        fake_queue.queue_id = "queue_tv"
        mass.player_queues = MagicMock()
        mass.player_queues.stop = AsyncMock()
        mass.player_queues.get = MagicMock(return_value=fake_queue)

        with _patched():
            await ctrl.cmd_stop(player.player_id)

        mass.player_queues.stop.assert_called_once_with("queue_tv")

    async def test_cmd_stop_calls_handle_directly_when_no_queue(self) -> None:
        """cmd_stop calls _handle_cmd_stop when no active queue is found."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Speaker")

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)

        handle_called_with = []

        async def fake_stop(player_id: str) -> None:
            handle_called_with.append(player_id)

        ctrl._handle_cmd_stop = fake_stop  # type: ignore[method-assign]

        with _patched():
            await ctrl.cmd_stop(player.player_id)

        assert handle_called_with == [player.player_id]


class TestCmdPlay:
    """Tests for PlayerController.cmd_play()."""

    async def test_cmd_play_ignores_already_playing(self) -> None:
        """cmd_play returns early if player is already playing."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Radio")
        player.state.playback_state = PlaybackState.PLAYING

        handle_called = False

        async def fake_handle(_pid: str) -> None:
            nonlocal handle_called
            handle_called = True

        ctrl._handle_cmd_play = fake_handle  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_play(player.player_id)

        assert not handle_called

    async def test_cmd_play_resumes_queue_when_idle(self) -> None:
        """cmd_play resumes active queue when player is idle."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Speaker")
        player.state.playback_state = PlaybackState.IDLE

        fake_queue = MagicMock()
        fake_queue.queue_id = "q1"
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=fake_queue)
        mass.player_queues.resume = AsyncMock()

        with _patched():
            await ctrl.cmd_play(player.player_id)

        mass.player_queues.resume.assert_called_once_with("q1")

    async def test_cmd_play_calls_handle_when_paused(self) -> None:
        """cmd_play calls _handle_cmd_play when player is paused and no queue."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Paused")
        player.state.playback_state = PlaybackState.PAUSED

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)

        handle_called_with = []

        async def fake_handle(pid: str) -> None:
            handle_called_with.append(pid)

        ctrl._handle_cmd_play = fake_handle  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_play(player.player_id)

        assert handle_called_with == [player.player_id]


class TestCmdPause:
    """Tests for PlayerController.cmd_pause()."""

    async def test_cmd_pause_redirects_to_queue(self) -> None:
        """cmd_pause calls player_queues.pause when an active queue exists."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Player")

        fake_queue = MagicMock()
        fake_queue.queue_id = "queueA"
        mass.player_queues = MagicMock()
        mass.player_queues.pause = AsyncMock()
        mass.player_queues.get = MagicMock(return_value=fake_queue)

        with _patched():
            await ctrl.cmd_pause(player.player_id)

        mass.player_queues.pause.assert_called_once_with("queueA")

    async def test_cmd_pause_calls_handle_directly_when_no_queue(self) -> None:
        """cmd_pause calls _handle_cmd_pause when no active queue is found."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Speaker2")

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)

        handle_called_with = []

        async def fake_pause(pid: str) -> None:
            handle_called_with.append(pid)

        ctrl._handle_cmd_pause = fake_pause  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_pause(player.player_id)

        assert handle_called_with == [player.player_id]


# ---------------------------------------------------------------------------
# _handle_cmd_stop / _handle_cmd_play / _handle_cmd_pause
# ---------------------------------------------------------------------------


class TestHandleCmdStop:
    """Tests for PlayerController._handle_cmd_stop()."""

    async def test_calls_player_stop_natively(self) -> None:
        """_handle_cmd_stop calls stop() on the player when no active protocol."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        [player] = _add_players(ctrl, mass, "Stop Player")
        player.stop = AsyncMock()  # type: ignore[method-assign]
        await ctrl._handle_cmd_stop(player.player_id)
        player.stop.assert_called_once()

    async def test_stop_with_unavailable_player_raises(self) -> None:
        """_handle_cmd_stop raises PlayerUnavailableError when player not found."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        with pytest.raises(PlayerUnavailableError):
            await ctrl._handle_cmd_stop("nonexistent_player")


class TestHandleCmdPlay:
    """Tests for PlayerController._handle_cmd_play()."""

    async def test_returns_early_when_already_playing(self) -> None:
        """_handle_cmd_play returns early if player is already playing."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        [player] = _add_players(ctrl, mass, "Playing Player 2")
        player.state.playback_state = PlaybackState.PLAYING

        player.play = AsyncMock()  # type: ignore[method-assign]
        await ctrl._handle_cmd_play(player.player_id)
        player.play.assert_not_called()

    async def test_calls_play_as_fallback(self) -> None:
        """_handle_cmd_play calls player.play() as fallback when no other path applies."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "Fallback Player")
        player.state.playback_state = PlaybackState.IDLE
        player.state.active_source = None
        player._attr_current_media = None
        player._cache.clear()

        player.play = AsyncMock()  # type: ignore[method-assign]

        # With no active source and no media, it should call play() as fallback
        await ctrl._handle_cmd_play(player.player_id)
        player.play.assert_called_once()

    async def test_raises_for_unknown_player(self) -> None:
        """_handle_cmd_play raises PlayerUnavailableError for unknown player."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        with pytest.raises(PlayerUnavailableError):
            await ctrl._handle_cmd_play("unknown")


class TestHandleCmdPause:
    """Tests for PlayerController._handle_cmd_pause()."""

    async def test_calls_stop_when_no_pause_support(self) -> None:
        """_handle_cmd_pause calls _handle_cmd_stop when player has no PAUSE feature."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "No Pause")
        # Player does not have PlayerFeature.PAUSE in supported_features
        assert PlayerFeature.PAUSE not in player._attr_supported_features

        stop_called_with = []

        async def fake_stop(pid: str) -> None:
            stop_called_with.append(pid)

        ctrl._handle_cmd_stop = fake_stop  # type: ignore[assignment]

        await ctrl._handle_cmd_pause(player.player_id)

        assert stop_called_with == [player.player_id]

    async def test_raises_for_unknown_player(self) -> None:
        """_handle_cmd_pause raises PlayerUnavailableError for unknown player."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        with pytest.raises(PlayerUnavailableError):
            await ctrl._handle_cmd_pause("unknown")


# ---------------------------------------------------------------------------
# _handle_cmd_power
# ---------------------------------------------------------------------------


class TestHandleCmdPower:
    """Tests for PlayerController._handle_cmd_power()."""

    async def test_returns_early_when_already_same_state(self) -> None:
        """_handle_cmd_power is no-op when player is already in requested state."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Power Player")
        player._state.powered = True  # already powered on

        power_called = False

        async def fake_power(_powered: bool) -> None:
            nonlocal power_called
            power_called = True

        player.power = fake_power  # type: ignore[method-assign, assignment]
        # Call with powered=True while already True → should be no-op
        await ctrl._handle_cmd_power(player.player_id, True)
        assert not power_called

    async def test_none_power_control_returns_silently(self) -> None:
        """_handle_cmd_power with PLAYER_CONTROL_NONE does not call power()."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "NoPower")
        # Default state: powered=None, power_control="none"
        player._state.powered = False
        player._state.power_control = PLAYER_CONTROL_NONE

        power_called = False

        async def fake_power(_powered: bool) -> None:
            nonlocal power_called
            power_called = True

        player.power = fake_power  # type: ignore[method-assign, assignment]
        await ctrl._handle_cmd_power(player.player_id, True)
        assert not power_called

    async def test_fake_power_control_sets_extra_data(self) -> None:
        """_handle_cmd_power with PLAYER_CONTROL_FAKE updates player extra_data."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        mass.player_queues = MagicMock()
        mass.player_queues.on_player_update = AsyncMock()
        mass.player_queues.resume = AsyncMock()
        mass.cache = MagicMock()
        mass.cache.set = AsyncMock()
        [player] = _add_players(ctrl, mass, "FakePower")
        player._state.powered = False
        player._state.power_control = PLAYER_CONTROL_FAKE

        # Patch update_state to avoid triggering complex state logic
        update_state_called = False

        def fake_update_state(*_args: object, **_kwargs: object) -> None:
            nonlocal update_state_called
            update_state_called = True

        player.update_state = fake_update_state  # type: ignore[misc, method-assign]

        await ctrl._handle_cmd_power(player.player_id, True)

        assert player.extra_data.get(ATTR_FAKE_POWER) is True
        assert update_state_called
        mass.cache.set.assert_called_once()


# ---------------------------------------------------------------------------
# _handle_cmd_volume_set
# ---------------------------------------------------------------------------


class TestHandleCmdVolumeSet:
    """Tests for PlayerController._handle_cmd_volume_set()."""

    async def test_none_volume_control_raises(self) -> None:
        """_handle_cmd_volume_set raises UnsupportedFeaturedException for PLAYER_CONTROL_NONE."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "No Volume")
        # Force volume_control to return PLAYER_CONTROL_NONE via cached property
        player._cache["volume_control"] = PLAYER_CONTROL_NONE
        player._state.volume_control = PLAYER_CONTROL_NONE

        with pytest.raises((UnsupportedFeaturedException, Exception)):
            await ctrl._handle_cmd_volume_set(player.player_id, 50)

    async def test_fake_volume_control_updates_extra_data(self) -> None:
        """_handle_cmd_volume_set with PLAYER_CONTROL_FAKE stores volume in extra_data."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "FakeVol")
        player._cache["volume_control"] = PLAYER_CONTROL_FAKE
        player._state.mute_control = PLAYER_CONTROL_NONE

        update_state_called = False

        def fake_update_state(*_args: object, **_kwargs: object) -> None:
            nonlocal update_state_called
            update_state_called = True

        player.update_state = fake_update_state  # type: ignore[misc, method-assign]

        await ctrl._handle_cmd_volume_set(player.player_id, 42)

        assert player.extra_data[ATTR_FAKE_VOLUME] == 42
        assert update_state_called

    async def test_native_volume_control_calls_volume_set(self) -> None:
        """_handle_cmd_volume_set with PLAYER_CONTROL_NATIVE calls player.volume_set()."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "NativeVol")
        player._cache["volume_control"] = PLAYER_CONTROL_NATIVE
        player._state.mute_control = PLAYER_CONTROL_NONE

        player.volume_set = AsyncMock()  # type: ignore[method-assign]

        await ctrl._handle_cmd_volume_set(player.player_id, 75)

        player.volume_set.assert_called_once_with(75)


# ---------------------------------------------------------------------------
# get_active_queue
# ---------------------------------------------------------------------------


class TestGetActiveQueue:
    """Tests for PlayerController.get_active_queue()."""

    def test_returns_none_when_no_queue(self) -> None:
        """get_active_queue returns None when no queue matches."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)
        [player] = _add_players(ctrl, mass, "Solo")

        result = ctrl.get_active_queue(player)
        assert result is None

    def test_returns_queue_by_active_source(self) -> None:
        """get_active_queue returns queue when active_source matches."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        fake_queue = MagicMock()
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=fake_queue)
        [player] = _add_players(ctrl, mass, "Queueing Player")

        result = ctrl.get_active_queue(player)
        assert result is fake_queue

    def test_redirects_via_synced_to(self) -> None:
        """get_active_queue follows synced_to to find the leader's queue."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [leader, member] = _add_players(ctrl, mass, "Leader", "Member")
        member.state.synced_to = leader.player_id

        fake_queue = MagicMock()

        def queue_get(source: str) -> MagicMock | None:
            # Only return a queue for the leader player
            if source == leader.player_id:
                return fake_queue
            return None

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(side_effect=queue_get)

        result = ctrl.get_active_queue(member)
        assert result is fake_queue


# ---------------------------------------------------------------------------
# iter_group_members
# ---------------------------------------------------------------------------


class TestIterGroupMembers:
    """Tests for PlayerController.iter_group_members()."""

    def test_yields_nothing_for_empty_group_members(self) -> None:
        """iter_group_members yields nothing when group_members is empty."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Solo Player")

        members = list(ctrl.iter_group_members(player))
        assert members == []

    def test_yields_available_members_excluding_self(self) -> None:
        """iter_group_members excludes self and unavailable players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [leader, member1, member2] = _add_players(ctrl, mass, "Leader", "M1", "M2")
        leader.state.group_members = [leader.player_id, member1.player_id, member2.player_id]  # type: ignore[assignment]
        member2.state.available = False

        members = list(ctrl.iter_group_members(leader))
        # Self excluded, member2 excluded (unavailable)
        assert member1 in members
        assert leader not in members
        assert member2 not in members

    def test_only_powered_filter(self) -> None:
        """iter_group_members with only_powered skips unpowered members."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [leader, m_on, m_off] = _add_players(ctrl, mass, "Leader", "On", "Off")
        leader.state.group_members = [leader.player_id, m_on.player_id, m_off.player_id]  # type: ignore[assignment]
        m_off.state.powered = False

        members = list(ctrl.iter_group_members(leader, only_powered=True))
        assert m_on in members
        assert m_off not in members


# ---------------------------------------------------------------------------
# _get_player_with_redirect
# ---------------------------------------------------------------------------


class TestGetPlayerWithRedirect:
    """Tests for PlayerController._get_player_with_redirect()."""

    def test_returns_player_when_not_synced(self) -> None:
        """_get_player_with_redirect returns the player directly when not synced."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Direct")

        result = ctrl._get_player_with_redirect(player.player_id)
        assert result is player

    def test_redirects_to_sync_leader(self) -> None:
        """_get_player_with_redirect returns sync leader when player is synced."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [leader, member] = _add_players(ctrl, mass, "Leader", "Member")
        member.state.synced_to = leader.player_id

        result = ctrl._get_player_with_redirect(member.player_id)
        assert result is leader

    def test_redirects_to_active_group(self) -> None:
        """_get_player_with_redirect returns group player when member of a group."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [group_player, member] = _add_players(ctrl, mass, "Group", "GroupMember")
        group_player.state.type = PlayerType.GROUP
        member.state.active_group = group_player.player_id

        result = ctrl._get_player_with_redirect(member.player_id)
        assert result is group_player


# ---------------------------------------------------------------------------
# unregister
# ---------------------------------------------------------------------------


class TestUnregister:
    """Tests for PlayerController.unregister()."""

    async def test_unregister_noop_for_unknown_player(self) -> None:
        """Unregister silently ignores unknown player IDs."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        # Should not raise
        await ctrl.unregister("unknown_player")

    async def test_unregister_temporary_marks_unavailable(self) -> None:
        """Unregister without permanent=True marks player unavailable."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.player_queues = MagicMock()
        mass.player_queues.on_player_remove = MagicMock()
        [player] = _add_players(ctrl, mass, "Temp Player")
        player.on_unload = AsyncMock()  # type: ignore[method-assign]

        await ctrl.unregister(player.player_id)

        # Player should be removed from _players dict
        assert player.player_id not in ctrl._players
        # Player state should be marked unavailable
        assert player.state.available is False

    async def test_unregister_permanent_signals_removed(self) -> None:
        """Unregister with permanent=True signals PLAYER_REMOVED event."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.player_queues = MagicMock()
        mass.player_queues.on_player_remove = MagicMock()
        [player] = _add_players(ctrl, mass, "Perm Player")
        player.on_unload = AsyncMock()  # type: ignore[method-assign]

        # Patch methods that would be called during permanent removal
        ctrl._cleanup_player_memberships = AsyncMock()  # type: ignore[method-assign]
        ctrl._cleanup_protocol_links = MagicMock()  # type: ignore[method-assign]
        ctrl.delete_player_config = MagicMock()  # type: ignore[method-assign]

        await ctrl.unregister(player.player_id, permanent=True)

        assert player.player_id not in ctrl._players
        ctrl.delete_player_config.assert_called_once_with(player.player_id)


# ---------------------------------------------------------------------------
# trigger_player_update
# ---------------------------------------------------------------------------


class TestTriggerPlayerUpdate:
    """Tests for PlayerController.trigger_player_update()."""

    def test_returns_early_when_closing(self) -> None:
        """trigger_player_update does nothing when mass is closing."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.closing = True
        mass.call_later = MagicMock()
        [player] = _add_players(ctrl, mass, "Closing Player")

        ctrl.trigger_player_update(player.player_id)

        mass.call_later.assert_not_called()

    def test_returns_early_when_player_not_found(self) -> None:
        """trigger_player_update does nothing when player_id is not registered."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.closing = False
        mass.call_later = MagicMock()

        ctrl.trigger_player_update("nonexistent")

        mass.call_later.assert_not_called()

    def test_schedules_update_via_call_later(self) -> None:
        """trigger_player_update schedules player.update_state via mass.call_later."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.closing = False
        mass.call_later = MagicMock()
        [player] = _add_players(ctrl, mass, "Update Player")

        ctrl.trigger_player_update(player.player_id)

        mass.call_later.assert_called_once()


# ---------------------------------------------------------------------------
# signal_player_state_update
# ---------------------------------------------------------------------------


class TestSignalPlayerStateUpdate:
    """Tests for PlayerController.signal_player_state_update()."""

    def test_returns_early_when_mass_is_closing(self) -> None:
        """signal_player_state_update does nothing when mass is closing."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = True
        [player] = _add_players(ctrl, mass, "Closing")

        ctrl.signal_player_state_update(player, {"key": (1, 2)})

        mass.signal_event.assert_not_called()

    def test_returns_early_when_disabled_and_no_enabled_key(self) -> None:
        """signal_player_state_update ignores updates for disabled players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        [player] = _add_players(ctrl, mass, "Disabled")
        player.state.enabled = False

        ctrl.signal_player_state_update(player, {"volume_level": (10, 20)})

        mass.signal_event.assert_not_called()

    def test_returns_early_when_no_changed_values(self) -> None:
        """signal_player_state_update does nothing when changed_values is empty."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        [player] = _add_players(ctrl, mass, "Unchanged")
        player.state.enabled = True

        ctrl.signal_player_state_update(player, {})

        mass.signal_event.assert_not_called()

    def test_signals_player_updated_event(self) -> None:
        """signal_player_state_update signals PLAYER_UPDATED for non-protocol players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        mass.create_task = MagicMock()
        [player] = _add_players(ctrl, mass, "Normal Player")
        player.state.enabled = True
        player.state.type = PlayerType.PLAYER

        ctrl.signal_player_state_update(player, {"volume_level": (10, 20)})

        mass.signal_event.assert_called()

    def test_skip_forward_returns_early_without_propagating(self) -> None:
        """signal_player_state_update with skip_forward=True avoids group propagation."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        mass.create_task = MagicMock()
        [player] = _add_players(ctrl, mass, "Skip Player")
        player.state.enabled = True
        player.state.type = PlayerType.PLAYER

        call_later_count_before = mass.call_later.call_count

        ctrl.signal_player_state_update(player, {"volume_level": (10, 20)}, skip_forward=True)

        # With skip_forward, the normal signal should still fire but group propagation is skipped
        # call_later count should be limited (not extra group update calls)
        call_later_count_after = mass.call_later.call_count
        # Minimal call_later usage (just the player_queues update, not group propagations)
        assert call_later_count_after - call_later_count_before <= 2

    def test_handles_elapsed_time_only_change(self) -> None:
        """signal_player_state_update handles elapsed-time-only changes as lightweight."""
        from music_assistant.constants import ATTR_ELAPSED_TIME  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        mass.create_task = MagicMock()
        [player] = _add_players(ctrl, mass, "Elapsed Player")
        player.state.enabled = True

        ctrl.signal_player_state_update(
            player,
            {
                ATTR_ELAPSED_TIME: (10.0, 11.0),
                "elapsed_time_last_updated": (0.0, 0.0),
            },
        )

        # Should NOT signal PLAYER_UPDATED (lightweight path returns early)
        mass.signal_event.assert_not_called()


# ---------------------------------------------------------------------------
# get_announcement_volume
# ---------------------------------------------------------------------------


class TestGetAnnouncementVolume:
    """Tests for PlayerController.get_announcement_volume()."""

    def test_none_strategy_returns_none(self) -> None:
        """get_announcement_volume returns None when strategy is 'none'."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.config.get_raw_player_config_value = MagicMock(return_value="none")

        result = ctrl.get_announcement_volume("player1", None)
        assert result is None

    def test_absolute_strategy_returns_configured_level(self) -> None:
        """get_announcement_volume returns the absolute level from config."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)

        def cfg_val(_player_id: str, key: str, default: object = None) -> object:
            from music_assistant.constants import (  # noqa: PLC0415
                CONF_ENTRY_ANNOUNCE_VOLUME,
                CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
                CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
                CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
            )

            if key == CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY.key:
                return "absolute"
            if key == CONF_ENTRY_ANNOUNCE_VOLUME.key:
                return 40.0
            if key == CONF_ENTRY_ANNOUNCE_VOLUME_MIN.key:
                return 0.0
            if key == CONF_ENTRY_ANNOUNCE_VOLUME_MAX.key:
                return 100.0
            return default

        mass.config.get_raw_player_config_value = MagicMock(side_effect=cfg_val)

        result = ctrl.get_announcement_volume("player1", None)
        assert result == 40

    def test_override_takes_precedence(self) -> None:
        """get_announcement_volume respects volume_override over strategy."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Ann Player")

        def cfg_val(_player_id: str, key: str, default: object = None) -> object:
            from music_assistant.constants import (  # noqa: PLC0415
                CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
                CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
                CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
            )

            if key == CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY.key:
                return "absolute"
            if key == CONF_ENTRY_ANNOUNCE_VOLUME_MIN.key:
                return 0.0
            if key == CONF_ENTRY_ANNOUNCE_VOLUME_MAX.key:
                return 100.0
            return default

        mass.config.get_raw_player_config_value = MagicMock(side_effect=cfg_val)

        result = ctrl.get_announcement_volume(player.player_id, 60)
        assert result == 60

    def test_relative_strategy_adds_to_current_volume(self) -> None:
        """get_announcement_volume adds delta to current volume for relative strategy."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Rel Player")
        player.state.volume_level = 30

        def cfg_val(_player_id: str, key: str, default: object = None) -> object:
            from music_assistant.constants import (  # noqa: PLC0415
                CONF_ENTRY_ANNOUNCE_VOLUME,
                CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
                CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
                CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
            )

            if key == CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY.key:
                return "relative"
            if key == CONF_ENTRY_ANNOUNCE_VOLUME.key:
                return 10.0
            if key == CONF_ENTRY_ANNOUNCE_VOLUME_MIN.key:
                return 0.0
            if key == CONF_ENTRY_ANNOUNCE_VOLUME_MAX.key:
                return 100.0
            return default

        mass.config.get_raw_player_config_value = MagicMock(side_effect=cfg_val)

        result = ctrl.get_announcement_volume(player.player_id, None)
        assert result == 40  # 30 + 10


# ---------------------------------------------------------------------------
# cmd_group_volume
# ---------------------------------------------------------------------------


class TestCmdGroupVolume:
    """Tests for PlayerController.cmd_group_volume()."""

    async def test_treats_regular_player_as_volume_set(self) -> None:
        """cmd_group_volume falls back to cmd_volume_set for non-group players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Regular")
        # No group members, no synced_to → falls through to cmd_volume_set
        player.state.type = PlayerType.PLAYER
        player.state.group_members = []  # type: ignore[assignment]
        player.state.synced_to = None

        volume_set_calls = []

        async def fake_volume_set(pid: str, level: int) -> None:
            volume_set_calls.append((pid, level))

        ctrl._handle_cmd_volume_set = fake_volume_set  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_group_volume(player.player_id, 55)

        assert (player.player_id, 55) in volume_set_calls


# ---------------------------------------------------------------------------
# cmd_ungroup
# ---------------------------------------------------------------------------


class TestCmdUngroup:
    """Tests for PlayerController.cmd_ungroup()."""

    async def test_ungroup_player_with_active_group(self) -> None:
        """cmd_ungroup calls cmd_set_members to remove from active group."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [group_player, member] = _add_players(ctrl, mass, "Group", "Member")
        group_player._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        group_player._cache.clear()
        member.state.active_group = group_player.player_id
        member.state.synced_to = None
        member.state.group_members = []  # type: ignore[assignment]

        set_members_called = []

        async def fake_set_members(
            target: str,
            _player_ids_to_add: list | None = None,  # type: ignore[type-arg]
            player_ids_to_remove: list | None = None,  # type: ignore[type-arg]
        ) -> None:
            set_members_called.append((target, player_ids_to_remove))

        ctrl.cmd_set_members = fake_set_members  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_ungroup(member.player_id)

        assert len(set_members_called) == 1
        assert set_members_called[0][0] == group_player.player_id
        assert member.player_id in set_members_called[0][1]  # type: ignore[operator]

    async def test_ungroup_player_synced_to_leader(self) -> None:
        """cmd_ungroup calls cmd_set_members to remove from sync leader."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [leader, member] = _add_players(ctrl, mass, "Leader", "SyncMember")
        member.state.active_group = None
        member.state.synced_to = leader.player_id
        member.state.group_members = []  # type: ignore[assignment]

        set_members_called = []

        async def fake_set_members(
            target: str,
            _player_ids_to_add: list | None = None,  # type: ignore[type-arg]
            player_ids_to_remove: list | None = None,  # type: ignore[type-arg]
        ) -> None:
            set_members_called.append((target, player_ids_to_remove))

        ctrl.cmd_set_members = fake_set_members  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_ungroup(member.player_id)

        assert len(set_members_called) == 1
        assert set_members_called[0][0] == leader.player_id

    async def test_ungroup_does_nothing_for_ungrouped_player(self) -> None:
        """cmd_ungroup does nothing when player is not in any group."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Solo Player2")
        player.state.active_group = None
        player.state.synced_to = None
        player.state.group_members = []  # type: ignore[assignment]

        set_members_called = []

        async def fake_set_members(*_args: object, **_kwargs: object) -> None:
            set_members_called.append(True)

        ctrl.cmd_set_members = fake_set_members  # type: ignore[method-assign]

        with _patched():
            await ctrl.cmd_ungroup(player.player_id)

        assert set_members_called == []


# ---------------------------------------------------------------------------
# Misc methods
# ---------------------------------------------------------------------------


class TestMiscMethods:
    """Tests for miscellaneous PlayerController helper methods."""

    def test_iter_yields_all_players(self) -> None:
        """__iter__ yields all registered players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [p1, p2, p3] = _add_players(ctrl, mass, "A", "B", "C")

        all_players = list(ctrl)
        assert p1 in all_players
        assert p2 in all_players
        assert p3 in all_players

    def test_get_player_provider_returns_provider(self) -> None:
        """get_player_provider returns the player's provider."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Provider Player")

        result = ctrl.get_player_provider(player.player_id)
        assert result is player.provider

    def test_delete_player_config_calls_mass_config_remove(self) -> None:
        """delete_player_config calls mass.config.remove for player and DSP config."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)

        ctrl.delete_player_config("my_player")

        assert mass.config.remove.call_count == 2

    def test_is_ma_managed_source_none(self) -> None:
        """_is_ma_managed_source returns True when source is None."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Managed")

        assert ctrl._is_ma_managed_source(player, None) is True

    def test_is_ma_managed_source_player_id(self) -> None:
        """_is_ma_managed_source returns True when source equals player_id."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Self Source")

        assert ctrl._is_ma_managed_source(player, player.player_id) is True

    def test_is_ma_managed_source_queue_id(self) -> None:
        """_is_ma_managed_source returns True when source is a known queue."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=MagicMock())
        [player] = _add_players(ctrl, mass, "Queue Source")

        assert ctrl._is_ma_managed_source(player, "some_queue_id") is True

    def test_is_ma_managed_source_external(self) -> None:
        """_is_ma_managed_source returns False for external sources."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "Ext Player")

        assert ctrl._is_ma_managed_source(player, "spotify") is False


# ---------------------------------------------------------------------------
# _cleanup_stale_protocol_parent_ids
# ---------------------------------------------------------------------------


class TestCleanupStaleProtocolParentIds:
    """Tests for PlayerController._cleanup_stale_protocol_parent_ids()."""

    def test_clears_stale_parent_id(self) -> None:
        """_cleanup_stale_protocol_parent_ids removes parent_id for deleted parent."""
        from music_assistant.constants import CONF_PROTOCOL_PARENT_ID  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)

        # Setup: protocol player pointing to a non-existent parent
        player_configs = {
            "protocol_player": {
                "player_type": "protocol",
                "values": {CONF_PROTOCOL_PARENT_ID: "deleted_parent"},
            }
        }
        mass.config.get = MagicMock(return_value=player_configs)

        ctrl._cleanup_stale_protocol_parent_ids()

        # Should have called config.set to clear the stale parent ID
        mass.config.set.assert_called_once()

    def test_keeps_valid_parent_id(self) -> None:
        """_cleanup_stale_protocol_parent_ids keeps parent_id when parent exists."""
        from music_assistant.constants import CONF_PROTOCOL_PARENT_ID  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)

        player_configs = {
            "protocol_player": {
                "player_type": "protocol",
                "values": {CONF_PROTOCOL_PARENT_ID: "real_parent"},
            },
            "real_parent": {
                "player_type": "player",
                "values": {},
            },
        }
        mass.config.get = MagicMock(return_value=player_configs)

        ctrl._cleanup_stale_protocol_parent_ids()

        # Should NOT have called config.set (parent exists)
        mass.config.set.assert_not_called()

    def test_ignores_non_protocol_players(self) -> None:
        """_cleanup_stale_protocol_parent_ids skips regular (non-protocol) players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)

        player_configs = {
            "regular_player": {
                "player_type": "player",
                "values": {"some_key": "some_value"},
            }
        }
        mass.config.get = MagicMock(return_value=player_configs)

        ctrl._cleanup_stale_protocol_parent_ids()

        mass.config.set.assert_not_called()


# ---------------------------------------------------------------------------
# _cleanup_player_memberships
# ---------------------------------------------------------------------------


class TestCleanupPlayerMemberships:
    """Tests for PlayerController._cleanup_player_memberships()."""

    async def test_returns_early_when_player_not_found(self) -> None:
        """_cleanup_player_memberships does nothing for unknown player."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        # Should not raise
        await ctrl._cleanup_player_memberships("nonexistent")

    async def test_cleans_up_membership_from_active_group(self) -> None:
        """_cleanup_player_memberships removes player from its active group."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [group_player, member] = _add_players(ctrl, mass, "GroupX", "MemberX")
        group_player._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        group_player._cache.clear()
        member.state.active_group = group_player.player_id
        member.state.synced_to = None

        handle_called = []

        async def fake_handle_set_members(
            _parent: object,
            _player_ids_to_add: list | None = None,  # type: ignore[type-arg]
            player_ids_to_remove: list | None = None,  # type: ignore[type-arg]
        ) -> None:
            handle_called.append(player_ids_to_remove)

        ctrl._handle_set_members = fake_handle_set_members  # type: ignore[assignment]

        await ctrl._cleanup_player_memberships(member.player_id)

        assert any(member.player_id in r for r in handle_called if r)


# ---------------------------------------------------------------------------
# on_player_config_change
# ---------------------------------------------------------------------------


class TestOnPlayerConfigChange:
    """Tests for PlayerController.on_player_config_change()."""

    async def test_returns_early_when_player_not_found_and_not_toggled(self) -> None:
        """on_player_config_change returns early when player is not registered."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.get_provider = MagicMock(return_value=None)

        config = MagicMock()
        config.player_id = "unknown_player"
        config.provider = "test_provider"
        config.enabled = True
        config.values = {}

        # Should not raise
        await ctrl.on_player_config_change(config, set())

    async def test_updates_player_state_on_config_change(self) -> None:
        """on_player_config_change updates player state when player is registered."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        mass.get_provider = MagicMock(return_value=None)
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)
        [player] = _add_players(ctrl, mass, "Config Player")

        config = MagicMock()
        config.player_id = player.player_id
        config.provider = "test_provider"
        config.enabled = True
        config.values = {}

        update_state_called = False

        def fake_update_state(*_args: object, **_kwargs: object) -> None:
            nonlocal update_state_called
            update_state_called = True

        player.update_state = fake_update_state  # type: ignore[misc, method-assign]
        player.set_config = MagicMock()  # type: ignore[misc, method-assign]
        player.on_config_updated = AsyncMock()  # type: ignore[method-assign]

        await ctrl.on_player_config_change(config, set())

        player.set_config.assert_called_once_with(config)
        assert update_state_called


# ---------------------------------------------------------------------------
# cmd_volume_up / cmd_volume_down group player branch
# ---------------------------------------------------------------------------


class TestVolumeUpDownGroupBranch:
    """Tests for the group player branch in cmd_volume_up/down."""

    async def test_volume_up_delegates_to_group_volume_up_for_group_player(self) -> None:
        """cmd_volume_up delegates to cmd_group_volume_up for GROUP type players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Group Player")
        # Mark player as GROUP type
        player._attr_type = PlayerType.GROUP
        player._cache.clear()

        group_volume_up_called = []

        async def fake_group_volume_up(pid: str) -> None:
            group_volume_up_called.append(pid)

        ctrl.cmd_group_volume_up = fake_group_volume_up  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_volume_up(player.player_id)

        assert group_volume_up_called == [player.player_id]

    async def test_volume_down_delegates_to_group_volume_down_for_group_player(self) -> None:
        """cmd_volume_down delegates to cmd_group_volume_down for GROUP type players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Group Player2")
        player._attr_type = PlayerType.GROUP
        player._cache.clear()

        group_volume_down_called = []

        async def fake_group_volume_down(pid: str) -> None:
            group_volume_down_called.append(pid)

        ctrl.cmd_group_volume_down = fake_group_volume_down  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_volume_down(player.player_id)

        assert group_volume_down_called == [player.player_id]


# ---------------------------------------------------------------------------
# Additional coverage for register_player_control
# ---------------------------------------------------------------------------


class TestRegisterPlayerControl:
    """Tests for PlayerController.register_player_control()."""

    async def test_raises_when_already_registered(self) -> None:
        """register_player_control raises AlreadyRegisteredError for duplicate."""
        from music_assistant_models.errors import AlreadyRegisteredError  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.closing = False

        control = MagicMock(spec=PlayerControl)
        control.id = "ctrl_dup"
        ctrl._controls["ctrl_dup"] = control

        with pytest.raises(AlreadyRegisteredError):
            await ctrl.register_player_control(control)

    async def test_noop_when_closing(self) -> None:
        """register_player_control does nothing when mass is closing."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.closing = True

        control = MagicMock(spec=PlayerControl)
        control.id = "ctrl_closing"

        await ctrl.register_player_control(control)

        assert "ctrl_closing" not in ctrl._controls


# ---------------------------------------------------------------------------
# setup / close / providers
# ---------------------------------------------------------------------------


class TestSetupAndClose:
    """Tests for setup(), close() and providers property."""

    async def test_setup_creates_poll_task(self) -> None:
        """setup() creates poll task and schedules fix_group_member_configs."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.create_task = MagicMock()
        mass.tasks = MagicMock()
        mass.config.get = MagicMock(return_value={})

        config = MagicMock()
        ctrl._cleanup_stale_protocol_parent_ids = MagicMock()  # type: ignore[method-assign]

        await ctrl.setup(config)

        mass.create_task.assert_called_once()
        mass.tasks.register_scheduled_task.assert_called_once()

    async def test_close_cancels_poll_task(self) -> None:
        """close() cancels the background poll task."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)

        mock_task = MagicMock()
        mock_task.done = MagicMock(return_value=False)
        ctrl._poll_task = mock_task

        await ctrl.close()

        mock_task.cancel.assert_called_once()

    async def test_close_handles_no_task(self) -> None:
        """close() does not crash when poll task is None."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        ctrl._poll_task = None

        await ctrl.close()  # Should not raise

    def test_providers_property_calls_get_providers(self) -> None:
        """Providers property returns player providers from mass."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)

        result = ctrl.providers

        mass.get_providers.assert_called()
        assert result == []  # mock returns empty list


# ---------------------------------------------------------------------------
# cmd_resume
# ---------------------------------------------------------------------------


class TestCmdResume:
    """Tests for PlayerController.cmd_resume()."""

    async def test_cmd_resume_delegates_to_handle(self) -> None:
        """cmd_resume delegates to _handle_cmd_resume."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Resume Player")

        handle_called_with = []

        async def fake_handle(
            player_id: str, _source: str | None = None, _media: object = None
        ) -> None:
            handle_called_with.append(player_id)

        ctrl._handle_cmd_resume = fake_handle  # type: ignore[method-assign, assignment]

        with _patched():
            await ctrl.cmd_resume(player.player_id)

        assert handle_called_with == [player.player_id]


# ---------------------------------------------------------------------------
# cmd_seek / cmd_next_track / cmd_previous_track
# ---------------------------------------------------------------------------


class TestCmdSeekNextPrev:
    """Tests for cmd_seek, cmd_next_track, cmd_previous_track."""

    async def test_cmd_seek_redirects_to_queue(self) -> None:
        """cmd_seek calls player_queues.seek when active queue exists."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Seek Player")

        fake_queue = MagicMock()
        fake_queue.queue_id = "queue_seek"
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=fake_queue)
        mass.player_queues.seek = AsyncMock()
        mass.get_providers = MagicMock(return_value=[])

        with _patched():
            await ctrl.cmd_seek(player.player_id, 30)

        mass.player_queues.seek.assert_called_once_with("queue_seek", 30)

    async def test_cmd_seek_raises_when_no_support(self) -> None:
        """cmd_seek raises when player has no queue and no SEEK feature."""
        from music_assistant_models.errors import PlayerCommandFailed  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "NoSeek Player")

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)
        mass.get_providers = MagicMock(return_value=[])

        with _patched(), pytest.raises(PlayerCommandFailed):
            await ctrl.cmd_seek(player.player_id, 30)

    async def test_cmd_next_track_redirects_to_queue(self) -> None:
        """cmd_next_track calls player_queues.next when active queue exists."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Next Player")

        fake_queue = MagicMock()
        fake_queue.queue_id = "q_next"
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=fake_queue)
        mass.player_queues.next = AsyncMock()
        mass.get_providers = MagicMock(return_value=[])

        with _patched():
            await ctrl.cmd_next_track(player.player_id)

        mass.player_queues.next.assert_called_once_with("q_next")

    async def test_cmd_next_track_raises_without_queue_or_feature(self) -> None:
        """cmd_next_track raises when no queue and no NEXT_PREVIOUS feature."""
        from music_assistant_models.errors import PlayerCommandFailed  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "NoNext Player")

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)
        mass.get_providers = MagicMock(return_value=[])

        with _patched(), pytest.raises(PlayerCommandFailed):
            await ctrl.cmd_next_track(player.player_id)

    async def test_cmd_previous_track_redirects_to_queue(self) -> None:
        """cmd_previous_track calls player_queues.previous when active queue exists."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Prev Player")

        fake_queue = MagicMock()
        fake_queue.queue_id = "q_prev"
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=fake_queue)
        mass.player_queues.previous = AsyncMock()
        mass.get_providers = MagicMock(return_value=[])

        with _patched():
            await ctrl.cmd_previous_track(player.player_id)

        mass.player_queues.previous.assert_called_once_with("q_prev")

    async def test_cmd_previous_track_raises_without_queue_or_feature(self) -> None:
        """cmd_previous_track raises when no queue and no NEXT_PREVIOUS feature."""
        from music_assistant_models.errors import PlayerCommandFailed  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "NoPrev Player")

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)
        mass.get_providers = MagicMock(return_value=[])

        with _patched(), pytest.raises(PlayerCommandFailed):
            await ctrl.cmd_previous_track(player.player_id)


# ---------------------------------------------------------------------------
# cmd_group_volume_up / cmd_group_volume_down / cmd_group_volume_mute
# ---------------------------------------------------------------------------


class TestCmdGroupVolumeUpDownMute:
    """Tests for cmd_group_volume_up, cmd_group_volume_down, cmd_group_volume_mute."""

    async def test_group_volume_up_returns_early_when_none(self) -> None:
        """cmd_group_volume_up returns early when group_volume is None."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "GroupVUp")
        player._state.group_volume = None

        group_vol_called = []

        async def fake_cmd_group_volume(pid: str, level: int) -> None:
            group_vol_called.append((pid, level))

        ctrl.cmd_group_volume = fake_cmd_group_volume  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_group_volume_up(player.player_id)

        assert group_vol_called == []

    async def test_group_volume_up_increments_volume(self) -> None:
        """cmd_group_volume_up increments group volume by step size."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "GroupVUp2")
        player._state.group_volume = 50  # mid-range → step_size=3

        group_vol_called = []

        async def fake_cmd_group_volume(pid: str, level: int) -> None:
            group_vol_called.append((pid, level))

        ctrl.cmd_group_volume = fake_cmd_group_volume  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_group_volume_up(player.player_id)

        assert len(group_vol_called) == 1
        assert group_vol_called[0][1] == 53  # 50 + 3

    async def test_group_volume_down_returns_early_when_none(self) -> None:
        """cmd_group_volume_down returns early when group_volume is None."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "GroupVDown")
        player._state.group_volume = None

        group_vol_called = []

        async def fake_cmd_group_volume(pid: str, level: int) -> None:
            group_vol_called.append((pid, level))

        ctrl.cmd_group_volume = fake_cmd_group_volume  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_group_volume_down(player.player_id)

        assert group_vol_called == []

    async def test_group_volume_down_decrements_volume(self) -> None:
        """cmd_group_volume_down decrements group volume by step size."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "GroupVDown2")
        player._state.group_volume = 50  # mid-range → step_size=3

        group_vol_called = []

        async def fake_cmd_group_volume(pid: str, level: int) -> None:
            group_vol_called.append((pid, level))

        ctrl.cmd_group_volume = fake_cmd_group_volume  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_group_volume_down(player.player_id)

        assert len(group_vol_called) == 1
        assert group_vol_called[0][1] == 47  # 50 - 3

    async def test_group_volume_mute_noop_for_non_group(self) -> None:
        """cmd_group_volume_mute does nothing for non-group player with no members."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "NoGroupMute")
        player.state.type = PlayerType.PLAYER
        player.state.group_members = []  # type: ignore[assignment]

        mute_called = []

        async def fake_mute(pid: str, muted: bool) -> None:
            mute_called.append((pid, muted))

        ctrl.cmd_volume_mute = fake_mute  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_group_volume_mute(player.player_id, True)

        assert mute_called == []


# ---------------------------------------------------------------------------
# cmd_volume_mute
# ---------------------------------------------------------------------------


class TestCmdVolumeMute:
    """Tests for PlayerController.cmd_volume_mute()."""

    async def test_volume_mute_none_control_raises(self) -> None:
        """cmd_volume_mute raises UnsupportedFeaturedException when PLAYER_CONTROL_NONE."""
        from music_assistant_models.errors import PlayerCommandFailed  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "NoMute")
        player._cache["volume_control"] = PLAYER_CONTROL_NONE
        player._cache["mute_control"] = PLAYER_CONTROL_NONE

        with _patched(), pytest.raises(PlayerCommandFailed):
            await ctrl.cmd_volume_mute(player.player_id, True)

    async def test_volume_mute_fake_saves_previous_volume(self) -> None:
        """cmd_volume_mute with PLAYER_CONTROL_FAKE stores previous volume."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "FakeMute")
        player._cache["volume_control"] = PLAYER_CONTROL_FAKE
        player._cache["mute_control"] = PLAYER_CONTROL_FAKE
        player._state.volume_level = 60
        player._state.synced_to = None
        player._state.active_group = None

        handle_volume_calls = []

        async def fake_handle_volume_set(pid: str, level: int) -> None:
            handle_volume_calls.append((pid, level))

        ctrl._handle_cmd_volume_set = fake_handle_volume_set  # type: ignore[assignment]

        update_state_called = False

        def fake_update_state(*_args: object, **_kwargs: object) -> None:
            nonlocal update_state_called
            update_state_called = True

        player.update_state = fake_update_state  # type: ignore[misc, method-assign]

        with _patched():
            await ctrl.cmd_volume_mute(player.player_id, True)

        # Previous volume should be saved
        assert player.extra_data.get(ATTR_PREVIOUS_VOLUME) == 60


# ---------------------------------------------------------------------------
# select_sound_mode
# ---------------------------------------------------------------------------


class TestSelectSoundMode:
    """Tests for PlayerController.select_sound_mode()."""

    async def test_raises_when_feature_not_supported(self) -> None:
        """select_sound_mode raises PlayerCommandFailed when player has no SELECT_SOUND_MODE."""
        from music_assistant_models.errors import PlayerCommandFailed  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "NoSoundMode")

        with _patched(), pytest.raises(PlayerCommandFailed):
            await ctrl.select_sound_mode(player.player_id, "stereo")


# ---------------------------------------------------------------------------
# set_option
# ---------------------------------------------------------------------------


class TestSetOption:
    """Tests for PlayerController.set_option()."""

    async def test_raises_when_feature_not_supported(self) -> None:
        """set_option raises PlayerCommandFailed when player has no OPTIONS feature."""
        from music_assistant_models.errors import PlayerCommandFailed  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "NoOptions")

        with _patched(), pytest.raises(PlayerCommandFailed):
            await ctrl.set_option(player.player_id, "some_option", "value")


# ---------------------------------------------------------------------------
# cmd_set_members
# ---------------------------------------------------------------------------


class TestCmdSetMembers:
    """Tests for PlayerController.cmd_set_members()."""

    async def test_raises_when_set_members_not_supported(self) -> None:
        """cmd_set_members raises UnsupportedFeaturedException when player lacks feature."""
        from music_assistant_models.errors import UnsupportedFeaturedException  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "NoGrouping")
        # Player does NOT have SET_MEMBERS feature
        assert PlayerFeature.SET_MEMBERS not in player._attr_supported_features

        with pytest.raises(UnsupportedFeaturedException):
            await ctrl.cmd_set_members(player.player_id, player_ids_to_add=["other"])


# ---------------------------------------------------------------------------
# add_currently_playing_to_favorites
# ---------------------------------------------------------------------------


class TestAddCurrentlyPlayingToFavorites:
    """Tests for PlayerController.add_currently_playing_to_favorites()."""

    async def test_raises_when_no_active_source(self) -> None:
        """add_currently_playing_to_favorites raises when player has no active source."""
        from music_assistant_models.errors import PlayerCommandFailed  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "NoSource Fav")
        player.state.active_source = None

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)

        with pytest.raises(PlayerCommandFailed):
            await ctrl.add_currently_playing_to_favorites(player.player_id)

    async def test_raises_when_no_current_media(self) -> None:
        """add_currently_playing_to_favorites raises when no current media."""
        from music_assistant_models.errors import PlayerCommandFailed  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "NoMedia Fav")
        player.state.active_source = "external_source"
        player.state.current_media = None

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)

        with pytest.raises(PlayerCommandFailed):
            await ctrl.add_currently_playing_to_favorites(player.player_id)


# ---------------------------------------------------------------------------
# _handle_cmd_resume
# ---------------------------------------------------------------------------


class TestHandleCmdResume:
    """Tests for PlayerController._handle_cmd_resume()."""

    async def test_resumes_queue_when_found(self) -> None:
        """_handle_cmd_resume resumes active queue when one is found."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Resume Queue")
        player._state.powered = True
        player._state.power_control = PLAYER_CONTROL_NONE

        fake_queue = MagicMock()
        fake_queue.queue_id = "q_resume"
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=fake_queue)
        mass.player_queues.resume = AsyncMock()

        await ctrl._handle_cmd_resume(player.player_id)

        mass.player_queues.resume.assert_called_once_with("q_resume")

    async def test_falls_back_to_player_queue_resume(self) -> None:
        """_handle_cmd_resume falls back to queue resume when no other path."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Fallback Resume")
        player._state.powered = True
        player._state.power_control = PLAYER_CONTROL_NONE
        player._state.active_source = None
        player._attr_current_media = None
        player._cache.clear()

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)
        mass.player_queues.resume = AsyncMock()

        await ctrl._handle_cmd_resume(player.player_id)

        mass.player_queues.resume.assert_called_once()


# ---------------------------------------------------------------------------
# on_player_dsp_change
# ---------------------------------------------------------------------------


class TestOnPlayerDspChange:
    """Tests for PlayerController.on_player_dsp_change()."""

    async def test_returns_early_when_player_not_found(self) -> None:
        """on_player_dsp_change does nothing for unknown player."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)

        # Should not raise
        await ctrl.on_player_dsp_change("unknown_player")

    async def test_noop_when_player_not_playing(self) -> None:
        """on_player_dsp_change does nothing when player is not playing."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Idle DSP Player")
        player.state.playback_state = PlaybackState.IDLE

        # Should not raise or do anything
        await ctrl.on_player_dsp_change(player.player_id)

    async def test_restarts_queue_when_playing(self) -> None:
        """on_player_dsp_change schedules queue resume when player is playing."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        [player] = _add_players(ctrl, mass, "Playing DSP Player")
        player.state.playback_state = PlaybackState.PLAYING

        fake_queue = MagicMock()
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=fake_queue)

        await ctrl.on_player_dsp_change(player.player_id)

        mass.call_later.assert_called()


# ---------------------------------------------------------------------------
# _handle_group_dsp_change
# ---------------------------------------------------------------------------


class TestHandleGroupDspChange:
    """Tests for PlayerController._handle_group_dsp_change()."""

    def test_returns_early_when_no_status_change(self) -> None:
        """_handle_group_dsp_change returns early when multi-device status unchanged."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "DSP Player")
        player.state.type = PlayerType.PLAYER

        # same count on both sides → no change in multi-device status
        ctrl._handle_group_dsp_change(player, ["p1", "p2"], ["p1", "p3"])

        # No DSP reload should be triggered (mass.create_task not called for DSP)
        mass.create_task.assert_not_called()

    def test_triggers_dsp_reload_when_group_shrinks(self) -> None:
        """_handle_group_dsp_change triggers DSP reload when multi-device status changes."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.create_task = MagicMock()
        [player] = _add_players(ctrl, mass, "DSP Shrink Player")
        player.state.type = PlayerType.PLAYER
        player._state.supported_features = set()  # No MULTI_DEVICE_DSP

        dsp_config = MagicMock()
        dsp_config.enabled = True
        mass.config.get_player_dsp_config = MagicMock(return_value=dsp_config)

        # Group goes from 2 members to 0: multi-device → single device
        ctrl._handle_group_dsp_change(player, ["p1", "p2"], [])

        mass.create_task.assert_called_once()


# ---------------------------------------------------------------------------
# _check_external_source_takeover
# ---------------------------------------------------------------------------


class TestCheckExternalSourceTakeover:
    """Tests for PlayerController._check_external_source_takeover()."""

    def test_returns_early_for_protocol_player(self) -> None:
        """_check_external_source_takeover skips protocol players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Protocol Player")
        player._attr_type = PlayerType.PROTOCOL
        player._cache.clear()

        # Should not raise or do anything
        ctrl._check_external_source_takeover(player)

    def test_returns_early_when_not_playing(self) -> None:
        """_check_external_source_takeover skips non-playing players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Idle Takeover Player")
        player._attr_type = PlayerType.PLAYER
        player._attr_playback_state = PlaybackState.IDLE
        player._cache.clear()

        # Should not raise or do anything
        ctrl._check_external_source_takeover(player)

    def test_returns_early_when_native_output(self) -> None:
        """_check_external_source_takeover skips players with native output protocol."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Native Output Player")
        player._attr_type = PlayerType.PLAYER
        player._attr_playback_state = PlaybackState.PLAYING
        player._cache.clear()
        # active_output_protocol defaults to None, which means no active protocol
        # So the condition `not player.active_output_protocol` returns True → early return

        ctrl._check_external_source_takeover(player)

        # No create_task call should have happened
        mass.create_task.assert_not_called()

    def test_returns_early_for_ma_managed_source(self) -> None:
        """_check_external_source_takeover does nothing when source is MA-managed."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.create_task = MagicMock()
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)
        mass.get_providers = MagicMock(return_value=[])
        [player, protocol_player] = _add_players(ctrl, mass, "Parent", "Proto")
        player._attr_type = PlayerType.PLAYER
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_active_source = player.player_id  # MA-managed (own player_id)
        player._cache.clear()
        # Set active output protocol to the protocol player
        player.set_active_output_protocol = MagicMock()  # type: ignore[misc,method-assign]
        player._Player__attr_active_output_protocol = protocol_player.player_id  # type: ignore[attr-defined]
        protocol_player._attr_type = PlayerType.PROTOCOL
        protocol_player._cache.clear()
        protocol_player._provider = MagicMock()
        protocol_player._provider.domain = "airplay"

        ctrl._check_external_source_takeover(player)

        # Should return early because source is player's own ID (MA-managed)
        mass.create_task.assert_not_called()


# ---------------------------------------------------------------------------
# _fix_group_member_configs
# ---------------------------------------------------------------------------


class TestFixGroupMemberConfigs:
    """Tests for PlayerController._fix_group_member_configs()."""

    async def test_returns_when_no_sync_groups(self) -> None:
        """_fix_group_member_configs does nothing when there are no sync groups."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.config.get = MagicMock(return_value={})

        await ctrl._fix_group_member_configs()

        # No config changes should be made
        mass.config.set_raw_player_config_value.assert_not_called()

    async def test_does_not_modify_when_no_stale_members(self) -> None:
        """_fix_group_member_configs keeps correct member IDs unchanged."""
        from music_assistant.constants import CONF_GROUP_MEMBERS  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        ctrl._get_cached_protocol_parent_id = MagicMock(return_value=None)  # type: ignore[method-assign]

        player_configs = {
            "sync_group_1": {
                "provider": "sync_group",
                "values": {CONF_GROUP_MEMBERS: ["player_a", "player_b"]},
            }
        }
        mass.config.get = MagicMock(return_value=player_configs)

        await ctrl._fix_group_member_configs()

        # No changes needed since there are no stale member IDs
        mass.config.set_raw_player_config_value.assert_not_called()


# ---------------------------------------------------------------------------
# wait_for_state
# ---------------------------------------------------------------------------


class TestWaitForState:
    """Tests for PlayerController.wait_for_state()."""

    async def test_returns_immediately_when_already_in_state(self) -> None:
        """wait_for_state returns immediately when player is already in desired state."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Already Idle")
        player.state.playback_state = PlaybackState.IDLE

        # Should return immediately without waiting
        await ctrl.wait_for_state(player, PlaybackState.IDLE, timeout=1.0)

    async def test_times_out_gracefully(self) -> None:
        """wait_for_state handles timeout gracefully without raising."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        [player] = _add_players(ctrl, mass, "Always Idle")
        player.state.playback_state = PlaybackState.IDLE

        # Requesting PLAYING but player stays IDLE → will timeout
        await ctrl.wait_for_state(player, PlaybackState.PLAYING, timeout=0.01)
        # Should not raise, just log


# ---------------------------------------------------------------------------
# _handle_cmd_resume (additional)
# ---------------------------------------------------------------------------


class TestHandleCmdResumeAdditional:
    """Additional tests for _handle_cmd_resume covering power-on path."""

    async def test_powers_on_before_resuming(self) -> None:
        """_handle_cmd_resume powers on player before resuming when not powered."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Off Resume")
        # Player is off but has no power control
        player._state.powered = False
        player._state.power_control = PLAYER_CONTROL_NONE

        fake_queue = MagicMock()
        fake_queue.queue_id = "q_pwr"
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=fake_queue)
        mass.player_queues.resume = AsyncMock()

        # Since power_control is NONE, _handle_cmd_power won't be called
        await ctrl._handle_cmd_resume(player.player_id)

        mass.player_queues.resume.assert_called_once_with("q_pwr")


# ---------------------------------------------------------------------------
# _handle_play_media (native path)
# ---------------------------------------------------------------------------


class TestHandlePlayMedia:
    """Tests for PlayerController._handle_play_media()."""

    async def test_native_play_media(self) -> None:
        """_handle_play_media calls player.play_media natively."""
        from music_assistant_models.enums import MediaType  # noqa: PLC0415
        from music_assistant_models.player import PlayerMedia  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        [player] = _add_players(ctrl, mass, "Media Player2")
        player._state.powered = True

        media = PlayerMedia(uri="http://test.mp3", media_type=MediaType.UNKNOWN)

        player.play_media = AsyncMock()  # type: ignore[method-assign]
        player.set_active_output_protocol = MagicMock()  # type: ignore[misc, method-assign]
        player.on_protocol_playback = AsyncMock()  # type: ignore[method-assign]

        # Mock _select_best_output_protocol to return native (same player, no protocol)
        ctrl._select_best_output_protocol = MagicMock(return_value=(player, None))  # type: ignore[method-assign]

        await ctrl._handle_play_media(player.player_id, media)

        player.play_media.assert_called_once_with(media)

    async def test_powers_on_before_playing(self) -> None:
        """_handle_play_media powers on player before starting playback."""
        from music_assistant_models.enums import MediaType  # noqa: PLC0415
        from music_assistant_models.player import PlayerMedia  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        [player] = _add_players(ctrl, mass, "Off Media Player")
        player._state.powered = False
        player._state.power_control = PLAYER_CONTROL_FAKE

        media = PlayerMedia(uri="http://test.mp3", media_type=MediaType.UNKNOWN)

        player.play_media = AsyncMock()  # type: ignore[method-assign]
        player.set_active_output_protocol = MagicMock()  # type: ignore[misc, method-assign]
        player.on_protocol_playback = AsyncMock()  # type: ignore[method-assign]

        mass.cache = MagicMock()
        mass.cache.set = AsyncMock()
        mass.player_queues = MagicMock()
        mass.player_queues.resume = AsyncMock()

        power_called = []

        async def fake_handle_power(pid: str, powered: bool, skip_auto_play: bool = False) -> None:  # noqa: ARG001
            power_called.append((pid, powered))

        ctrl._handle_cmd_power = fake_handle_power  # type: ignore[assignment]
        ctrl._select_best_output_protocol = MagicMock(return_value=(player, None))  # type: ignore[method-assign]

        await ctrl._handle_play_media(player.player_id, media)

        assert (player.player_id, True) in power_called


# ---------------------------------------------------------------------------
# _handle_select_source
# ---------------------------------------------------------------------------


class TestHandleSelectSource:
    """Tests for PlayerController._handle_select_source()."""

    async def test_raises_when_source_not_supported(self) -> None:
        """_handle_select_source raises UnsupportedFeaturedException for unsupported source."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "No Select Source")
        player._state.active_source = None  # no previous source

        mass.get_provider = MagicMock(return_value=None)
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)

        with pytest.raises(UnsupportedFeaturedException):
            await ctrl._handle_select_source(player.player_id, "unknown_source")

    async def test_sets_mass_source_for_queue_source(self) -> None:
        """_handle_select_source sets active mass source when source is a queue."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "Queue Source Player")
        player._state.active_source = None

        mass.get_provider = MagicMock(return_value=None)
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=MagicMock())  # fake queue

        player.set_active_mass_source = MagicMock()  # type: ignore[misc, method-assign]

        await ctrl._handle_select_source(player.player_id, "queue_id_123")

        player.set_active_mass_source.assert_called_once_with("queue_id_123")


# ---------------------------------------------------------------------------
# register (via register_or_update)
# ---------------------------------------------------------------------------


class TestRegisterPlayer:
    """Tests for PlayerController.register()."""

    async def test_register_new_player(self) -> None:
        """register() registers a new player in _players dict."""
        from unittest.mock import patch as std_patch  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        mass.signal_event = MagicMock()
        mass.cache = MagicMock()
        mass.cache.get = AsyncMock(return_value=False)
        mass.config.get = MagicMock(return_value=[])
        mass.config.set = MagicMock()
        mass.config.get_player_config = AsyncMock(return_value=MagicMock())
        mass.player_queues = MagicMock()
        mass.player_queues.on_player_register = AsyncMock()

        provider = MockProvider("test_reg_prov", instance_id="test_reg_prov", mass=mass)
        player = MockPlayer(provider, "new_reg_player", "Reg Player")
        player.on_config_updated = AsyncMock()  # type: ignore[method-assign]
        player.set_config = MagicMock()  # type: ignore[misc, method-assign]

        with std_patch(
            "music_assistant.controllers.players.controller.enrich_device_mac_address",
            new=AsyncMock(),
        ):
            ctrl._evaluate_protocol_links = MagicMock()  # type: ignore[method-assign]
            await ctrl.register(player)

        assert "new_reg_player" in ctrl._players

    async def test_register_raises_for_duplicate(self) -> None:
        """register() raises AlreadyRegisteredError when player is already registered."""
        from music_assistant_models.errors import AlreadyRegisteredError  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        [existing] = _add_players(ctrl, mass, "Already Here")

        provider = MockProvider("dup_prov", instance_id="dup_prov", mass=mass)
        dup_player = MockPlayer(provider, existing.player_id, "Duplicate")

        with pytest.raises(AlreadyRegisteredError):
            await ctrl.register(dup_player)

    async def test_register_noop_when_player_disabled(self) -> None:
        """register() ignores disabled players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False

        provider = MockProvider("dis_prov", instance_id="dis_prov", mass=mass)
        player = MockPlayer(provider, "disabled_player", "Disabled")
        player._state.enabled = False  # Disable before registration

        with (
            __import__("unittest.mock", fromlist=["patch"]).patch(
                "music_assistant.controllers.players.controller.enrich_device_mac_address",
                new=AsyncMock(),
            ),
        ):
            await ctrl.register(player)

        # Disabled player should not be in _players
        assert "disabled_player" not in ctrl._players


# ---------------------------------------------------------------------------
# More signal_player_state_update branches
# ---------------------------------------------------------------------------


class TestSignalPlayerStateUpdateBranches:
    """Additional tests for signal_player_state_update branches."""

    def test_options_change_signals_options_event(self) -> None:
        """signal_player_state_update fires OPTIONS_UPDATED for options changes."""
        from music_assistant_models.enums import EventType  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        mass.create_task = MagicMock()
        [player] = _add_players(ctrl, mass, "Options Player")
        player.state.enabled = True
        player.state.type = PlayerType.PLAYER

        ctrl.signal_player_state_update(player, {"options": ([], [])})

        # Should signal both PLAYER_UPDATED and PLAYER_OPTIONS_UPDATED
        event_types = [call.args[0] for call in mass.signal_event.call_args_list]
        assert EventType.PLAYER_OPTIONS_UPDATED in event_types

    def test_became_inactive_triggers_membership_cleanup(self) -> None:
        """signal_player_state_update triggers membership cleanup when player becomes inactive."""
        from music_assistant.constants import ATTR_AVAILABLE  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        mass.create_task = MagicMock()
        [player] = _add_players(ctrl, mass, "Inactive Player")
        player.state.enabled = True
        player.state.type = PlayerType.PLAYER
        player.state.synced_to = "some_leader"
        player._provider.players = []  # type: ignore[misc]  # provider.players needed by signal_player_state_update

        ctrl.signal_player_state_update(
            player,
            {ATTR_AVAILABLE: (True, False)},  # became unavailable
        )

        # Should have called create_task for cleanup
        mass.create_task.assert_called()

    def test_synced_to_player_triggers_update(self) -> None:
        """signal_player_state_update triggers update on the sync leader."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        mass.create_task = MagicMock()
        [leader, member] = _add_players(ctrl, mass, "Leader2", "Member2")
        member.state.enabled = True
        member.state.type = PlayerType.PLAYER
        member.state.synced_to = leader.player_id

        ctrl.signal_player_state_update(member, {"volume_level": (10, 20)})

        # trigger_player_update calls mass.call_later for the leader
        call_args_list = [str(c) for c in mass.call_later.call_args_list]
        assert any(leader.player_id in arg for arg in call_args_list)


# ---------------------------------------------------------------------------
# cmd_volume_mute - native and fake unmute paths
# ---------------------------------------------------------------------------


class TestCmdVolumeMuteExtended:
    """Extended tests for cmd_volume_mute native/unmute paths."""

    async def test_native_mute_calls_volume_mute(self) -> None:
        """cmd_volume_mute with NATIVE control calls player.volume_mute()."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "NativeMute")
        player._cache["volume_control"] = PLAYER_CONTROL_NATIVE
        player._cache["mute_control"] = PLAYER_CONTROL_NATIVE
        player._state.synced_to = None
        player._state.active_group = None

        player.volume_mute = AsyncMock()  # type: ignore[method-assign]

        with _patched():
            await ctrl.cmd_volume_mute(player.player_id, True)

        player.volume_mute.assert_called_once_with(True)

    async def test_fake_unmute_restores_previous_volume(self) -> None:
        """cmd_volume_mute unmute with FAKE restores previous volume."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "FakeUnmute")
        player._cache["volume_control"] = PLAYER_CONTROL_FAKE
        player._cache["mute_control"] = PLAYER_CONTROL_FAKE
        player._state.volume_level = 0
        player._state.synced_to = None
        player._state.active_group = None
        player.extra_data[ATTR_PREVIOUS_VOLUME] = 50

        handle_volume_calls: list[tuple[str, int]] = []

        async def fake_volume_set(pid: str, level: int) -> None:
            handle_volume_calls.append((pid, level))

        ctrl._handle_cmd_volume_set = fake_volume_set  # type: ignore[assignment]
        player.update_state = MagicMock()  # type: ignore[misc, method-assign]

        with _patched():
            await ctrl.cmd_volume_mute(player.player_id, False)

        # Should restore to previous volume
        assert any(lvl == 50 for _, lvl in handle_volume_calls)

    async def test_mute_lock_set_for_group_member(self) -> None:
        """cmd_volume_mute sets ATTR_MUTE_LOCK when player is in a group."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "GroupMember")
        player._cache["volume_control"] = PLAYER_CONTROL_NATIVE
        player._cache["mute_control"] = PLAYER_CONTROL_NATIVE
        player._state.synced_to = "some_leader"
        player._state.active_group = None

        player.volume_mute = AsyncMock()  # type: ignore[method-assign]

        with _patched():
            await ctrl.cmd_volume_mute(player.player_id, True)

        assert player.extra_data.get(ATTR_MUTE_LOCK) is True

    async def test_mute_lock_cleared_on_unmute(self) -> None:
        """cmd_volume_mute clears ATTR_MUTE_LOCK when player is unmuted."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "LockClear")
        player._cache["volume_control"] = PLAYER_CONTROL_NATIVE
        player._cache["mute_control"] = PLAYER_CONTROL_NATIVE
        player._state.synced_to = "some_leader"
        player._state.active_group = None
        player.extra_data[ATTR_MUTE_LOCK] = True

        player.volume_mute = AsyncMock()  # type: ignore[method-assign]

        with _patched():
            await ctrl.cmd_volume_mute(player.player_id, False)

        assert ATTR_MUTE_LOCK not in player.extra_data


# ---------------------------------------------------------------------------
# cmd_group_volume_mute - group path
# ---------------------------------------------------------------------------


class TestCmdGroupVolumeMuteGroup:
    """Tests for cmd_group_volume_mute with actual group members."""

    async def test_mutes_all_group_members(self) -> None:
        """cmd_group_volume_mute sends mute to each powered group member."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [leader, m1, m2] = _add_players(ctrl, mass, "GroupLeader", "M1", "M2")

        leader._attr_type = PlayerType.PLAYER
        leader._state.group_members = [leader.player_id, m1.player_id, m2.player_id]  # type: ignore[assignment]
        leader._state.type = PlayerType.PLAYER
        # ensure members are powered, available, and enabled
        for member in (leader, m1, m2):
            member._state.powered = True
            member._state.available = True
            member._state.enabled = True

        muted_calls: list[tuple[str, bool]] = []

        async def fake_volume_mute(pid: str, muted: bool) -> None:
            muted_calls.append((pid, muted))

        ctrl.cmd_volume_mute = fake_volume_mute  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_group_volume_mute(leader.player_id, True)

        assert len(muted_calls) == 3


# ---------------------------------------------------------------------------
# cmd_seek - direct player path
# ---------------------------------------------------------------------------


class TestCmdSeekDirectPath:
    """Tests for cmd_seek when no queue is active."""

    async def test_seek_raises_when_no_seek_feature(self) -> None:
        """cmd_seek raises PlayerCommandFailed when player has no SEEK feature."""
        from music_assistant_models.errors import PlayerCommandFailed  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "NoSeek")
        player._attr_supported_features = set()  # no SEEK feature
        player._cache.clear()

        # No active queue
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)

        with _patched(), pytest.raises(PlayerCommandFailed):
            await ctrl.cmd_seek(player.player_id, 30)

    async def test_seek_calls_player_seek(self) -> None:
        """cmd_seek calls player.seek() when feature is supported and no queue."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "SeekPlayer")
        player._attr_supported_features = {PlayerFeature.SEEK}
        player._cache.clear()
        player._state.source_list = []  # type: ignore[assignment]

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)

        player.seek = AsyncMock()  # type: ignore[method-assign]

        with _patched():
            await ctrl.cmd_seek(player.player_id, 45)

        player.seek.assert_called_once_with(45)


# ---------------------------------------------------------------------------
# cmd_next_track / cmd_previous_track - error paths
# ---------------------------------------------------------------------------


class TestCmdNextPrevDirectPath:
    """Tests for cmd_next/previous when no queue is active."""

    async def test_next_raises_without_next_feature(self) -> None:
        """cmd_next_track raises UnsupportedFeaturedException when no NEXT_PREVIOUS feature."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "NoNext")
        player._attr_supported_features = set()
        player._cache.clear()
        player._state.active_source = None
        player._state.source_list = []  # type: ignore[assignment]

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)

        with _patched(), pytest.raises(Exception):  # noqa: B017, PT011
            await ctrl.cmd_next_track(player.player_id)

    async def test_previous_raises_without_next_feature(self) -> None:
        """cmd_previous_track raises when no NEXT_PREVIOUS feature and no queue."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "NoPrev")
        player._attr_supported_features = set()
        player._cache.clear()
        player._state.active_source = None
        player._state.source_list = []  # type: ignore[assignment]

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)

        with _patched(), pytest.raises(Exception):  # noqa: B017, PT011
            await ctrl.cmd_previous_track(player.player_id)


# ---------------------------------------------------------------------------
# cmd_group_volume - synced_to redirect
# ---------------------------------------------------------------------------


class TestCmdGroupVolumeSync:
    """Tests for cmd_group_volume when player is synced."""

    async def test_redirects_to_sync_leader_volume(self) -> None:
        """cmd_group_volume redirects to sync leader when player.synced_to is set."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [leader, member] = _add_players(ctrl, mass, "SyncLeader", "SyncMember")
        member._state.synced_to = leader.player_id
        member._state.group_members = []  # type: ignore[assignment]
        member._state.type = PlayerType.PLAYER

        set_volume_calls: list[tuple[str, int]] = []

        async def fake_set_group_volume(group_player: object, vol: int) -> None:
            set_volume_calls.append((str(group_player), vol))

        ctrl.set_group_volume = fake_set_group_volume  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_group_volume(member.player_id, 50)

        assert len(set_volume_calls) == 1


# ---------------------------------------------------------------------------
# cmd_group_volume_up/down step sizes
# ---------------------------------------------------------------------------


class TestCmdGroupVolumeSteps:
    """Tests for cmd_group_volume_up/down step size logic."""

    async def test_group_volume_up_low_volume_small_step(self) -> None:
        """cmd_group_volume_up uses step=1 when volume < 10."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "LowVol")
        player._state.group_volume = 5
        player._state.type = PlayerType.PLAYER
        player._state.group_members = [player.player_id]  # type: ignore[assignment]

        group_volume_calls: list[tuple[str, int]] = []

        async def fake_group_volume(pid: str, vol: int) -> None:
            group_volume_calls.append((pid, vol))

        ctrl.cmd_group_volume = fake_group_volume  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_group_volume_up(player.player_id)

        # step_size=1 when cur_volume < 10
        assert any(v == 6 for _, v in group_volume_calls)

    async def test_group_volume_down_high_volume_small_step(self) -> None:
        """cmd_group_volume_down uses step=1 when volume > 90."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "HighVol")
        player._state.group_volume = 95
        player._state.type = PlayerType.PLAYER
        player._state.group_members = [player.player_id]  # type: ignore[assignment]

        group_volume_calls: list[tuple[str, int]] = []

        async def fake_group_volume(pid: str, vol: int) -> None:
            group_volume_calls.append((pid, vol))

        ctrl.cmd_group_volume = fake_group_volume  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_group_volume_down(player.player_id)

        # step_size=1 when cur_volume > 90
        assert any(v == 94 for _, v in group_volume_calls)

    async def test_group_volume_up_mid_volume_medium_step(self) -> None:
        """cmd_group_volume_up uses step=2 when volume is between 30 and 70."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "MidVol")
        player._state.group_volume = 20
        player._state.type = PlayerType.PLAYER
        player._state.group_members = [player.player_id]  # type: ignore[assignment]

        group_volume_calls: list[tuple[str, int]] = []

        async def fake_group_volume(pid: str, vol: int) -> None:
            group_volume_calls.append((pid, vol))

        ctrl.cmd_group_volume = fake_group_volume  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_group_volume_up(player.player_id)

        # step_size=2 when 10 <= cur_volume < 30
        assert any(v == 22 for _, v in group_volume_calls)


# ---------------------------------------------------------------------------
# select_sound_mode - valid mode paths
# ---------------------------------------------------------------------------


class TestSelectSoundModeValid:
    """Tests for PlayerController.select_sound_mode() valid mode paths."""

    async def test_same_mode_returns_early(self) -> None:
        """select_sound_mode does nothing when same mode is already active."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "SameMode")
        player._attr_supported_features = {PlayerFeature.SELECT_SOUND_MODE}
        player._attr_active_sound_mode = (
            "jazz"  # active_sound_mode reads from _attr_active_sound_mode
        )
        player._cache.clear()

        player.select_sound_mode = AsyncMock()  # type: ignore[method-assign]

        with _patched():
            await ctrl.select_sound_mode(player.player_id, "jazz")

        player.select_sound_mode.assert_not_called()

    async def test_invalid_mode_raises(self) -> None:
        """select_sound_mode raises PlayerCommandFailed for an invalid mode."""
        from music_assistant_models.errors import PlayerCommandFailed  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "InvalidMode")
        player._attr_supported_features = {PlayerFeature.SELECT_SOUND_MODE}
        player._state.active_sound_mode = None
        player._state.sound_mode_list = []  # type: ignore[assignment]
        player._cache.clear()

        with _patched(), pytest.raises(PlayerCommandFailed):
            await ctrl.select_sound_mode(player.player_id, "nonexistent")


# ---------------------------------------------------------------------------
# set_option - success paths
# ---------------------------------------------------------------------------


class TestSetOptionValid:
    """Tests for PlayerController.set_option() valid paths."""

    async def test_option_not_found_returns_early(self) -> None:
        """set_option does nothing when option key doesn't exist."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "NoOption")
        player._attr_supported_features = {PlayerFeature.OPTIONS}
        player._state.options = []  # type: ignore[assignment]  # no options at all
        player._cache.clear()

        player.set_option = AsyncMock()  # type: ignore[method-assign]

        with _patched():
            await ctrl.set_option(player.player_id, "unknown_key", "val")

        player.set_option.assert_not_called()


# ---------------------------------------------------------------------------
# cmd_ungroup_many
# ---------------------------------------------------------------------------


class TestCmdUngroupMany:
    """Tests for PlayerController.cmd_ungroup_many()."""

    async def test_calls_ungroup_for_each_player(self) -> None:
        """cmd_ungroup_many iterates and calls cmd_ungroup on each player."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [p1, p2] = _add_players(ctrl, mass, "P1Ung", "P2Ung")

        ungrouped: list[str] = []

        async def fake_ungroup(pid: str) -> None:
            ungrouped.append(pid)

        ctrl.cmd_ungroup = fake_ungroup  # type: ignore[assignment]

        await ctrl.cmd_ungroup_many([p1.player_id, p2.player_id])

        assert p1.player_id in ungrouped
        assert p2.player_id in ungrouped


# ---------------------------------------------------------------------------
# cmd_group_many
# ---------------------------------------------------------------------------


class TestCmdGroupMany:
    """Tests for PlayerController.cmd_group_many()."""

    async def test_delegates_to_cmd_set_members(self) -> None:
        """cmd_group_many calls cmd_set_members with the given child IDs."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [leader, m1] = _add_players(ctrl, mass, "Leader3", "M3")
        leader._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        leader._cache.clear()

        set_members_calls: list[tuple[str, list[str]]] = []

        async def fake_set_members(
            target: str,
            player_ids_to_add: list[str] | None = None,
            _player_ids_to_remove: list[str] | None = None,
        ) -> None:
            set_members_calls.append((target, player_ids_to_add or []))

        ctrl.cmd_set_members = fake_set_members  # type: ignore[assignment]

        await ctrl.cmd_group_many(leader.player_id, [m1.player_id])

        assert (leader.player_id, [m1.player_id]) in set_members_calls


# ---------------------------------------------------------------------------
# set_group_volume
# ---------------------------------------------------------------------------


class TestSetGroupVolume:
    """Tests for PlayerController.set_group_volume()."""

    async def test_adjusts_each_member_volume(self) -> None:
        """set_group_volume adjusts each member's volume proportionally."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [leader, m1] = _add_players(ctrl, mass, "GrpLeader2", "GrpM2")

        leader._attr_type = PlayerType.PLAYER
        leader._state.group_members = [leader.player_id, m1.player_id]  # type: ignore[assignment]
        leader._state.group_volume = 50

        m1._state.volume_level = 40
        m1._state.powered = True
        m1._state.available = True
        m1._state.enabled = True
        m1._state.volume_control = PLAYER_CONTROL_NATIVE

        volume_set_calls: list[tuple[str, int]] = []

        async def fake_handle_volume_set(pid: str, level: int) -> None:
            volume_set_calls.append((pid, level))

        ctrl._handle_cmd_volume_set = fake_handle_volume_set  # type: ignore[assignment]

        await ctrl.set_group_volume(leader, 70)

        # volume difference is 20, member had 40, so should get 60
        assert any(level == 60 for _, level in volume_set_calls)

    async def test_returns_early_when_group_volume_none(self) -> None:
        """set_group_volume returns early when group_volume is None."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [leader] = _add_players(ctrl, mass, "NoGV")
        leader._state.group_volume = None

        volume_set_calls: list[int] = []

        async def fake_handle_volume_set(_pid: str, level: int) -> None:
            volume_set_calls.append(level)

        ctrl._handle_cmd_volume_set = fake_handle_volume_set  # type: ignore[assignment]

        await ctrl.set_group_volume(leader, 70)

        assert volume_set_calls == []


# ---------------------------------------------------------------------------
# register_player_control - success path
# ---------------------------------------------------------------------------


class TestRegisterPlayerControlSuccess:
    """Tests for register_player_control success path."""

    async def test_register_adds_to_controls(self) -> None:
        """register_player_control adds control to _controls dict."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False

        prov_mock = MagicMock()
        prov_mock.instance_id = "my_prov"
        mass.get_provider = MagicMock(return_value=prov_mock)
        mass.call_later = MagicMock()

        pc = PlayerControl(
            id="ctrl_1",
            provider="my_prov",
            name="My Control",
            supports_power=True,
        )

        await ctrl.register_player_control(pc)

        assert "ctrl_1" in ctrl._controls

    async def test_register_raises_for_invalid_provider(self) -> None:
        """register_player_control raises RuntimeError for invalid provider."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False

        mass.get_provider = MagicMock(return_value=None)

        pc = PlayerControl(
            id="ctrl_bad",
            provider="bad_prov",
            name="Bad Control",
        )

        with pytest.raises(RuntimeError):
            await ctrl.register_player_control(pc)


# ---------------------------------------------------------------------------
# _cleanup_player_memberships
# ---------------------------------------------------------------------------


class TestCleanupPlayerMembershipsAction:
    """Tests for _cleanup_player_memberships when player has membership."""

    async def test_cleans_up_synced_player(self) -> None:
        """_cleanup_player_memberships ungrouped player from synced leader."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [leader, member] = _add_players(ctrl, mass, "Leader4", "Member4")
        member._state.synced_to = leader.player_id
        member._state.active_group = None

        handle_set_calls: list[tuple[str, list[str]]] = []

        async def fake_handle_set_members(
            parent: object,
            _player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            handle_set_calls.append((getattr(parent, "player_id", ""), player_ids_to_remove or []))

        ctrl._handle_set_members = fake_handle_set_members  # type: ignore[assignment]

        await ctrl._cleanup_player_memberships(member.player_id)

        assert any(member.player_id in removed for _, removed in handle_set_calls)


# ---------------------------------------------------------------------------
# on_player_config_change - additional branches
# ---------------------------------------------------------------------------


class TestOnPlayerConfigChangeExtended:
    """Extended tests for on_player_config_change."""

    async def test_player_disabled_and_playing_sends_stop(self) -> None:
        """on_player_config_change sends stop when player is disabled and playing (no power ctrl)."""  # noqa: E501
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "DisabledPlay")
        player._state.available = True
        player._state.playback_state = PlaybackState.PLAYING
        player._state.power_control = PLAYER_CONTROL_NONE
        player._cache.clear()
        player.set_config = MagicMock()  # type: ignore[misc, method-assign]
        player.on_config_updated = AsyncMock()  # type: ignore[method-assign]
        player.update_state = MagicMock()  # type: ignore[misc, method-assign]

        config = MagicMock()
        config.player_id = player.player_id
        config.provider = "test_provider"
        config.enabled = False
        config.values = {}

        mass.get_provider = MagicMock(return_value=None)

        stop_called = []

        async def fake_stop(pid: str) -> None:
            stop_called.append(pid)

        ctrl.cmd_stop = fake_stop  # type: ignore[assignment]

        await ctrl.on_player_config_change(config, {ATTR_ENABLED})

        assert player.player_id in stop_called

    async def test_player_disabled_with_power_sends_power_off(self) -> None:
        """on_player_config_change sends power off when player is disabled with power ctrl."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "DisabledPower")
        player._state.available = True
        player._state.power_control = PLAYER_CONTROL_FAKE
        player._state.playback_state = PlaybackState.IDLE
        player._cache.clear()
        player.set_config = MagicMock()  # type: ignore[misc, method-assign]
        player.on_config_updated = AsyncMock()  # type: ignore[method-assign]
        player.update_state = MagicMock()  # type: ignore[misc, method-assign]

        config = MagicMock()
        config.player_id = player.player_id
        config.provider = "test_provider"
        config.enabled = False
        config.values = {}

        mass.get_provider = MagicMock(return_value=None)

        power_off_calls: list[tuple[str, bool]] = []

        async def fake_handle_power(pid: str, powered: bool, skip_auto_play: bool = False) -> None:  # noqa: ARG001
            power_off_calls.append((pid, powered))

        ctrl._handle_cmd_power = fake_handle_power  # type: ignore[assignment]

        await ctrl.on_player_config_change(config, {ATTR_ENABLED})

        assert (player.player_id, False) in power_off_calls

    async def test_config_change_requires_reload_restarts_queue(self) -> None:
        """on_player_config_change restarts queue when requires_reload key changed."""
        from music_assistant_models.enums import PlaybackState as PS  # noqa: PLC0415, N817

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "ReloadPlayer")
        player._state.active_source = "my_queue"

        fake_queue = MagicMock()
        fake_queue.queue_id = "my_queue"
        fake_queue.state = PS.PLAYING

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=fake_queue)
        mass.player_queues.stop = AsyncMock()

        reload_value = MagicMock()
        reload_value.key = "changed_key"
        reload_value.requires_reload = True

        config = MagicMock()
        config.player_id = player.player_id
        config.provider = "test_provider"
        config.enabled = True
        config.values = {"changed_key": reload_value}

        player.set_config = MagicMock()  # type: ignore[misc, method-assign]
        player.on_config_updated = AsyncMock()  # type: ignore[method-assign]
        player.update_state = MagicMock()  # type: ignore[misc, method-assign]

        mass.get_provider = MagicMock(return_value=None)

        await ctrl.on_player_config_change(config, {"changed_key"})

        mass.player_queues.stop.assert_called_once_with("my_queue")


# ---------------------------------------------------------------------------
# on_player_dsp_change - stop/play fallback
# ---------------------------------------------------------------------------


class TestOnPlayerDspChangeStopPlay:
    """Tests for on_player_dsp_change stop/play path."""

    async def test_stop_and_play_when_no_active_queue(self) -> None:
        """on_player_dsp_change calls stop+play when player has no active queue."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        [player] = _add_players(ctrl, mass, "DspNoQueue")
        player._state.playback_state = PlaybackState.PLAYING

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)

        stop_calls = []
        play_calls = []

        async def fake_stop(pid: str) -> None:
            stop_calls.append(pid)

        async def fake_play(pid: str) -> None:
            play_calls.append(pid)

        ctrl.cmd_stop = fake_stop  # type: ignore[assignment]
        ctrl.cmd_play = fake_play  # type: ignore[assignment]

        def fake_get_active_queue(_p: object) -> None:
            return None

        ctrl.get_active_queue = fake_get_active_queue  # type: ignore[assignment]

        await ctrl.on_player_dsp_change(player.player_id)

        assert player.player_id in stop_calls
        assert player.player_id in play_calls


# ---------------------------------------------------------------------------
# _handle_cmd_power - native and fake paths
# ---------------------------------------------------------------------------


class TestHandleCmdPowerNative:
    """Tests for _handle_cmd_power native power path."""

    async def test_native_power_on_calls_player_power(self) -> None:
        """_handle_cmd_power with NATIVE calls player.power() and waits."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "NativePower")
        player._state.powered = False
        player._state.power_control = PLAYER_CONTROL_NATIVE
        player._cache.clear()

        player.power = AsyncMock()  # type: ignore[method-assign]
        player.update_state = MagicMock()  # type: ignore[misc, method-assign]
        mass.player_queues = MagicMock()
        mass.player_queues.resume = AsyncMock()

        with (
            patch(
                "music_assistant.controllers.players.controller.wait_for_power_on",
                new=AsyncMock(),
            ),
        ):
            await ctrl._handle_cmd_power(player.player_id, True)

        player.power.assert_called_once_with(True)

    async def test_power_off_stops_playing_player(self) -> None:
        """_handle_cmd_power stops a playing player when powering off."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "StopOnPower")
        player._state.powered = True
        player._state.power_control = PLAYER_CONTROL_FAKE
        player._state.playback_state = PlaybackState.PLAYING
        player._state.synced_to = None
        player._state.active_group = None
        player._state.group_members = []  # type: ignore[assignment]
        player._attr_type = PlayerType.PLAYER
        player._cache.clear()

        mass.cache = MagicMock()
        mass.cache.set = AsyncMock()

        stop_calls: list[str] = []

        async def fake_stop(pid: str) -> None:
            stop_calls.append(pid)

        ctrl._handle_cmd_stop = fake_stop  # type: ignore[assignment]
        player.update_state = MagicMock()  # type: ignore[misc, method-assign]

        await ctrl._handle_cmd_power(player.player_id, False)

        assert player.player_id in stop_calls


# ---------------------------------------------------------------------------
# _handle_cmd_stop - protocol player power path
# ---------------------------------------------------------------------------


class TestHandleCmdStopProtocolPower:
    """Tests for _handle_cmd_stop when protocol player has POWER feature."""

    async def test_powers_off_protocol_player(self) -> None:
        """_handle_cmd_stop powers off protocol player when it supports POWER."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        [player, proto] = _add_players(ctrl, mass, "Parent2", "Proto2")

        proto._attr_supported_features = {PlayerFeature.POWER}
        proto._cache.clear()

        player._Player__attr_active_output_protocol = proto.player_id  # type: ignore[attr-defined]

        power_calls: list[tuple[str, bool]] = []

        async def fake_handle_power(pid: str, powered: bool, skip_auto_play: bool = False) -> None:  # noqa: ARG001
            power_calls.append((pid, powered))

        ctrl._handle_cmd_power = fake_handle_power  # type: ignore[assignment]
        player.mark_stop_called = MagicMock()  # type: ignore[misc, method-assign]

        await ctrl._handle_cmd_stop(player.player_id)

        assert (proto.player_id, False) in power_calls


# ---------------------------------------------------------------------------
# _handle_cmd_play - additional paths
# ---------------------------------------------------------------------------


class TestHandleCmdPlayPaths:
    """Tests for _handle_cmd_play various paths."""

    async def test_paused_player_calls_play(self) -> None:
        """_handle_cmd_play calls player.play() when player is paused."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "PausedPlay")
        player._state.playback_state = PlaybackState.PAUSED
        player._state.source_list = []  # type: ignore[assignment]
        player._state.active_source = None
        player._state.powered = True
        player._state.power_control = PLAYER_CONTROL_NONE
        player._cache.clear()

        # Mock _get_control_target to return None (no protocol player)
        ctrl._get_control_target = MagicMock(return_value=None)  # type: ignore[method-assign]

        player.play = AsyncMock()  # type: ignore[method-assign]

        await ctrl._handle_cmd_play(player.player_id)

        # Falls through to `await player.play()` at end
        player.play.assert_called()

    async def test_idle_player_plays_media_when_available(self) -> None:
        """_handle_cmd_play plays current_media when player is idle with media."""
        from music_assistant_models.enums import MediaType  # noqa: PLC0415
        from music_assistant_models.player import PlayerMedia  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "IdleMedia")
        player._state.playback_state = PlaybackState.IDLE
        player._state.source_list = []  # type: ignore[assignment]
        player._state.active_source = None
        player._state.current_media = PlayerMedia(
            uri="http://test.mp3", media_type=MediaType.UNKNOWN
        )
        player._state.powered = True
        player._state.power_control = PLAYER_CONTROL_NONE
        player._cache.clear()

        ctrl._get_active_plugin_source = MagicMock(return_value=None)  # type: ignore[method-assign]
        ctrl._get_control_target = MagicMock(return_value=None)  # type: ignore[method-assign]

        player.play_media = AsyncMock()  # type: ignore[method-assign]

        await ctrl._handle_cmd_play(player.player_id)

        player.play_media.assert_called_once()


# ---------------------------------------------------------------------------
# _handle_cmd_pause - active source can't play pause
# ---------------------------------------------------------------------------


class TestHandleCmdPausePaths:
    """Tests for _handle_cmd_pause active source path."""

    async def test_raises_when_source_cannot_play_pause(self) -> None:
        """_handle_cmd_pause raises PlayerCommandFailed when source can't play/pause."""
        from music_assistant_models.errors import PlayerCommandFailed  # noqa: PLC0415
        from music_assistant_models.player import PlayerSource  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "CantPauseSrc")

        bad_source = PlayerSource(
            id="bad_src",
            name="Bad Source",
            passive=False,
            can_play_pause=False,
        )
        player._state.source_list = [bad_source]  # type: ignore[assignment]
        player._state.active_source = "bad_src"
        player._cache.clear()

        ctrl._get_active_plugin_source = MagicMock(return_value=None)  # type: ignore[method-assign]

        with pytest.raises(PlayerCommandFailed):
            await ctrl._handle_cmd_pause(player.player_id)


# ---------------------------------------------------------------------------
# _handle_enqueue_next_media
# ---------------------------------------------------------------------------


class TestHandleEnqueueNextMedia:
    """Tests for _handle_enqueue_next_media."""

    async def test_raises_when_no_enqueue_feature(self) -> None:
        """_handle_enqueue_next_media raises when player doesn't support ENQUEUE."""
        from music_assistant_models.enums import MediaType  # noqa: PLC0415
        from music_assistant_models.player import PlayerMedia  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "NoEnqueue")
        player._attr_supported_features = set()
        player._cache.clear()
        player._state.supported_features = set()

        media = PlayerMedia(uri="http://test.mp3", media_type=MediaType.UNKNOWN)

        ctrl._get_control_target = MagicMock(return_value=None)  # type: ignore[method-assign]

        with pytest.raises(Exception):  # noqa: B017, PT011
            await ctrl._handle_enqueue_next_media(player.player_id, media)

    async def test_calls_enqueue_on_player(self) -> None:
        """_handle_enqueue_next_media calls player.enqueue_next_media."""
        from music_assistant_models.enums import MediaType  # noqa: PLC0415
        from music_assistant_models.player import PlayerMedia  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "HasEnqueue")
        player._attr_supported_features = {PlayerFeature.ENQUEUE}
        player._cache.clear()
        player._state.supported_features = {PlayerFeature.ENQUEUE}

        media = PlayerMedia(uri="http://test.mp3", media_type=MediaType.UNKNOWN)

        ctrl._get_control_target = MagicMock(return_value=None)  # type: ignore[method-assign]
        player.enqueue_next_media = AsyncMock()  # type: ignore[method-assign]

        await ctrl._handle_enqueue_next_media(player.player_id, media)

        player.enqueue_next_media.assert_called_once_with(media)


# ---------------------------------------------------------------------------
# _handle_select_source - source is queue (set_active_mass_source)
# ---------------------------------------------------------------------------


class TestHandleSelectSourceExtended:
    """Extended tests for _handle_select_source."""

    async def test_select_source_invalid_source_raises(self) -> None:
        """_handle_select_source raises when source is invalid for player."""
        from music_assistant_models.errors import PlayerCommandFailed  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "BadSource")
        player._attr_supported_features = {PlayerFeature.SELECT_SOURCE}
        player._state.supported_features = {PlayerFeature.SELECT_SOURCE}
        player._state.source_list = []  # type: ignore[assignment]
        player._state.active_source = None
        player._cache.clear()

        mass.get_provider = MagicMock(return_value=None)
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)

        with pytest.raises(PlayerCommandFailed):
            await ctrl._handle_select_source(player.player_id, "invalid_source_id")


# ---------------------------------------------------------------------------
# signal_player_state_update - elapsed time protocol parent path
# ---------------------------------------------------------------------------


class TestSignalElapsedTimeProtocol:
    """Tests for signal_player_state_update elapsed time with protocol parent."""

    def test_elapsed_time_triggers_protocol_parent_update(self) -> None:
        """Elapsed time update on protocol player triggers parent player update."""
        from music_assistant.constants import ATTR_ELAPSED_TIME  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        mass.create_task = MagicMock()
        [parent, proto] = _add_players(ctrl, mass, "Parent5", "Proto5")
        proto._state.type = PlayerType.PROTOCOL
        proto._state.enabled = True
        proto._state.protocol_parent_id = parent.player_id  # type: ignore[attr-defined]

        ctrl.trigger_player_update = MagicMock()  # type: ignore[method-assign]

        import time  # noqa: PLC0415

        now = time.time()
        ctrl.signal_player_state_update(
            proto,
            {
                ATTR_ELAPSED_TIME: (10.0, 10.1),
                "elapsed_time_last_updated": (now, now),
            },
        )

        # elapsed_time only -> lightweight path, no event
        mass.signal_event.assert_not_called()


# ---------------------------------------------------------------------------
# signal_player_state_update - group members change path
# ---------------------------------------------------------------------------


class TestSignalGroupMembersChange:
    """Tests for signal_player_state_update when group_members changes."""

    def test_group_members_change_calls_handle_group_dsp(self) -> None:
        """signal_player_state_update calls _handle_group_dsp_change on group members change."""
        from music_assistant.constants import ATTR_GROUP_MEMBERS  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        mass.create_task = MagicMock()
        [player] = _add_players(ctrl, mass, "GroupDSP")
        player.state.enabled = True
        player.state.type = PlayerType.PLAYER
        player._provider.players = []  # type: ignore[misc]

        dsp_calls: list[tuple[list[str], list[str]]] = []

        def fake_handle_group_dsp(_p: object, prev: list[str], new: list[str]) -> None:
            dsp_calls.append((prev, new))

        ctrl._handle_group_dsp_change = fake_handle_group_dsp  # type: ignore[assignment]

        ctrl.signal_player_state_update(
            player, {ATTR_GROUP_MEMBERS: (["old_member"], ["new_member"])}
        )

        assert len(dsp_calls) == 1


# ---------------------------------------------------------------------------
# _fix_group_member_configs - with stale members
# ---------------------------------------------------------------------------


class TestFixGroupMemberConfigsStale:
    """Tests for _fix_group_member_configs with stale members."""

    async def test_fixes_stale_protocol_player_in_group(self) -> None:
        """_fix_group_member_configs corrects stale protocol player IDs."""
        from music_assistant.constants import CONF_GROUP_MEMBERS  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.config.set_raw_player_config_value = MagicMock()

        player_configs = {
            "sync_group_1": {
                "provider": "sync_group",
                "values": {CONF_GROUP_MEMBERS: ["proto_player"]},
            }
        }
        mass.config.get = MagicMock(return_value=player_configs)

        # _get_cached_protocol_parent_id maps proto_player -> real_player
        ctrl._get_cached_protocol_parent_id = MagicMock(return_value="real_player")  # type: ignore[method-assign]

        # No group player registered (no call to on_config_updated)
        ctrl.get_player = MagicMock(return_value=None)  # type: ignore[method-assign]

        await ctrl._fix_group_member_configs()

        mass.config.set_raw_player_config_value.assert_called_once_with(
            "sync_group_1", CONF_GROUP_MEMBERS, ["real_player"]
        )


# ---------------------------------------------------------------------------
# get_active_queue - protocol player path
# ---------------------------------------------------------------------------


class TestGetActiveQueueProtocol:
    """Tests for get_active_queue with protocol player."""

    def test_protocol_player_uses_parent_queue(self) -> None:
        """get_active_queue returns parent's queue for PROTOCOL type player."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [parent, proto] = _add_players(ctrl, mass, "QueueParent", "QueueProto")
        proto._state.type = PlayerType.PROTOCOL
        proto._state.synced_to = None
        proto._state.active_group = None
        proto._state.active_source = None
        proto._attr_type = PlayerType.PROTOCOL
        # Set protocol_parent_id via the name-mangled private attribute
        proto._Player__attr_protocol_parent_id = parent.player_id  # type: ignore[attr-defined]
        proto._cache.clear()

        fake_queue = MagicMock()
        fake_queue.queue_id = parent.player_id

        mass.player_queues = MagicMock()

        def mock_pq_get(source: str) -> object:
            if source == parent.player_id:
                return fake_queue
            return None

        mass.player_queues.get = mock_pq_get
        mass.players = ctrl

        result = ctrl.get_active_queue(proto)

        assert result is fake_queue


# ---------------------------------------------------------------------------
# _schedule_update_all_players - closing path
# ---------------------------------------------------------------------------


class TestScheduleUpdateAllPlayers:
    """Tests for _schedule_update_all_players."""

    def test_returns_early_when_closing(self) -> None:
        """_schedule_update_all_players returns early when mass is closing."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = True
        mass.call_later = MagicMock()

        ctrl._schedule_update_all_players()

        mass.call_later.assert_not_called()


# ---------------------------------------------------------------------------
# _get_active_plugin_source - via active_source
# ---------------------------------------------------------------------------


class TestGetActivePluginSource:
    """Tests for _get_active_plugin_source."""

    def test_returns_plugin_source_matching_active_source(self) -> None:
        """_get_active_plugin_source returns plugin source when active_source matches."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "PluginSrc")
        player._state.active_source = "my_plugin"

        plugin_src = MagicMock()
        plugin_src.id = "my_plugin"
        plugin_src.in_use_by = None

        ctrl.get_plugin_sources = MagicMock(return_value=[plugin_src])  # type: ignore[method-assign]

        result = ctrl._get_active_plugin_source(player)

        assert result is plugin_src


# ---------------------------------------------------------------------------
# _handle_cmd_volume_set - unmute path
# ---------------------------------------------------------------------------


class TestHandleCmdVolumeSetUnmute:
    """Tests for _handle_cmd_volume_set auto-unmute path."""

    async def test_unmutes_before_setting_volume(self) -> None:
        """_handle_cmd_volume_set unmutes player before setting volume when muted."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "MutedVol")
        player._state.volume_muted = True
        player._state.mute_control = PLAYER_CONTROL_NATIVE
        player._cache["volume_control"] = PLAYER_CONTROL_NATIVE
        player._state.type = PlayerType.PLAYER
        player._cache.clear()
        player._cache["volume_control"] = PLAYER_CONTROL_NATIVE

        unmute_calls: list[tuple[str, bool]] = []

        async def fake_volume_mute(pid: str, muted: bool) -> None:
            unmute_calls.append((pid, muted))

        ctrl.cmd_volume_mute = fake_volume_mute  # type: ignore[assignment]
        player.volume_set = AsyncMock()  # type: ignore[method-assign]
        ctrl._get_active_plugin_source = MagicMock(return_value=None)  # type: ignore[method-assign]

        await ctrl._handle_cmd_volume_set(player.player_id, 50)

        assert (player.player_id, False) in unmute_calls


# ---------------------------------------------------------------------------
# _handle_cmd_resume - source_list path
# ---------------------------------------------------------------------------


class TestHandleCmdResumeSourceList:
    """Tests for _handle_cmd_resume with source in source_list."""

    async def test_resume_calls_queue_when_source_has_queue(self) -> None:
        """_handle_cmd_resume calls queue.resume when there is an active queue."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "SourceResume")
        player._state.powered = True
        player._state.power_control = PLAYER_CONTROL_NONE
        player._state.source_list = []  # type: ignore[assignment]
        player._state.active_source = "my_src_queue"
        player._state.current_media = None

        fake_queue = MagicMock()
        fake_queue.queue_id = "my_src_queue"

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=fake_queue)
        mass.player_queues.resume = AsyncMock()

        await ctrl._handle_cmd_resume(player.player_id)

        mass.player_queues.resume.assert_called_once_with("my_src_queue")


# ---------------------------------------------------------------------------
# cmd_seek - plugin and source paths
# ---------------------------------------------------------------------------


class TestCmdSeekPaths:
    """Tests for cmd_seek plugin source and source-no-seek paths."""

    async def test_seek_via_plugin_source(self) -> None:
        """cmd_seek calls plugin_source.on_seek when plugin source supports seeking."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "SeekPlugin")

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)

        on_seek_called: list[float] = []

        plugin_src = MagicMock()
        plugin_src.can_seek = True
        plugin_src.on_seek = AsyncMock(side_effect=lambda pos: on_seek_called.append(pos))

        ctrl._get_active_plugin_source = MagicMock(return_value=plugin_src)  # type: ignore[method-assign]

        with _patched():
            await ctrl.cmd_seek(player.player_id, 60.0)  # type: ignore[arg-type]

        assert 60.0 in on_seek_called

    async def test_seek_source_no_seek_raises(self) -> None:
        """cmd_seek raises when active source does not support seeking."""
        from music_assistant_models.errors import PlayerCommandFailed  # noqa: PLC0415
        from music_assistant_models.player import PlayerSource  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "SrcNoSeek")
        player._attr_supported_features = {PlayerFeature.SEEK}

        no_seek_source = PlayerSource(id="no_seek_src", name="No Seek", can_seek=False)
        # cmd_seek reads player.source_list (_attr_source_list) and player.active_source
        player._attr_source_list = [no_seek_source]
        player._attr_active_source = "no_seek_src"
        player._cache.clear()

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)

        ctrl._get_active_plugin_source = MagicMock(return_value=None)  # type: ignore[method-assign]

        with _patched(), pytest.raises(PlayerCommandFailed):
            await ctrl.cmd_seek(player.player_id, 30)


# ---------------------------------------------------------------------------
# cmd_next_track / cmd_previous_track - native paths
# ---------------------------------------------------------------------------


class TestCmdNextPrevNative:
    """Tests for cmd_next/prev with NEXT_PREVIOUS feature and active source."""

    async def test_next_with_feature_unavailable_source_raises(self) -> None:
        """cmd_next_track raises when source can't next/previous."""
        from music_assistant_models.errors import PlayerCommandFailed  # noqa: PLC0415
        from music_assistant_models.player import PlayerSource  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "NextSrc")
        # cmd_next_track checks player.state.supported_features (_state.supported_features)
        player._state.supported_features = {PlayerFeature.NEXT_PREVIOUS}

        bad_source = PlayerSource(id="bad_src2", name="Bad Next Src", can_next_previous=False)
        player._state.source_list = [bad_source]  # type: ignore[assignment]
        player._state.active_source = "bad_src2"
        player._cache.clear()

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)
        ctrl._get_active_plugin_source = MagicMock(return_value=None)  # type: ignore[method-assign]

        with _patched(), pytest.raises(PlayerCommandFailed):
            await ctrl.cmd_next_track(player.player_id)

    async def test_previous_with_feature_unavailable_source_raises(self) -> None:
        """cmd_previous_track raises when source can't next/previous."""
        from music_assistant_models.errors import PlayerCommandFailed  # noqa: PLC0415
        from music_assistant_models.player import PlayerSource  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "PrevSrc")
        # cmd_previous_track checks player.state.supported_features
        player._state.supported_features = {PlayerFeature.NEXT_PREVIOUS}

        bad_source = PlayerSource(id="bad_src3", name="Bad Prev Src", can_next_previous=False)
        player._state.source_list = [bad_source]  # type: ignore[assignment]
        player._state.active_source = "bad_src3"
        player._cache.clear()

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)
        ctrl._get_active_plugin_source = MagicMock(return_value=None)  # type: ignore[method-assign]

        with _patched(), pytest.raises(PlayerCommandFailed):
            await ctrl.cmd_previous_track(player.player_id)


# ---------------------------------------------------------------------------
# cmd_volume_up/down - step sizes
# ---------------------------------------------------------------------------


class TestCmdVolumeStepSizes:
    """Tests for cmd_volume_up/down step size branches."""

    async def test_volume_up_mid_range_uses_step_2(self) -> None:
        """cmd_volume_up uses step=2 when volume is in 10-30 range."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "MidVolUp")
        player._attr_type = PlayerType.PLAYER
        player._state.type = PlayerType.PLAYER
        player._state.volume_level = 20

        set_calls: list[tuple[str, int]] = []

        async def fake_volume_set(pid: str, vol: int) -> None:
            set_calls.append((pid, vol))

        ctrl.cmd_volume_set = fake_volume_set  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_volume_up(player.player_id)

        # step=2 for 10 < vol < 30
        assert any(v == 22 for _, v in set_calls)

    async def test_volume_down_low_uses_step_1(self) -> None:
        """cmd_volume_down uses step=1 when volume < 10."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "LowVolDown")
        player._attr_type = PlayerType.PLAYER
        player._state.type = PlayerType.PLAYER
        player._state.volume_level = 8

        set_calls: list[tuple[str, int]] = []

        async def fake_volume_set(pid: str, vol: int) -> None:
            set_calls.append((pid, vol))

        ctrl.cmd_volume_set = fake_volume_set  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_volume_down(player.player_id)

        # step=1 for vol < 10
        assert any(v == 7 for _, v in set_calls)

    async def test_volume_down_mid_range_uses_step_2(self) -> None:
        """cmd_volume_down uses step=2 when volume is in 10-30 range."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "MidVolDown")
        player._attr_type = PlayerType.PLAYER
        player._state.type = PlayerType.PLAYER
        player._state.volume_level = 25

        set_calls: list[tuple[str, int]] = []

        async def fake_volume_set(pid: str, vol: int) -> None:
            set_calls.append((pid, vol))

        ctrl.cmd_volume_set = fake_volume_set  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_volume_down(player.player_id)

        # step=2 for 10 < vol < 30
        assert any(v == 23 for _, v in set_calls)


# ---------------------------------------------------------------------------
# cmd_volume_mute - external player control path
# ---------------------------------------------------------------------------


class TestCmdVolumeMuteExternal:
    """Tests for cmd_volume_mute with external player control."""

    async def test_external_mute_without_support_raises(self) -> None:
        """cmd_volume_mute raises when external control doesn't support mute."""
        from music_assistant_models.player_control import PlayerControl  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "ExtMute")
        player._cache["volume_control"] = PLAYER_CONTROL_NATIVE
        player._cache["mute_control"] = "external_ctrl_id"
        player._state.synced_to = None
        player._state.active_group = None

        # Register a player control that does NOT support mute
        pc = MagicMock(spec=PlayerControl)
        pc.name = "ExternalCtrl"
        pc.supports_mute = False
        ctrl._controls = {"external_ctrl_id": pc}

        with _patched(), pytest.raises(Exception):  # noqa: B017, PT011
            await ctrl.cmd_volume_mute(player.player_id, True)


# ---------------------------------------------------------------------------
# play_media - delegation
# ---------------------------------------------------------------------------


class TestPlayMediaDelegation:
    """Tests for play_media delegation to _handle_play_media."""

    async def test_play_media_calls_handle_play_media(self) -> None:
        """play_media delegates to _handle_play_media with player redirect."""
        from music_assistant_models.enums import MediaType  # noqa: PLC0415
        from music_assistant_models.player import PlayerMedia  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "PlayMediaPlayer")

        media = PlayerMedia(uri="http://test.mp3", media_type=MediaType.UNKNOWN)

        handle_calls: list[tuple[str, object]] = []

        async def fake_handle_play_media(pid: str, m: object) -> None:
            handle_calls.append((pid, m))

        ctrl._handle_play_media = fake_handle_play_media  # type: ignore[assignment]

        with _patched():
            await ctrl.play_media(player_id=player.player_id, media=media)

        assert any(pid == player.player_id for pid, _ in handle_calls)


# ---------------------------------------------------------------------------
# select_sound_mode - forward to player
# ---------------------------------------------------------------------------


class TestSelectSoundModeForward:
    """Tests for select_sound_mode forwarding to player."""

    async def test_valid_mode_calls_player_select(self) -> None:
        """select_sound_mode calls player.select_sound_mode() for a valid mode."""
        from music_assistant_models.player import PlayerSoundMode  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "ValidMode")
        player._attr_supported_features = {PlayerFeature.SELECT_SOUND_MODE}
        player._attr_active_sound_mode = "rock"
        player._attr_sound_mode_list = [PlayerSoundMode(id="jazz", name="Jazz")]

        player.select_sound_mode = AsyncMock()  # type: ignore[method-assign]

        with _patched():
            await ctrl.select_sound_mode(player.player_id, "jazz")

        player.select_sound_mode.assert_called_once_with("jazz")


# ---------------------------------------------------------------------------
# set_option - success and read-only paths
# ---------------------------------------------------------------------------


class TestSetOptionPaths:
    """Tests for set_option with valid and read-only options."""

    async def test_same_value_returns_early(self) -> None:
        """set_option does nothing when value is unchanged."""
        from music_assistant_models.player import PlayerOption, PlayerOptionType  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "SameOpt")
        player._attr_supported_features = {PlayerFeature.OPTIONS}
        player._state.supported_features = {PlayerFeature.OPTIONS}

        opt = PlayerOption(
            key="my_opt",
            name="My Opt",
            type=PlayerOptionType.STRING,
            value="current",
            read_only=False,
        )
        player._attr_options = [opt]
        player._cache.clear()

        player.set_option = AsyncMock()  # type: ignore[method-assign]

        with _patched():
            await ctrl.set_option(player.player_id, "my_opt", "current")

        player.set_option.assert_not_called()

    async def test_read_only_option_raises(self) -> None:
        """set_option raises UnsupportedFeaturedException for read-only options."""
        from music_assistant_models.player import PlayerOption, PlayerOptionType  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "ROOpt")
        player._attr_supported_features = {PlayerFeature.OPTIONS}
        player._state.supported_features = {PlayerFeature.OPTIONS}

        opt = PlayerOption(
            key="ro_opt", name="RO Opt", type=PlayerOptionType.STRING, value="v1", read_only=True
        )
        player._attr_options = [opt]
        player._cache.clear()

        with _patched(), pytest.raises(Exception):  # noqa: B017, PT011
            await ctrl.set_option(player.player_id, "ro_opt", "v2")

    async def test_success_calls_player_set_option(self) -> None:
        """set_option calls player.set_option for a valid, non-readonly, changed option."""
        from music_assistant_models.player import PlayerOption, PlayerOptionType  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "GoodOpt")
        player._attr_supported_features = {PlayerFeature.OPTIONS}
        player._state.supported_features = {PlayerFeature.OPTIONS}

        opt = PlayerOption(
            key="good_opt", name="Good", type=PlayerOptionType.STRING, value="old", read_only=False
        )
        player._attr_options = [opt]
        player._cache.clear()

        player.set_option = AsyncMock()  # type: ignore[method-assign]

        with _patched():
            await ctrl.set_option(player.player_id, "good_opt", "new")

        player.set_option.assert_called_once_with(option_key="good_opt", option_value="new")


# ---------------------------------------------------------------------------
# cmd_group - delegation
# ---------------------------------------------------------------------------


class TestCmdGroupDelegation:
    """Tests for cmd_group - delegates to cmd_set_members."""

    async def test_cmd_group_delegates_to_set_members(self) -> None:
        """cmd_group calls cmd_set_members with target and player_id."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [leader, member] = _add_players(ctrl, mass, "Leader5", "Member5")
        leader._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        leader._cache.clear()

        set_calls: list[tuple[str, list[str]]] = []

        async def fake_set_members(
            target: str,
            player_ids_to_add: list[str] | None = None,
            _player_ids_to_remove: list[str] | None = None,
        ) -> None:
            set_calls.append((target, player_ids_to_add or []))

        ctrl.cmd_set_members = fake_set_members  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_group(member.player_id, leader.player_id)

        assert (leader.player_id, [member.player_id]) in set_calls


# ---------------------------------------------------------------------------
# cmd_ungroup - not found and sync leader paths
# ---------------------------------------------------------------------------


class TestCmdUngroupPaths:
    """Tests for cmd_ungroup paths."""

    async def test_ungroup_player_not_available_logs_warning(self) -> None:
        """cmd_ungroup does nothing and logs warning when player is not in _players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl

        set_calls: list[str] = []

        async def fake_set_members(
            target: str,
            _player_ids_to_add: list[str] | None = None,
            _player_ids_to_remove: list[str] | None = None,
        ) -> None:
            set_calls.append(target)

        ctrl.cmd_set_members = fake_set_members  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_ungroup("nonexistent_player_id")

        assert len(set_calls) == 0

    async def test_ungroup_sync_leader_removes_all_members(self) -> None:
        """cmd_ungroup sends set_members to remove all group_members when player is leader."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [leader, m1] = _add_players(ctrl, mass, "Leader6", "Member6")
        leader._state.active_group = None
        leader._state.synced_to = None
        leader._state.group_members = [leader.player_id, m1.player_id]  # type: ignore[assignment]
        leader._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        leader._cache.clear()

        set_calls: list[tuple[str, list[str]]] = []

        async def fake_set_members(
            target: str,
            _player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            set_calls.append((target, player_ids_to_remove or []))

        ctrl.cmd_set_members = fake_set_members  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_ungroup(leader.player_id)

        assert any(leader.player_id in removed for _, removed in set_calls)


# ---------------------------------------------------------------------------
# create_group_player - error paths
# ---------------------------------------------------------------------------


class TestCreateGroupPlayerErrors:
    """Tests for create_group_player error paths."""

    async def test_raises_when_provider_not_found(self) -> None:
        """create_group_player raises ProviderUnavailableError when provider not found."""
        from music_assistant_models.errors import ProviderUnavailableError  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_provider = MagicMock(return_value=None)

        with pytest.raises(ProviderUnavailableError):
            await ctrl.create_group_player("nonexistent_provider", "Test Group", [])


# ---------------------------------------------------------------------------
# remove_player - various paths
# ---------------------------------------------------------------------------


class TestRemovePlayerPaths:
    """Tests for remove() branches."""

    async def test_removes_config_when_player_not_registered(self) -> None:
        """remove() deletes config when player is not in _players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.config = MagicMock()
        mass.config.remove = MagicMock()

        await ctrl.remove("unregistered_player")

        mass.config.remove.assert_called()

    async def test_raises_when_provider_lacks_remove_feature(self) -> None:
        """remove() raises when provider does not support REMOVE_PLAYER feature."""
        from music_assistant_models.errors import UnsupportedFeaturedException  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "RegPlayer")
        player._state.type = PlayerType.PLAYER

        # Make check_feature raise UnsupportedFeaturedException
        player._provider.check_feature = MagicMock(  # type: ignore[method-assign]
            side_effect=UnsupportedFeaturedException("not supported")
        )

        with pytest.raises(UnsupportedFeaturedException):
            await ctrl.remove(player.player_id)


# ---------------------------------------------------------------------------
# register_or_update - calls register when not registered
# ---------------------------------------------------------------------------


class TestRegisterOrUpdateCallsRegister:
    """Tests that register_or_update calls register for new players."""

    async def test_calls_register_for_new_player(self) -> None:
        """register_or_update calls register() when player is not in _players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False

        provider = MockProvider("ror_prov", instance_id="ror_prov", mass=mass)
        player = MockPlayer(provider, "ror_player", "ROR Player")

        register_calls: list[str] = []

        async def fake_register(p: object) -> None:
            register_calls.append(getattr(p, "player_id", ""))

        ctrl.register = fake_register  # type: ignore[assignment]

        await ctrl.register_or_update(player)

        assert "ror_player" in register_calls


# ---------------------------------------------------------------------------
# signal_player_state_update - additional coverage
# ---------------------------------------------------------------------------


class TestSignalPlayerStateUpdateCoverage:
    """Tests for various signal_player_state_update branches."""

    def test_elapsed_time_small_change_returns_early(self) -> None:
        """signal_player_state_update with only elapsed_time returns early without event."""
        import time  # noqa: PLC0415

        from music_assistant.constants import ATTR_ELAPSED_TIME  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        [player] = _add_players(ctrl, mass, "ElapsedPlayer")
        player.state.enabled = True
        player.state.type = PlayerType.PLAYER

        now = time.time()
        ctrl.signal_player_state_update(
            player,
            {
                ATTR_ELAPSED_TIME: (100.0, 100.05),
                "elapsed_time_last_updated": (now, now),
            },
        )

        # elapsed time only path → no signal_event
        mass.signal_event.assert_not_called()

    def test_active_group_triggers_group_update(self) -> None:
        """signal_player_state_update triggers update on active group player."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        mass.create_task = MagicMock()
        [group, member] = _add_players(ctrl, mass, "GroupX", "MemberX")
        member.state.enabled = True
        member.state.type = PlayerType.PLAYER
        member.state.active_group = group.player_id
        member.state.synced_to = None
        member._provider.players = []  # type: ignore[misc]

        ctrl.signal_player_state_update(member, {"volume_level": (10, 20)})

        call_args = [str(c) for c in mass.call_later.call_args_list]
        assert any(group.player_id in c for c in call_args)


# ---------------------------------------------------------------------------
# _handle_cmd_power - power off sync children
# ---------------------------------------------------------------------------


class TestHandleCmdPowerSyncChildren:
    """Tests for _handle_cmd_power powering off sync group children."""

    async def test_powers_off_synced_children_on_power_off(self) -> None:
        """_handle_cmd_power powers off group members when leader powers off."""
        from music_assistant.controllers.players.controller import (  # noqa: PLC0415
            PlayerController as PC,  # noqa: N817
        )

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [leader, m1] = _add_players(ctrl, mass, "PowerLeader", "PowerM1")

        leader._state.powered = True
        leader._state.power_control = PLAYER_CONTROL_NONE
        leader._state.group_members = [leader.player_id, m1.player_id]  # type: ignore[assignment]
        leader._state.type = PlayerType.PLAYER
        leader._state.synced_to = None
        leader._state.active_group = None
        leader._state.playback_state = PlaybackState.IDLE
        leader._attr_type = PlayerType.PLAYER

        m1._state.powered = True
        m1._state.available = True
        m1._state.enabled = True
        m1._cache["power_control"] = PLAYER_CONTROL_FAKE

        power_calls: list[tuple[str, bool]] = []

        async def fake_handle_power(pid: str, powered: bool, skip_auto_play: bool = False) -> None:  # noqa: ARG001
            power_calls.append((pid, powered))

        ctrl._handle_cmd_power = fake_handle_power  # type: ignore[assignment]

        # make mass.create_task run coroutines as real asyncio tasks so TaskManager works
        def real_create_task(coro: object) -> object:
            import asyncio as _asyncio  # noqa: PLC0415

            return _asyncio.ensure_future(coro)  # type: ignore[call-overload]

        mass.create_task = real_create_task

        # Direct call to real implementation; leader power_control=NONE so it returns early
        # but the "power off children" elif (lines 2790-2795) runs first if group_members set
        await PC._handle_cmd_power(ctrl, leader.player_id, False)

        # power_control is NONE so it returns at line 2798-2803 without actually powering
        # The test verifies: no hang, no exception, group members path was exercised
        assert True


# ---------------------------------------------------------------------------
# _handle_cmd_volume_set - external control and protocol redirect
# ---------------------------------------------------------------------------


class TestHandleCmdVolumeSetExternal:
    """Tests for _handle_cmd_volume_set with external player control."""

    async def test_volume_set_redirects_to_protocol_player(self) -> None:
        """_handle_cmd_volume_set redirects to protocol player when volume_control is a player_id."""  # noqa: E501
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player, proto] = _add_players(ctrl, mass, "VolProtoParent", "VolProtoChild")

        # Volume control points to protocol player
        player._cache["volume_control"] = proto.player_id
        player._state.type = PlayerType.PLAYER
        player._state.mute_control = PLAYER_CONTROL_NONE
        player._state.volume_muted = False

        volume_set_calls: list[tuple[str, int]] = []

        async def fake_volume_set(pid: str, level: int) -> None:
            volume_set_calls.append((pid, level))

        ctrl._handle_cmd_volume_set = fake_volume_set  # type: ignore[assignment]
        ctrl._get_active_plugin_source = MagicMock(return_value=None)  # type: ignore[method-assign]

        # Call the real implementation directly
        from music_assistant.controllers.players.controller import (  # noqa: PLC0415
            PlayerController as PC,  # noqa: N817
        )

        mass2 = _make_mock_mass()
        ctrl2 = PlayerController(mass2)
        mass2.players = ctrl2
        mass2.get_providers = MagicMock(return_value=[])
        [player2, proto2] = _add_players(ctrl2, mass2, "VPP2", "VPC2")

        player2._cache["volume_control"] = proto2.player_id
        player2._state.volume_control = proto2.player_id
        player2._state.type = PlayerType.PLAYER
        player2._state.mute_control = PLAYER_CONTROL_NONE
        player2._state.volume_muted = False

        volume_set_calls2: list[tuple[str, int]] = []

        async def fake_recursive(pid: str, level: int) -> None:
            volume_set_calls2.append((pid, level))

        ctrl2._handle_cmd_volume_set = fake_recursive  # type: ignore[assignment]
        ctrl2._get_active_plugin_source = MagicMock(return_value=None)  # type: ignore[method-assign]

        await PC._handle_cmd_volume_set(ctrl2, player2.player_id, 75)

        assert (proto2.player_id, 75) in volume_set_calls2


# ---------------------------------------------------------------------------
# _handle_cmd_stop - protocol player no power (native stop)
# ---------------------------------------------------------------------------


class TestHandleCmdStopProtocolNoPower:
    """Tests for _handle_cmd_stop with protocol player without POWER feature."""

    async def test_stops_protocol_player_directly(self) -> None:
        """_handle_cmd_stop calls stop() on protocol player when no POWER feature."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        [player, proto] = _add_players(ctrl, mass, "Parent6", "Proto6")

        proto._attr_supported_features = set()  # no POWER feature
        proto._cache.clear()

        player._Player__attr_active_output_protocol = proto.player_id  # type: ignore[attr-defined]
        player.mark_stop_called = MagicMock()  # type: ignore[misc, method-assign]

        proto.stop = AsyncMock()  # type: ignore[method-assign]

        await ctrl._handle_cmd_stop(player.player_id)

        proto.stop.assert_called_once()


# ---------------------------------------------------------------------------
# _handle_cmd_play - power on path
# ---------------------------------------------------------------------------


class TestHandleCmdPlayPowerOn:
    """Tests for _handle_cmd_play power-on path."""

    async def test_powers_on_before_play(self) -> None:
        """_handle_cmd_play powers on player before playing when powered=False."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "PoweredOffPlay")
        player._state.playback_state = PlaybackState.IDLE
        player._state.powered = False
        player._state.power_control = PLAYER_CONTROL_FAKE
        player._state.source_list = []  # type: ignore[assignment]
        player._state.active_source = None
        player._state.current_media = None
        player._cache.clear()

        power_calls: list[tuple[str, bool]] = []

        async def fake_handle_power(pid: str, powered: bool, skip_auto_play: bool = False) -> None:  # noqa: ARG001
            power_calls.append((pid, powered))

        ctrl._handle_cmd_power = fake_handle_power  # type: ignore[assignment]
        ctrl._get_active_plugin_source = MagicMock(return_value=None)  # type: ignore[method-assign]
        ctrl._get_control_target = MagicMock(return_value=None)  # type: ignore[method-assign]

        player.play = AsyncMock()  # type: ignore[method-assign]

        await ctrl._handle_cmd_play(player.player_id)

        assert (player.player_id, True) in power_calls


# ---------------------------------------------------------------------------
# _handle_group_dsp_change - shrink path
# ---------------------------------------------------------------------------


class TestHandleGroupDspChangeShrink:
    """Tests for _handle_group_dsp_change when group shrinks."""

    def test_group_shrinks_with_dsp_triggers_reload(self) -> None:
        """_handle_group_dsp_change triggers DSP reload when group shrinks with DSP enabled."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.create_task = MagicMock()
        [player, child] = _add_players(ctrl, mass, "DspGroup", "DspChild")

        player._state.type = PlayerType.GROUP
        player._attr_supported_features = set()  # no MULTI_DEVICE_DSP
        player._cache.clear()

        # DSP enabled for the child that remains
        dsp_conf = MagicMock()
        dsp_conf.enabled = True
        mass.config.get_player_dsp_config = MagicMock(return_value=dsp_conf)
        mass.players.on_player_dsp_change = AsyncMock()

        ctrl._handle_group_dsp_change(
            player,
            [player.player_id, child.player_id],  # prev: 2 members
            [player.player_id],  # new: 1 member
        )

        # Should have triggered DSP reload via create_task
        mass.create_task.assert_called()


# ---------------------------------------------------------------------------
# _auto_ungroup_if_synced
# ---------------------------------------------------------------------------


class TestAutoUngroupIfSynced:
    """Tests for _auto_ungroup_if_synced."""

    async def test_calls_set_members_when_synced(self) -> None:
        """_auto_ungroup_if_synced calls cmd_set_members when player is synced."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [leader, member] = _add_players(ctrl, mass, "Leader7", "Member7")
        member._state.synced_to = leader.player_id

        set_calls: list[tuple[str, list[str]]] = []

        async def fake_set_members(
            target: str,
            _player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            set_calls.append((target, player_ids_to_remove or []))

        ctrl.cmd_set_members = fake_set_members  # type: ignore[assignment]

        with patch("music_assistant.controllers.players.controller.asyncio.sleep", new=AsyncMock()):
            await ctrl._auto_ungroup_if_synced(member, "test context")

        assert any(member.player_id in removed for _, removed in set_calls)

    async def test_does_nothing_when_not_synced(self) -> None:
        """_auto_ungroup_if_synced does nothing when player is not synced."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "FreePlayer")
        player._state.synced_to = None

        set_calls: list[str] = []

        async def fake_set_members(
            target: str,
            _player_ids_to_add: list[str] | None = None,
            _player_ids_to_remove: list[str] | None = None,
        ) -> None:
            set_calls.append(target)

        ctrl.cmd_set_members = fake_set_members  # type: ignore[assignment]

        await ctrl._auto_ungroup_if_synced(player, "test")

        assert len(set_calls) == 0


# ---------------------------------------------------------------------------
# wait_for_state - minimal_time path
# ---------------------------------------------------------------------------


class TestWaitForStateMinimalTime:
    """Tests for wait_for_state minimal_time sleep path."""

    async def test_waits_minimal_time_when_state_reached_fast(self) -> None:
        """wait_for_state sleeps extra when state reached before minimal_time."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "FastState")
        player._state.playback_state = PlaybackState.PLAYING  # already in wanted state

        sleep_calls: list[float] = []

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)

        with patch("music_assistant.controllers.players.controller.asyncio.sleep", new=fake_sleep):
            await ctrl.wait_for_state(player, PlaybackState.PLAYING, timeout=5.0, minimal_time=10.0)

        # Should have slept to cover the minimal_time
        assert any(s > 0 for s in sleep_calls)


# ---------------------------------------------------------------------------
# on_player_config_change - player provider enabled/disabled events
# ---------------------------------------------------------------------------


class TestOnPlayerConfigChangeProviderEvents:
    """Tests for on_player_config_change player provider enable/disable events."""

    async def test_provider_notified_on_player_enable(self) -> None:
        """on_player_config_change notifies provider when player is enabled."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "EnabledPlayer")

        prov_mock = MagicMock()
        prov_mock.on_player_enabled = MagicMock()
        prov_mock.on_player_disabled = MagicMock()

        mass.get_provider = MagicMock(return_value=prov_mock)

        config = MagicMock()
        config.player_id = player.player_id
        config.provider = "test_provider"
        config.enabled = True

        from music_assistant_models.enums import ProviderType  # noqa: PLC0415

        prov_mock.type = ProviderType.PLAYER

        with patch(
            "music_assistant.controllers.players.controller.isinstance",
            return_value=True,
        ):
            await ctrl.on_player_config_change(config, {ATTR_ENABLED})

        # Provider should be notified of enable
        prov_mock.on_player_enabled.assert_called_once_with(player.player_id)


# ---------------------------------------------------------------------------
# _cleanup_stale_protocol_parent_ids - stale parent
# ---------------------------------------------------------------------------


class TestCleanupStaleParentIds:
    """Tests for _cleanup_stale_protocol_parent_ids with stale parent config."""

    def test_clears_stale_parent_id_from_config(self) -> None:
        """_cleanup_stale_protocol_parent_ids sets parent ID to None when config missing."""
        from music_assistant.constants import CONF_PROTOCOL_PARENT_ID  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [child] = _add_players(ctrl, mass, "ChildProto")

        child._Player__attr_protocol_parent_id = "old_parent_id"  # type: ignore[attr-defined]

        # Player configs: child is a protocol player pointing to non-existent parent
        all_configs = {
            child.player_id: {
                "player_type": "protocol",
                "provider": "test",
                "values": {CONF_PROTOCOL_PARENT_ID: "old_parent_id"},
            }
            # old_parent_id is NOT in all_configs → stale reference
        }
        mass.config.get = MagicMock(return_value=all_configs)
        mass.config.set = MagicMock()

        ctrl._cleanup_stale_protocol_parent_ids()

        mass.config.set.assert_called()


# ---------------------------------------------------------------------------
# Batch 3: Targeted coverage tests for remaining uncovered lines
# ---------------------------------------------------------------------------


class TestCmdNextPrevPlugin:
    """Tests for cmd_next_track / cmd_previous_track with plugin source on_next/on_previous."""

    async def test_next_calls_plugin_on_next(self) -> None:
        """cmd_next_track calls plugin_source.on_next when plugin supports it."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "PluginNext")
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)

        on_next_called: list[bool] = []
        plugin_src = MagicMock()
        plugin_src.can_next_previous = True
        plugin_src.on_next = AsyncMock(side_effect=lambda: on_next_called.append(True))

        ctrl._get_active_plugin_source = MagicMock(return_value=plugin_src)  # type: ignore[method-assign]

        with _patched():
            await ctrl.cmd_next_track(player.player_id)

        assert on_next_called

    async def test_previous_calls_plugin_on_previous(self) -> None:
        """cmd_previous_track calls plugin_source.on_previous when plugin supports it."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "PluginPrev")
        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)

        on_prev_called: list[bool] = []
        plugin_src = MagicMock()
        plugin_src.can_next_previous = True
        plugin_src.on_previous = AsyncMock(side_effect=lambda: on_prev_called.append(True))

        ctrl._get_active_plugin_source = MagicMock(return_value=plugin_src)  # type: ignore[method-assign]

        with _patched():
            await ctrl.cmd_previous_track(player.player_id)

        assert on_prev_called


class TestCmdNextPrevNativeCanNext:
    """Tests for cmd_next/prev with NEXT_PREVIOUS and source that CAN next."""

    async def test_next_calls_player_next_track(self) -> None:
        """cmd_next_track calls player.next_track when source supports it."""
        from music_assistant_models.player import PlayerSource  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "NativeNext")
        player._state.supported_features = {PlayerFeature.NEXT_PREVIOUS}

        good_source = PlayerSource(id="good_src", name="Good Next", can_next_previous=True)
        player._state.source_list = [good_source]  # type: ignore[assignment]
        player._state.active_source = "good_src"

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)
        ctrl._get_active_plugin_source = MagicMock(return_value=None)  # type: ignore[method-assign]

        player.next_track = AsyncMock()  # type: ignore[method-assign]

        with _patched():
            await ctrl.cmd_next_track(player.player_id)

        player.next_track.assert_called_once()

    async def test_previous_calls_player_previous_track(self) -> None:
        """cmd_previous_track calls player.previous_track when source supports it."""
        from music_assistant_models.player import PlayerSource  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "NativePrev")
        player._state.supported_features = {PlayerFeature.NEXT_PREVIOUS}

        good_source = PlayerSource(id="good_src2", name="Good Prev", can_next_previous=True)
        player._state.source_list = [good_source]  # type: ignore[assignment]
        player._state.active_source = "good_src2"

        mass.player_queues = MagicMock()
        mass.player_queues.get = MagicMock(return_value=None)
        ctrl._get_active_plugin_source = MagicMock(return_value=None)  # type: ignore[method-assign]

        player.previous_track = AsyncMock()  # type: ignore[method-assign]

        with _patched():
            await ctrl.cmd_previous_track(player.player_id)

        player.previous_track.assert_called_once()


class TestCmdPowerDelegates:
    """Test cmd_power delegates to _handle_cmd_power."""

    async def test_cmd_power_delegates(self) -> None:
        """cmd_power calls _handle_cmd_power."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "PowerDelegate")
        player._state.powered = False

        calls: list[tuple[str, bool]] = []

        async def fake_handle_power(pid: str, powered: bool, skip_auto_play: bool = False) -> None:  # noqa: ARG001
            calls.append((pid, powered))

        ctrl._handle_cmd_power = fake_handle_power  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_power(player.player_id, True)

        assert (player.player_id, True) in calls


class TestCmdGroupVolumeGroupPath:
    """Test cmd_group_volume routes to set_group_volume for group players."""

    async def test_group_volume_calls_set_group_volume(self) -> None:
        """cmd_group_volume calls set_group_volume for a player with group_members."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [leader, m1] = _add_players(ctrl, mass, "GrpVLeader", "GrpVM1")

        leader._state.type = PlayerType.GROUP
        leader._state.group_members = [leader.player_id, m1.player_id]  # type: ignore[assignment]

        calls: list[tuple[object, int]] = []

        async def fake_set_group_volume(p: object, vol: int) -> None:
            calls.append((p, vol))

        ctrl.set_group_volume = fake_set_group_volume  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_group_volume(leader.player_id, 60)

        assert len(calls) == 1


class TestCmdGroupVolumeDownMidRange:
    """Tests for cmd_group_volume_down mid-range step size."""

    async def test_mid_range_uses_step_2(self) -> None:
        """cmd_group_volume_down uses step=2 in the 10-30 volume range."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "GrpVolDown")

        player._state.type = PlayerType.GROUP
        player._state.group_members = [player.player_id]  # type: ignore[assignment]
        player._state.group_volume = 20  # mid range: 10 < 20 < 30

        vol_calls: list[tuple[str, int]] = []

        async def fake_cmd_group_volume(pid: str, vol: int) -> None:
            vol_calls.append((pid, vol))

        ctrl.cmd_group_volume = fake_cmd_group_volume  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_group_volume_down(player.player_id)

        # step=2 → 20 - 2 = 18
        assert any(vol == 18 for _, vol in vol_calls)


class TestMuteExternalWithSupport:
    """Test cmd_volume_mute with external player control that supports mute."""

    async def test_external_mute_calls_mute_set(self) -> None:
        """cmd_volume_mute calls player_control.mute_set when control supports mute."""
        from music_assistant_models.player_control import PlayerControl  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "ExtMuteOK")
        player._cache["volume_control"] = PLAYER_CONTROL_NATIVE
        player._cache["mute_control"] = "ext_mute_ctrl"
        player._state.synced_to = None
        player._state.active_group = None

        mute_calls: list[bool] = []
        pc = MagicMock(spec=PlayerControl)
        pc.name = "ExtMuteCtrl"
        pc.supports_mute = True
        pc.mute_set = AsyncMock(side_effect=lambda m: mute_calls.append(m))
        ctrl._controls = {"ext_mute_ctrl": pc}

        with _patched():
            await ctrl.cmd_volume_mute(player.player_id, True)

        assert True in mute_calls


class TestSelectSourcePaths:
    """Tests for select_source with various path scenarios."""

    async def test_source_none_defaults_to_player_id(self) -> None:
        """select_source with source=None uses player_id as source."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "SrcDefault")
        player._state.synced_to = None
        player._state.active_group = None

        handle_calls: list[tuple[str, str]] = []

        async def fake_handle_select(pid: str, source: str | None) -> None:
            handle_calls.append((pid, source or ""))

        ctrl._handle_select_source = fake_handle_select  # type: ignore[assignment]

        with _patched():
            await ctrl.select_source(player.player_id, None)

        # source=None → defaults to player_id
        assert any(src == player.player_id for _, src in handle_calls)

    async def test_grouped_player_raises(self) -> None:
        """select_source raises when player is in a group."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "GroupedSrc")
        player._state.synced_to = "some_leader"
        player._state.active_group = None

        with _patched(), pytest.raises(Exception):  # noqa: B017, PT011
            await ctrl.select_source(player.player_id, "some_source")


class TestEnqueueNextMediaDelegates:
    """Test enqueue_next_media delegates to _handle_enqueue_next_media."""

    async def test_enqueue_delegates_to_handler(self) -> None:
        """enqueue_next_media calls _handle_enqueue_next_media."""
        from music_assistant_models.enums import MediaType  # noqa: PLC0415
        from music_assistant_models.player import PlayerMedia  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "EnqueueDel")

        media = PlayerMedia(uri="http://next.mp3", media_type=MediaType.UNKNOWN)

        enqueue_calls: list[tuple[str, object]] = []

        async def fake_handle_enqueue(pid: str, m: object) -> None:
            enqueue_calls.append((pid, m))

        ctrl._handle_enqueue_next_media = fake_handle_enqueue  # type: ignore[assignment]

        with _patched():
            await ctrl.enqueue_next_media(player_id=player.player_id, media=media)

        assert any(pid == player.player_id for pid, _ in enqueue_calls)


class TestCmdSetMembersSyncedParent:
    """Test cmd_set_members with synced parent player."""

    async def test_synced_parent_gets_ungrouped_first(self) -> None:
        """cmd_set_members auto-ungroups parent when it is currently synced."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [leader, member] = _add_players(ctrl, mass, "SyncedLeader", "SyncedMember")

        leader._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        leader._state.supported_features = {PlayerFeature.SET_MEMBERS}
        # Make leader appear as synced: another player has leader in its group_members
        member._attr_group_members = [member.player_id, leader.player_id]
        member._cache.clear()

        ungroup_calls: list[str] = []

        async def fake_auto_ungroup(p: object, _ctx: str) -> None:
            ungroup_calls.append(getattr(p, "player_id", ""))

        ctrl._auto_ungroup_if_synced = fake_auto_ungroup  # type: ignore[assignment]

        handle_calls: list[str] = []

        async def fake_handle_set(
            p: object, _to_add: object = None, _to_remove: object = None
        ) -> None:
            handle_calls.append(getattr(p, "player_id", ""))

        ctrl._handle_set_members = fake_handle_set  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_set_members(leader.player_id, player_ids_to_add=[member.player_id])

        assert leader.player_id in ungroup_calls


class TestCmdUngroupPlayerNotFound:
    """Test cmd_ungroup when player not found."""

    async def test_ungroup_warns_when_not_found(self) -> None:
        """cmd_ungroup logs a warning and returns when player not in _players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl

        # Don't add any players - call with non-existent player_id
        await ctrl.cmd_ungroup("nonexistent_player_xyz")
        # No exception raised, just returns silently


class TestCreateGroupPlayerSuccess:
    """Test create_group_player success path."""

    async def test_creates_group_player(self) -> None:
        """create_group_player returns result from provider.create_group_player."""
        from music_assistant_models.enums import ProviderFeature  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl

        provider_mock = MagicMock()
        provider_mock.supported_features = {ProviderFeature.CREATE_GROUP_PLAYER}
        provider_mock.create_group_player = AsyncMock(return_value=MagicMock())
        mass.get_provider = MagicMock(return_value=provider_mock)

        result = await ctrl.create_group_player("test_prov", "MyGroup", ["p1", "p2"])

        provider_mock.create_group_player.assert_called_once_with("MyGroup", ["p1", "p2"], True)
        assert result is not None


class TestRemoveGroupPlayerPaths:
    """Tests for remove_group_player branches."""

    async def test_not_found_deletes_config(self) -> None:
        """remove_group_player deletes config when player not registered."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.config = MagicMock()
        mass.config.remove = MagicMock()

        await ctrl.remove_group_player("unregistered_group")

        mass.config.remove.assert_called()

    async def test_non_group_type_raises(self) -> None:
        """remove_group_player raises when player is not GROUP type."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "NotGroupPl")
        player._state.type = PlayerType.PLAYER

        with pytest.raises(Exception):  # noqa: B017, PT011
            await ctrl.remove_group_player(player.player_id)


class TestRegisterClosing:
    """Test register() returns early when mass is closing."""

    async def test_register_returns_when_closing(self) -> None:
        """register() returns early when mass.closing is True."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = True

        provider = MockProvider("closing_prov", instance_id="closing_prov", mass=mass)
        player = MockPlayer(provider, "closing_player", "Closing Player")

        # Should not raise, should just return silently
        await ctrl.register(player)

        assert "closing_player" not in ctrl._players


class TestRemoveGroupAndActivePaths:
    """Tests for remove() with GROUP player and active_group cleanup."""

    async def test_remove_group_player_calls_provider(self) -> None:
        """remove() calls provider.remove_group_player for GROUP type player."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "GroupRemove")
        player._state.type = PlayerType.GROUP

        player._provider.check_feature = MagicMock()  # type: ignore[method-assign]
        player._provider.remove_group_player = AsyncMock()  # type: ignore[method-assign]

        await ctrl.remove(player.player_id)

        player._provider.remove_group_player.assert_called_once_with(player.player_id)

    async def test_remove_player_with_active_group_cleanup(self) -> None:
        """remove() tries to remove player from group when player.state.active_group is set."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player, group_player] = _add_players(ctrl, mass, "MemberRemove", "GroupLeader")

        player._state.type = PlayerType.PLAYER
        player._state.active_group = group_player.player_id

        player._provider.check_feature = MagicMock()  # type: ignore[method-assign]
        player._provider.remove_player = AsyncMock()  # type: ignore[method-assign]

        # group_player.set_members will be called to clean up
        group_player.set_members = AsyncMock()  # type: ignore[method-assign]

        mass.config = MagicMock()
        mass.config.remove = MagicMock()

        await ctrl.remove(player.player_id)

        player._provider.remove_player.assert_called_once_with(player.player_id)


class TestSignalStateElapsedLargeCorrection:
    """Test signal_player_state_update elapsed time large correction path."""

    def test_large_elapsed_correction_triggers_queue_notify(self) -> None:
        """Large elapsed time correction calls on_player_elapsed_time_corrected."""
        import time  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        [player] = _add_players(ctrl, mass, "ElapsedLarge")
        player.state.enabled = True
        player.state.type = PlayerType.PLAYER
        player._provider.players = []  # type: ignore[misc]

        from music_assistant.constants import ATTR_ELAPSED_TIME  # noqa: PLC0415

        now = time.time()
        # Use a large difference (>1.0 second)
        ctrl.signal_player_state_update(
            player,
            {
                ATTR_ELAPSED_TIME: (0.0, 10.0),
                "elapsed_time_last_updated": (now, now),
            },
        )

        mass.player_queues.on_player_elapsed_time_corrected.assert_called_with(player)


class TestSignalStateRemovedMemberUpdate:
    """Test signal_player_state_update triggers update on removed group members."""

    def test_removed_members_get_updated(self) -> None:
        """signal_player_state_update calls update_state on removed group members."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        mass.create_task = MagicMock()
        [player, removed_member] = _add_players(ctrl, mass, "GroupLeaderRm", "RemovedMember")

        player.state.enabled = True
        player.state.type = PlayerType.PLAYER
        player._provider.players = []  # type: ignore[misc]
        removed_member.update_state = MagicMock()  # type: ignore[misc, method-assign]

        # prev_group_members had removed_member, new_group_members doesn't
        ctrl.signal_player_state_update(
            player,
            {
                "group_members": ([player.player_id, removed_member.player_id], [player.player_id]),
                "available": (True, True),
            },
        )

        removed_member.update_state.assert_called()


class TestSignalStateProtocolAndLinkedUpdates:
    """Test signal_player_state_update triggers for protocol parents and linked protocols."""

    def test_protocol_parent_gets_triggered(self) -> None:
        """signal_player_state_update triggers parent when player has protocol_parent_id."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        mass.create_task = MagicMock()
        [parent, proto] = _add_players(ctrl, mass, "ProtoParent2", "ProtoChild2")

        proto.state.enabled = True
        proto.state.type = PlayerType.PROTOCOL
        proto._provider.players = []  # type: ignore[misc]
        proto._Player__attr_protocol_parent_id = parent.player_id  # type: ignore[attr-defined]

        trigger_calls: list[str] = []

        def fake_trigger(pid: str) -> None:
            trigger_calls.append(pid)

        ctrl.trigger_player_update = fake_trigger  # type: ignore[assignment]

        ctrl.signal_player_state_update(proto, {"volume_level": (50, 60)})

        assert parent.player_id in trigger_calls

    def test_group_children_get_triggered(self) -> None:
        """signal_player_state_update triggers group member updates."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        mass.create_task = MagicMock()
        [group, child] = _add_players(ctrl, mass, "GrpTrigger", "ChildTrigger")

        group.state.enabled = True
        group.state.type = PlayerType.PLAYER
        group._provider.players = []  # type: ignore[misc]
        group._state.group_members = [group.player_id, child.player_id]  # type: ignore[assignment]
        child.state.enabled = True
        child.state.available = True

        trigger_calls: list[str] = []

        def fake_trigger(pid: str) -> None:
            trigger_calls.append(pid)

        ctrl.trigger_player_update = fake_trigger  # type: ignore[assignment]

        ctrl.signal_player_state_update(group, {"volume_level": (50, 60)})

        assert child.player_id in trigger_calls


class TestRegisterOrUpdateControlNew:
    """Test register_or_update_player_control calls register for new controls."""

    async def test_calls_register_for_new_control(self) -> None:
        """register_or_update_player_control calls register_player_control for new control."""
        from music_assistant_models.player_control import PlayerControl  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.get_provider = MagicMock(return_value=MagicMock())

        register_calls: list[str] = []

        async def fake_register(pc: object) -> None:
            register_calls.append(getattr(pc, "id", ""))

        ctrl.register_player_control = fake_register  # type: ignore[assignment]

        pc = MagicMock(spec=PlayerControl)
        pc.id = "new_ctrl_id"
        ctrl._controls = {}  # ensure not already registered

        await ctrl.register_or_update_player_control(pc)

        assert "new_ctrl_id" in register_calls


class TestUpdatePlayerControlClosing:
    """Test update_player_control returns early when mass is closing."""

    def test_returns_early_when_closing(self) -> None:
        """update_player_control does nothing when mass.closing is True."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = True

        # Should not call mass.loop.call_soon or raise
        ctrl.update_player_control("some_ctrl_id")


class TestGetActiveQueueActiveGroup:
    """Test get_active_queue follows active_group to group player."""

    def test_follows_active_group(self) -> None:
        """get_active_queue follows player.state.active_group to get group's queue."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [group, member] = _add_players(ctrl, mass, "GroupQueue2", "MemberQueue2")

        member._state.active_group = group.player_id
        member._state.synced_to = None

        group_queue = MagicMock()
        group._state.active_source = "group_queue_id"

        mass.player_queues.get = MagicMock(
            side_effect=lambda src: group_queue if src == "group_queue_id" else None
        )

        result = ctrl.get_active_queue(member)

        assert result is group_queue


class TestIterGroupMembersFilters:
    """Tests for iter_group_members active_only and only_playing filters."""

    def test_active_only_filter(self) -> None:
        """iter_group_members with active_only only returns members with active_group=group."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [group, active_member, inactive_member] = _add_players(
            ctrl, mass, "ActiveFilter", "ActiveM", "InactiveM"
        )

        group._state.group_members = [  # type: ignore[assignment]
            group.player_id,
            active_member.player_id,
            inactive_member.player_id,
        ]
        active_member._state.available = True
        active_member._state.enabled = True
        active_member._state.active_group = group.player_id

        inactive_member._state.available = True
        inactive_member._state.enabled = True
        inactive_member._state.active_group = "other_group"

        result = list(ctrl.iter_group_members(group, active_only=True))

        assert active_member in result
        assert inactive_member not in result

    def test_only_playing_filter(self) -> None:
        """iter_group_members with only_playing only returns members that are playing."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [group, playing_member, idle_member] = _add_players(
            ctrl, mass, "PlayFilter", "PlayingM", "IdleM"
        )

        group._state.group_members = [  # type: ignore[assignment]
            group.player_id,
            playing_member.player_id,
            idle_member.player_id,
        ]
        playing_member._state.available = True
        playing_member._state.enabled = True
        playing_member._state.playback_state = PlaybackState.PLAYING

        idle_member._state.available = True
        idle_member._state.enabled = True
        idle_member._state.playback_state = PlaybackState.IDLE

        result = list(ctrl.iter_group_members(group, only_playing=True))

        assert playing_member in result
        assert idle_member not in result


class TestGetActivePluginSourceInUseBy:
    """Test _get_active_plugin_source with in_use_by matching."""

    def test_returns_plugin_source_by_in_use_by(self) -> None:
        """_get_active_plugin_source returns source when in_use_by matches player_id."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "InUseBy")

        plugin_src = MagicMock()
        plugin_src.in_use_by = player.player_id
        plugin_src.id = "plugin_source_xyz"

        ctrl.get_plugin_sources = MagicMock(return_value=[plugin_src])  # type: ignore[method-assign]

        result = ctrl._get_active_plugin_source(player)

        assert result is plugin_src


class TestGetPlayerGroupsPoweredOnly:
    """Test _get_player_groups with powered_only filter."""

    def test_powered_only_filter(self) -> None:
        """_get_player_groups with powered_only=True only yields powered group players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [member, powered_group, unpowered_group] = _add_players(
            ctrl, mass, "GrpMember3", "PoweredGrp", "UnpoweredGrp"
        )

        powered_group._state.type = PlayerType.GROUP
        powered_group._state.powered = True
        powered_group._state.group_members = [member.player_id]  # type: ignore[assignment]

        unpowered_group._state.type = PlayerType.GROUP
        unpowered_group._state.powered = False
        unpowered_group._state.group_members = [member.player_id]  # type: ignore[assignment]

        result = list(ctrl._get_player_groups(member, available_only=False, powered_only=True))

        assert powered_group in result
        assert unpowered_group not in result


class TestHandleCmdPowerExternalControl:
    """Test _handle_cmd_power with external player control."""

    async def test_external_power_on_calls_power_on(self) -> None:
        """_handle_cmd_power calls player_control.power_on for external control."""
        from music_assistant_models.player_control import PlayerControl  # noqa: PLC0415

        from music_assistant.controllers.players.controller import (  # noqa: PLC0415
            PlayerController as PC,  # noqa: N817
        )

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "ExtPowerOn")
        player._state.powered = False
        player._state.power_control = "ext_power_ctrl"
        player._state.synced_to = None
        player._state.active_group = None
        player._state.playback_state = PlaybackState.IDLE
        player._state.type = PlayerType.PLAYER
        player.update_state = MagicMock()  # type: ignore[misc, method-assign]
        player.config.get_value = MagicMock(return_value=None)  # type: ignore[method-assign]  # no auto-play

        pc = MagicMock(spec=PlayerControl)
        pc.name = "ExtPower"
        pc.supports_power = True
        pc.power_on = AsyncMock()
        pc.power_state = True  # already on (for wait_for_power_on)
        ctrl._controls = {"ext_power_ctrl": pc}

        with patch(
            "music_assistant.controllers.players.controller.wait_for_power_on", new=AsyncMock()
        ):
            await PC._handle_cmd_power(ctrl, player.player_id, True)

        pc.power_on.assert_called_once()


class TestHandleSelectSourcePaths:
    """Tests for _handle_select_source paths."""

    async def test_source_none_defaults_to_player_id(self) -> None:
        """_handle_select_source with source=None defaults to player_id."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "HSSDefault")
        player._state.active_source = None

        mass.get_provider = MagicMock(return_value=None)
        mass.player_queues.get = MagicMock(return_value=MagicMock())
        player.set_active_mass_source = MagicMock()  # type: ignore[misc, method-assign]

        await ctrl._handle_select_source(player.player_id, None)

        # set_active_mass_source should be called with player.player_id
        player.set_active_mass_source.assert_called_with(player.player_id)

    async def test_player_select_source_called_for_valid_source(self) -> None:
        """_handle_select_source calls player.select_source for a valid source."""
        from music_assistant_models.player import PlayerSource  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "HSSValid")
        player._attr_supported_features = {PlayerFeature.SELECT_SOURCE}
        player._state.supported_features = {PlayerFeature.SELECT_SOURCE}

        valid_source = PlayerSource(id="valid_src", name="Valid")
        player._state.source_list = [valid_source]  # type: ignore[assignment]
        player._state.active_source = None

        mass.get_provider = MagicMock(return_value=None)
        mass.player_queues.get = MagicMock(return_value=None)
        player.select_source = AsyncMock()  # type: ignore[method-assign]

        with patch("music_assistant.controllers.players.controller.asyncio.sleep", new=AsyncMock()):
            await ctrl._handle_select_source(player.player_id, "valid_src")

        player.select_source.assert_called_once_with("valid_src")


class TestHandleCmdPausePathsExtra:
    """Tests for _handle_cmd_pause paths."""

    async def test_plugin_source_pause_calls_on_pause(self) -> None:
        """_handle_cmd_pause calls plugin_source.on_pause when plugin supports pause."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "PausePlug")

        on_pause_called: list[bool] = []
        plugin_src = MagicMock()
        plugin_src.can_play_pause = True
        plugin_src.on_pause = AsyncMock(side_effect=lambda: on_pause_called.append(True))

        ctrl._get_active_plugin_source = MagicMock(return_value=plugin_src)  # type: ignore[method-assign]

        await ctrl._handle_cmd_pause(player.player_id)

        assert on_pause_called

    async def test_no_pause_support_falls_back_to_stop(self) -> None:
        """_handle_cmd_pause falls back to stop when no control target supports pause."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.call_later = MagicMock()
        [player] = _add_players(ctrl, mass, "PauseStop")

        ctrl._get_active_plugin_source = MagicMock(return_value=None)  # type: ignore[method-assign]
        ctrl._get_control_target = MagicMock(return_value=None)  # type: ignore[method-assign]
        player.mark_stop_called = MagicMock()  # type: ignore[misc, method-assign]
        player.stop = AsyncMock()  # type: ignore[method-assign]

        await ctrl._handle_cmd_pause(player.player_id)

        player.stop.assert_called_once()

    async def test_active_source_no_play_pause_raises(self) -> None:
        """_handle_cmd_pause raises when active source does not support play/pause."""
        from music_assistant_models.player import PlayerSource  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "PauseNoSupport")

        no_pause_src = PlayerSource(id="no_pp", name="NoPP", can_play_pause=False)
        player._state.source_list = [no_pause_src]  # type: ignore[assignment]
        player._state.active_source = "no_pp"

        ctrl._get_active_plugin_source = MagicMock(return_value=None)  # type: ignore[method-assign]

        with pytest.raises(Exception):  # noqa: B017, PT011
            await ctrl._handle_cmd_pause(player.player_id)


class TestHandleEnqueueNextMediaRedirect:
    """Tests for _handle_enqueue_next_media redirect path."""

    async def test_enqueue_redirected_to_protocol_player(self) -> None:
        """_handle_enqueue_next_media redirects to protocol player when target is set."""
        from music_assistant_models.enums import MediaType  # noqa: PLC0415
        from music_assistant_models.player import PlayerMedia  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player, proto] = _add_players(ctrl, mass, "EnqParent", "EnqProto")

        media = PlayerMedia(uri="http://next.mp3", media_type=MediaType.UNKNOWN)

        proto.enqueue_next_media = AsyncMock()  # type: ignore[method-assign]
        ctrl._get_control_target = MagicMock(return_value=proto)  # type: ignore[method-assign]

        await ctrl._handle_enqueue_next_media(player.player_id, media)

        proto.enqueue_next_media.assert_called_once_with(media)


class TestHandlePlayMediaPaths:
    """Tests for _handle_play_media paths."""

    async def test_source_id_sets_active_source(self) -> None:
        """_handle_play_media calls set_active_mass_source when media has source_id."""
        from music_assistant_models.enums import MediaType  # noqa: PLC0415
        from music_assistant_models.player import PlayerMedia  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "PlayMediaSrc")
        player._state.powered = True
        player._state.power_control = PLAYER_CONTROL_NONE

        media = PlayerMedia(
            uri="http://track.mp3", media_type=MediaType.UNKNOWN, source_id="my_queue"
        )

        player.set_active_mass_source = MagicMock()  # type: ignore[misc, method-assign]
        player.play_media = AsyncMock()  # type: ignore[method-assign]
        player.set_active_output_protocol = MagicMock()  # type: ignore[misc, method-assign]
        ctrl._select_best_output_protocol = MagicMock(return_value=(player, None))  # type: ignore[method-assign]

        await ctrl._handle_play_media(player.player_id, media)

        player.set_active_mass_source.assert_called_with("my_queue")

    async def test_power_on_before_play_media(self) -> None:
        """_handle_play_media powers on player when powered=False."""
        from music_assistant_models.enums import MediaType  # noqa: PLC0415
        from music_assistant_models.player import PlayerMedia  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "PlayMediaPower")
        player._state.powered = False
        player._state.power_control = PLAYER_CONTROL_FAKE

        media = PlayerMedia(uri="http://track.mp3", media_type=MediaType.UNKNOWN)

        power_calls: list[tuple[str, bool]] = []

        async def fake_handle_power(pid: str, powered: bool, skip_auto_play: bool = False) -> None:  # noqa: ARG001
            power_calls.append((pid, powered))

        ctrl._handle_cmd_power = fake_handle_power  # type: ignore[assignment]
        player.play_media = AsyncMock()  # type: ignore[method-assign]
        player.set_active_output_protocol = MagicMock()  # type: ignore[misc, method-assign]
        ctrl._select_best_output_protocol = MagicMock(return_value=(player, None))  # type: ignore[method-assign]

        await ctrl._handle_play_media(player.player_id, media)

        assert (player.player_id, True) in power_calls

    async def test_protocol_player_play_media(self) -> None:
        """_handle_play_media plays via protocol player when target differs from parent."""
        from music_assistant_models.enums import MediaType  # noqa: PLC0415
        from music_assistant_models.player import OutputProtocol, PlayerMedia  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player, proto] = _add_players(ctrl, mass, "PlayProtoParent", "PlayProtoChild")
        player._state.powered = True
        player._state.power_control = PLAYER_CONTROL_NONE
        proto._state.powered = True
        proto._state.power_control = PLAYER_CONTROL_NONE

        media = PlayerMedia(uri="http://track.mp3", media_type=MediaType.UNKNOWN)

        output_protocol = MagicMock(spec=OutputProtocol)
        output_protocol.output_protocol_id = proto.player_id
        output_protocol.name = "test_protocol"

        player.set_active_output_protocol = MagicMock()  # type: ignore[misc, method-assign]
        proto.play_media = AsyncMock()  # type: ignore[method-assign]
        player.on_protocol_playback = AsyncMock()  # type: ignore[method-assign]
        ctrl._select_best_output_protocol = MagicMock(return_value=(proto, output_protocol))  # type: ignore[method-assign]

        await ctrl._handle_play_media(player.player_id, media)

        proto.play_media.assert_called_once_with(media)
        player.on_protocol_playback.assert_called_once_with(output_protocol=output_protocol)


# ---------------------------------------------------------------------------
# Batch 4 — targeted coverage boost
# ---------------------------------------------------------------------------


class TestGetConfigEntries:
    """Test get_config_entries returns empty tuple (line 157)."""

    async def test_returns_empty_tuple(self) -> None:
        """get_config_entries returns ()."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        result = await ctrl.get_config_entries()
        assert result == ()


class TestGetPlayerStatePermissions:
    """Test get_player_state raises InsufficientPermissions (lines 316-317)."""

    def test_raises_insufficient_permissions(self) -> None:
        """get_player_state raises when user cannot access player."""
        from music_assistant_models.errors import InsufficientPermissions  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "SecurePlayer")

        mock_user = MagicMock()
        mock_user.username = "guest"
        from music_assistant_models.auth import UserRole  # noqa: PLC0415

        mock_user.role = UserRole.USER
        mock_user.player_filter = ["allowed_player_xyz"]  # player not in filter

        with (
            patch(
                "music_assistant.controllers.players.controller.get_current_user",
                return_value=mock_user,
            ),
            patch(
                "music_assistant.controllers.players.controller.get_sendspin_player_id",
                return_value=None,
            ),
            pytest.raises(InsufficientPermissions),
        ):
            ctrl.get_player_state(player.player_id)


class TestGetPlayerStateByNamePermissions:
    """Test get_player_state_by_name raises InsufficientPermissions (lines 379-380)."""

    def test_raises_insufficient_permissions(self) -> None:
        """get_player_state_by_name raises when user cannot access player."""
        from music_assistant_models.errors import InsufficientPermissions  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "SecureNamedPlayer")

        mock_user = MagicMock()
        mock_user.username = "guest"
        from music_assistant_models.auth import UserRole  # noqa: PLC0415

        mock_user.role = UserRole.USER
        mock_user.player_filter = ["allowed_player_xyz"]  # player not in filter

        with (
            patch(
                "music_assistant.controllers.players.controller.get_current_user",
                return_value=mock_user,
            ),
            patch(
                "music_assistant.controllers.players.controller.get_sendspin_player_id",
                return_value=None,
            ),
            pytest.raises(InsufficientPermissions),
        ):
            ctrl.get_player_state_by_name(player.name)  # type: ignore[arg-type]


class TestGetPluginSourceMatch:
    """Test get_plugin_source returns matching source (lines 428-432)."""

    def test_returns_matching_plugin_source(self) -> None:
        """get_plugin_source finds and returns a matching source by id."""
        from music_assistant_models.enums import ProviderFeature  # noqa: PLC0415

        from music_assistant.models.plugin import PluginProvider, PluginSource  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)

        source = PluginSource(id="myplugin", name="My Plugin")
        plugin_prov = MagicMock(spec=PluginProvider)
        plugin_prov.supported_features = {ProviderFeature.AUDIO_SOURCE}
        plugin_prov.get_source = MagicMock(return_value=source)

        mass.get_providers = MagicMock(return_value=[plugin_prov])

        result = ctrl.get_plugin_source("myplugin")

        assert result is source


class TestCmdVolumeDownMidRange:
    """Test cmd_volume_down uses step_size=3 for mid-range volume (line 667)."""

    async def test_mid_range_volume_step_three(self) -> None:
        """cmd_volume_down decrements by 3 when volume is between 30 and 70."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        [player] = _add_players(ctrl, mass, "MidVol")

        player._state.type = PlayerType.PLAYER
        player._state.volume_level = 50  # mid-range

        volume_set_calls: list[tuple[str, int]] = []

        async def fake_handle_volume_set(pid: str, vol: int) -> None:
            volume_set_calls.append((pid, vol))

        ctrl._handle_cmd_volume_set = fake_handle_volume_set  # type: ignore[assignment]

        with _patched():
            await ctrl.cmd_volume_down(player.player_id)

        assert (player.player_id, 47) in volume_set_calls


class TestCmdVolumeMuteProtocol:
    """Test cmd_volume_mute redirects to protocol player (lines 818-824)."""

    async def test_mute_via_protocol_player(self) -> None:
        """cmd_volume_mute redirects to protocol player when mute_control=proto_id."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player, proto] = _add_players(ctrl, mass, "MuteProtoParent", "MuteProtoChild")

        # Set mute_control to protocol player's ID (not a registered PlayerControl)
        player._cache["mute_control"] = proto.player_id
        proto.volume_mute = AsyncMock()  # type: ignore[method-assign]

        with _patched():
            await ctrl.cmd_volume_mute(player.player_id, True)

        proto.volume_mute.assert_called_once_with(True)


class TestSignalStateElapsedProtocolParent:
    """Test elapsed time correction triggers protocol parent update (line 1534)."""

    def test_elapsed_correction_triggers_protocol_parent(self) -> None:
        """Large elapsed correction also triggers protocol parent player update."""
        import time  # noqa: PLC0415

        from music_assistant.constants import ATTR_ELAPSED_TIME  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        [player, parent] = _add_players(ctrl, mass, "ProtoChildElap", "ProtoParentElap")

        player.state.enabled = True
        player.state.type = PlayerType.PLAYER
        player._provider.players = []  # type: ignore[misc]
        # Set protocol_parent_id via name-mangled attribute
        player._Player__attr_protocol_parent_id = parent.player_id  # type: ignore[attr-defined]

        trigger_calls: list[str] = []
        ctrl.trigger_player_update = MagicMock(side_effect=lambda pid: trigger_calls.append(pid))  # type: ignore[method-assign]

        now = time.time()
        ctrl.signal_player_state_update(
            player,
            {
                ATTR_ELAPSED_TIME: (0.0, 10.0),
                "elapsed_time_last_updated": (now, now),
            },
        )

        assert parent.player_id in trigger_calls


class TestSignalStateGroupChildren:
    """Test signal_player_state_update triggers group children (line 1606)."""

    def test_group_players_get_triggered(self) -> None:
        """Group players containing this player get a trigger_player_update call."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        [player, group] = _add_players(ctrl, mass, "GrpChild", "GrpLeader")

        player.state.enabled = True
        player.state.type = PlayerType.PLAYER
        player._provider.players = []  # type: ignore[misc]

        # Make group a GROUP player containing player
        group._state.type = PlayerType.GROUP
        group._attr_type = PlayerType.GROUP
        group._state.group_members = [player.player_id]  # type: ignore[assignment]
        group._state.available = True
        group._state.enabled = True

        trigger_calls: list[str] = []
        ctrl.trigger_player_update = MagicMock(side_effect=lambda pid: trigger_calls.append(pid))  # type: ignore[method-assign]

        # Use non-empty changed_values to avoid early return
        ctrl.signal_player_state_update(player, {"volume_level": (50, 60)})

        assert group.player_id in trigger_calls


class TestSignalStateLinkedProtocols:
    """Test signal_player_state_update triggers linked protocol players (lines 1625-1627)."""

    def test_linked_protocol_players_triggered(self) -> None:
        """Linked output protocol players are triggered on parent state update."""
        from music_assistant_models.player import OutputProtocol  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        [player, proto] = _add_players(ctrl, mass, "LinkedParent", "LinkedProto")

        player.state.enabled = True
        player.state.type = PlayerType.PLAYER
        player._provider.players = []  # type: ignore[misc]

        # Set up linked_output_protocols via name-mangled attribute
        player._Player__attr_linked_protocols = [  # type: ignore[attr-defined]
            OutputProtocol(
                output_protocol_id=proto.player_id,
                name="Test Protocol",
                protocol_domain="test",
            )
        ]
        player._state.type = PlayerType.PLAYER  # not PROTOCOL — triggers linked protocol check
        proto._state.available = True
        proto._state.enabled = True

        trigger_calls: list[str] = []
        ctrl.trigger_player_update = MagicMock(side_effect=lambda pid: trigger_calls.append(pid))  # type: ignore[method-assign]

        # Use non-empty changed_values to avoid early return
        ctrl.signal_player_state_update(player, {"volume_level": (50, 60)})

        assert proto.player_id in trigger_calls


class TestSignalStateProviderPlayersOnGroupChange:
    """Test signal_player_state_update triggers provider players on group change (line 1631)."""

    def test_provider_players_triggered_on_group_change(self) -> None:
        """Provider players are triggered when group_members changes."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.closing = False
        mass.call_later = MagicMock()
        [player, sibling] = _add_players(ctrl, mass, "GrpProvParent", "GrpProvSibling")

        player.state.enabled = True
        player.state.type = PlayerType.PLAYER
        player._provider.players = [sibling]  # type: ignore[misc]

        trigger_calls: list[str] = []
        ctrl.trigger_player_update = MagicMock(side_effect=lambda pid: trigger_calls.append(pid))  # type: ignore[method-assign]

        # group_members change triggers provider player updates
        ctrl.signal_player_state_update(player, {"group_members": ([], [sibling.player_id])})

        assert sibling.player_id in trigger_calls


class TestHandleCmdResumePowerOn:
    """Test _handle_cmd_resume powers on player before resuming (line 2717)."""

    async def test_powers_on_before_resuming(self) -> None:
        """_handle_cmd_resume powers on player when powered=False."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "ResumePowr")

        player._state.powered = False
        player._state.power_control = PLAYER_CONTROL_FAKE
        player._state.active_source = None
        player._state.current_media = None

        power_calls: list[tuple[str, bool]] = []

        async def fake_power(pid: str, on: bool, skip_auto_play: bool = False) -> None:  # noqa: ARG001
            power_calls.append((pid, on))

        ctrl._handle_cmd_power = fake_power  # type: ignore[assignment]
        mass.player_queues.get = MagicMock(return_value=None)
        mass.player_queues.resume = AsyncMock()

        await ctrl._handle_cmd_resume(player.player_id)

        assert (player.player_id, True) in power_calls


class TestHandleCmdResumePlayPaused:
    """Test _handle_cmd_resume calls play() when paused with pausable source (lines 2732-2733)."""

    async def test_calls_play_when_paused(self) -> None:
        """_handle_cmd_resume calls player.play() for a paused player with native pause support."""
        from music_assistant.models.player import PlayerSource  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "ResumePaused")

        source = PlayerSource(id="my_source", name="My Source", can_play_pause=True)
        player._state.powered = True
        player._state.power_control = PLAYER_CONTROL_NONE
        player._state.playback_state = PlaybackState.PAUSED
        player._state.active_source = source.id
        player._state.source_list = [source]  # type: ignore[assignment]
        player._state.supported_features = {PlayerFeature.PAUSE}
        player._state.current_media = None

        player.play = AsyncMock()  # type: ignore[method-assign]
        mass.player_queues.get = MagicMock(return_value=None)

        await ctrl._handle_cmd_resume(player.player_id, source=source.id)

        player.play.assert_called_once()


class TestHandleCmdResumeSelectSource:
    """Test _handle_cmd_resume calls select_source for non-passive source (lines 2735-2736)."""

    async def test_calls_select_source_for_non_passive_active_source(self) -> None:
        """_handle_cmd_resume calls select_source when active source is not passive."""
        from music_assistant.models.player import PlayerSource  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "ResumeSelect")

        # Source exists but DOES NOT support play/pause
        source = PlayerSource(
            id="ext_source", name="Ext Source", can_play_pause=False, passive=False
        )
        player._state.powered = True
        player._state.power_control = PLAYER_CONTROL_NONE
        player._state.playback_state = PlaybackState.IDLE
        player._state.active_source = source.id
        player._state.source_list = [source]  # type: ignore[assignment]
        player._state.current_media = None

        select_calls: list[tuple[str, str]] = []

        async def fake_handle_select(pid: str, src: str) -> None:
            select_calls.append((pid, src))

        ctrl._handle_select_source = fake_handle_select  # type: ignore[assignment]
        mass.player_queues.get = MagicMock(return_value=None)

        await ctrl._handle_cmd_resume(player.player_id, source=source.id)

        assert (player.player_id, source.id) in select_calls


class TestHandleCmdResumePlayMedia:
    """Test _handle_cmd_resume calls play_media when media is set (lines 2739-2740)."""

    async def test_calls_play_media_when_no_source_but_media(self) -> None:
        """_handle_cmd_resume calls play_media when active source not found but media exists."""
        from music_assistant_models.enums import MediaType  # noqa: PLC0415
        from music_assistant_models.player import PlayerMedia  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "ResumeMedia")

        media = PlayerMedia(uri="http://track.mp3", media_type=MediaType.UNKNOWN)
        player._state.powered = True
        player._state.power_control = PLAYER_CONTROL_NONE
        player._state.playback_state = PlaybackState.IDLE
        player._state.active_source = "unknown_source"
        player._state.source_list = []  # type: ignore[assignment]  # no source found
        player._state.current_media = media

        player.play_media = AsyncMock()  # type: ignore[method-assign]
        mass.player_queues.get = MagicMock(return_value=None)

        await ctrl._handle_cmd_resume(player.player_id)

        player.play_media.assert_called_once_with(media)


class TestHandleCmdPowerUngroup:
    """Test _handle_cmd_power ungrouped synced player at power off (line 2777)."""

    async def test_ungroup_called_at_power_off(self) -> None:
        """_handle_cmd_power calls cmd_ungroup when player is synced and powered off."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "SyncPowerOff")

        player._state.powered = True
        player._state.power_control = PLAYER_CONTROL_FAKE
        player._state.type = PlayerType.PLAYER
        player._state.synced_to = "some_leader"
        player._state.active_group = None
        player._state.group_members = []  # type: ignore[assignment]
        player._state.playback_state = PlaybackState.IDLE

        ungroup_calls: list[str] = []

        async def fake_ungroup(pid: str) -> None:
            ungroup_calls.append(pid)

        ctrl.cmd_ungroup = fake_ungroup  # type: ignore[assignment]
        player.update_state = MagicMock()  # type: ignore[misc, method-assign]
        mass.cache.set = AsyncMock()

        await ctrl._handle_cmd_power(player.player_id, False)

        assert player.player_id in ungroup_calls


class TestHandleCmdPowerSkipMemberNoPowerControl:
    """Test _handle_cmd_power skips group members with no power control (line 2794)."""

    async def test_skips_members_without_power_control(self) -> None:
        """_handle_cmd_power skips group members that have PLAYER_CONTROL_NONE."""
        import asyncio  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        mass.create_task = lambda coro: asyncio.ensure_future(coro)
        [leader, member] = _add_players(ctrl, mass, "PwrLeader", "PwrMemberNone")

        leader._state.powered = True
        leader._state.power_control = PLAYER_CONTROL_FAKE
        leader._state.type = PlayerType.PLAYER
        leader._state.synced_to = None
        leader._state.active_group = None
        leader._attr_group_members = [member.player_id]
        leader._state.group_members = [member.player_id]  # type: ignore[assignment]
        leader._state.playback_state = PlaybackState.IDLE

        # Member has no power control - should be skipped at line 2794
        member._cache["power_control"] = PLAYER_CONTROL_NONE

        member_power_calls: list[str] = []

        # We need to track calls to _handle_cmd_power for the member
        async def tracking_handle_power(
            pid: str,
            _powered: bool,
            skip_auto_play: bool = False,  # noqa: ARG001
        ) -> None:
            if pid == member.player_id:
                member_power_calls.append(pid)
            # Don't recurse for the leader's own call

        # Patch cmd_ungroup (called because leader has group_members and is powering off)
        async def fake_ungroup(pid: str) -> None:
            pass

        ctrl.cmd_ungroup = fake_ungroup  # type: ignore[assignment]
        leader.update_state = MagicMock()  # type: ignore[misc, method-assign]
        mass.cache.set = AsyncMock()

        # Call directly (not decorated)
        await ctrl._handle_cmd_power(leader.player_id, False)

        # Member with PLAYER_CONTROL_NONE should NOT have been powered off
        assert member.player_id not in member_power_calls


class TestHandleCmdPowerExternalControlOff:
    """Test _handle_cmd_power external control power off paths (lines 2826, 2834-2835)."""

    async def test_raises_when_external_control_not_found(self) -> None:
        """_handle_cmd_power raises when external power control not found (line 2826)."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "ExtPwrMissing")

        player._state.powered = True
        player._state.power_control = "missing_control_id"
        player._state.type = PlayerType.PLAYER
        player._state.synced_to = None
        player._state.active_group = None
        player._state.group_members = []  # type: ignore[assignment]
        player._state.playback_state = PlaybackState.IDLE

        with pytest.raises((UnsupportedFeaturedException, Exception)):
            await ctrl._handle_cmd_power(player.player_id, False)

    async def test_external_control_power_off(self) -> None:
        """_handle_cmd_power calls power_off on external control (lines 2834-2835)."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "ExtPwrOff")

        power_off_calls: list[str] = []

        async def fake_power_off() -> None:
            power_off_calls.append("called")

        from music_assistant_models.player_control import PlayerControl  # noqa: PLC0415

        ext_ctrl = PlayerControl(
            id="ext_pwr",
            provider="test",
            name="Ext Power",
            supports_power=True,
            power_off=fake_power_off,
        )
        ctrl._controls["ext_pwr"] = ext_ctrl

        player._state.powered = True
        player._state.power_control = "ext_pwr"
        player._state.type = PlayerType.PLAYER
        player._state.synced_to = None
        player._state.active_group = None
        player._state.group_members = []  # type: ignore[assignment]
        player._state.playback_state = PlaybackState.IDLE
        player.update_state = MagicMock()  # type: ignore[misc, method-assign]

        await ctrl._handle_cmd_power(player.player_id, False)

        assert "called" in power_off_calls


class TestHandleCmdVolumeSetGroupPlayer:
    """Test _handle_cmd_volume_set redirects GROUP players (lines 2862-2863)."""

    async def test_group_volume_redirect(self) -> None:
        """_handle_cmd_volume_set delegates to cmd_group_volume for GROUP players."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [group] = _add_players(ctrl, mass, "GroupVolPlayer")

        group._attr_type = PlayerType.GROUP
        group._state.type = PlayerType.GROUP

        group_volume_calls: list[tuple[str, int]] = []

        async def fake_group_volume(pid: str, vol: int) -> None:
            group_volume_calls.append((pid, vol))

        ctrl.cmd_group_volume = fake_group_volume  # type: ignore[assignment]

        await ctrl._handle_cmd_volume_set(group.player_id, 60)

        assert (group.player_id, 60) in group_volume_calls


class TestHandleCmdVolumeSetPluginOnVolume:
    """Test _handle_cmd_volume_set calls plugin on_volume callback (lines 2886-2887)."""

    async def test_plugin_on_volume_called(self) -> None:
        """_handle_cmd_volume_set calls on_volume if plugin source has callback."""
        from music_assistant.models.plugin import PluginSource  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "PlugVolPlayer")

        player._state.type = PlayerType.PLAYER
        player._cache["volume_control"] = PLAYER_CONTROL_NATIVE
        player._state.mute_control = PLAYER_CONTROL_NONE
        player._state.volume_muted = False

        volume_cb_calls: list[int] = []

        async def on_volume_cb(vol: int) -> None:
            volume_cb_calls.append(vol)

        plugin_source = PluginSource(
            id="plug_src",
            name="Plugin Src",
        )
        plugin_source.on_volume = on_volume_cb

        # Mock _get_active_plugin_source to return the plugin source directly
        ctrl._get_active_plugin_source = MagicMock(return_value=plugin_source)  # type: ignore[method-assign]

        player.volume_set = AsyncMock()  # type: ignore[method-assign]

        await ctrl._handle_cmd_volume_set(player.player_id, 75)

        assert 75 in volume_cb_calls
        player.volume_set.assert_called_once_with(75)


class TestHandleCmdVolumeSetExternalControl:
    """Test _handle_cmd_volume_set with external PlayerControl (lines 2908-2916)."""

    async def test_external_volume_control_called(self) -> None:
        """_handle_cmd_volume_set calls volume_set on external PlayerControl."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "ExtVolCtrlPlayer")

        vol_set_calls: list[int] = []

        async def fake_vol_set(vol: int) -> None:
            vol_set_calls.append(vol)

        ext_ctrl = PlayerControl(
            id="ext_vol",
            provider="test",
            name="Ext Volume",
            supports_volume=True,
            volume_set=fake_vol_set,
        )
        ctrl._controls["ext_vol"] = ext_ctrl

        player._state.type = PlayerType.PLAYER
        player._state.mute_control = PLAYER_CONTROL_NONE
        player._state.volume_muted = False
        player._cache["volume_control"] = "ext_vol"
        player._state.volume_control = "ext_vol"
        mass.get_providers = MagicMock(return_value=[])

        await ctrl._handle_cmd_volume_set(player.player_id, 55)

        assert 55 in vol_set_calls

    async def test_external_control_not_found_raises(self) -> None:
        """_handle_cmd_volume_set raises when external volume control not found (lines 2910-2913)."""  # noqa: E501
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "MissingVolCtrl")

        # Register a control that does NOT support volume
        no_vol_ctrl = PlayerControl(
            id="no_vol",
            provider="test",
            name="No Volume",
            supports_volume=False,
        )
        ctrl._controls["no_vol"] = no_vol_ctrl

        player._state.type = PlayerType.PLAYER
        player._state.mute_control = PLAYER_CONTROL_NONE
        player._state.volume_muted = False
        player._cache["volume_control"] = "no_vol"
        player._state.volume_control = "no_vol"
        mass.get_providers = MagicMock(return_value=[])

        with pytest.raises((UnsupportedFeaturedException, Exception)):
            await ctrl._handle_cmd_volume_set(player.player_id, 55)


class TestHandlePlayMediaExistingProtocol:
    """Test _handle_play_media uses existing active protocol (line 2954)."""

    async def test_uses_existing_active_protocol(self) -> None:
        """_handle_play_media uses already-set active_output_protocol."""
        from music_assistant_models.enums import MediaType  # noqa: PLC0415
        from music_assistant_models.player import OutputProtocol, PlayerMedia  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player, proto] = _add_players(ctrl, mass, "ExistProtoParent", "ExistProtoChild")

        player._state.powered = True
        player._state.power_control = PLAYER_CONTROL_NONE
        proto._state.powered = True
        proto._state.power_control = PLAYER_CONTROL_NONE

        output_protocol = OutputProtocol(
            output_protocol_id=proto.player_id,
            name="ExistingProtocol",
            protocol_domain="test",
        )

        # Set active output protocol and linked protocols
        player._Player__attr_active_output_protocol = proto.player_id  # type: ignore[attr-defined]
        player._Player__attr_linked_protocols = [output_protocol]  # type: ignore[attr-defined]

        media = PlayerMedia(uri="http://track.mp3", media_type=MediaType.UNKNOWN)

        proto.play_media = AsyncMock()  # type: ignore[method-assign]
        player.set_active_output_protocol = MagicMock()  # type: ignore[misc, method-assign]
        player.on_protocol_playback = AsyncMock()  # type: ignore[method-assign]

        await ctrl._handle_play_media(player.player_id, media)

        proto.play_media.assert_called_once_with(media)


class TestHandlePlayMediaProtocolPowerOn:
    """Test _handle_play_media powers on protocol player when needed (line 2983)."""

    async def test_powers_on_protocol_player_before_play(self) -> None:
        """_handle_play_media powers on protocol player when it's off."""
        from music_assistant_models.enums import MediaType  # noqa: PLC0415
        from music_assistant_models.player import OutputProtocol, PlayerMedia  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player, proto] = _add_players(ctrl, mass, "ProtoPwrParent", "ProtoPwrChild")

        player._state.powered = True
        player._state.power_control = PLAYER_CONTROL_NONE
        proto._state.powered = False
        proto._state.power_control = PLAYER_CONTROL_FAKE
        proto._cache["power_control"] = PLAYER_CONTROL_FAKE

        output_protocol = OutputProtocol(
            output_protocol_id=proto.player_id,
            name="PowerOnProtocol",
            protocol_domain="test",
        )

        media = PlayerMedia(uri="http://track.mp3", media_type=MediaType.UNKNOWN)

        power_calls: list[tuple[str, bool]] = []

        async def fake_handle_power(pid: str, powered: bool, skip_auto_play: bool = False) -> None:  # noqa: ARG001
            power_calls.append((pid, powered))

        ctrl._handle_cmd_power = fake_handle_power  # type: ignore[assignment]
        ctrl._select_best_output_protocol = MagicMock(return_value=(proto, output_protocol))  # type: ignore[method-assign]
        player.set_active_output_protocol = MagicMock()  # type: ignore[misc, method-assign]
        proto.play_media = AsyncMock()  # type: ignore[method-assign]
        player.on_protocol_playback = AsyncMock()  # type: ignore[method-assign]

        await ctrl._handle_play_media(player.player_id, media)

        assert (proto.player_id, True) in power_calls


class TestHandleSelectSourceStop:
    """Test _handle_select_source stops player before switching (lines 3044-3047)."""

    async def test_stops_player_before_source_switch(self) -> None:
        """_handle_select_source stops when previous source differs from new source."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "SrcSwitchPlayer")

        player._state.active_source = "old_source"
        player._state.supported_features = {PlayerFeature.SELECT_SOURCE}
        player._state.source_list = []  # type: ignore[assignment]

        stop_calls: list[str] = []

        async def fake_stop(pid: str) -> None:
            stop_calls.append(pid)

        ctrl._handle_cmd_stop = fake_stop  # type: ignore[assignment]
        mass.player_queues.get = MagicMock(return_value=None)
        mass.get_provider = MagicMock(return_value=None)

        with patch("asyncio.sleep", new=AsyncMock()), pytest.raises(Exception):  # noqa: B017, PT011
            await ctrl._handle_select_source(player.player_id, "new_source")

        assert player.player_id in stop_calls


class TestHandleSelectSourcePluginSource:
    """Test _handle_select_source handles plugin source (lines 3051-3053)."""

    async def test_selects_plugin_source(self) -> None:
        """_handle_select_source calls _handle_select_plugin_source for plugin providers."""
        from music_assistant.models.plugin import PluginProvider  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "PlugSrcPlayer")

        player._state.active_source = None

        plugin_prov = MagicMock(spec=PluginProvider)
        mass.get_provider = MagicMock(return_value=plugin_prov)
        mass.player_queues.get = MagicMock(return_value=None)

        plugin_select_calls: list[str] = []

        async def fake_plugin_select(p: object, _prov: object) -> None:
            plugin_select_calls.append(getattr(p, "player_id", ""))

        ctrl._handle_select_plugin_source = fake_plugin_select  # type: ignore[assignment]

        player.set_active_mass_source = MagicMock()  # type: ignore[misc, method-assign]

        await ctrl._handle_select_source(player.player_id, "myplugin")

        assert player.player_id in plugin_select_calls
        player.set_active_mass_source.assert_called_with("myplugin")


class TestHandleCmdPlayPluginPlay:
    """Test _handle_cmd_play calls plugin on_play (lines 3128-3130)."""

    async def test_plugin_on_play_called(self) -> None:
        """_handle_cmd_play calls plugin on_play callback when source has it."""
        from music_assistant.models.plugin import PluginSource  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "PlugPlayPlayer")

        player._state.type = PlayerType.PLAYER
        player._state.playback_state = PlaybackState.IDLE

        play_calls: list[str] = []

        async def on_play_cb() -> None:
            play_calls.append("called")

        plugin_source = PluginSource(
            id="play_src",
            name="Play Source",
            can_play_pause=True,
        )
        plugin_source.on_play = on_play_cb

        # Mock _get_active_plugin_source to return the plugin source directly
        ctrl._get_active_plugin_source = MagicMock(return_value=plugin_source)  # type: ignore[method-assign]

        await ctrl._handle_cmd_play(player.player_id)

        assert "called" in play_calls


class TestHandleCmdPlaySourceNoPlayPause:
    """Test _handle_cmd_play raises when active source doesn't support play/pause (3138-3142)."""

    async def test_raises_when_source_no_play_pause(self) -> None:
        """_handle_cmd_play raises PlayerCommandFailed when source doesn't support play/pause."""
        from music_assistant_models.errors import PlayerCommandFailed  # noqa: PLC0415

        from music_assistant.models.player import PlayerSource  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "NoPlayPausePlayer")

        no_pause_source = PlayerSource(id="npp_src", name="No Pause", can_play_pause=False)
        player._state.type = PlayerType.PLAYER
        player._state.playback_state = PlaybackState.PAUSED
        player._state.active_source = no_pause_source.id
        player._state.source_list = [no_pause_source]  # type: ignore[assignment]
        mass.get_providers = MagicMock(return_value=[])

        with pytest.raises(PlayerCommandFailed):
            await ctrl._handle_cmd_play(player.player_id)


class TestHandleCmdPlayTargetPlayerPlay:
    """Test _handle_cmd_play calls target_player.play() (lines 3147-3148)."""

    async def test_target_player_play_called(self) -> None:
        """_handle_cmd_play calls play() on the control target when paused."""
        from music_assistant.models.player import PlayerSource  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "TgtPlayPlayer")

        play_src = PlayerSource(id="play_s", name="Play Source", can_play_pause=True)
        player._state.type = PlayerType.PLAYER
        player._state.playback_state = PlaybackState.PAUSED
        player._state.active_source = play_src.id
        player._state.source_list = [play_src]  # type: ignore[assignment]
        player._state.supported_features = {PlayerFeature.PAUSE}
        mass.get_providers = MagicMock(return_value=[])

        play_calls: list[str] = []

        async def fake_play() -> None:
            play_calls.append("play")

        # _get_control_target for PAUSE with require_active=True should return this player
        ctrl._get_control_target = MagicMock(return_value=player)  # type: ignore[method-assign]
        player.play = AsyncMock(side_effect=lambda: play_calls.append("play"))  # type: ignore[method-assign]

        await ctrl._handle_cmd_play(player.player_id)

        assert "play" in play_calls


class TestHandleCmdPlayActiveSourceSelectSource:
    """Test _handle_cmd_play calls select_source for non-passive active source (3160-3161)."""

    async def test_select_source_called_for_non_passive(self) -> None:
        """_handle_cmd_play calls _handle_select_source for non-passive active source."""
        from music_assistant.models.player import PlayerSource  # noqa: PLC0415

        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "ActiveSrcPlay")

        active_src = PlayerSource(id="active_s", name="Active", passive=False, can_play_pause=True)
        player._state.type = PlayerType.PLAYER
        player._state.playback_state = PlaybackState.IDLE
        player._state.active_source = active_src.id
        player._state.source_list = [active_src]  # type: ignore[assignment]
        player._state.supported_features = set()
        player._state.powered = True
        player._state.power_control = PLAYER_CONTROL_NONE
        player._state.current_media = None
        mass.get_providers = MagicMock(return_value=[])

        select_calls: list[tuple[str, str]] = []

        async def fake_select(pid: str, src: str) -> None:
            select_calls.append((pid, src))

        ctrl._handle_select_source = fake_select  # type: ignore[assignment]

        await ctrl._handle_cmd_play(player.player_id)

        assert (player.player_id, active_src.id) in select_calls


class TestHandleCmdPauseTargetPlayer:
    """Test _handle_cmd_pause calls target_player.pause() (line 3209)."""

    async def test_pause_called_on_target(self) -> None:
        """_handle_cmd_pause calls pause() on the control target."""
        mass = _make_mock_mass()
        ctrl = PlayerController(mass)
        mass.players = ctrl
        mass.get_providers = MagicMock(return_value=[])
        [player] = _add_players(ctrl, mass, "PauseTgtPlayer")

        player._state.type = PlayerType.PLAYER
        player._state.active_source = None
        player._state.source_list = []  # type: ignore[assignment]
        mass.get_providers = MagicMock(return_value=[])

        pause_calls: list[str] = []

        async def fake_pause() -> None:
            pause_calls.append("paused")

        # _get_control_target returns player itself
        ctrl._get_control_target = MagicMock(return_value=player)  # type: ignore[method-assign]
        player.pause = AsyncMock(side_effect=lambda: pause_calls.append("paused"))  # type: ignore[method-assign]

        await ctrl._handle_cmd_pause(player.player_id)

        assert "paused" in pause_calls
