"""Tests for provider unload."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import MediaType, ProviderType
from music_assistant_models.errors import LoginFailed
from music_assistant_models.provider import ProviderManifest

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.tasks import TasksController
from music_assistant.controllers.tasks.constants import TASK_UPDATE_TIMER_ID
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    import pytest
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
