"""Tests for provider/device.py — MA Player ↔ Yandex device mapper."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest

# Use the PlaybackState from conftest's mock enums
from music_assistant_models.enums import PlaybackState

if TYPE_CHECKING:
    from music_assistant_models.player import Player

from music_assistant.providers.yandex_smarthome.constants import (
    INSTANCE_CHANNEL,
    INSTANCE_INPUT_SOURCE,
    INSTANCE_MUTE,
    INSTANCE_ON,
    INSTANCE_PAUSE,
    INSTANCE_VOLUME,
    YANDEX_DEVICE_TYPE_MEDIA,
)
from music_assistant.providers.yandex_smarthome.device import (
    execute_capability_action,
    get_device_description,
    get_device_state,
    is_player_exposable,
    make_error_action_result,
    make_error_device_state,
    normalize_device_name,
)
from music_assistant.providers.yandex_smarthome.schema import (
    CapabilityAction,
    CapabilityActionState,
    YandexCapabilityType,
)


@dataclass
class MockDeviceInfo:
    """Mock device info for testing."""

    model: str = "Test Speaker"


@dataclass
class MockPlayerSource:
    """Minimal mock of music_assistant_models.player.PlayerSource."""

    id: str = "source_1"
    name: str = "Source 1"


@dataclass
class MockPlayer:
    """Minimal mock of music_assistant_models.player.Player."""

    player_id: str = "test_player_1"
    name: str = "Living Room Speaker"
    available: bool = True
    enabled: bool = True
    powered: bool | None = True
    playback_state: PlaybackState = PlaybackState.IDLE
    volume_level: int | None = 50
    volume_muted: bool | None = False
    synced_to: str | None = None
    device_info: MockDeviceInfo | None = None
    supported_features: set[str] = field(default_factory=set)
    source_list: list[MockPlayerSource] = field(default_factory=list)
    active_source: str | None = None
    group_members: list[str] = field(default_factory=list)
    group_volume: int | None = None
    group_volume_muted: bool | None = None


def _player(**kwargs: Any) -> Player:
    """Build a MockPlayer viewed through the real Player type."""
    return cast("Player", MockPlayer(**kwargs))


class MockPlayers:
    """Mock of mass.players controller."""

    def __init__(self) -> None:
        """Initialize mock players controller."""
        self.cmd_play = AsyncMock()
        self.cmd_stop = AsyncMock()
        self.cmd_pause = AsyncMock()
        self.cmd_power = AsyncMock()
        self.cmd_volume_set = AsyncMock()
        self.cmd_volume_mute = AsyncMock()
        self.cmd_group_volume = AsyncMock()
        self.cmd_group_volume_mute = AsyncMock()
        self.cmd_next_track = AsyncMock()
        self.cmd_previous_track = AsyncMock()
        self.select_source = AsyncMock()
        self._players: dict[str, Player] = {}

    def get_player(self, player_id: str) -> Player | None:
        """Return player by ID."""
        return self._players.get(player_id)


class MockPlayerQueues:
    """Mock of mass.player_queues controller."""

    def __init__(self) -> None:
        """Initialize mock player_queues controller."""
        self.play_media = AsyncMock()


@dataclass
class MockMass:
    """Mock MusicAssistant for testing."""

    players: MockPlayers = field(default_factory=MockPlayers)
    player_queues: MockPlayerQueues = field(default_factory=MockPlayerQueues)


# ---------------------------------------------------------------------------
# Tests: get_device_description
# ---------------------------------------------------------------------------


class TestGetDeviceDescription:
    """Tests for get_device_description."""

    def test_basic_description(self) -> None:
        """Test basic description without mute support."""
        player = _player()
        desc = get_device_description(player)
        assert desc.id == "test_player_1"
        assert desc.name == "Living Room Speaker"
        assert desc.type == YANDEX_DEVICE_TYPE_MEDIA
        # 4 base capabilities: on_off, volume, pause, channel (no mute without feature)
        assert len(desc.capabilities) == 4
        instances = {c.parameters.instance for c in desc.capabilities if c.parameters}
        assert INSTANCE_MUTE not in instances

    def test_description_with_mute(self) -> None:
        """Test description includes mute toggle when VOLUME_MUTE feature is set."""
        player = _player(supported_features={"volume_mute"})
        desc = get_device_description(player)
        assert len(desc.capabilities) == 5
        instances = {c.parameters.instance for c in desc.capabilities if c.parameters}
        assert INSTANCE_MUTE in instances

    def test_description_group_has_mute(self) -> None:
        """Group players always get mute toggle even without VOLUME_MUTE feature."""
        player = _player(group_members=["child1", "child2"])
        desc = get_device_description(player)
        assert len(desc.capabilities) == 5
        instances = {c.parameters.instance for c in desc.capabilities if c.parameters}
        assert INSTANCE_MUTE in instances

    def test_capability_types(self) -> None:
        """Test capability types."""
        player = _player()
        desc = get_device_description(player)
        types = [c.type for c in desc.capabilities]
        assert YandexCapabilityType.ON_OFF in types
        assert YandexCapabilityType.RANGE in types
        assert YandexCapabilityType.TOGGLE in types

    def test_volume_range_params(self) -> None:
        """Test volume range params."""
        player = _player()
        desc = get_device_description(player)
        range_cap = next(c for c in desc.capabilities if c.type == YandexCapabilityType.RANGE)
        assert range_cap.parameters is not None
        assert range_cap.parameters.instance == "volume"
        assert range_cap.parameters.range is not None
        assert range_cap.parameters.range.min == 0
        assert range_cap.parameters.range.max == 100

    def test_device_info_model(self) -> None:
        """Test device info model."""
        player = _player(device_info=MockDeviceInfo(model="KEF LS50"))
        desc = get_device_description(player)
        assert desc.device_info is not None
        assert desc.device_info.model == "KEF LS50"

    def test_device_info_default(self) -> None:
        """Test device info default."""
        player = _player()
        desc = get_device_description(player)
        assert desc.device_info is not None
        assert desc.device_info.model == "MA Player"

    def test_name_normalized(self) -> None:
        """Test that device name is normalized for Yandex."""
        player = _player(name="KEF-LS50 (Kitchen)")
        desc = get_device_description(player)
        assert desc.name == "KEF LS 50 Kitchen"


# ---------------------------------------------------------------------------
# Tests: normalize_device_name
# ---------------------------------------------------------------------------


class TestNormalizeDeviceName:
    """Tests for normalize_device_name."""

    def test_passthrough_clean_name(self) -> None:
        """Clean names pass through unchanged."""
        assert normalize_device_name("Living Room Speaker") == "Living Room Speaker"

    def test_russian_name(self) -> None:
        """Russian names pass through."""
        assert normalize_device_name("Колонка в гостиной") == "Колонка в гостиной"

    def test_strip_special_chars(self) -> None:
        """Special characters replaced with spaces."""
        assert normalize_device_name("KEF-LS50 (Kitchen)") == "KEF LS 50 Kitchen"

    def test_space_between_letters_and_digits(self) -> None:
        """Mandatory space between letters and digits."""
        assert normalize_device_name("Sonos5") == "Sonos 5"
        assert normalize_device_name("3колонка") == "3 колонка"  # noqa: RUF001

    def test_collapse_multiple_spaces(self) -> None:
        """Multiple spaces collapsed to one."""
        assert normalize_device_name("KEF   LS50") == "KEF LS 50"

    def test_strip_edges(self) -> None:
        """Leading/trailing spaces stripped."""
        assert normalize_device_name("  Speaker  ") == "Speaker"

    def test_mixed_russian_english(self) -> None:
        """Mixed Russian and English."""
        assert normalize_device_name("Колонка JBL5") == "Колонка JBL 5"

    def test_empty_fallback(self) -> None:
        """If normalization produces empty string, return original."""
        assert normalize_device_name("---") == "---"

    def test_digits_only(self) -> None:
        """Digit-only names preserved."""
        assert normalize_device_name("123") == "123"


class TestGetDeviceState:
    """Tests for get_device_state."""

    def test_idle_state(self) -> None:
        """Test idle state without mute support."""
        player = _player(playback_state=PlaybackState.IDLE, volume_level=30, volume_muted=False)
        state = get_device_state(player)
        assert state.id == "test_player_1"

        by_instance = {c.state.instance: c.state.value for c in state.capabilities}
        assert by_instance[INSTANCE_ON] is True  # powered on = "on" regardless of playback
        assert by_instance[INSTANCE_VOLUME] == 30
        assert INSTANCE_MUTE not in by_instance
        assert by_instance[INSTANCE_PAUSE] is True

    def test_idle_state_with_mute(self) -> None:
        """Test idle state with mute support."""
        player = _player(
            playback_state=PlaybackState.IDLE,
            volume_level=30,
            volume_muted=False,
            supported_features={"volume_mute"},
        )
        state = get_device_state(player)

        by_instance = {c.state.instance: c.state.value for c in state.capabilities}
        assert by_instance[INSTANCE_MUTE] is False

    def test_playing_state(self) -> None:
        """Test playing state."""
        player = _player(playback_state=PlaybackState.PLAYING, volume_level=75)
        state = get_device_state(player)

        by_instance = {c.state.instance: c.state.value for c in state.capabilities}
        assert by_instance[INSTANCE_ON] is True
        assert by_instance[INSTANCE_VOLUME] == 75
        assert by_instance[INSTANCE_PAUSE] is False

    def test_paused_state(self) -> None:
        """Test paused state."""
        player = _player(playback_state=PlaybackState.PAUSED, volume_level=50)
        state = get_device_state(player)

        by_instance = {c.state.instance: c.state.value for c in state.capabilities}
        assert by_instance[INSTANCE_ON] is True  # paused is still "on"
        assert by_instance[INSTANCE_PAUSE] is True

    def test_none_volume(self) -> None:
        """Test none volume."""
        player = _player(volume_level=None, volume_muted=None, supported_features={"volume_mute"})
        state = get_device_state(player)

        by_instance = {c.state.instance: c.state.value for c in state.capabilities}
        assert by_instance[INSTANCE_VOLUME] == 0
        assert by_instance[INSTANCE_MUTE] is False

    def test_group_state_includes_mute(self) -> None:
        """Group players should include mute state using group_volume_muted."""
        player = _player(
            playback_state=PlaybackState.PLAYING,
            volume_level=40,
            volume_muted=False,
            group_members=["c1", "c2"],
            group_volume=75,
            group_volume_muted=True,
        )
        state = get_device_state(player)

        by_instance = {c.state.instance: c.state.value for c in state.capabilities}
        assert by_instance[INSTANCE_VOLUME] == 75
        assert by_instance[INSTANCE_MUTE] is True


# ---------------------------------------------------------------------------
# Tests: execute_capability_action
# ---------------------------------------------------------------------------


class TestExecuteCapabilityAction:
    """Tests for execute_capability_action."""

    @pytest.mark.asyncio
    async def test_on_off_true_plays(self) -> None:
        """Test on off true plays."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type=YandexCapabilityType.ON_OFF,
            state=CapabilityActionState(instance="on", value=True),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.cmd_play.assert_awaited_once_with("p1")
        mass.players.cmd_power.assert_not_awaited()
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_on_off_false_stops(self) -> None:
        """Test on off false stops."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type=YandexCapabilityType.ON_OFF,
            state=CapabilityActionState(instance="on", value=False),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.cmd_stop.assert_awaited_once_with("p1")
        mass.players.cmd_power.assert_not_awaited()
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_on_powers_on_when_supported(self) -> None:
        """ON with power feature should power on then play."""
        mass = MockMass()
        player = _player(player_id="p1", supported_features={"power"})
        mass.players._players["p1"] = player
        action = CapabilityAction(
            type=YandexCapabilityType.ON_OFF,
            state=CapabilityActionState(instance="on", value=True),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.cmd_power.assert_awaited_once_with("p1", True)
        mass.players.cmd_play.assert_awaited_once_with("p1")
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_off_powers_off_when_supported(self) -> None:
        """OFF with power feature should stop then power off."""
        mass = MockMass()
        player = _player(player_id="p1", supported_features={"power"})
        mass.players._players["p1"] = player
        action = CapabilityAction(
            type=YandexCapabilityType.ON_OFF,
            state=CapabilityActionState(instance="on", value=False),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.cmd_stop.assert_awaited_once_with("p1")
        mass.players.cmd_power.assert_awaited_once_with("p1", False)
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_volume_absolute(self) -> None:
        """Test volume absolute."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type=YandexCapabilityType.RANGE,
            state=CapabilityActionState(instance="volume", value=65),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 65)
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_volume_relative_up(self) -> None:
        """Test volume relative up."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type=YandexCapabilityType.RANGE,
            state=CapabilityActionState(instance="volume", value=10, relative=True),
        )
        result = await execute_capability_action(mass, "p1", action, current_volume=50)
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 60)
        assert result.state.value == 60

    @pytest.mark.asyncio
    async def test_volume_relative_clamp_max(self) -> None:
        """Test volume relative clamp max."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type=YandexCapabilityType.RANGE,
            state=CapabilityActionState(instance="volume", value=20, relative=True),
        )
        result = await execute_capability_action(mass, "p1", action, current_volume=90)
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 100)
        assert result.state.value == 100

    @pytest.mark.asyncio
    async def test_volume_relative_clamp_min(self) -> None:
        """Test volume relative clamp min."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type=YandexCapabilityType.RANGE,
            state=CapabilityActionState(instance="volume", value=-20, relative=True),
        )
        result = await execute_capability_action(mass, "p1", action, current_volume=10)
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 0)
        assert result.state.value == 0

    @pytest.mark.asyncio
    async def test_mute_toggle(self) -> None:
        """Test mute toggle."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type=YandexCapabilityType.TOGGLE,
            state=CapabilityActionState(instance="mute", value=True),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.cmd_volume_mute.assert_awaited_once_with("p1", True)
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_mute_toggle_group(self) -> None:
        """Group mute should use cmd_group_volume_mute."""
        mass = MockMass()
        group = _player(player_id="grp", group_members=["c1", "c2"])
        mass.players._players["grp"] = group
        action = CapabilityAction(
            type=YandexCapabilityType.TOGGLE,
            state=CapabilityActionState(instance="mute", value=True),
        )
        result = await execute_capability_action(mass, "grp", action)
        assert result.state.action_result.status == "DONE"
        mass.players.cmd_group_volume_mute.assert_awaited_once_with("grp", True)

    @pytest.mark.asyncio
    async def test_volume_set_group(self) -> None:
        """Group volume should use cmd_group_volume."""
        mass = MockMass()
        group = _player(player_id="grp", group_members=["c1", "c2"])
        mass.players._players["grp"] = group
        action = CapabilityAction(
            type=YandexCapabilityType.RANGE,
            state=CapabilityActionState(instance="volume", value=70),
        )
        result = await execute_capability_action(mass, "grp", action)
        assert result.state.action_result.status == "DONE"
        mass.players.cmd_group_volume.assert_awaited_once_with("grp", 70)
        mass.players.cmd_volume_set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pause_true(self) -> None:
        """Test pause true."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type=YandexCapabilityType.TOGGLE,
            state=CapabilityActionState(instance="pause", value=True),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.cmd_pause.assert_awaited_once_with("p1")
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_pause_false_plays(self) -> None:
        """Test pause false plays."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type=YandexCapabilityType.TOGGLE,
            state=CapabilityActionState(instance="pause", value=False),
        )
        await execute_capability_action(mass, "p1", action)
        mass.players.cmd_play.assert_awaited_once_with("p1")

    @pytest.mark.asyncio
    async def test_unknown_capability_returns_error(self) -> None:
        """Test unknown capability returns error."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type="devices.capabilities.unknown",
            state=CapabilityActionState(instance="foo", value=42),
        )
        result = await execute_capability_action(mass, "p1", action)
        assert result.state.action_result.status == "ERROR"
        assert result.state.action_result.error_code == "INVALID_ACTION"

    @pytest.mark.asyncio
    async def test_command_exception_returns_error(self) -> None:
        """Test command exception returns error."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        mass.players.cmd_play.side_effect = RuntimeError("Connection lost")
        action = CapabilityAction(
            type=YandexCapabilityType.ON_OFF,
            state=CapabilityActionState(instance="on", value=True),
        )
        result = await execute_capability_action(mass, "p1", action)
        assert result.state.action_result.status == "ERROR"
        assert result.state.action_result.error_code == "INTERNAL_ERROR"

    @pytest.mark.asyncio
    async def test_on_off_non_bool_value_returns_invalid_action(self) -> None:
        """ON_OFF with non-bool value (e.g. string 'false') must not power on."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type=YandexCapabilityType.ON_OFF,
            state=CapabilityActionState(instance="on", value="false"),
        )
        result = await execute_capability_action(mass, "p1", action)
        assert result.state.action_result.status == "ERROR"
        assert result.state.action_result.error_code == "INVALID_ACTION"
        mass.players.cmd_play.assert_not_awaited()
        mass.players.cmd_stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mute_non_bool_value_returns_invalid_action(self) -> None:
        """Mute toggle with non-bool value must not call cmd_volume_mute."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type=YandexCapabilityType.TOGGLE,
            state=CapabilityActionState(instance="mute", value="false"),
        )
        result = await execute_capability_action(mass, "p1", action)
        assert result.state.action_result.status == "ERROR"
        assert result.state.action_result.error_code == "INVALID_ACTION"
        mass.players.cmd_volume_mute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pause_non_bool_value_returns_invalid_action(self) -> None:
        """Pause toggle with non-bool value must not call cmd_pause/cmd_play."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type=YandexCapabilityType.TOGGLE,
            state=CapabilityActionState(instance="pause", value="true"),
        )
        result = await execute_capability_action(mass, "p1", action)
        assert result.state.action_result.status == "ERROR"
        assert result.state.action_result.error_code == "INVALID_ACTION"
        mass.players.cmd_pause.assert_not_awaited()
        mass.players.cmd_play.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_volume_range_bool_value_returns_invalid_action(self) -> None:
        """Volume RANGE with bool value must not silently set volume to 0/1."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1", volume_level=50)
        action = CapabilityAction(
            type=YandexCapabilityType.RANGE,
            state=CapabilityActionState(instance="volume", value=True),
        )
        result = await execute_capability_action(mass, "p1", action, current_volume=50)
        assert result.state.action_result.status == "ERROR"
        assert result.state.action_result.error_code == "INVALID_ACTION"
        mass.players.cmd_volume_set.assert_not_awaited()
        mass.players.cmd_group_volume.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_channel_range_bool_value_returns_invalid_action(self) -> None:
        """Channel RANGE with bool value must not silently skip tracks."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type=YandexCapabilityType.RANGE,
            state=CapabilityActionState(instance="channel", value=True, relative=True),
        )
        result = await execute_capability_action(mass, "p1", action)
        assert result.state.action_result.status == "ERROR"
        assert result.state.action_result.error_code == "INVALID_ACTION"
        mass.players.cmd_next_track.assert_not_awaited()
        mass.players.cmd_previous_track.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_player_returns_device_unreachable(self) -> None:
        """Player not found should return DEVICE_UNREACHABLE."""
        mass = MockMass()
        action = CapabilityAction(
            type=YandexCapabilityType.ON_OFF,
            state=CapabilityActionState(instance="on", value=True),
        )
        result = await execute_capability_action(mass, "nonexistent", action)
        assert result.state.action_result.status == "ERROR"
        assert result.state.action_result.error_code == "DEVICE_UNREACHABLE"


# ---------------------------------------------------------------------------
# Tests: is_player_exposable
# ---------------------------------------------------------------------------


class TestIsPlayerExposable:
    """Tests for is_player_exposable."""

    def test_normal_player(self) -> None:
        """Test normal player."""
        assert is_player_exposable(_player()) is True

    def test_unavailable(self) -> None:
        """Test unavailable."""
        assert is_player_exposable(_player(available=False)) is False

    def test_disabled(self) -> None:
        """Test disabled."""
        assert is_player_exposable(_player(enabled=False)) is False

    def test_synced_to_another(self) -> None:
        """Test synced to another."""
        assert is_player_exposable(_player(synced_to="other_player")) is False


# ---------------------------------------------------------------------------
# Tests: error helpers
# ---------------------------------------------------------------------------


class TestErrorHelpers:
    """Tests for error helper functions."""

    def test_make_error_device_state(self) -> None:
        """Test make error device state."""
        state = make_error_device_state("p1")
        assert state.id == "p1"
        assert state.error_code == "DEVICE_UNREACHABLE"
        assert state.capabilities == []

    def test_make_error_action_result(self) -> None:
        """Test make error action result."""
        actions = [
            CapabilityAction(
                type=YandexCapabilityType.ON_OFF,
                state=CapabilityActionState(instance="on", value=True),
            ),
            CapabilityAction(
                type=YandexCapabilityType.RANGE,
                state=CapabilityActionState(instance="volume", value=50),
            ),
        ]
        results = make_error_action_result("p1", actions)
        assert len(results) == 2
        assert all(r.state.action_result.status == "ERROR" for r in results)
        assert all(r.state.action_result.error_code == "DEVICE_UNREACHABLE" for r in results)


# ---------------------------------------------------------------------------
# Tests: channel capability (next/previous track)
# ---------------------------------------------------------------------------


class TestChannelCapability:
    """Tests for channel capability handling."""

    def test_channel_in_description(self) -> None:
        """Channel capability should always be present in device description."""
        player = _player()
        desc = get_device_description(player)
        channel_caps = [
            c
            for c in desc.capabilities
            if c.type == YandexCapabilityType.RANGE
            and c.parameters
            and c.parameters.instance == INSTANCE_CHANNEL
        ]
        assert len(channel_caps) == 1
        params = channel_caps[0].parameters
        assert params is not None
        assert params.random_access is False
        assert params.range is not None
        assert params.range.min == 0
        assert params.range.max == 999

    def test_channel_state_always_zero(self) -> None:
        """Channel state should always report value 0."""
        player = _player(playback_state=PlaybackState.PLAYING)
        state = get_device_state(player)
        channel_states = [c for c in state.capabilities if c.state.instance == INSTANCE_CHANNEL]
        assert len(channel_states) == 1
        assert channel_states[0].state.value == 0

    @pytest.mark.asyncio
    async def test_channel_relative_positive_next_track(self) -> None:
        """Relative +1 channel → cmd_next_track."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type=YandexCapabilityType.RANGE,
            state=CapabilityActionState(instance="channel", value=1, relative=True),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.cmd_next_track.assert_awaited_once_with("p1")
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_channel_relative_negative_prev_track(self) -> None:
        """Relative -1 channel → cmd_previous_track."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type=YandexCapabilityType.RANGE,
            state=CapabilityActionState(instance="channel", value=-1, relative=True),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.cmd_previous_track.assert_awaited_once_with("p1")
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_channel_non_relative_ignored(self) -> None:
        """Non-relative channel set is a no-op (returns DONE)."""
        mass = MockMass()
        mass.players._players["p1"] = _player(player_id="p1")
        action = CapabilityAction(
            type=YandexCapabilityType.RANGE,
            state=CapabilityActionState(instance="channel", value=5, relative=False),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.cmd_next_track.assert_not_awaited()
        mass.players.cmd_previous_track.assert_not_awaited()
        assert result.state.action_result.status == "DONE"


# ---------------------------------------------------------------------------
# Tests: input_source capability (mode/input_source)
# ---------------------------------------------------------------------------


class TestInputSourceCapability:
    """Tests for input source capability handling."""

    def test_no_source_list_no_mode_cap(self) -> None:
        """Player without source_list should not have mode capability."""
        player = _player(source_list=[])
        desc = get_device_description(player)
        mode_caps = [c for c in desc.capabilities if c.type == YandexCapabilityType.MODE]
        assert len(mode_caps) == 0

    def test_with_sources_has_mode_cap(self) -> None:
        """Player with source_list should have mode(input_source) capability."""
        sources = [
            MockPlayerSource(id="hdmi1", name="HDMI 1"),
            MockPlayerSource(id="optical", name="Optical"),
        ]
        player = _player(source_list=sources, supported_features={"select_source"})
        desc = get_device_description(player)
        mode_caps = [c for c in desc.capabilities if c.type == YandexCapabilityType.MODE]
        assert len(mode_caps) == 1
        params = mode_caps[0].parameters
        assert params is not None
        assert params.instance == INSTANCE_INPUT_SOURCE
        assert params.modes is not None
        assert len(params.modes) == 2
        assert params.modes[0].value == "one"
        assert params.modes[1].value == "two"

    def test_max_10_sources(self) -> None:
        """Only the first 10 sources should be mapped."""
        sources = [MockPlayerSource(id=f"s{i}", name=f"Source {i}") for i in range(15)]
        player = _player(source_list=sources, supported_features={"select_source"})
        desc = get_device_description(player)
        mode_caps = [c for c in desc.capabilities if c.type == YandexCapabilityType.MODE]
        params = mode_caps[0].parameters
        assert params is not None
        assert params.modes is not None
        assert len(params.modes) == 10

    def test_state_with_active_source(self) -> None:
        """State should report current source as mode value."""
        sources = [
            MockPlayerSource(id="hdmi1", name="HDMI 1"),
            MockPlayerSource(id="optical", name="Optical"),
        ]
        player = _player(
            source_list=sources,
            active_source="Optical",
            playback_state=PlaybackState.PLAYING,
            supported_features={"select_source"},
        )
        state = get_device_state(player)
        mode_states = [c for c in state.capabilities if c.state.instance == INSTANCE_INPUT_SOURCE]
        assert len(mode_states) == 1
        assert mode_states[0].state.value == "two"  # index 1 → "two"

    def test_state_no_active_source(self) -> None:
        """No active source → no input_source state reported."""
        sources = [MockPlayerSource(id="hdmi1", name="HDMI 1")]
        player = _player(
            source_list=sources, active_source=None, supported_features={"select_source"}
        )
        state = get_device_state(player)
        mode_states = [c for c in state.capabilities if c.state.instance == INSTANCE_INPUT_SOURCE]
        assert len(mode_states) == 0

    @pytest.mark.asyncio
    async def test_select_source_action(self) -> None:
        """Mode action should call select_source with resolved source id."""
        sources = [
            MockPlayerSource(id="hdmi1", name="HDMI 1"),
            MockPlayerSource(id="optical", name="Optical"),
        ]
        player = _player(player_id="p1", source_list=sources, supported_features={"select_source"})
        mass = MockMass()
        mass.players._players["p1"] = player

        action = CapabilityAction(
            type=YandexCapabilityType.MODE,
            state=CapabilityActionState(instance="input_source", value="two"),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.select_source.assert_awaited_once_with("p1", "optical")
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_unknown_source_mode_returns_error(self) -> None:
        """Invalid mode value should return INVALID_ACTION error."""
        player = _player(player_id="p1", source_list=[])
        mass = MockMass()
        mass.players._players["p1"] = player

        action = CapabilityAction(
            type=YandexCapabilityType.MODE,
            state=CapabilityActionState(instance="input_source", value="five"),
        )
        result = await execute_capability_action(mass, "p1", action)
        assert result.state.action_result.status == "ERROR"
        assert result.state.action_result.error_code == "INVALID_ACTION"


# ---------------------------------------------------------------------------
# Tests: input_source playlist sources (mode/input_source backed by playlists)
# ---------------------------------------------------------------------------


class TestPlaylistInputSources:
    """Tests for playlist-backed input_source modes."""

    def test_playlists_only_register_mode_cap(self) -> None:
        """Player with no native sources but configured playlists gets mode cap."""
        player = _player(source_list=[])
        playlist_uris = ["library://playlist/1", "library://playlist/2", "library://playlist/3"]
        desc = get_device_description(player, playlist_uris=playlist_uris)
        mode_caps = [c for c in desc.capabilities if c.type == YandexCapabilityType.MODE]
        assert len(mode_caps) == 1
        params = mode_caps[0].parameters
        assert params is not None
        modes = params.modes
        assert modes is not None
        assert [m.value for m in modes] == ["one", "two", "three"]

    def test_native_then_playlists_capped_at_10(self) -> None:
        """5 native sources + 7 playlists → 10 combined modes, native first."""
        sources = [MockPlayerSource(id=f"s{i}", name=f"Source {i}") for i in range(5)]
        playlist_uris = [f"library://playlist/{i}" for i in range(7)]
        player = _player(source_list=sources, supported_features={"select_source"})
        desc = get_device_description(player, playlist_uris=playlist_uris)
        mode_caps = [c for c in desc.capabilities if c.type == YandexCapabilityType.MODE]
        assert len(mode_caps) == 1
        params = mode_caps[0].parameters
        assert params is not None
        assert params.modes is not None
        assert len(params.modes) == 10

    def test_native_full_ignores_playlists(self) -> None:
        """If native sources already fill all 10 slots, playlists are ignored."""
        sources = [MockPlayerSource(id=f"s{i}", name=f"Source {i}") for i in range(10)]
        playlist_uris = ["library://playlist/extra"]
        player = _player(source_list=sources, supported_features={"select_source"})
        desc = get_device_description(player, playlist_uris=playlist_uris)
        mode_caps = [c for c in desc.capabilities if c.type == YandexCapabilityType.MODE]
        assert len(mode_caps) == 1
        params = mode_caps[0].parameters
        assert params is not None
        assert params.modes is not None
        assert len(params.modes) == 10

    def test_state_with_native_active_in_combined_mode(self) -> None:
        """Native active source still reports correct state when playlists also configured."""
        sources = [
            MockPlayerSource(id="hdmi1", name="HDMI 1"),
            MockPlayerSource(id="optical", name="Optical"),
        ]
        player = _player(
            source_list=sources,
            active_source="Optical",
            playback_state=PlaybackState.PLAYING,
            supported_features={"select_source"},
        )
        state = get_device_state(player, playlist_uris=["library://playlist/x"])
        mode_states = [c for c in state.capabilities if c.state.instance == INSTANCE_INPUT_SOURCE]
        assert len(mode_states) == 1
        assert mode_states[0].state.value == "two"

    @pytest.mark.asyncio
    async def test_native_action_routes_to_select_source(self) -> None:
        """Native slot value resolves to mass.players.select_source, not play_media."""
        sources = [MockPlayerSource(id="hdmi1", name="HDMI 1")]
        player = _player(player_id="p1", source_list=sources, supported_features={"select_source"})
        mass = MockMass()
        mass.players._players["p1"] = player

        action = CapabilityAction(
            type=YandexCapabilityType.MODE,
            state=CapabilityActionState(instance="input_source", value="one"),
        )
        result = await execute_capability_action(
            mass, "p1", action, playlist_uris=["library://playlist/x"]
        )
        mass.players.select_source.assert_awaited_once_with("p1", "hdmi1")
        mass.player_queues.play_media.assert_not_awaited()
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_playlist_action_calls_play_media(self) -> None:
        """Playlist slot triggers player_queues.play_media with URI."""
        player = _player(player_id="p1", source_list=[], powered=True)
        mass = MockMass()
        mass.players._players["p1"] = player
        uris = ["library://playlist/jazz", "library://playlist/rock"]

        action = CapabilityAction(
            type=YandexCapabilityType.MODE,
            state=CapabilityActionState(instance="input_source", value="two"),
        )
        result = await execute_capability_action(mass, "p1", action, playlist_uris=uris)
        mass.player_queues.play_media.assert_awaited_once_with(
            queue_id="p1", media="library://playlist/rock"
        )
        mass.players.select_source.assert_not_awaited()
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_playlist_action_powers_on_if_off(self) -> None:
        """Player with power feature off should be powered on before playback."""
        player = _player(
            player_id="p1",
            source_list=[],
            powered=False,
            supported_features={"power"},
        )
        mass = MockMass()
        mass.players._players["p1"] = player

        action = CapabilityAction(
            type=YandexCapabilityType.MODE,
            state=CapabilityActionState(instance="input_source", value="one"),
        )
        result = await execute_capability_action(
            mass, "p1", action, playlist_uris=["library://playlist/jazz"]
        )
        mass.players.cmd_power.assert_awaited_once_with("p1", True)
        mass.player_queues.play_media.assert_awaited_once_with(
            queue_id="p1", media="library://playlist/jazz"
        )
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_playlist_action_skips_power_when_already_on(self) -> None:
        """Powered player skips cmd_power but still plays media."""
        player = _player(
            player_id="p1",
            source_list=[],
            powered=True,
            supported_features={"power"},
        )
        mass = MockMass()
        mass.players._players["p1"] = player

        action = CapabilityAction(
            type=YandexCapabilityType.MODE,
            state=CapabilityActionState(instance="input_source", value="one"),
        )
        await execute_capability_action(
            mass, "p1", action, playlist_uris=["library://playlist/jazz"]
        )
        mass.players.cmd_power.assert_not_awaited()
        mass.player_queues.play_media.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_slot_past_combined_returns_error(self) -> None:
        """Mode value beyond combined native+playlists → INVALID_ACTION."""
        player = _player(player_id="p1", source_list=[])
        mass = MockMass()
        mass.players._players["p1"] = player

        action = CapabilityAction(
            type=YandexCapabilityType.MODE,
            state=CapabilityActionState(instance="input_source", value="three"),
        )
        result = await execute_capability_action(
            mass, "p1", action, playlist_uris=["library://playlist/jazz"]
        )
        mass.player_queues.play_media.assert_not_awaited()
        assert result.state.action_result.status == "ERROR"
        assert result.state.action_result.error_code == "INVALID_ACTION"


# ---------------------------------------------------------------------------
# Tests: player filter (exposed_ids)
# ---------------------------------------------------------------------------


class TestPlayerFilter:
    """Tests for player filtering with exposed_ids."""

    def test_no_filter_exposes_all(self) -> None:
        """Without exposed_ids, all valid players are exposed."""
        assert is_player_exposable(_player()) is True

    def test_filter_includes_player(self) -> None:
        """Player in the filter set is exposed."""
        assert is_player_exposable(_player(player_id="p1"), exposed_ids={"p1", "p2"}) is True

    def test_filter_excludes_player(self) -> None:
        """Player not in the filter set is NOT exposed."""
        assert is_player_exposable(_player(player_id="p3"), exposed_ids={"p1", "p2"}) is False

    def test_empty_filter_exposes_all(self) -> None:
        """Empty set filter should expose all players (same as None)."""
        assert is_player_exposable(_player(player_id="p1"), exposed_ids=set()) is True

    def test_filter_still_checks_available(self) -> None:
        """Even in filter, unavailable players are not exposed."""
        assert (
            is_player_exposable(_player(player_id="p1", available=False), exposed_ids={"p1"})
            is False
        )
