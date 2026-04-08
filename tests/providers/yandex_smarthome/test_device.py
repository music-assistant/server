"""Tests for provider/device.py — MA Player ↔ Yandex device mapper."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest

# Use the PlaybackState from conftest's mock enums
from music_assistant_models.enums import PlaybackState

from provider.constants import (
    INSTANCE_CHANNEL,
    INSTANCE_INPUT_SOURCE,
    INSTANCE_MUTE,
    INSTANCE_ON,
    INSTANCE_PAUSE,
    INSTANCE_VOLUME,
    YANDEX_DEVICE_TYPE_RECEIVER,
)
from provider.device import (
    execute_capability_action,
    get_device_description,
    get_device_state,
    is_player_exposable,
    make_error_action_result,
    make_error_device_state,
)
from provider.schema import (
    CapabilityAction,
    CapabilityActionState,
    YandexCapabilityType,
)


@dataclass
class MockDeviceInfo:
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


class MockPlayers:
    """Mock of mass.players controller."""

    def __init__(self) -> None:
        self.cmd_play = AsyncMock()
        self.cmd_stop = AsyncMock()
        self.cmd_pause = AsyncMock()
        self.cmd_power = AsyncMock()
        self.cmd_volume_set = AsyncMock()
        self.cmd_volume_mute = AsyncMock()
        self.cmd_next_track = AsyncMock()
        self.cmd_previous_track = AsyncMock()
        self.select_source = AsyncMock()
        self._players: dict[str, MockPlayer] = {}

    def get_player(self, player_id: str) -> MockPlayer | None:
        return self._players.get(player_id)


@dataclass
class MockMass:
    players: MockPlayers = field(default_factory=MockPlayers)


# ---------------------------------------------------------------------------
# Tests: get_device_description
# ---------------------------------------------------------------------------


class TestGetDeviceDescription:
    def test_basic_description(self) -> None:
        player = MockPlayer()
        desc = get_device_description(player)
        assert desc.id == "test_player_1"
        assert desc.name == "Living Room Speaker"
        assert desc.type == YANDEX_DEVICE_TYPE_RECEIVER
        # 5 base capabilities: on_off, volume, mute, pause, channel
        assert len(desc.capabilities) == 5

    def test_capability_types(self) -> None:
        player = MockPlayer()
        desc = get_device_description(player)
        types = [c.type for c in desc.capabilities]
        assert YandexCapabilityType.ON_OFF in types
        assert YandexCapabilityType.RANGE in types
        assert YandexCapabilityType.TOGGLE in types

    def test_volume_range_params(self) -> None:
        player = MockPlayer()
        desc = get_device_description(player)
        range_cap = next(c for c in desc.capabilities if c.type == YandexCapabilityType.RANGE)
        assert range_cap.parameters is not None
        assert range_cap.parameters.instance == "volume"
        assert range_cap.parameters.range is not None
        assert range_cap.parameters.range.min == 0
        assert range_cap.parameters.range.max == 100

    def test_device_info_model(self) -> None:
        player = MockPlayer(device_info=MockDeviceInfo(model="KEF LS50"))
        desc = get_device_description(player)
        assert desc.device_info is not None
        assert desc.device_info.model == "KEF LS50"

    def test_device_info_default(self) -> None:
        player = MockPlayer()
        desc = get_device_description(player)
        assert desc.device_info is not None
        assert desc.device_info.model == "MA Player"


# ---------------------------------------------------------------------------
# Tests: get_device_state
# ---------------------------------------------------------------------------


class TestGetDeviceState:
    def test_idle_state(self) -> None:
        player = MockPlayer(playback_state=PlaybackState.IDLE, volume_level=30, volume_muted=False)
        state = get_device_state(player)
        assert state.id == "test_player_1"

        by_instance = {c.state.instance: c.state.value for c in state.capabilities}
        assert by_instance[INSTANCE_ON] is True  # always on while available
        assert by_instance[INSTANCE_VOLUME] == 30
        assert by_instance[INSTANCE_MUTE] is False
        assert by_instance[INSTANCE_PAUSE] is False

    def test_playing_state(self) -> None:
        player = MockPlayer(playback_state=PlaybackState.PLAYING, volume_level=75)
        state = get_device_state(player)

        by_instance = {c.state.instance: c.state.value for c in state.capabilities}
        assert by_instance[INSTANCE_ON] is True
        assert by_instance[INSTANCE_VOLUME] == 75
        assert by_instance[INSTANCE_PAUSE] is False

    def test_paused_state(self) -> None:
        player = MockPlayer(playback_state=PlaybackState.PAUSED, volume_level=50)
        state = get_device_state(player)

        by_instance = {c.state.instance: c.state.value for c in state.capabilities}
        assert by_instance[INSTANCE_ON] is True  # paused is still "on"
        assert by_instance[INSTANCE_PAUSE] is True

    def test_none_volume(self) -> None:
        player = MockPlayer(volume_level=None, volume_muted=None)
        state = get_device_state(player)

        by_instance = {c.state.instance: c.state.value for c in state.capabilities}
        assert by_instance[INSTANCE_VOLUME] == 0
        assert by_instance[INSTANCE_MUTE] is False


# ---------------------------------------------------------------------------
# Tests: execute_capability_action
# ---------------------------------------------------------------------------


class TestExecuteCapabilityAction:
    @pytest.mark.asyncio
    async def test_on_off_true_plays(self) -> None:
        mass = MockMass()
        action = CapabilityAction(
            type=YandexCapabilityType.ON_OFF,
            state=CapabilityActionState(instance="on", value=True),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.cmd_play.assert_awaited_once_with("p1")
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_on_off_false_stops(self) -> None:
        mass = MockMass()
        action = CapabilityAction(
            type=YandexCapabilityType.ON_OFF,
            state=CapabilityActionState(instance="on", value=False),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.cmd_stop.assert_awaited_once_with("p1")
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_volume_absolute(self) -> None:
        mass = MockMass()
        action = CapabilityAction(
            type=YandexCapabilityType.RANGE,
            state=CapabilityActionState(instance="volume", value=65),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 65)
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_volume_relative_up(self) -> None:
        mass = MockMass()
        action = CapabilityAction(
            type=YandexCapabilityType.RANGE,
            state=CapabilityActionState(instance="volume", value=10, relative=True),
        )
        result = await execute_capability_action(mass, "p1", action, current_volume=50)
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 60)
        assert result.state.value == 60

    @pytest.mark.asyncio
    async def test_volume_relative_clamp_max(self) -> None:
        mass = MockMass()
        action = CapabilityAction(
            type=YandexCapabilityType.RANGE,
            state=CapabilityActionState(instance="volume", value=20, relative=True),
        )
        result = await execute_capability_action(mass, "p1", action, current_volume=90)
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 100)
        assert result.state.value == 100

    @pytest.mark.asyncio
    async def test_volume_relative_clamp_min(self) -> None:
        mass = MockMass()
        action = CapabilityAction(
            type=YandexCapabilityType.RANGE,
            state=CapabilityActionState(instance="volume", value=-20, relative=True),
        )
        result = await execute_capability_action(mass, "p1", action, current_volume=10)
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 0)
        assert result.state.value == 0

    @pytest.mark.asyncio
    async def test_mute_toggle(self) -> None:
        mass = MockMass()
        action = CapabilityAction(
            type=YandexCapabilityType.TOGGLE,
            state=CapabilityActionState(instance="mute", value=True),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.cmd_volume_mute.assert_awaited_once_with("p1", True)
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_pause_true(self) -> None:
        mass = MockMass()
        action = CapabilityAction(
            type=YandexCapabilityType.TOGGLE,
            state=CapabilityActionState(instance="pause", value=True),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.cmd_pause.assert_awaited_once_with("p1")
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_pause_false_plays(self) -> None:
        mass = MockMass()
        action = CapabilityAction(
            type=YandexCapabilityType.TOGGLE,
            state=CapabilityActionState(instance="pause", value=False),
        )
        await execute_capability_action(mass, "p1", action)
        mass.players.cmd_play.assert_awaited_once_with("p1")

    @pytest.mark.asyncio
    async def test_unknown_capability_returns_error(self) -> None:
        mass = MockMass()
        action = CapabilityAction(
            type="devices.capabilities.unknown",
            state=CapabilityActionState(instance="foo", value=42),
        )
        result = await execute_capability_action(mass, "p1", action)
        assert result.state.action_result.status == "ERROR"
        assert result.state.action_result.error_code == "INVALID_ACTION"

    @pytest.mark.asyncio
    async def test_command_exception_returns_error(self) -> None:
        mass = MockMass()
        mass.players.cmd_play.side_effect = RuntimeError("Connection lost")
        action = CapabilityAction(
            type=YandexCapabilityType.ON_OFF,
            state=CapabilityActionState(instance="on", value=True),
        )
        result = await execute_capability_action(mass, "p1", action)
        assert result.state.action_result.status == "ERROR"
        assert result.state.action_result.error_code == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Tests: is_player_exposable
# ---------------------------------------------------------------------------


class TestIsPlayerExposable:
    def test_normal_player(self) -> None:
        assert is_player_exposable(MockPlayer()) is True

    def test_unavailable(self) -> None:
        assert is_player_exposable(MockPlayer(available=False)) is False

    def test_disabled(self) -> None:
        assert is_player_exposable(MockPlayer(enabled=False)) is False

    def test_synced_to_another(self) -> None:
        assert is_player_exposable(MockPlayer(synced_to="other_player")) is False


# ---------------------------------------------------------------------------
# Tests: error helpers
# ---------------------------------------------------------------------------


class TestErrorHelpers:
    def test_make_error_device_state(self) -> None:
        state = make_error_device_state("p1")
        assert state.id == "p1"
        assert state.error_code == "DEVICE_UNREACHABLE"
        assert state.capabilities == []

    def test_make_error_action_result(self) -> None:
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
    def test_channel_in_description(self) -> None:
        """Channel capability should always be present in device description."""
        player = MockPlayer()
        desc = get_device_description(player)
        channel_caps = [
            c
            for c in desc.capabilities
            if c.type == YandexCapabilityType.RANGE
            and c.parameters
            and c.parameters.instance == INSTANCE_CHANNEL
        ]
        assert len(channel_caps) == 1
        cap = channel_caps[0]
        assert cap.parameters.random_access is False
        assert cap.parameters.range is not None
        assert cap.parameters.range.min == 0
        assert cap.parameters.range.max == 999

    def test_channel_state_always_zero(self) -> None:
        """Channel state should always report value 0."""
        player = MockPlayer(playback_state=PlaybackState.PLAYING)
        state = get_device_state(player)
        channel_states = [c for c in state.capabilities if c.state.instance == INSTANCE_CHANNEL]
        assert len(channel_states) == 1
        assert channel_states[0].state.value == 0

    @pytest.mark.asyncio
    async def test_channel_relative_positive_next_track(self) -> None:
        """Relative +1 channel → cmd_next_track."""
        mass = MockMass()
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
    def test_no_source_list_no_mode_cap(self) -> None:
        """Player without source_list should not have mode capability."""
        player = MockPlayer(source_list=[])
        desc = get_device_description(player)
        mode_caps = [c for c in desc.capabilities if c.type == YandexCapabilityType.MODE]
        assert len(mode_caps) == 0

    def test_with_sources_has_mode_cap(self) -> None:
        """Player with source_list should have mode(input_source) capability."""
        sources = [
            MockPlayerSource(id="hdmi1", name="HDMI 1"),
            MockPlayerSource(id="optical", name="Optical"),
        ]
        player = MockPlayer(source_list=sources, supported_features={"select_source"})
        desc = get_device_description(player)
        mode_caps = [c for c in desc.capabilities if c.type == YandexCapabilityType.MODE]
        assert len(mode_caps) == 1
        cap = mode_caps[0]
        assert cap.parameters.instance == INSTANCE_INPUT_SOURCE
        assert cap.parameters.modes is not None
        assert len(cap.parameters.modes) == 2
        assert cap.parameters.modes[0].value == "one"
        assert cap.parameters.modes[1].value == "two"

    def test_max_10_sources(self) -> None:
        """Only the first 10 sources should be mapped."""
        sources = [MockPlayerSource(id=f"s{i}", name=f"Source {i}") for i in range(15)]
        player = MockPlayer(source_list=sources, supported_features={"select_source"})
        desc = get_device_description(player)
        mode_caps = [c for c in desc.capabilities if c.type == YandexCapabilityType.MODE]
        assert len(mode_caps[0].parameters.modes) == 10

    def test_state_with_active_source(self) -> None:
        """State should report current source as mode value."""
        sources = [
            MockPlayerSource(id="hdmi1", name="HDMI 1"),
            MockPlayerSource(id="optical", name="Optical"),
        ]
        player = MockPlayer(
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
        player = MockPlayer(
            source_list=sources, active_source=None, supported_features={"select_source"}
        )
        state = get_device_state(player)
        mode_states = [c for c in state.capabilities if c.state.instance == INSTANCE_INPUT_SOURCE]
        assert len(mode_states) == 0

    @pytest.mark.asyncio
    async def test_select_source_action(self) -> None:
        """Mode action should call select_source with resolved source name."""
        sources = [
            MockPlayerSource(id="hdmi1", name="HDMI 1"),
            MockPlayerSource(id="optical", name="Optical"),
        ]
        player = MockPlayer(
            player_id="p1", source_list=sources, supported_features={"select_source"}
        )
        mass = MockMass()
        mass.players._players["p1"] = player

        action = CapabilityAction(
            type=YandexCapabilityType.MODE,
            state=CapabilityActionState(instance="input_source", value="two"),
        )
        result = await execute_capability_action(mass, "p1", action)
        mass.players.select_source.assert_awaited_once_with("p1", "Optical")
        assert result.state.action_result.status == "DONE"

    @pytest.mark.asyncio
    async def test_unknown_source_mode_returns_error(self) -> None:
        """Invalid mode value should return INVALID_ACTION error."""
        player = MockPlayer(player_id="p1", source_list=[])
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
# Tests: player filter (exposed_ids)
# ---------------------------------------------------------------------------


class TestPlayerFilter:
    def test_no_filter_exposes_all(self) -> None:
        """Without exposed_ids, all valid players are exposed."""
        assert is_player_exposable(MockPlayer()) is True

    def test_filter_includes_player(self) -> None:
        """Player in the filter set is exposed."""
        assert is_player_exposable(MockPlayer(player_id="p1"), exposed_ids={"p1", "p2"}) is True

    def test_filter_excludes_player(self) -> None:
        """Player not in the filter set is NOT exposed."""
        assert is_player_exposable(MockPlayer(player_id="p3"), exposed_ids={"p1", "p2"}) is False

    def test_empty_filter_exposes_all(self) -> None:
        """Empty set filter should expose all players (same as None)."""
        assert is_player_exposable(MockPlayer(player_id="p1"), exposed_ids=set()) is True

    def test_filter_still_checks_available(self) -> None:
        """Even in filter, unavailable players are not exposed."""
        assert (
            is_player_exposable(MockPlayer(player_id="p1", available=False), exposed_ids={"p1"})
            is False
        )
