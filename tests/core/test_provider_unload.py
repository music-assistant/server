"""Tests for provider unload."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import EventType, MediaType, ProviderType
from music_assistant_models.errors import LoginFailed
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_PLAYERS
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.tasks import TasksController
from music_assistant.controllers.tasks.constants import TASK_UPDATE_TIMER_ID
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderError


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


@pytest.mark.parametrize("is_removed", [False, True])
async def test_unload_provider_unregisters_hidden_players(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    is_removed: bool,
) -> None:
    """
    Unloading a player provider also unregisters its disabled and initializing players.

    Removing the provider deletes their configs as well, a plain reload keeps them.
    """
    mass_minimal.players = PlayerController(mass_minimal)
    mass_minimal.music = MagicMock(unschedule_provider_sync=AsyncMock())
    mass_minimal.player_queues = MagicMock()
    # wiping a player config strips that player from the user access filters
    mass_minimal.webserver = MagicMock(auth=MagicMock(remove_from_user_filters=AsyncMock()))
    # discovery is not set up on the minimal instance and plays no part in this test
    monkeypatch.setattr(mass_minimal.discovery, "on_provider_unload", MagicMock())

    unloaded_players: list[str] = []

    class RecordingPlayer(Player):
        """Player that records that it was unloaded."""

        async def on_unload(self) -> None:
            """Handle unload of the player."""
            unloaded_players.append(self.player_id)
            await super().on_unload()

    def add_provider(instance_id: str) -> PlayerProvider:
        provider_config = ProviderConfig(
            values={},
            type=ProviderType.PLAYER,
            domain="test_player_provider",
            instance_id=instance_id,
            name="Test player provider",
        )
        monkeypatch.setattr(provider_config, "get_value", lambda *_args, **_kwargs: "GLOBAL")
        provider = PlayerProvider(
            mass_minimal,
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
        mass_minimal._providers[instance_id] = provider
        return provider

    def add_player(
        provider: PlayerProvider,
        player_id: str,
        enabled: bool = True,
        initialized: bool = True,
    ) -> RecordingPlayer:
        player = RecordingPlayer(provider, player_id)
        if initialized:
            player.set_initialized()
        if not enabled:
            # config and state are kept in sync so the player reads as disabled
            # even if its state gets recalculated from the config
            player.config.enabled = False
            player.state.enabled = False
        mass_minimal.players._players[player_id] = player
        return player

    provider = add_provider("test_player_provider--instance")
    other_provider = add_provider("test_player_provider--other")
    add_player(provider, "enabled_player")
    # a player provider may deliberately keep a disabled player registered (msx_bridge does)
    add_player(provider, "disabled_player", enabled=False)
    # a player that is still being set up when its provider goes away
    add_player(provider, "initializing_player", initialized=False)
    other_player = add_player(other_provider, "other_provider_player")
    provider_player_ids = {"enabled_player", "disabled_player", "initializing_player"}
    all_player_ids = provider_player_ids | {other_player.player_id}

    try:
        await mass_minimal.unload_provider(provider.instance_id, is_removed=is_removed)
    finally:
        # unregistering schedules a debounced state update for the players that remain
        mass_minimal.cancel_timer(f"player_update_state_{other_player.player_id}")

    assert set(unloaded_players) == provider_player_ids
    assert set(mass_minimal.players._players) == {other_player.player_id}

    # a removed provider takes the configs of all its players with it, including the ones
    # its own players listing hides; a plain reload must leave every config untouched so
    # the players come back with their settings
    stored_configs = {
        player_id
        for player_id in all_player_ids
        if mass_minimal.config.get(f"{CONF_PLAYERS}/{player_id}") is not None
    }
    assert stored_configs == ({other_player.player_id} if is_removed else all_player_ids)

    # the queue controller is the other consumer of the removal flag: it drops the
    # persisted queue of a player only when that player is gone for good
    assert {
        (mock_call.args[0], mock_call.kwargs["permanent"])
        for mock_call in mass_minimal.player_queues.on_player_remove.call_args_list
    } == {(player_id, is_removed) for player_id in provider_player_ids}


class _TeardownProvider(PlayerProvider):
    """Player provider that records whether its own unload ran."""

    unloaded = False

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload of the provider."""
        self.unloaded = True


class _TeardownPlayer(Player):
    """Player that records its teardown and optionally fails it."""

    def __init__(self, provider: PlayerProvider, player_id: str, fails: bool = False) -> None:
        """
        Initialize the player.

        :param provider: Player provider this player belongs to.
        :param player_id: ID of the player.
        :param fails: Raise from on_unload to simulate a provider that fails to release it.
        """
        super().__init__(provider, player_id)
        self._attr_name = player_id
        self._attr_available = True
        self._fails = fails
        self.unloaded = False

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        self.unloaded = True
        if self._fails:
            msg = "device is gone"
            raise RuntimeError(msg)


