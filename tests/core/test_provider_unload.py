"""Tests for provider unload."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import EventType, IdentifierType, MediaType, ProviderType
from music_assistant_models.errors import LoginFailed
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_PLAYER_PROTOCOLS, CONF_PLAYERS
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.tasks import TasksController
from music_assistant.controllers.tasks.constants import TASK_UPDATE_TIMER_ID
from music_assistant.mass import MusicAssistant
from music_assistant.models.metadata_provider import MetadataProvider
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.player_protocol import PlayerProtocol
from music_assistant.models.player_provider import PlayerProvider

# the real server with fake providers, for the tests of what the unload does to players
from tests.scenarios.conftest import scenario  # noqa: F401

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderError

    from tests.scenarios.harness import FakeProtocol, Scenario

KITCHEN = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"}
BEDROOM = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B0"}
OFFICE = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:C0"}


def _make_mass(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MusicAssistant, list[ProviderError], AsyncMock]:
    """Return a bare MusicAssistant (bypassing __init__) recording last-error writes."""
    mass = object.__new__(MusicAssistant)
    recorded: list[ProviderError] = []
    config = MagicMock()
    config.update_provider_last_error = MagicMock(
        side_effect=lambda _instance_id, error: recorded.append(error)
    )
    unload = AsyncMock()
    monkeypatch.setattr(mass, "config", config, raising=False)
    monkeypatch.setattr(mass, "unload_provider", unload)
    return mass, recorded, unload


async def test_unload_provider_with_error_preserves_auth_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LoginFailed keeps its error code + translation so the provider shows AUTH_REQUIRED."""
    mass, recorded, unload = _make_mass(monkeypatch)
    await mass.unload_provider_with_error("spotify--1", LoginFailed("token revoked"))
    assert recorded[0].error_code == LoginFailed.error_code
    assert recorded[0].translation_key == LoginFailed.translation_key
    unload.assert_awaited_once_with("spotify--1")


async def test_unload_provider_with_error_string_is_generic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain string message is recorded as a generic error (code 999)."""
    mass, recorded, _unload = _make_mass(monkeypatch)
    await mass.unload_provider_with_error("airplay--1", "daemon failed to start")
    assert recorded[0].error_code == 999
    assert recorded[0].message == "daemon failed to start"


async def test_unload_provider_waits_for_running_sync(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider must not be unloaded while its own library sync is still unwinding."""
    mass_minimal.tasks = TasksController(mass_minimal)
    await mass_minimal.tasks.setup(await mass_minimal.config.get_core_config("tasks"))
    mass_minimal.tasks.initialized.set()
    mass_minimal.music = MusicController(mass_minimal)
    # discovery is not set up on the minimal instance and plays no part in this test
    monkeypatch.setattr(mass_minimal.discovery, "on_provider_unload", MagicMock())

    sync_started = asyncio.Event()
    sync_finished = False
    sync_finished_on_unload: bool | None = None

    class SyncingProvider(MusicProvider):
        """Provider that records the sync state observed by its unload."""

        async def sync_library(self, media_type: MediaType) -> None:
            """Unused: the sync task handler is registered directly by this test."""

        async def unload(self, is_removed: bool = False) -> None:
            """Handle unload of the provider."""
            nonlocal sync_finished_on_unload
            sync_finished_on_unload = sync_finished

    provider_config = ProviderConfig(
        values={},
        type=ProviderType.MUSIC,
        domain="test_provider",
        instance_id="test_provider--instance",
        name="Test provider",
    )
    monkeypatch.setattr(provider_config, "get_value", lambda *_args, **_kwargs: "GLOBAL")
    provider = SyncingProvider(
        mass_minimal,
        manifest=ProviderManifest(
            type=ProviderType.MUSIC,
            domain="test_provider",
            name="Test provider",
            description="Test provider",
            codeowners=["@music-assistant"],
        ),
        config=provider_config,
    )
    provider.available = True
    mass_minimal._providers[provider.instance_id] = provider

    async def sync_handler() -> None:
        nonlocal sync_finished
        sync_started.set()
        try:
            await asyncio.sleep(30)
        finally:
            # cleanup that yields to the event loop, like a sync releasing its resources
            await asyncio.sleep(0.05)
            sync_finished = True

    task_id = mass_minimal.music._get_sync_task_id(provider, MediaType.TRACK)
    mass_minimal.tasks.register_scheduled_task(
        task_id=task_id,
        name="Sync tracks",
        handler=sync_handler,
        schedule=TaskSchedule.hourly(every=12),
    )
    mass_minimal.tasks.run_task(task_id)
    await asyncio.wait_for(sync_started.wait(), timeout=2)

    try:
        await mass_minimal.unload_provider(provider.instance_id)
    finally:
        mass_minimal.cancel_timer(TASK_UPDATE_TIMER_ID)
        await mass_minimal.tasks.close()

    assert sync_finished_on_unload is True


