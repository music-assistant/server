"""
Tests for the volume and mute commands of the players controller.

Every player of these tests has a native protocol with an id of its own (``proto_<player id>``),
which is what receives the device commands and reports the device state.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import NamedTuple, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import ConfigEntry, CoreConfig
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import ConfigEntryType, PlayerFeature, PlayerType, ProtocolRole
from music_assistant_models.errors import UnsupportedFeaturedException
from music_assistant_models.player_control import PlayerControl

from music_assistant.constants import (
    ATTR_FAKE_MUTE,
    ATTR_MUTE_LOCK,
    ATTR_PREVIOUS_VOLUME,
    CONF_MAX_VOLUME,
    CONF_MIN_VOLUME,
    CONF_MUTE_CONTROL,
    CONF_VOLUME_CONTROL,
    CONF_VOLUME_STEP,
)
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players import volume as players_volume
from music_assistant.models.player import Player
from tests.common import MockProtocol, MockProvider, attach_mock, use_real_create_task


def _proto(player_id: str) -> str:
    """Return the id of the native protocol of a player in these tests."""
    return f"proto_{player_id}"


def _attach(
    provider: MockProvider,
    player_id: str,
    name: str | None = None,
    *,
    player_type: PlayerType = PlayerType.PLAYER,
) -> tuple[Player, MockProtocol]:
    """
    Return a player with the given id and its native protocol, which has an id of its own.

    :param provider: The (mock) provider of the protocol; its mass holds the controller.
    :param player_id: Id of the player.
    :param name: The name the device reports.
    :param player_type: The type of player the protocol reports.
    """
    return attach_mock(
        provider,
        _proto(player_id),
        name,
        # a group is a virtual player
        role=ProtocolRole.VIRTUAL if player_type == PlayerType.GROUP else ProtocolRole.NATIVE,
        player_id=player_id,
        player_type=player_type,
    )


def _own(player: Player) -> MockProtocol:
    """Return the native protocol of a player."""
    return cast("MockProtocol", player.own_protocol)


def _controller(mock_mass: MagicMock) -> PlayerController:
    """Return a players controller that is the one of the mocked mass."""
    controller = PlayerController(mock_mass)
    mock_mass.players = controller
    return controller


def _refresh(*players: Player, force: bool = True) -> None:
    """Recalculate the state of the given players, in order, without publishing it."""
    for player in players:
        player._cache.clear()
        for protocol in player.attached_protocols.values():
            protocol._cache.clear()
        player.update_state(signal_event=False, force_update=force)


def _report_volume_set(protocol: MockProtocol) -> AsyncMock:
    """Let a protocol report back the volume it is set to, like a real device; return it."""
    volume_set = AsyncMock(
        side_effect=lambda volume: setattr(protocol, "_attr_volume_level", volume)
    )
    protocol.volume_set = volume_set  # type: ignore[method-assign]
    return volume_set


def _player_config_stub(
    values: dict[str, object] | None = None,
    *,
    min_volume: int = 0,
    max_volume: int = 100,
) -> Callable[..., object]:
    """
    Build a ``get_player_value`` side effect.

    :param values: Extra config keys to answer, e.g. ``{CONF_MUTE_CONTROL: PLAYER_CONTROL_FAKE}``.
    :param min_volume: Value returned for the ``min_volume`` key.
    :param max_volume: Value returned for the ``max_volume`` key.
    """
    config: dict[str, object] = {
        CONF_MIN_VOLUME: min_volume,
        CONF_MAX_VOLUME: max_volume,
        **(values or {}),
    }

    def _conf(_player_id: str, key: str, default: object = None) -> object:
        if key in config:
            return config[key]
        return default

    return _conf


def _volume_step_config(step: int | None) -> CoreConfig:
    """
    Build a "players" CoreConfig carrying the given ``volume_step`` value.

    Pass ``None`` to leave the entry unresolved and genuinely exercise the default
    (``CoreConfig.get_value`` returns the entry's ``value`` verbatim, not its
    ``default_value``, so a real "not configured" state is ``value=None``).
    """
    return CoreConfig(
        domain="players",
        values={
            CONF_VOLUME_STEP: ConfigEntry(
                key=CONF_VOLUME_STEP,
                type=ConfigEntryType.INTEGER,
                default_value=0,
                value=step,
            )
        },
    )


@pytest.fixture
def mock_mass() -> Iterator[MagicMock]:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    # kept so teardown still sees its calls when a test swaps mass.create_task
    create_task = mass.create_task = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_player_value = MagicMock(side_effect=_player_config_stub())
    # Return "GLOBAL" for log level config (standard default)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    mass.player_queues.get = MagicMock(return_value=None)
    yield mass
    # a mocked create_task never runs what it is handed, so close what no test started
    for scheduled in create_task.call_args_list:
        target = scheduled.args[0] if scheduled.args else None
        if inspect.iscoroutine(target) and inspect.getcoroutinestate(target) == "CORO_CREATED":
            target.close()


@pytest.fixture
def controller(mock_mass: MagicMock) -> PlayerController:
    """Create a PlayerController instance."""
    return _controller(mock_mass)


def _synced_pair(
    mock_mass: MagicMock,
    *,
    volumes: dict[str, int] | None = None,
    features: set[PlayerFeature] | None = None,
) -> tuple[PlayerController, dict[str, Player]]:
    """
    Build a leader synced to one member, whose devices report back the volume they are set to.

    :param volumes: Volume level per player, defaults to 50 for both.
    :param features: The features of both devices, when other than the default.
    """
    controller = _controller(mock_mass)
    provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
    players: dict[str, Player] = {}
    for player_id in ("leader", "member"):
        player, protocol = _attach(provider, player_id, player_id.title())
        if features is not None:
            protocol._attr_supported_features = set(features)
        protocol._attr_volume_level = (volumes or {}).get(player_id, 50)
        _report_volume_set(protocol)
        players[player_id] = player
    _own(players["leader"])._attr_group_members = [_proto("member")]
    _refresh(*players.values(), force=False)
    # a second, forced pass so the leader derives its group volume from the member
    _refresh(*players.values())
    return controller, players


class TestVolumeScalingOnRedirect:
    """min/max volume scaling must survive a redirect to an audio protocol or external control."""

    @staticmethod
    def _player(
        mock_mass: MagicMock, volume_control: str
    ) -> tuple[PlayerController, Player, MockProtocol]:
        """
        Build a player with an audio protocol, whose volume goes through the given control.

        :return: The controller, the player and its audio protocol.
        """
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        audio_provider = MockProvider("sendspin", instance_id="sendspin", mass=mock_mass)
        player, _ = _attach(provider, "user_player", "User Player")
        _, audio = attach_mock(
            audio_provider, "audio_user", "User Player", role=ProtocolRole.AUDIO, player=player
        )
        audio.volume_set = AsyncMock()  # type: ignore[method-assign]
        config_stub = mock_mass.config.get_player_value.side_effect

        def _conf(player_id: str, key: str, default: object = None) -> object:
            if key == CONF_VOLUME_CONTROL:
                return volume_control
            return config_stub(player_id, key, default)

        mock_mass.config.get_player_value = MagicMock(side_effect=_conf)
        _refresh(player)
        return controller, player, audio

    @pytest.mark.asyncio
    async def test_protocol_redirect_forwards_scaled_volume(self, mock_mass: MagicMock) -> None:
        """A volume command redirected to an audio protocol honors the user-facing max_volume."""

        def _conf(player_id: str, key: str, default: object = None) -> object:
            if key == "min_volume":
                return 0
            if key == "max_volume":
                # the player caps at 50; a lookup under any other id finds no limits
                return 50 if player_id == "user_player" else 100
            return default

        mock_mass.config.get_player_value = MagicMock(side_effect=_conf)
        controller, player, audio = self._player(mock_mass, "audio_user")
        assert player.volume_control == "audio_user"

        with patch.object(controller, "_get_active_audio_source", return_value=None):
            await controller._handle_cmd_volume_set("user_player", 100)

        # logical 100 with a max_volume of 50 must reach the audio protocol as 50, not 100
        cast("AsyncMock", audio.volume_set).assert_awaited_once_with(50)

    @pytest.mark.asyncio
    async def test_protocol_redirect_applies_the_limits_only_once(
        self, mock_mass: MagicMock
    ) -> None:
        """A limit that answers for any id does not scale the command a second time."""

        def _conf(_player_id: str, key: str, default: object = None) -> object:
            if key == "min_volume":
                return 0
            if key == "max_volume":
                # whatever id is asked for: only the addressed player's limit may apply
                return 50
            return default

        mock_mass.config.get_player_value = MagicMock(side_effect=_conf)
        controller, _player, audio = self._player(mock_mass, "audio_user")

        with patch.object(controller, "_get_active_audio_source", return_value=None):
            await controller._handle_cmd_volume_set("user_player", 100)

        cast("AsyncMock", audio.volume_set).assert_awaited_once_with(50)

    @pytest.mark.asyncio
    async def test_external_control_redirect_forwards_scaled_volume(
        self, mock_mass: MagicMock
    ) -> None:
        """A volume command redirected to an external control honors the user-facing max_volume."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub(max_volume=50)
        )
        volume_set = AsyncMock()
        control = PlayerControl(
            id="ext_control",
            provider="test",
            name="External Amp",
            supports_volume=True,
            volume_set=volume_set,
        )
        controller, player, audio = self._player(mock_mass, "ext_control")
        controller._controls = {"ext_control": control}
        _refresh(player)

        with patch.object(controller, "_get_active_audio_source", return_value=None):
            await controller._handle_cmd_volume_set("user_player", 100)

        volume_set.assert_awaited_once_with(50)
        cast("AsyncMock", audio.volume_set).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_external_control_without_volume_support_raises(
        self, mock_mass: MagicMock
    ) -> None:
        """A volume command redirected to a control lacking volume support is rejected."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub(max_volume=50)
        )
        volume_set = AsyncMock()
        control = PlayerControl(
            id="ext_control",
            provider="test",
            name="External Amp",
            supports_volume=False,
            volume_set=volume_set,
        )
        controller, player, _audio = self._player(mock_mass, "ext_control")
        controller._controls = {"ext_control": control}
        _refresh(player)

        with (
            patch.object(controller, "_get_active_audio_source", return_value=None),
            pytest.raises(UnsupportedFeaturedException),
        ):
            await controller._handle_cmd_volume_set("user_player", 100)

        volume_set.assert_not_awaited()


class TestEnforceVolumeLimits:
    """External volume changes outside the min/max range must be corrected."""

    @staticmethod
    def _set_limits(mock_mass: MagicMock, min_volume: int, max_volume: int) -> None:
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub(min_volume=min_volume, max_volume=max_volume)
        )

    @staticmethod
    def _player(logical_volume: int | None) -> Player:
        return cast(
            "Player",
            SimpleNamespace(
                player_id="user_player",
                state=SimpleNamespace(volume_level=logical_volume),
            ),
        )

    def test_out_of_range_volume_is_corrected(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A device volume above max_volume (logical > 100) is clamped back to logical 100."""
        self._set_limits(mock_mass, 0, 80)
        # device volume 100 with max 80 resolves to logical 125
        player = self._player(125)
        with patch.object(controller, "_handle_cmd_volume_set", MagicMock()) as cmd:
            controller._enforce_volume_limits(player)
        cmd.assert_called_once_with("user_player", 100)
        mock_mass.create_task.assert_called_once()

    def test_below_min_volume_is_corrected(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A device volume below min_volume (logical < 0) is clamped back to logical 0."""
        self._set_limits(mock_mass, 20, 100)
        # device volume 10 with min 20 resolves to a negative logical volume
        player = self._player(-13)
        with patch.object(controller, "_handle_cmd_volume_set", MagicMock()) as cmd:
            controller._enforce_volume_limits(player)
        cmd.assert_called_once_with("user_player", 0)

    def test_in_range_volume_is_untouched(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A logical volume within 0-100 needs no correction."""
        self._set_limits(mock_mass, 0, 80)
        player = self._player(100)
        with patch.object(controller, "_handle_cmd_volume_set", MagicMock()) as cmd:
            controller._enforce_volume_limits(player)
        cmd.assert_not_called()

    def test_no_limits_configured_is_noop(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Default 0-100 limits skip enforcement entirely."""
        self._set_limits(mock_mass, 0, 100)
        player = self._player(100)
        with patch.object(controller, "_handle_cmd_volume_set", MagicMock()) as cmd:
            controller._enforce_volume_limits(player)
        cmd.assert_not_called()

    def test_unknown_volume_is_noop(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A player without a resolved volume level is left alone."""
        self._set_limits(mock_mass, 0, 80)
        player = self._player(None)
        with patch.object(controller, "_handle_cmd_volume_set", MagicMock()) as cmd:
            controller._enforce_volume_limits(player)
        cmd.assert_not_called()


class TestFakeMuteControl:
    """Fake mute must report the muted state and restore the volume on unmute."""

    def _make_player(
        self, mock_mass: MagicMock, volume_level: int | None = 40
    ) -> tuple[PlayerController, Player, AsyncMock]:
        """
        Build a controller with a single player using fake mute control.

        :param mock_mass: the mocked MusicAssistant instance.
        :param volume_level: initial volume level of the player, None if unknown.
        """
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub({CONF_MUTE_CONTROL: PLAYER_CONTROL_FAKE})
        )
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player, protocol = _attach(provider, "player_1", "Player 1")
        protocol._attr_volume_level = volume_level
        # let the mocked native volume control behave like a real device
        volume_set = _report_volume_set(protocol)
        _refresh(player)
        return controller, player, volume_set

    async def test_mute_then_unmute_restores_volume(self, mock_mass: MagicMock) -> None:
        """Muting reports volume_muted=True and unmuting restores the previous volume."""
        controller, player, volume_set = self._make_player(mock_mass)

        await controller.cmd_volume_mute("player_1", True)
        muted_state = player.state
        assert muted_state.volume_muted is True
        assert muted_state.volume_level == 0
        assert player.extra_data[ATTR_PREVIOUS_VOLUME] == 40

        await controller.cmd_volume_mute("player_1", False)
        volume_set.assert_awaited_with(40)
        # simulate the device reporting back its state after the volume command
        player.update_state()
        unmuted_state = player.state
        assert unmuted_state.volume_muted is False
        assert unmuted_state.volume_level == 40

    async def test_repeated_mute_keeps_previous_volume(self, mock_mass: MagicMock) -> None:
        """A repeated mute command must not overwrite the stored volume with 0."""
        controller, player, volume_set = self._make_player(mock_mass)

        await controller.cmd_volume_mute("player_1", True)
        await controller.cmd_volume_mute("player_1", True)
        assert player.extra_data[ATTR_PREVIOUS_VOLUME] == 40
        assert player.volume_muted is True

        await controller.cmd_volume_mute("player_1", False)
        volume_set.assert_awaited_with(40)

    async def test_unmute_with_unknown_previous_volume(self, mock_mass: MagicMock) -> None:
        """Unmuting a player whose volume was unknown at mute time restores a low volume."""
        controller, player, volume_set = self._make_player(mock_mass, volume_level=None)

        await controller.cmd_volume_mute("player_1", True)
        assert player.extra_data[ATTR_PREVIOUS_VOLUME] is None

        await controller.cmd_volume_mute("player_1", False)
        volume_set.assert_awaited_with(1)
        player.update_state()
        assert player.volume_muted is False

    async def test_unmute_of_unmuted_player_keeps_volume(self, mock_mass: MagicMock) -> None:
        """An unmute command for a player that is not muted may not touch the volume."""
        controller, player, volume_set = self._make_player(mock_mass, volume_level=50)

        await controller.cmd_volume_mute("player_1", False)
        volume_set.assert_not_awaited()
        assert player.volume_level == 50

    async def test_unmute_restores_a_stored_zero_volume(self, mock_mass: MagicMock) -> None:
        """A player that was already silent stays silent after mute and unmute."""
        controller, _player, volume_set = self._make_player(mock_mass, volume_level=0)

        await controller.cmd_volume_mute("player_1", True)
        await controller.cmd_volume_mute("player_1", False)
        volume_set.assert_awaited_with(0)

    async def test_volume_set_clears_fake_mute(self, mock_mass: MagicMock) -> None:
        """A regular volume change while fake muted implies an unmute."""
        controller, player, _volume_set = self._make_player(mock_mass)

        await controller.cmd_volume_mute("player_1", True)
        muted_state = player.state
        assert muted_state.volume_muted is True

        await controller.cmd_volume_set("player_1", 25)
        # simulate the device reporting back its state after the volume command
        player.update_state()
        unmuted_state = player.state
        assert unmuted_state.volume_muted is False
        assert unmuted_state.volume_level == 25


class TestVolumeStep:
    """The volume_step core config setting controls the size of a single volume nudge."""

    def _make_player(
        self, mock_mass: MagicMock, step: int | None, volume_level: int
    ) -> tuple[PlayerController, Player]:
        """Build a controller with a single player and the given volume_step config."""
        controller = _controller(mock_mass)
        controller.config = _volume_step_config(step)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player, protocol = _attach(provider, "player_1", "Player 1")
        protocol._attr_volume_level = volume_level
        # let the mocked native volume control behave like a real device
        _report_volume_set(protocol)
        _refresh(player, force=False)
        return controller, player

    def _make_synced_pair(
        self, mock_mass: MagicMock, step: int | None
    ) -> tuple[PlayerController, dict[str, Player]]:
        """Build a leader synced to one member, both at volume 50, with a volume_step config."""
        controller, players = _synced_pair(mock_mass)
        controller.config = _volume_step_config(step)
        return controller, players

    @pytest.mark.parametrize(
        ("start", "expected"),
        [(5, 6), (20, 22), (50, 53), (80, 82), (95, 96)],
    )
    async def test_default_step_up_matches_the_adaptive_ladder(
        self, mock_mass: MagicMock, start: int, expected: int
    ) -> None:
        """With volume_step at its default (0), volume_up keeps today's adaptive ladder."""
        controller, player = self._make_player(mock_mass, None, start)

        await controller.cmd_volume_up("player_1")

        player.update_state()
        assert player.volume_level == expected

    @pytest.mark.parametrize(
        ("start", "expected"),
        [(5, 4), (20, 18), (50, 47), (80, 78), (95, 94)],
    )
    async def test_default_step_down_matches_the_adaptive_ladder(
        self, mock_mass: MagicMock, start: int, expected: int
    ) -> None:
        """With volume_step at its default (0), volume_down keeps today's adaptive ladder."""
        controller, player = self._make_player(mock_mass, None, start)

        await controller.cmd_volume_down("player_1")

        player.update_state()
        assert player.volume_level == expected

    async def test_configured_step_moves_up_by_a_flat_amount_mid_range(
        self, mock_mass: MagicMock
    ) -> None:
        """A configured flat step of 5 moves by exactly 5 in the middle of the range."""
        controller, player = self._make_player(mock_mass, 5, 50)

        await controller.cmd_volume_up("player_1")

        player.update_state()
        assert player.volume_level == 55

    async def test_configured_step_moves_up_by_a_flat_amount_near_the_extreme(
        self, mock_mass: MagicMock
    ) -> None:
        """A configured flat step of 5 near the extreme overrides the finer ladder step."""
        controller, player = self._make_player(mock_mass, 5, 5)

        await controller.cmd_volume_up("player_1")

        player.update_state()
        assert player.volume_level == 10

    async def test_configured_step_moves_down_by_a_flat_amount_mid_range(
        self, mock_mass: MagicMock
    ) -> None:
        """A configured flat step of 5 moves down by exactly 5 in the middle of the range."""
        controller, player = self._make_player(mock_mass, 5, 50)

        await controller.cmd_volume_down("player_1")

        player.update_state()
        assert player.volume_level == 45

    async def test_configured_step_moves_down_by_a_flat_amount_near_the_extreme(
        self, mock_mass: MagicMock
    ) -> None:
        """A configured flat step of 5 near the extreme overrides the finer ladder step."""
        controller, player = self._make_player(mock_mass, 5, 95)

        await controller.cmd_volume_down("player_1")

        player.update_state()
        assert player.volume_level == 90

    async def test_large_configured_step_clamps_up_at_the_maximum(
        self, mock_mass: MagicMock
    ) -> None:
        """A large configured step clamps volume_up at 100."""
        controller, player = self._make_player(mock_mass, 10, 95)

        await controller.cmd_volume_up("player_1")

        player.update_state()
        assert player.volume_level == 100

    async def test_large_configured_step_clamps_down_at_zero(self, mock_mass: MagicMock) -> None:
        """A large configured step clamps volume_down at 0."""
        controller, player = self._make_player(mock_mass, 10, 5)

        await controller.cmd_volume_down("player_1")

        player.update_state()
        assert player.volume_level == 0

    async def test_group_volume_up_with_default_step_uses_the_ladder(
        self, mock_mass: MagicMock
    ) -> None:
        """cmd_group_volume_up honours the default (0) adaptive ladder too."""
        controller, players = self._make_synced_pair(mock_mass, None)

        await controller.cmd_group_volume_up("leader")

        for player in players.values():
            player.update_state()
            assert player.volume_level == 53

    async def test_group_volume_down_with_default_step_uses_the_ladder(
        self, mock_mass: MagicMock
    ) -> None:
        """cmd_group_volume_down honours the default (0) adaptive ladder too."""
        controller, players = self._make_synced_pair(mock_mass, None)

        await controller.cmd_group_volume_down("leader")

        for player in players.values():
            player.update_state()
            assert player.volume_level == 47

    async def test_group_volume_up_with_configured_step(self, mock_mass: MagicMock) -> None:
        """cmd_group_volume_up honours a configured flat step."""
        controller, players = self._make_synced_pair(mock_mass, 5)

        await controller.cmd_group_volume_up("leader")

        for player in players.values():
            player.update_state()
            assert player.volume_level == 55

    async def test_group_volume_down_with_configured_step(self, mock_mass: MagicMock) -> None:
        """cmd_group_volume_down honours a configured flat step."""
        controller, players = self._make_synced_pair(mock_mass, 5)

        await controller.cmd_group_volume_down("leader")

        for player in players.values():
            player.update_state()
            assert player.volume_level == 45

    async def test_config_entry_exposes_default_and_range(
        self, controller: PlayerController
    ) -> None:
        """get_config_entries returns the volume_step entry with its default and range."""
        entries = await controller.get_config_entries()

        entry = next(entry for entry in entries if entry.key == CONF_VOLUME_STEP)
        assert entry.type == ConfigEntryType.INTEGER
        assert entry.default_value == 0
        assert entry.range == (0, 10)


class SlowDevice(NamedTuple):
    """A mocked device that takes its time to answer its first volume command."""

    player_id: str
    reached: asyncio.Event


class TestGroupVolumeOrdering:
    """Group volume commands that overlap are handled one after the other."""

    def _make_synced_pair(
        self,
        mock_mass: MagicMock,
        slow_device: SlowDevice | None = None,
        volumes: dict[str, int] | None = None,
    ) -> tuple[PlayerController, dict[str, Player]]:
        """
        Build a mute capable leader synced to one member.

        :param slow_device: When given, the named player takes its time to answer its
            first volume command and reports as soon as it received that command.
        :param volumes: Volume level per player, defaults to 50 for both.
        """
        controller, players = _synced_pair(
            mock_mass,
            volumes=volumes,
            features={PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE},
        )
        controller.config = _volume_step_config(5)
        for player in players.values():
            protocol = _own(player)

            # let the mocked native volume control behave like a real device, that may
            # take long enough to answer for a later command to overtake it
            async def _volume_set(
                volume: int, _player: Player = player, _protocol: MockProtocol = protocol
            ) -> None:
                if (
                    slow_device is not None
                    and slow_device.player_id == _player.player_id
                    and not slow_device.reached.is_set()
                ):
                    slow_device.reached.set()
                    await asyncio.sleep(0.2)
                _protocol._attr_volume_level = volume

            protocol.volume_set = AsyncMock(side_effect=_volume_set)  # type: ignore[method-assign]
            protocol.volume_mute = AsyncMock(  # type: ignore[method-assign]
                side_effect=lambda muted, _protocol=protocol: setattr(
                    _protocol, "_attr_volume_muted", muted
                )
            )
        return controller, players

    async def test_the_last_command_decides_the_group_volume(self, mock_mass: MagicMock) -> None:
        """A slow command may not overrule the volume of a later, faster one."""
        slow_leader = SlowDevice("leader", asyncio.Event())
        controller, players = self._make_synced_pair(mock_mass, slow_leader)

        first = asyncio.create_task(controller.cmd_group_volume("leader", 80))
        # only send the second command once the first one reached the device
        await slow_leader.reached.wait()
        second = asyncio.create_task(controller.cmd_group_volume("leader", 30))
        await asyncio.gather(first, second)

        for player in players.values():
            player.update_state()
            assert player.volume_level == 30

    async def test_a_command_for_a_member_waits_for_one_for_its_leader(
        self, mock_mass: MagicMock
    ) -> None:
        """Addressing the same group by member or by leader may not overlap."""
        slow_leader = SlowDevice("leader", asyncio.Event())
        controller, players = self._make_synced_pair(mock_mass, slow_leader)
        in_flight = 0
        overlapped = False
        set_group_volume = controller.set_group_volume

        async def _track_overlap(group_player: Player, volume_level: int) -> None:
            nonlocal in_flight, overlapped
            in_flight += 1
            overlapped = overlapped or in_flight > 1
            try:
                await set_group_volume(group_player, volume_level)
            finally:
                in_flight -= 1

        controller.set_group_volume = _track_overlap  # type: ignore[method-assign]

        first = asyncio.create_task(controller.cmd_group_volume("leader", 80))
        await slow_leader.reached.wait()
        # a synced member adjusts the very same group as its leader
        second = asyncio.create_task(controller.cmd_group_volume("member", 30))
        await asyncio.gather(first, second)

        assert overlapped is False
        for player in players.values():
            player.update_state()
            assert player.volume_level == 30

    async def test_a_nudge_from_a_member_steps_the_volume_of_the_group(
        self, mock_mass: MagicMock
    ) -> None:
        """A group nudge addressed to a member steps the group, not the member itself."""
        controller, players = self._make_synced_pair(mock_mass, volumes={"member": 40})
        # the group sits at the volume of its loudest member, the member at its own
        assert players["leader"].group_volume == 50
        assert players["member"].group_volume == 40

        await controller.cmd_group_volume_up("member")

        for player in players.values():
            player.update_state()
        assert players["leader"].volume_level == 55

    async def test_an_individual_volume_command_waits_for_the_group(
        self, mock_mass: MagicMock
    ) -> None:
        """A member's own volume command may not be overtaken by a group change."""
        slow_member = SlowDevice("member", asyncio.Event())
        controller, players = self._make_synced_pair(mock_mass, slow_member)

        group = asyncio.create_task(controller.cmd_group_volume("leader", 80))
        await slow_member.reached.wait()
        individual = asyncio.create_task(controller.cmd_volume_set("member", 10))
        await asyncio.gather(group, individual)

        for player in players.values():
            player.update_state()
        assert players["member"].volume_level == 10
        assert players["leader"].volume_level == 80

    async def test_a_muted_leader_keeps_its_mute_on_a_group_volume_change(
        self, mock_mass: MagicMock
    ) -> None:
        """A muted sync leader keeps its mute through a group volume change without blocking."""
        controller, players = self._make_synced_pair(mock_mass)
        _own(players["leader"])._attr_volume_muted = True
        _refresh(players["leader"])

        # a sync leader is a member of its own group, so the fan-out sets the volume
        # of the very player the group command is running for. This must not deadlock
        # on a nested cmd_volume_mute call under the group's own volume lock.
        async with asyncio.timeout(5):
            await controller.cmd_group_volume("leader", 30)

        players["leader"].update_state()
        assert players["leader"].volume_muted is True
        assert players["leader"].volume_level == 30


class LateReportingDevice:
    """A mocked device that only reports the volume it was given back when told to."""

    def __init__(self, command_delay: float = 0) -> None:
        """
        Initialize the mocked device.

        :param command_delay: Seconds a single volume command takes to reach the device.
        """
        self.commands: list[int] = []
        self._command_delay = command_delay
        self._pending: dict[Player, int] = {}

    def bind(self, player: Player) -> None:
        """Make the native protocol of the given player answer its volume commands like this."""

        async def _volume_set(volume: int, _player: Player = player) -> None:
            self.commands.append(volume)
            if self._command_delay:
                await asyncio.sleep(self._command_delay)
            self._pending[_player] = volume

        _own(player).volume_set = AsyncMock(side_effect=_volume_set)  # type: ignore[method-assign]

    def report(self) -> None:
        """Report the volume of every command received so far back to the player state."""
        for player, volume in self._pending.items():
            _own(player)._attr_volume_level = volume
            player.update_state(force_update=True, signal_event=False)
        self._pending.clear()


class TestVolumeNudgeTarget:
    """Volume nudges step from the level last commanded, not from a lagging report."""

    def _make_player(
        self, mock_mass: MagicMock, command_delay: float = 0
    ) -> tuple[PlayerController, Player, LateReportingDevice]:
        """
        Build a single player at volume 50 whose device reports back on request.

        :param command_delay: Seconds a single volume command takes to reach the device.
        """
        controller = _controller(mock_mass)
        controller.config = _volume_step_config(5)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player, protocol = _attach(provider, "player_1", "Player 1")
        protocol._attr_volume_level = 50
        device = LateReportingDevice(command_delay)
        device.bind(player)
        _refresh(player, force=False)
        # a second, forced pass so the player derives its group volume from its own state
        _refresh(player)
        return controller, player, device

    def _make_synced_pair(
        self, mock_mass: MagicMock, command_delay: float = 0
    ) -> tuple[PlayerController, dict[str, Player], LateReportingDevice]:
        """
        Build a leader synced to one member, both at volume 50, both late reporting.

        :param command_delay: Seconds a single volume command takes to reach the device.
        """
        controller, players = _synced_pair(mock_mass)
        controller.config = _volume_step_config(5)
        device = LateReportingDevice(command_delay)
        for player in players.values():
            device.bind(player)
        return controller, players, device

    async def test_nudges_up_stack_before_the_player_reports_back(
        self, mock_mass: MagicMock
    ) -> None:
        """Three volume ups in a row climb, even with no report in between."""
        controller, _player, device = self._make_player(mock_mass)

        for _ in range(3):
            await controller.cmd_volume_up("player_1")

        assert device.commands == [55, 60, 65]

    async def test_nudges_down_stack_before_the_player_reports_back(
        self, mock_mass: MagicMock
    ) -> None:
        """Three volume downs in a row descend, even with no report in between."""
        controller, _player, device = self._make_player(mock_mass)

        for _ in range(3):
            await controller.cmd_volume_down("player_1")

        assert device.commands == [45, 40, 35]

    async def test_nudges_that_overlap_each_get_their_own_step(self, mock_mass: MagicMock) -> None:
        """Volume ups that arrive while an earlier one is still on its way all count."""
        controller, _player, device = self._make_player(mock_mass, command_delay=0.05)

        await asyncio.gather(*(controller.cmd_volume_up("player_1") for _ in range(3)))

        assert device.commands == [55, 60, 65]

    async def test_a_queued_nudge_does_not_undo_the_level_a_later_one_claimed(
        self, mock_mass: MagicMock
    ) -> None:
        """A nudge waiting for the volume lock may not take the level back down."""
        controller, _player, device = self._make_player(mock_mass, command_delay=0.05)
        tasks = [asyncio.create_task(controller.cmd_volume_up("player_1")) for _ in range(3)]
        # let all three claim their level; the second and third then queue on the lock
        await asyncio.sleep(0)
        # wait for the second one to reach the device, so a nudge sent now reads whatever
        # that (by then oldest) command left behind
        while len(device.commands) < 2:
            await asyncio.sleep(0.005)
        tasks.append(asyncio.create_task(controller.cmd_volume_up("player_1")))

        await asyncio.gather(*tasks)

        assert device.commands == [55, 60, 65, 70]

    async def test_a_group_nudge_moves_every_member_up(self, mock_mass: MagicMock) -> None:
        """A group nudge up may not send a member the other way."""
        controller, players, device = self._make_synced_pair(mock_mass)
        # the loudest member is the one that was just turned down on its own, so the
        # level it still reports sits above the level the group is being stepped to
        _own(players["member"])._attr_volume_level = 80
        for _ in range(3):
            _refresh(*players.values())

        await controller.cmd_volume_set("member", 40)
        await controller.cmd_group_volume_up("leader")

        assert device.commands == [40, 55, 46]

    async def test_a_group_nudge_on_an_ungrouped_player_steps_its_own_volume(
        self, mock_mass: MagicMock
    ) -> None:
        """A group nudge falls back to the player itself, which has no group to step."""
        controller, _player, device = self._make_player(mock_mass)

        for _ in range(3):
            await controller.cmd_group_volume_up("player_1")

        assert device.commands == [55, 60, 65]

    async def test_a_nudge_steps_from_the_level_the_slider_was_left_at(
        self, mock_mass: MagicMock
    ) -> None:
        """A nudge right after a volume set steps from that set level."""
        controller, _player, device = self._make_player(mock_mass)

        await controller.cmd_volume_set("player_1", 20)
        await controller.cmd_volume_up("player_1")

        assert device.commands == [20, 25]

    async def test_a_change_on_the_device_wins_once_the_last_command_ages_out(
        self, mock_mass: MagicMock
    ) -> None:
        """A volume turned down on the device itself is the base of the next nudge."""
        controller, player, device = self._make_player(mock_mass)
        await controller.cmd_volume_up("player_1")
        device.report()
        # the volume is turned down on the device itself, well after that command
        _own(player)._attr_volume_level = 20
        player.update_state(force_update=True, signal_event=False)

        with patch.object(players_volume, "VOLUME_TARGET_EXPIRY", 0):
            await controller.cmd_volume_up("player_1")

        assert device.commands[-1] == 25

    async def test_group_nudges_up_stack_before_the_players_report_back(
        self, mock_mass: MagicMock
    ) -> None:
        """Three group volume ups in a row climb, even with no report in between."""
        controller, _players, device = self._make_synced_pair(mock_mass)

        for _ in range(3):
            await controller.cmd_group_volume_up("leader")

        assert device.commands == [55, 55, 60, 60, 65, 65]

    async def test_group_nudges_down_stack_before_the_players_report_back(
        self, mock_mass: MagicMock
    ) -> None:
        """Three group volume downs in a row descend, even with no report in between."""
        controller, _players, device = self._make_synced_pair(mock_mass)

        for _ in range(3):
            await controller.cmd_group_volume_down("leader")

        assert device.commands == [45, 45, 40, 40, 35, 35]

    async def test_a_group_nudge_after_a_member_was_set_on_its_own_keeps_the_step(
        self, mock_mass: MagicMock
    ) -> None:
        """Setting one member does not send the group back to the level it reports."""
        controller, _players, device = self._make_synced_pair(mock_mass)

        await controller.cmd_group_volume_up("leader")
        await controller.cmd_volume_set("member", 10)
        await controller.cmd_group_volume_up("leader")

        # the loudest member was commanded to 55, so the group steps from there, and the
        # member that was just turned down keeps its share of the group volume
        assert device.commands == [55, 55, 10, 60, 20]

    async def test_a_group_volume_beyond_the_range_does_not_pin_the_next_nudge(
        self, mock_mass: MagicMock
    ) -> None:
        """An out of range group volume leaves the members at 100, not above it."""
        controller, _players, device = self._make_synced_pair(mock_mass)

        await controller.cmd_group_volume("leader", 200)
        await controller.cmd_group_volume_down("leader")

        assert device.commands == [100, 100, 95, 95]

    async def test_a_group_nudge_from_a_member_shares_the_target_of_its_leader(
        self, mock_mass: MagicMock
    ) -> None:
        """Group nudges addressed to a member and to its leader step the same group."""
        controller, _players, device = self._make_synced_pair(mock_mass)

        await controller.cmd_group_volume_up("leader")
        await controller.cmd_group_volume_up("member")

        assert device.commands == [55, 55, 60, 60]


class TestGroupVolumeReference:
    """A group volume adjustment interpolates from the levels its members are really at."""

    def _make_group(
        self, mock_mass: MagicMock, first_volume: int = 50, second_volume: int = 50
    ) -> tuple[PlayerController, dict[str, Player], dict[str, list[int]]]:
        """
        Build a group player with two members, at the given volumes.

        :param first_volume: Volume level the first member starts at.
        :param second_volume: Volume level the second member starts at.
        :return: The controller, the players by id and the volumes commanded per member.
        """
        controller = _controller(mock_mass)
        controller.config = _volume_step_config(5)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        commands: dict[str, list[int]] = {}
        members: dict[str, Player] = {}
        for player_id, volume in (("member_1", first_volume), ("member_2", second_volume)):
            member, member_protocol = _attach(provider, player_id, player_id.title())
            member_protocol._attr_volume_level = volume
            commands[player_id] = []

            async def _volume_set(volume: int, _recorded: list[int] = commands[player_id]) -> None:
                _recorded.append(volume)

            member_protocol.volume_set = AsyncMock(side_effect=_volume_set)  # type: ignore[method-assign]
            members[player_id] = member
        group, group_protocol = _attach(provider, "group", "Group", player_type=PlayerType.GROUP)
        # a group names its members by player id
        group_protocol._attr_group_members = list(members)
        players = {"group": group, **members}
        _refresh(*players.values(), force=False)
        # a second, forced pass so the group derives its group volume from the members
        _refresh(*players.values())
        return controller, players, commands

    def _report(self, player: Player, volume: int) -> None:
        """Let the player report the given volume level, the way its provider would."""
        protocol = _own(player)
        protocol._attr_volume_level = volume
        protocol._cache.clear()
        player.update_state(force_update=True)

    async def test_a_member_turned_down_on_the_device_keeps_its_level(
        self, mock_mass: MagicMock
    ) -> None:
        """A group nudge up may not undo a member that was turned down on the device."""
        controller, players, commands = self._make_group(mock_mass)
        await controller.cmd_group_volume_up("group")
        self._report(players["member_1"], 55)
        self._report(players["member_2"], 55)

        with patch.object(players_volume, "VOLUME_TARGET_EXPIRY", 0):
            # the member is turned down on the device itself, well after that command
            self._report(players["member_2"], 20)
            await controller.cmd_group_volume_up("group")

        # the group steps from 55 to 60, and the member keeps its (much lower) share
        assert commands["member_1"] == [55, 60]
        assert commands["member_2"] == [55, 29]

    async def test_a_member_reporting_the_level_it_was_given_keeps_the_balance(
        self, mock_mass: MagicMock
    ) -> None:
        """Members confirming a group nudge may not become the reference themselves."""
        controller, players, commands = self._make_group(mock_mass)
        await controller.cmd_volume_set("member_2", 20)
        self._report(players["member_2"], 20)

        await controller.cmd_group_volume_up("group")
        self._report(players["member_1"], 55)
        self._report(players["member_2"], 28)
        await controller.cmd_group_volume_down("group")

        # stepping back down to where the group started restores the balance it had
        assert commands["member_1"] == [55, 50]
        assert commands["member_2"] == [20, 28, 20]

    async def test_a_member_that_dropped_off_no_longer_sets_the_reference(
        self, mock_mass: MagicMock
    ) -> None:
        """A group nudge up may not turn the group down over an unreachable member."""
        controller, players, commands = self._make_group(
            mock_mass, first_volume=30, second_volume=80
        )
        await controller.cmd_group_volume_up("group")
        self._report(players["member_1"], 48)
        self._report(players["member_2"], 85)

        # the loudest member drops off the network; a permanent group keeps it as a member
        member_2 = _own(players["member_2"])
        member_2._attr_available = False
        member_2._cache.clear()
        players["member_2"].update_state(force_update=True)
        await controller.cmd_group_volume_up("group")

        assert commands["member_1"] == [48, 53]
        assert commands["member_2"] == [85]

    async def test_a_group_reporting_its_own_volume_keeps_the_balance(
        self, mock_mass: MagicMock
    ) -> None:
        """A group that reports a volume of its own may not reset its own reference."""
        controller, players, commands = self._make_group(
            mock_mass, first_volume=50, second_volume=20
        )
        await controller.cmd_group_volume_up("group")
        self._report(players["member_1"], 55)
        self._report(players["member_2"], 28)
        # a cast group reports the volume of its members as its own
        self._report(players["group"], 55)
        await controller.cmd_group_volume_down("group")

        assert commands["member_1"] == [55, 50]
        assert commands["member_2"] == [28, 20]

    async def test_a_member_clamped_to_its_volume_limit_keeps_that_level(
        self, mock_mass: MagicMock
    ) -> None:
        """Correcting a volume that ran past its limit is not a level the group commanded."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub(min_volume=20)
        )
        use_real_create_task(mock_mass)
        # with a min volume of 20 the members report device volumes of 20-100 for 0-100
        controller, players, commands = self._make_group(
            mock_mass, first_volume=60, second_volume=60
        )
        await controller.cmd_group_volume_up("group")
        self._report(players["member_1"], 64)
        self._report(players["member_2"], 64)

        # the member is turned below its own minimum, so it is corrected back up to 0
        self._report(players["member_2"], 10)
        await controller.cmd_group_volume_up("group")

        assert commands["member_1"] == [64, 68]
        assert commands["member_2"] == [64, 20, 28]


class TestFakeMuteInGroup:
    """A fake muted player in a group follows the mute lock, just like a native mute."""

    def _make_synced_pair(
        self, mock_mass: MagicMock, *, member_mute_control: str = PLAYER_CONTROL_FAKE
    ) -> tuple[PlayerController, dict[str, Player]]:
        """
        Build a leader synced to one member.

        :param member_mute_control: Mute control of the member, the leader always uses fake mute.
        """

        def _conf(player_id: str, key: str, default: object = None) -> object:
            if key == CONF_MUTE_CONTROL and player_id == "member":
                return member_mute_control
            return _player_config_stub({CONF_MUTE_CONTROL: PLAYER_CONTROL_FAKE})(
                player_id, key, default
            )

        mock_mass.config.get_player_value = MagicMock(side_effect=_conf)
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        players: dict[str, Player] = {}
        for player_id in ("leader", "member"):
            player, protocol = _attach(provider, player_id, player_id.title())
            protocol._attr_supported_features = {
                PlayerFeature.VOLUME_SET,
                PlayerFeature.VOLUME_MUTE,
            }
            protocol._attr_volume_level = 50
            # let the mocked native volume control behave like a real device
            _report_volume_set(protocol)
            players[player_id] = player
        _own(players["leader"])._attr_group_members = [_proto("member")]
        _refresh(*players.values(), force=False)
        # a second pass, so the group volume of the leader accounts for its member
        for player in players.values():
            player.update_state(signal_event=False)
        return controller, players

    async def test_group_volume_keeps_a_muted_pair_muted(self, mock_mass: MagicMock) -> None:
        """A group volume change may not bring a muted fake mute pair back to life."""
        controller, players = self._make_synced_pair(mock_mass)
        await controller.cmd_group_volume_mute("leader", True)

        await controller.cmd_group_volume("leader", 30)

        for player in players.values():
            player.update_state()
            assert player.volume_muted is True
            assert player.volume_level == 0

    async def test_a_muted_member_does_not_inflate_a_group_nudge(
        self, mock_mass: MagicMock
    ) -> None:
        """A volume level a muted member never receives may not step the group."""
        controller, players = self._make_synced_pair(mock_mass)
        controller.config = _volume_step_config(5)
        await controller.cmd_volume_mute("member", True)
        # let the group volume of the leader account for the muted member
        players["leader"].update_state(signal_event=False)
        # the member is held silent, so this level never reaches it
        await controller.cmd_volume_set("member", 60)

        await controller.cmd_group_volume_up("leader")

        players["leader"].update_state()
        assert players["leader"].volume_level == 55

    async def test_a_nudge_after_unmuting_steps_from_the_restored_volume(
        self, mock_mass: MagicMock
    ) -> None:
        """Unmuting hands the next nudge the volume it restored, not the muted 0."""
        controller, players = self._make_synced_pair(mock_mass)
        controller.config = _volume_step_config(5)
        await controller.cmd_volume_mute("member", True)
        players["leader"].update_state(signal_event=False)
        # a level set while muted never reaches the member
        await controller.cmd_volume_set("member", 60)

        await controller.cmd_volume_mute("member", False)
        await controller.cmd_volume_up("member")

        players["member"].update_state()
        assert players["member"].volume_level == 55

    async def test_group_volume_down_keeps_a_muted_member_muted(self, mock_mass: MagicMock) -> None:
        """Turning a group down leaves a single muted member silent, at its own volume."""
        controller, players = self._make_synced_pair(mock_mass)
        await controller.cmd_volume_mute("member", True)
        # let the group volume of the leader account for the muted member
        players["leader"].update_state(signal_event=False)

        await controller.cmd_group_volume("leader", 25)

        for player in players.values():
            player.update_state()
        member_state = players["member"].state
        assert member_state.volume_muted is True
        assert member_state.volume_level == 0
        # the player that is not muted follows the group volume as usual
        assert players["leader"].volume_level == 25
        # unmuting brings the member back at the volume it had before it was muted
        await controller.cmd_volume_mute("member", False)
        players["member"].update_state()
        assert players["member"].volume_level == 50

    async def test_unmute_restores_the_volume_from_before_the_mute(
        self, mock_mass: MagicMock
    ) -> None:
        """A group volume change while muted may not alter the volume to restore."""
        controller, players = self._make_synced_pair(mock_mass)
        await controller.cmd_group_volume_mute("leader", True)
        await controller.cmd_group_volume("leader", 30)

        await controller.cmd_group_volume_mute("leader", False)

        for player in players.values():
            player.update_state()
            assert player.volume_muted is False
            assert player.volume_level == 50

    async def test_group_volume_keeps_a_mixed_pair_muted(self, mock_mass: MagicMock) -> None:
        """Members with a different mute control stay muted alike on a group volume change."""
        controller, players = self._make_synced_pair(
            mock_mass, member_mute_control=PLAYER_CONTROL_NATIVE
        )
        member_protocol = _own(players["member"])
        mute = AsyncMock(
            side_effect=lambda muted: setattr(member_protocol, "_attr_volume_muted", muted)
        )
        member_protocol.volume_mute = mute  # type: ignore[method-assign]
        await controller.cmd_group_volume_mute("leader", True)

        await controller.cmd_group_volume("leader", 30)

        mute.assert_awaited_once_with(True)
        for player in players.values():
            player.update_state()
            assert player.volume_muted is True


class TestMuteControlGuard:
    """Muting is gated on the mute control, independently of the volume control."""

    def _make_player(
        self,
        mock_mass: MagicMock,
        mute_control: str,
        volume_control: str,
        controls: dict[str, PlayerControl] | None = None,
        features: set[PlayerFeature] | None = None,
    ) -> tuple[PlayerController, Player, MockProtocol]:
        """Build a controller with a single player using the given control config."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub(
                {CONF_MUTE_CONTROL: mute_control, CONF_VOLUME_CONTROL: volume_control}
            )
        )
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player, protocol = _attach(provider, "player_1", "Player 1")
        if features is not None:
            protocol._attr_supported_features = features
        controller._controls = controls or {}
        _refresh(player, force=False)
        return controller, player, protocol

    async def test_external_mute_control_without_volume_control(self, mock_mass: MagicMock) -> None:
        """A player without volume control still mutes through an external PlayerControl."""
        mute_set = AsyncMock()
        control = PlayerControl(
            id="ext_mute",
            provider="test",
            name="External Mute",
            supports_mute=True,
            mute_set=mute_set,
        )
        controller, player, _protocol = self._make_player(
            mock_mass,
            mute_control="ext_mute",
            volume_control=PLAYER_CONTROL_NONE,
            controls={"ext_mute": control},
        )
        assert player._mute_control == "ext_mute"

        await controller.cmd_volume_mute("player_1", True)
        mute_set.assert_awaited_once_with(True)

    async def test_native_mute_control_without_volume_control(self, mock_mass: MagicMock) -> None:
        """A player without volume control still mutes natively."""
        controller, _player, protocol = self._make_player(
            mock_mass,
            mute_control=PLAYER_CONTROL_NATIVE,
            volume_control=PLAYER_CONTROL_NONE,
            # native mute is only honored while the player advertises the feature
            features={PlayerFeature.VOLUME_MUTE},
        )
        volume_mute = AsyncMock()
        protocol.volume_mute = volume_mute  # type: ignore[method-assign]

        await controller.cmd_volume_mute("player_1", True)
        volume_mute.assert_awaited_once_with(True)

    async def test_mute_control_none_raises(self, mock_mass: MagicMock) -> None:
        """A player with volume control but no mute control rejects the command."""
        controller, _player, protocol = self._make_player(
            mock_mass,
            mute_control=PLAYER_CONTROL_NONE,
            volume_control=PLAYER_CONTROL_NATIVE,
        )
        volume_mute = AsyncMock()
        protocol.volume_mute = volume_mute  # type: ignore[method-assign]

        with pytest.raises(UnsupportedFeaturedException):
            await controller.cmd_volume_mute("player_1", True)
        volume_mute.assert_not_awaited()

    async def test_fake_mute_without_volume_control_raises(self, mock_mass: MagicMock) -> None:
        """Fake mute needs a volume control to drive, so it rejects the command outright."""
        controller, player, protocol = self._make_player(
            mock_mass,
            mute_control=PLAYER_CONTROL_FAKE,
            volume_control=PLAYER_CONTROL_NONE,
        )
        protocol._attr_volume_level = 40

        with pytest.raises(UnsupportedFeaturedException):
            await controller.cmd_volume_mute("player_1", True)
        assert ATTR_PREVIOUS_VOLUME not in player.extra_data
        assert ATTR_FAKE_MUTE not in player.extra_data

    async def test_vanished_mute_control_raises(self, mock_mass: MagicMock) -> None:
        """A mute control that disappeared after being resolved is reported, not ignored."""
        control = PlayerControl(
            id="ext_mute",
            provider="test",
            name="External Mute",
            supports_mute=True,
            mute_set=AsyncMock(),
        )
        controller, player, _protocol = self._make_player(
            mock_mass,
            mute_control="ext_mute",
            volume_control=PLAYER_CONTROL_NONE,
            controls={"ext_mute": control},
        )
        # the resolved control is cached on the player, so removing it here leaves
        # the player pointing at a control that no longer exists
        assert player._mute_control == "ext_mute"
        controller._controls = {}

        with pytest.raises(UnsupportedFeaturedException):
            await controller.cmd_volume_mute("player_1", True)

    async def test_unmute_clears_mute_lock_without_mute_control(self, mock_mass: MagicMock) -> None:
        """Unmuting clears a mute lock left behind by a since-removed mute control."""
        controller, player, _protocol = self._make_player(
            mock_mass,
            mute_control=PLAYER_CONTROL_NONE,
            volume_control=PLAYER_CONTROL_NATIVE,
        )
        player.extra_data[ATTR_MUTE_LOCK] = True

        with pytest.raises(UnsupportedFeaturedException):
            await controller.cmd_volume_mute("player_1", False)
        assert ATTR_MUTE_LOCK not in player.extra_data

    async def test_failed_mute_sets_no_mute_lock(self, mock_mass: MagicMock) -> None:
        """A grouped player whose mute command failed is not left holding a mute lock."""
        control = PlayerControl(
            id="ext_mute",
            provider="test",
            name="External Mute",
            supports_mute=False,
        )
        controller, player, _protocol = self._make_player(
            mock_mass,
            mute_control="ext_mute",
            volume_control=PLAYER_CONTROL_NONE,
            controls={"ext_mute": control},
        )
        player.state.synced_to = "leader"

        with pytest.raises(UnsupportedFeaturedException):
            await controller.cmd_volume_mute("player_1", True)
        assert ATTR_MUTE_LOCK not in player.extra_data

    async def test_failed_mute_keeps_existing_mute_lock(self, mock_mass: MagicMock) -> None:
        """A failed mute leaves the lock of an earlier successful mute in place."""
        control = PlayerControl(
            id="ext_mute",
            provider="test",
            name="External Mute",
            supports_mute=False,
        )
        controller, player, _protocol = self._make_player(
            mock_mass,
            mute_control="ext_mute",
            volume_control=PLAYER_CONTROL_NONE,
            controls={"ext_mute": control},
        )
        player.state.synced_to = "leader"
        player.extra_data[ATTR_MUTE_LOCK] = True

        with pytest.raises(UnsupportedFeaturedException):
            await controller.cmd_volume_mute("player_1", True)
        assert player.extra_data[ATTR_MUTE_LOCK] is True


