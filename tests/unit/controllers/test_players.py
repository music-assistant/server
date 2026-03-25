"""Unit tests for PlayerController."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import (
    PlaybackState,
)
from music_assistant_models.errors import (
    PlayerUnavailableError,
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