@pytest.mark.parametrize("removed", [False, True], ids=["unload", "removal"])
async def test_unload_of_a_player_provider_detaches_its_protocols(
    scenario: Scenario,  # noqa: F811
    removed: bool,
) -> None:
    """
    Unloading a player provider detaches its protocols, also of a disabled player.

    A plain unload keeps the players, unavailable, with their configs and queues, so they
    come back with their settings. Removing the provider deletes the configs as well,
    including the one of a disabled player, and a player goes with its last protocol.
    """
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    kitchen = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    kitchen_audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    bedroom = sonos.protocol("RINCON_BEDROOM", "Bedroom", identifiers=BEDROOM)
    office = sonos.protocol("RINCON_OFFICE", "Office", identifiers=OFFICE)
    for protocol in (kitchen, kitchen_audio, bedroom, office):
        await scenario.register(protocol)
    kitchen_id = scenario.single_player(kitchen, kitchen_audio).player_id
    bedroom_id = scenario.single_player(bedroom).player_id
    office_id = scenario.single_player(office).player_id
    await scenario.play(bedroom_id)
    await scenario.disable(office_id)
    detached = _record_detach(kitchen, kitchen_audio, bedroom, office)

    if removed:
        await scenario.remove_provider(sonos)
    else:
        await scenario.mass.unload_provider(sonos.instance_id)
    await scenario.idle()

    assert sorted(detached) == ["RINCON_BEDROOM", "RINCON_KITCHEN"]
    assert [protocol.protocol_id for protocol in scenario.mass.players.iter_protocols()] == [
        "ap_kitchen"
    ]
    kitchen_state = scenario.player(kitchen_id)
    assert kitchen_state is not None
    assert kitchen_state.available
    stored_players, stored_protocols = _stored(scenario)
    if removed:
        assert stored_players == [kitchen_id]
        assert stored_protocols == ["ap_kitchen"]
        assert scenario.player(bedroom_id) is None
        # the queue of a player that is gone for good goes with it
        assert await scenario.saved_queue(bedroom_id) is None
    else:
        assert stored_players == sorted([kitchen_id, bedroom_id, office_id])
        assert stored_protocols == sorted(
            ["RINCON_KITCHEN", "ap_kitchen", "RINCON_BEDROOM", "RINCON_OFFICE"]
        )
        bedroom_state = scenario.player(bedroom_id)
        assert bedroom_state is not None
        assert not bedroom_state.available
        assert await scenario.saved_queue(bedroom_id)


class _TeardownProvider(PlayerProvider):
    """Player provider that records whether its own unload ran."""

    unloaded = False

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload of the provider."""
        self.unloaded = True


class _TeardownProtocol(PlayerProtocol):
    """Protocol that records its teardown and optionally fails it."""

    def __init__(self, provider: PlayerProvider, protocol_id: str, fails: bool = False) -> None:
        """
        Initialize the protocol.

        :param provider: Player provider this protocol belongs to.
        :param protocol_id: ID of the protocol.
        :param fails: Raise from on_detach to simulate a provider that fails to release it.
        """
        super().__init__(provider, protocol_id)
        self._attr_name = protocol_id
        self._attr_available = True
        self._fails = fails
        self.detached = False

    async def on_detach(self) -> None:
        """Handle logic when the protocol is detached from its player."""
        self.detached = True
        await super().on_detach()
        if self._fails:
            msg = "device is gone"
            raise RuntimeError(msg)

    async def stop(self) -> None:
        """Handle a stop command."""


async def _setup_player_provider(
    mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    failing_protocol_id: str,
) -> tuple[_TeardownProvider, list[_TeardownProtocol], list[EventType]]:
    """
    Wire a minimal mass with one player provider owning three attached protocols.

    :param mass: Minimal MusicAssistant instance to wire up.
    :param monkeypatch: Pytest monkeypatch fixture.
    :param failing_protocol_id: ID of the protocol whose on_detach must raise.
    :return: The provider, its protocols (in attach order) and the recorded event types.
    """
    provider = await _setup_bare_player_provider(mass, monkeypatch, _TeardownProvider)
    monkeypatch.setattr(mass, "_update_available_providers_cache", AsyncMock())
    signalled: list[EventType] = []
    monkeypatch.setattr(
        mass, "signal_event", lambda event, *_args, **_kwargs: signalled.append(event)
    )
    protocols = [
        _TeardownProtocol(provider, protocol_id, fails=protocol_id == failing_protocol_id)
        for protocol_id in ("device_a", "device_b", "device_c")
    ]
    for protocol in protocols:
        await mass.players.register(protocol)
    assert all(protocol.player is not None for protocol in protocols)
    return provider, protocols, signalled


async def test_failing_protocol_teardown_does_not_strand_the_others(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A protocol that fails to release must not block the rest of the provider unload."""
    provider, protocols, signalled = await _setup_player_provider(
        mass_minimal, monkeypatch, "device_a"
    )
    player_ids = {protocol.player.player_id for protocol in protocols if protocol.player}

    await mass_minimal.unload_provider(provider.instance_id)

    assert all(protocol.detached for protocol in protocols)
    assert mass_minimal.players._protocols == {}
    # the players stay, unavailable, until their device is found again
    assert set(mass_minimal.players._players) == player_ids
    assert not any(player.state.available for player in mass_minimal.players._players.values())
    assert provider.unloaded
    assert provider.instance_id not in mass_minimal._providers
    assert EventType.PROVIDERS_UPDATED in signalled


