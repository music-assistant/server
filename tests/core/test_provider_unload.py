"""Tests for provider unload."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import MediaType, ProviderType
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