class TestGroupMuteMemberFilter:
    """Group mute skips members that have no mute control of their own."""

    async def test_member_without_mute_control_is_skipped(self, mock_mass: MagicMock) -> None:
        """A member without a mute control must not fail the whole group command."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        leader, leader_protocol = _attach(provider, "leader", "Leader")
        leader_protocol._attr_supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
        }
        leader_protocol._attr_group_members = [_proto("leader"), _proto("member")]
        member, member_protocol = _attach(provider, "member", "Member")
        member_protocol._attr_supported_features = {PlayerFeature.VOLUME_SET}
        _refresh(leader, member, force=False)
        leader_mute = AsyncMock()
        leader_protocol.volume_mute = leader_mute  # type: ignore[method-assign]

        await controller.cmd_group_volume_mute("leader", True)
        leader_mute.assert_awaited_once_with(True)


class TestGroupPlayerMuteRedirect:
    """A mute command on a group player is handled at group level."""

    def _setup(self, mock_mass: MagicMock) -> tuple[PlayerController, Player, Player]:
        """Build a controller with a group player holding a single mute capable member."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        group, group_protocol = _attach(provider, "group", "Group", player_type=PlayerType.GROUP)
        group_protocol._attr_supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
        }
        group_protocol._attr_group_members = ["member"]
        member, member_protocol = _attach(provider, "member", "Member")
        member_protocol._attr_supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
        }
        _refresh(group, member, force=False)
        return controller, group, member

    async def test_mute_on_group_player_is_forwarded_to_members(self, mock_mass: MagicMock) -> None:
        """A group player has no mute of its own, so the members must be muted instead."""
        controller, _group, member = self._setup(mock_mass)
        member_mute = AsyncMock()
        _own(member).volume_mute = member_mute  # type: ignore[method-assign]

        await controller.cmd_volume_mute("group", True)

        member_mute.assert_awaited_once_with(True)

    async def test_mute_on_group_player_without_own_mute_control(
        self, mock_mass: MagicMock
    ) -> None:
        """A group that has no mute control of its own must still mute its members."""
        controller, group, member = self._setup(mock_mass)
        _own(group)._attr_supported_features = {PlayerFeature.VOLUME_SET}
        _refresh(group, force=False)
        assert group._mute_control == PLAYER_CONTROL_NONE
        member_mute = AsyncMock()
        _own(member).volume_mute = member_mute  # type: ignore[method-assign]

        await controller.cmd_volume_mute("group", True)

        member_mute.assert_awaited_once_with(True)

    async def test_mute_on_group_player_without_mute_capable_members(
        self, mock_mass: MagicMock
    ) -> None:
        """A group whose members cannot mute must not raise, just like group mute itself."""
        controller, _group, member = self._setup(mock_mass)
        _own(member)._attr_supported_features = {PlayerFeature.VOLUME_SET}
        _refresh(member, force=False)

        await controller.cmd_volume_mute("group", True)