async def test_failing_detach_still_deregisters_the_provider(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detach that raises must still leave the provider deregistered and signalled."""
    provider, _protocols, signalled = await _setup_player_provider(
        mass_minimal, monkeypatch, "device_a"
    )
    monkeypatch.setattr(
        mass_minimal.players, "unregister", AsyncMock(side_effect=RuntimeError("teardown blew up"))
    )

    unload_error: Exception | None = None
    try:
        await mass_minimal.unload_provider(provider.instance_id)
    except RuntimeError as err:
        unload_error = err

    assert provider.instance_id not in mass_minimal._providers
    assert EventType.PROVIDERS_UPDATED in signalled
    assert unload_error is None


async def _setup_bare_player_provider(
    mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    provider_class: type[PlayerProvider] = PlayerProvider,
) -> Any:
    """
    Put a bare player provider on a minimal mass instance, ready to be unloaded.

    :param mass: Minimal MusicAssistant instance to wire up.
    :param monkeypatch: Pytest monkeypatch fixture.
    :param provider_class: The class of the provider to create.
    """
    mass.players = PlayerController(mass)
    mass.music = MagicMock(unschedule_provider_sync=AsyncMock())
    # no queues exist in these tests, so get() must report a miss rather than a mock
    mass.player_queues = MagicMock(on_player_register=AsyncMock(), get=MagicMock(return_value=None))
    # discovery is not set up on the minimal instance and plays no part in these tests
    monkeypatch.setattr(mass.discovery, "on_provider_unload", MagicMock())
    # an attach reads the player's cached power state, so the cache has to work here
    # for it to run all the way to the point where the protocol enters the registry
    await mass.cache.setup(await mass.config.get_core_config("cache"))

    provider_config = ProviderConfig(
        values={},
        type=ProviderType.PLAYER,
        domain="test_player_provider",
        instance_id="test_player_provider--instance",
        name="Test player provider",
    )
    monkeypatch.setattr(provider_config, "get_value", lambda *_args, **_kwargs: "GLOBAL")
    provider = provider_class(
        mass,
        manifest=ProviderManifest(
            type=ProviderType.PLAYER,
            domain="test_player_provider",
            name="Test player provider",
            description="Test player provider",
            codeowners=["@music-assistant"],
        ),
        config=provider_config,
    )
    provider.available = True
    mass._providers[provider.instance_id] = provider
    return provider


async def test_unload_provider_rejects_late_protocol_attach(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A player provider on its way out can no longer register protocols."""
    provider = await _setup_bare_player_provider(mass_minimal, monkeypatch)
    monkeypatch.setattr(
        "music_assistant.controllers.players.lifecycle.enrich_device_mac_address",
        AsyncMock(),
    )

    late_protocol = _TeardownProtocol(provider, "late_device")

    class LateAttachingProtocol(_TeardownProtocol):
        """Protocol whose detach lets a discovery callback attach another protocol."""

        async def on_detach(self) -> None:
            """Handle logic when the protocol is detached from its player."""
            await super().on_detach()
            # stands in for a discovery that was already running when the unload started
            # and only reaches the controller once the protocols have been detached
            await self.mass.players.register(late_protocol)

    protocol = LateAttachingProtocol(provider, "existing_device")
    await mass_minimal.players.register(protocol)
    assert protocol.player is not None
    existing_player_id = protocol.player.player_id

    await mass_minimal.unload_provider(provider.instance_id)

    # without the guard the late protocol survives the unload with no provider behind it,
    # so it is never detached and its on_detach never runs
    assert mass_minimal.players._protocols == {}
    assert late_protocol.player is None
    assert set(mass_minimal.players._players) == {existing_player_id}


async def test_unload_provider_rejects_in_flight_protocol_attach(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attach that is already running when the unload starts is dropped as well."""
    provider = await _setup_bare_player_provider(mass_minimal, monkeypatch)

    attach_started = asyncio.Event()
    resume_attach = asyncio.Event()

    async def _blocked_enrich(*_args: object, **_kwargs: object) -> None:
        """Park the attach in one of its awaits until the test releases it."""
        attach_started.set()
        await resume_attach.wait()

    monkeypatch.setattr(
        "music_assistant.controllers.players.lifecycle.enrich_device_mac_address",
        _blocked_enrich,
    )

    protocol = _TeardownProtocol(provider, "in_flight")
    attach = asyncio.create_task(mass_minimal.players.register(protocol))
    await asyncio.wait_for(attach_started.wait(), timeout=5)

    # start the unload with the attach parked, and release it once the provider is
    # flagged: the protocol is not in the registry yet, so the detach pass cannot see it
    unload = asyncio.create_task(mass_minimal.unload_provider(provider.instance_id))
    async with asyncio.timeout(5):
        while not provider.unloading:
            await asyncio.sleep(0)
    resume_attach.set()
    await asyncio.gather(attach, unload)

    assert mass_minimal.players._protocols == {}
    assert mass_minimal.players._players == {}
    assert protocol.player is None


async def test_unload_cancels_pending_post_load(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unloading a provider mid post-load cancels that task before it can announce readiness."""
    music = MagicMock()
    music.unschedule_provider_sync = AsyncMock()
    monkeypatch.setattr(mass_minimal, "music", music, raising=False)
    monkeypatch.setattr(mass_minimal, "_update_available_providers_cache", AsyncMock())
    monkeypatch.setattr(mass_minimal.discovery, "on_provider_unload", MagicMock())

    gate = asyncio.Event()
    resumed = False

    class GatedProvider(MetadataProvider):
        """Metadata provider whose post-load blocks until the test releases it."""

        async def loaded_in_mass(self) -> None:
            """Block in the post-load step the way a slow migration would."""
            nonlocal resumed
            await gate.wait()
            resumed = True

    provider_config = ProviderConfig(
        values={},
        type=ProviderType.METADATA,
        domain="test_meta",
        instance_id="test_meta--1",
        name="Test metadata",
    )
    monkeypatch.setattr(provider_config, "get_value", lambda *_args, **_kwargs: "GLOBAL")
    provider = GatedProvider(
        mass_minimal,
        manifest=ProviderManifest(
            type=ProviderType.METADATA,
            domain="test_meta",
            name="Test metadata",
            description="Test metadata",
            codeowners=["@music-assistant"],
        ),
        config=provider_config,
    )

    await mass_minimal._register_loaded_provider(provider, provider_config)
    await asyncio.sleep(0)  # let the post load task reach the gate
    post_load_task = mass_minimal._tracked_tasks["post_load_provider_test_meta--1"]
    assert not post_load_task.done()

    await mass_minimal.unload_provider(provider.instance_id)
    await asyncio.sleep(0)

    # the post-load task was cancelled by unload, so it never ran past the gate to
    # announce a provider that is already gone
    assert post_load_task.cancelled()
    assert resumed is False
    assert provider.instance_id not in mass_minimal._providers


def _stored(server: Scenario) -> tuple[list[str], list[str]]:
    """Return the ids of the stored player configs and of the stored protocol configs."""
    config = server.mass.config
    return sorted(config.get(CONF_PLAYERS, {})), sorted(config.get(CONF_PLAYER_PROTOCOLS, {}))


def _record_detach(*protocols: FakeProtocol) -> list[str]:
    """
    Record the ids of the protocols that get detached.

    :param protocols: The protocol objects to watch.
    """
    detached: list[str] = []

    def _watch(protocol: FakeProtocol) -> None:
        on_detach = protocol.on_detach

        async def _on_detach() -> None:
            detached.append(protocol.protocol_id)
            await on_detach()

        protocol.on_detach = _on_detach  # type: ignore[method-assign]

    for protocol in protocols:
        _watch(protocol)
    return detached