def _setup_player_provider(
    mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    failing_player_id: str,
) -> tuple[_TeardownProvider, list[_TeardownPlayer], list[EventType]]:
    """
    Wire a minimal mass with one player provider owning three registered players.

    :param mass: Minimal MusicAssistant instance to wire up.
    :param monkeypatch: Pytest monkeypatch fixture.
    :param failing_player_id: ID of the player whose on_unload must raise.
    :return: The provider, its players (in registration order) and the recorded event types.
    """
    monkeypatch.setattr(
        mass, "music", MagicMock(unschedule_provider_sync=AsyncMock()), raising=False
    )
    monkeypatch.setattr(mass, "player_queues", MagicMock(), raising=False)
    monkeypatch.setattr(mass, "_update_available_providers_cache", AsyncMock())
    monkeypatch.setattr(mass.discovery, "on_provider_unload", MagicMock())
    signalled: list[EventType] = []
    monkeypatch.setattr(
        mass, "signal_event", lambda event, *_args, **_kwargs: signalled.append(event)
    )
    monkeypatch.setattr(mass, "players", PlayerController(mass), raising=False)

    provider = _TeardownProvider(
        mass,
        manifest=ProviderManifest(
            type=ProviderType.PLAYER,
            domain="test_player_provider",
            name="Test player provider",
            description="Test player provider",
            codeowners=["@music-assistant"],
        ),
        config=ProviderConfig(
            values={},
            type=ProviderType.PLAYER,
            domain="test_player_provider",
            instance_id="test_player_provider--instance",
            name="Test player provider",
        ),
    )
    mass._providers[provider.instance_id] = provider

    players = [
        _TeardownPlayer(provider, player_id, fails=player_id == failing_player_id)
        for player_id in ("player_a", "player_b", "player_c")
    ]
    for player in players:
        mass.players._players[player.player_id] = player
        player.set_initialized()
    return provider, players, signalled


async def test_failing_player_teardown_does_not_strand_the_others(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A player that fails to release must not block the rest of the provider unload."""
    provider, players, signalled = _setup_player_provider(mass_minimal, monkeypatch, "player_a")

    await mass_minimal.unload_provider(provider.instance_id)

    assert all(player.unloaded for player in players)
    assert mass_minimal.players._players == {}
    assert provider.unloaded
    assert provider.instance_id not in mass_minimal._providers
    assert EventType.PROVIDERS_UPDATED in signalled


async def test_failing_unregister_still_deregisters_the_provider(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unregister that raises must still leave the provider deregistered and signalled."""
    provider, _players, signalled = _setup_player_provider(mass_minimal, monkeypatch, "player_a")
    monkeypatch.setattr(
        mass_minimal.players, "unregister", AsyncMock(side_effect=RuntimeError("teardown blew up"))
    )

    await mass_minimal.unload_provider(provider.instance_id)

    assert provider.instance_id not in mass_minimal._providers
    assert EventType.PROVIDERS_UPDATED in signalled


async def _setup_bare_player_provider(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> PlayerProvider:
    """Put a bare player provider on a minimal mass instance, ready to be unloaded."""
    mass.players = PlayerController(mass)
    mass.music = MagicMock(unschedule_provider_sync=AsyncMock())
    # no queues exist in these tests, so get() must report a miss rather than a mock
    mass.player_queues = MagicMock(on_player_register=AsyncMock(), get=MagicMock(return_value=None))
    # discovery is not set up on the minimal instance and plays no part in these tests
    monkeypatch.setattr(mass.discovery, "on_provider_unload", MagicMock())
    # a registration reads the player's cached power state, so the cache has to work here
    # for it to run all the way to the point where the player enters the registry
    await mass.cache.setup(await mass.config.get_core_config("cache"))

    provider_config = ProviderConfig(
        values={},
        type=ProviderType.PLAYER,
        domain="test_player_provider",
        instance_id="test_player_provider--instance",
        name="Test player provider",
    )
    monkeypatch.setattr(provider_config, "get_value", lambda *_args, **_kwargs: "GLOBAL")
    provider = PlayerProvider(
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


async def test_unload_provider_rejects_late_player_registration(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A player provider on its way out can no longer register players."""
    provider = await _setup_bare_player_provider(mass_minimal, monkeypatch)
    monkeypatch.setattr(
        "music_assistant.controllers.players.controller.enrich_device_mac_address",
        AsyncMock(),
    )

    late_player = Player(provider, "late_player")

    class LateRegisteringPlayer(Player):
        """Player whose unload lets a discovery callback register another player."""

        async def on_unload(self) -> None:
            """Handle unload of the player."""
            await super().on_unload()
            # stands in for a discovery that was already running when the unload started
            # and only reaches the controller once the players have been unregistered
            await self.mass.players.register_or_update(late_player)

    player = LateRegisteringPlayer(provider, "existing_player")
    player.set_initialized()
    mass_minimal.players._players[player.player_id] = player

    await mass_minimal.unload_provider(provider.instance_id)

    # without the guard the late player survives the unload with no provider behind it,
    # so it is never unregistered and its on_unload never runs
    assert mass_minimal.players._players == {}


async def test_unload_provider_rejects_in_flight_player_registration(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registration that is already running when the unload starts is dropped as well."""
    provider = await _setup_bare_player_provider(mass_minimal, monkeypatch)

    registration_started = asyncio.Event()
    resume_registration = asyncio.Event()

    async def _blocked_enrich(*_args: object, **_kwargs: object) -> None:
        """Park the registration in one of its awaits until the test releases it."""
        registration_started.set()
        await resume_registration.wait()

    monkeypatch.setattr(
        "music_assistant.controllers.players.controller.enrich_device_mac_address",
        _blocked_enrich,
    )

    register = asyncio.create_task(mass_minimal.players.register(Player(provider, "in_flight")))
    await asyncio.wait_for(registration_started.wait(), timeout=5)

    # start the unload with the registration parked, and release it once the provider is
    # flagged: the player is not in the registry yet, so the unregister pass cannot see it
    unload = asyncio.create_task(mass_minimal.unload_provider(provider.instance_id))
    async with asyncio.timeout(5):
        while not provider.unloading:
            await asyncio.sleep(0)
    resume_registration.set()
    await asyncio.gather(register, unload)

    assert mass_minimal.players._players == {}