class TestGroupMuteOnNonGroupPlayer:
    """A group mute command works on any player, just like the group volume command."""

    def _setup(
        self, mock_mass: MagicMock, *members: str
    ) -> tuple[PlayerController, dict[str, Player]]:
        """Build a controller with a mute capable leader synced to the given members."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        players: dict[str, Player] = {}
        for player_id in ("leader", *members):
            player, protocol = _attach(provider, player_id, player_id.title())
            protocol._attr_supported_features = {
                PlayerFeature.VOLUME_SET,
                PlayerFeature.VOLUME_MUTE,
            }
            protocol._attr_volume_level = 50
            players[player_id] = player
        if members:
            # the leader is not listed as its own member here, so the tests also cover
            # that a sync leader is injected into its own final group_members
            _own(players["leader"])._attr_group_members = [_proto(member) for member in members]
        _refresh(*players.values(), force=False)
        return controller, players

    def _stub_mutes(self, players: dict[str, Player]) -> dict[str, AsyncMock]:
        """Replace the native mute command of every given player with a mock."""
        mutes: dict[str, AsyncMock] = {}
        for player_id, player in players.items():
            mutes[player_id] = AsyncMock()
            _own(player).volume_mute = mutes[player_id]  # type: ignore[method-assign]
        return mutes

    async def test_group_mute_on_synced_member_redirects_to_leader(
        self, mock_mass: MagicMock
    ) -> None:
        """A member of a sync group mutes the whole group through its sync leader."""
        controller, players = self._setup(mock_mass, "member")
        assert players["member"].synced_to == "leader"
        mutes = self._stub_mutes(players)

        await controller.cmd_group_volume_mute("member", True)

        mutes["leader"].assert_awaited_once_with(True)
        mutes["member"].assert_awaited_once_with(True)
        assert ATTR_MUTE_LOCK in players["member"].extra_data

    async def test_group_mute_on_sync_leader_mutes_the_leader_once(
        self, mock_mass: MagicMock
    ) -> None:
        """A sync leader is part of its own member list, so it must be muted only once."""
        controller, players = self._setup(mock_mass, "member")
        mutes = self._stub_mutes(players)

        await controller.cmd_group_volume_mute("leader", True)

        mutes["leader"].assert_awaited_once_with(True)
        mutes["member"].assert_awaited_once_with(True)

    async def test_group_mute_on_plain_player_mutes_that_player(self, mock_mass: MagicMock) -> None:
        """A player that is not grouped at all is muted as a normal player."""
        controller, players = self._setup(mock_mass)
        mutes = self._stub_mutes(players)

        await controller.cmd_group_volume_mute("leader", True)

        mutes["leader"].assert_awaited_once_with(True)

    async def test_group_mute_on_plain_player_without_mute_control(
        self, mock_mass: MagicMock
    ) -> None:
        """A plain player that cannot mute reports that, just like a normal mute command."""
        controller, players = self._setup(mock_mass)
        _own(players["leader"])._attr_supported_features = {PlayerFeature.VOLUME_SET}
        _refresh(players["leader"], force=False)

        with pytest.raises(UnsupportedFeaturedException):
            await controller.cmd_group_volume_mute("leader", True)

    async def test_group_unmute_on_synced_member_redirects_to_leader(
        self, mock_mass: MagicMock
    ) -> None:
        """Unmuting through a member clears the mute (and mute lock) of every group member."""
        controller, players = self._setup(mock_mass, "member")
        mutes = self._stub_mutes(players)
        players["member"].extra_data[ATTR_MUTE_LOCK] = True

        await controller.cmd_group_volume_mute("member", False)

        mutes["leader"].assert_awaited_once_with(False)
        mutes["member"].assert_awaited_once_with(False)
        assert ATTR_MUTE_LOCK not in players["member"].extra_data

    async def test_group_mute_locks_the_sync_leader_too(self, mock_mass: MagicMock) -> None:
        """A sync leader is as much part of the group as its members, so it is locked too."""
        controller, players = self._setup(mock_mass, "member")
        self._stub_mutes(players)

        await controller.cmd_group_volume_mute("leader", True)

        assert ATTR_MUTE_LOCK in players["leader"].extra_data
        assert ATTR_MUTE_LOCK in players["member"].extra_data

    async def test_group_volume_keeps_a_muted_sync_pair_muted(self, mock_mass: MagicMock) -> None:
        """A group volume change may not half-unmute a muted pair of directly synced players."""
        controller, players = self._setup(mock_mass, "member")
        self._stub_mutes(players)
        await controller.cmd_group_volume_mute("leader", True)
        # the mock devices do not act on the mute command, so reflect it in their state
        for player in players.values():
            protocol = _own(player)
            protocol._attr_volume_muted = True
            player.update_state(signal_event=False)
            protocol.volume_set = AsyncMock()  # type: ignore[method-assign]
        # re-stub so only the mute commands of the group volume change are counted
        mutes = self._stub_mutes(players)

        await controller.cmd_group_volume("leader", 30)

        for mute in mutes.values():
            mute.assert_not_awaited()


class TestMuteLockAfterUngroup:
    """
    Mute persistence across a volume-set command.

    A native mute is never lifted by a volume command, grouped or not. A fake mute
    lock is honored only while the player it belongs to is still grouped.
    """

    def _make_synced_pair(
        self, mock_mass: MagicMock, member_mute_control: str
    ) -> tuple[PlayerController, dict[str, Player]]:
        """
        Build a leader with one synced member.

        :param member_mute_control: Mute control to configure on both players.
        """
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub({CONF_MUTE_CONTROL: member_mute_control})
        )
        controller, players = _synced_pair(
            mock_mass, features={PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE}
        )
        return controller, players

    def _dissolve_group(self, players: dict[str, Player]) -> None:
        """Drop the sync group, the way a provider side topology change does."""
        _own(players["leader"])._attr_group_members = []
        for player in players.values():
            player.refresh_state(signal_event=False)

    async def test_fake_muted_player_follows_volume_again(self, mock_mass: MagicMock) -> None:
        """A fake muted player is no longer forced silent once its group is gone."""
        controller, players = self._make_synced_pair(mock_mass, PLAYER_CONTROL_FAKE)
        await controller.cmd_volume_mute("member", True)
        self._dissolve_group(players)

        await controller.cmd_volume_set("member", 70)

        players["member"].update_state()
        assert players["member"].volume_level == 70
        assert players["member"].volume_muted is False

    async def test_natively_muted_player_keeps_its_mute_after_ungroup(
        self, mock_mass: MagicMock
    ) -> None:
        """A natively muted player keeps its mute on a volume change, group gone or not."""
        controller, players = self._make_synced_pair(mock_mass, PLAYER_CONTROL_NATIVE)
        member_protocol = _own(players["member"])
        mute = AsyncMock(
            side_effect=lambda muted: setattr(member_protocol, "_attr_volume_muted", muted)
        )
        member_protocol.volume_mute = mute  # type: ignore[method-assign]
        await controller.cmd_volume_mute("member", True)
        self._dissolve_group(players)

        await controller.cmd_volume_set("member", 70)

        mute.assert_awaited_once_with(True)
        players["member"].update_state()
        assert players["member"].volume_level == 70
        assert players["member"].volume_muted is True

    async def test_still_grouped_player_keeps_its_lock(self, mock_mass: MagicMock) -> None:
        """A muted player that is still grouped stays silent on a volume change."""
        controller, players = self._make_synced_pair(mock_mass, PLAYER_CONTROL_FAKE)
        await controller.cmd_volume_mute("member", True)

        await controller.cmd_volume_set("member", 70)

        players["member"].update_state()
        assert players["member"].volume_level == 0
        assert players["member"].volume_muted is True

    async def test_volume_through_an_audio_protocol_follows_the_lock_of_the_player(
        self, mock_mass: MagicMock
    ) -> None:
        """A player whose volume goes through its audio protocol stays silent while locked."""
        controller, players = self._make_synced_pair(mock_mass, PLAYER_CONTROL_FAKE)
        member = players["member"]
        audio_provider = MockProvider("sendspin", instance_id="sendspin", mass=mock_mass)
        _, audio = attach_mock(
            audio_provider, "audio_member", "Member", role=ProtocolRole.AUDIO, player=member
        )
        audio._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE}
        audio._attr_volume_level = 50
        audio_volume_set = _report_volume_set(audio)
        config_stub = mock_mass.config.get_player_value.side_effect

        def _conf(player_id: str, key: str, default: object = None) -> object:
            if player_id == "member" and key == CONF_VOLUME_CONTROL:
                return "audio_member"
            return config_stub(player_id, key, default)

        mock_mass.config.get_player_value = MagicMock(side_effect=_conf)
        _refresh(member)
        assert member.volume_control == "audio_member"

        # the lock is earned while the player is grouped
        await controller.cmd_volume_mute("member", True)

        # while it holds the lock, a volume command is forced to 0 (stays silent)
        await controller._handle_cmd_volume_set("member", 70)
        assert audio_volume_set.await_args_list[-1].args == (0,)

        self._dissolve_group(players)
        await controller._handle_cmd_volume_set("member", 70)

        assert audio_volume_set.await_args_list[-1].args == (70,)
