"""Tests for Sonos provider discovery."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosonos.exceptions import CannotConnect
from zeroconf import ServiceStateChange

from music_assistant.mass import MusicAssistant
from music_assistant.providers.sonos.player import SonosPlayer
from music_assistant.providers.sonos.provider import SonosPlayerProvider

PLAYER_ID = "sonos_player"
SERVICE_NAME = "Toilet._sonos._tcp.local."


def _bind_provider(mass: MusicAssistant | MagicMock) -> SonosPlayerProvider:
    """Create a Sonos provider bound to the given MusicAssistant."""
    provider = SonosPlayerProvider.__new__(SonosPlayerProvider)
    provider.mass = mass
    provider.logger = logging.getLogger("test.sonos.discovery")
    provider._ignored_disabled_players = set()
    provider._pending_setup_tasks = set()
    provider._pending_refresh_tasks = set()
    provider._unloaded = False
    provider.upnp = MagicMock()
    return provider


def _make_provider(enabled: bool = False) -> tuple[SonosPlayerProvider, MagicMock]:
    """Create a Sonos provider with mocked discovery dependencies."""
    mass = MagicMock()
    mass.players.wants_protocol.return_value = enabled
    mass.players.get_protocol.return_value = None
    return _bind_provider(mass), mass


def _bind_discovering_provider(mass: MusicAssistant) -> SonosPlayerProvider:
    """Create a Sonos provider that discovers players on the real timer machinery."""
    mass.config = MagicMock()
    mass.players = MagicMock()
    mass.players.wants_protocol.return_value = True
    mass.players.get_protocol.return_value = None
    mass.streams = MagicMock()
    return _bind_provider(mass)


def _make_discovery_info(player_id: str = PLAYER_ID) -> MagicMock:
    """Create minimal Sonos mDNS discovery information."""
    info = MagicMock()
    info.decoded_properties = {"uuid": player_id}
    return info


@pytest.mark.asyncio
async def test_disabled_discovery_is_not_scheduled_repeatedly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test repeated announcements for a disabled player are ignored once."""
    provider, mass = _make_provider()
    info = _make_discovery_info()

    with caplog.at_level(logging.DEBUG, logger=provider.logger.name):
        await provider.on_mdns_service_state_change(SERVICE_NAME, ServiceStateChange.Added, info)
        await provider.on_mdns_service_state_change(SERVICE_NAME, ServiceStateChange.Updated, info)

    mass.call_later.assert_not_called()
    ignored_records = [
        record for record in caplog.records if "in discovery as it is disabled" in record.message
    ]
    assert len(ignored_records) == 1


@pytest.mark.asyncio
async def test_disabled_discovery_logs_again_after_reenable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a new disabled period produces a new discovery diagnostic."""
    provider, mass = _make_provider()
    info = _make_discovery_info()

    with caplog.at_level(logging.DEBUG, logger=provider.logger.name):
        await provider.on_mdns_service_state_change(SERVICE_NAME, ServiceStateChange.Added, info)
        mass.players.wants_protocol.return_value = True
        await provider.on_mdns_service_state_change(SERVICE_NAME, ServiceStateChange.Updated, info)
        mass.players.wants_protocol.return_value = False
        await provider.on_mdns_service_state_change(SERVICE_NAME, ServiceStateChange.Updated, info)

    mass.call_later.assert_called_once()
    ignored_records = [
        record for record in caplog.records if "in discovery as it is disabled" in record.message
    ]
    assert len(ignored_records) == 2


@pytest.mark.asyncio
async def test_a_player_set_up_in_the_meantime_is_not_set_up_again(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test a delayed setup of a player that is registered by then is skipped quietly."""
    provider, mass = _make_provider(enabled=True)
    mass.players.get_protocol.return_value = MagicMock(spec=SonosPlayer)
    get_discovery_info = AsyncMock()
    monkeypatch.setattr(
        "music_assistant.providers.sonos.provider.get_discovery_info", get_discovery_info
    )

    with caplog.at_level(logging.DEBUG, logger=provider.logger.name):
        await provider._setup_player(PLAYER_ID, SERVICE_NAME, _make_discovery_info())

    get_discovery_info.assert_not_awaited()
    assert "in discovery as it is already set up" in caplog.text


@pytest.mark.asyncio
async def test_unload_cancels_a_pending_player_setup(timer_mass: MusicAssistant) -> None:
    """Test a discovered player that is still waiting to be set up is dropped on unload."""
    provider = _bind_discovering_provider(timer_mass)

    await provider.on_mdns_service_state_change(
        SERVICE_NAME, ServiceStateChange.Added, _make_discovery_info()
    )
    handle = timer_mass._tracked_timers[f"setup_sonos_{PLAYER_ID}"]

    await provider.unload(is_removed=True)

    assert handle.cancelled()
    assert provider._pending_setup_tasks == set()


@pytest.mark.asyncio
async def test_unload_cancels_a_player_setup_that_already_started(
    timer_mass: MusicAssistant,
) -> None:
    """Test a player setup that is already running is aborted when the provider unloads."""
    provider = _bind_discovering_provider(timer_mass)
    setup_started = asyncio.Event()

    async def _setup_player(*_args: object) -> None:
        setup_started.set()
        await asyncio.sleep(5)

    provider._setup_player = _setup_player  # type: ignore[assignment]
    await provider.on_mdns_service_state_change(
        SERVICE_NAME, ServiceStateChange.Added, _make_discovery_info()
    )
    task_id = f"setup_sonos_{PLAYER_ID}"
    # let the armed timer fire now instead of waiting out the discovery debounce
    timer_mass.call_later(0, provider._setup_player, task_id=task_id)
    await setup_started.wait()
    task = timer_mass._tracked_tasks[task_id]

    await provider.unload(is_removed=True)

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_an_announcement_arriving_after_the_unload_is_ignored(
    timer_mass: MusicAssistant,
) -> None:
    """Test a discovery callback that lands after the unload does not arm a new setup."""
    provider = _bind_discovering_provider(timer_mass)

    await provider.unload(is_removed=True)
    await provider.on_mdns_service_state_change(
        SERVICE_NAME, ServiceStateChange.Added, _make_discovery_info()
    )

    assert timer_mass._tracked_timers == {}
    assert provider._pending_setup_tasks == set()


@pytest.mark.asyncio
async def test_unload_sweeps_every_discovered_player(timer_mass: MusicAssistant) -> None:
    """Test a burst of discovered players leaves no setup behind after unload."""
    provider = _bind_discovering_provider(timer_mass)

    for index in range(3):
        await provider.on_mdns_service_state_change(
            f"Speaker{index}._sonos._tcp.local.",
            ServiceStateChange.Added,
            _make_discovery_info(f"sonos_player_{index}"),
        )
    assert len(timer_mass._tracked_timers) == 3

    await provider.unload(is_removed=True)

    assert timer_mass._tracked_timers == {}
    assert provider._pending_setup_tasks == set()


@pytest.mark.asyncio
async def test_a_withdrawn_announcement_is_handed_to_the_player() -> None:
    """Test a removed Sonos announcement lets the player decide whether it went to sleep."""
    provider, mass = _make_provider()
    player = MagicMock(spec=SonosPlayer)
    goodbye = object()
    player.on_mdns_goodbye = MagicMock(return_value=goodbye)
    mass.players.get_protocol.return_value = player

    await provider.on_mdns_service_state_change(
        "RINCON_C438750D189C01400@Move 2._sonos._tcp.local.", ServiceStateChange.Removed, None
    )

    mass.players.get_protocol.assert_called_once_with("RINCON_C438750D189C01400")
    mass.create_task.assert_called_once_with(goodbye)


def _manual_ip_provider(attached: bool = False, wanted: bool = True) -> SonosPlayerProvider:
    """
    Create a provider with one speaker configured by IP address.

    :param attached: Whether the speaker at that address has an attached protocol.
    :param wanted: Whether the server wants the speaker.
    """
    mass = MagicMock()
    mass.players.get_protocol.return_value = MagicMock(spec=SonosPlayer) if attached else None
    mass.players.wants_protocol.return_value = wanted
    provider = _bind_provider(mass)
    provider.config = MagicMock()
    provider.config.get_value.return_value = ["192.168.1.60"]
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize(("attached", "wanted"), [(True, True), (False, False)])
async def test_discovery_skips_a_manual_speaker_that_is_attached_or_not_wanted(
    monkeypatch: pytest.MonkeyPatch, attached: bool, wanted: bool
) -> None:
    """Discovering again leaves an attached speaker alone and sets up no disabled one."""
    provider = _manual_ip_provider(attached=attached, wanted=wanted)
    monkeypatch.setattr(
        "music_assistant.providers.sonos.provider.get_discovery_info",
        AsyncMock(return_value={"device": {"id": PLAYER_ID}}),
    )
    player_class = MagicMock()
    monkeypatch.setattr("music_assistant.providers.sonos.provider.SonosPlayer", player_class)

    await provider.discover_players()

    player_class.assert_not_called()


@pytest.mark.asyncio
async def test_discovery_sets_up_a_manual_speaker_without_an_attached_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A removed or re-enabled speaker at a configured address is set up again."""
    provider = _manual_ip_provider()
    discovery_info = {"device": {"id": PLAYER_ID}}
    monkeypatch.setattr(
        "music_assistant.providers.sonos.provider.get_discovery_info",
        AsyncMock(return_value=discovery_info),
    )
    player_class = MagicMock()
    player_class.return_value.setup = AsyncMock()
    monkeypatch.setattr("music_assistant.providers.sonos.provider.SonosPlayer", player_class)

    await provider.discover_players()

    player_class.assert_called_once_with(provider, PLAYER_ID, discovery_info=discovery_info)
    player_class.return_value.setup.assert_awaited_once()


@pytest.mark.asyncio
async def test_discovery_goes_on_when_a_manual_speaker_can_not_be_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A speaker that refuses the connection does not fail the discovery."""
    provider = _manual_ip_provider()
    monkeypatch.setattr(
        "music_assistant.providers.sonos.provider.get_discovery_info",
        AsyncMock(return_value={"device": {"id": PLAYER_ID}}),
    )
    player_class = MagicMock()
    player_class.return_value.setup = AsyncMock(side_effect=CannotConnect(OSError("refused")))
    monkeypatch.setattr("music_assistant.providers.sonos.provider.SonosPlayer", player_class)

    await provider.discover_players()

    player_class.return_value.setup.assert_awaited_once()
