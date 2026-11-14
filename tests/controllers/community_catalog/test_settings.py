"""Tests for the switch of the community catalog and the list of its providers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import ConfigEntryType, EventType, ProviderTier
from music_assistant_models.errors import ActionUnavailable

from music_assistant.controllers.community_catalog.controller import _INDEX_CHECK
from music_assistant.controllers.community_catalog.models import CommunityProviderState
from tests.conftest import full_mass_context
from tests.controllers.community_catalog.conftest import ADDED, RELEASED, catalog_index

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant.mass import MusicAssistant
    from tests.controllers.community_catalog.conftest import Registry

DOMAIN = "community_catalog"


async def test_switch_is_hidden_and_off(mass: MusicAssistant) -> None:
    """The switch is a hidden entry that starts off, and the catalog is in no list of the app."""
    entries = await mass.config.get_core_config_entries(DOMAIN)
    enabled = next(entry for entry in entries if entry.key == "catalog_enabled")

    assert enabled.type == ConfigEntryType.BOOLEAN
    assert enabled.hidden is True
    assert enabled.default_value is False
    assert (await mass.config.get_core_config(DOMAIN)).get_value("catalog_enabled") is False
    assert mass.translations.get_translation(
        f"core.{DOMAIN}.config_entries.catalog_enabled.description"
    )
    assert DOMAIN not in [config.domain for config in await mass.config.get_core_configs()]
    assert DOMAIN not in [manifest.domain for manifest in mass.get_provider_manifests()]


async def test_nothing_happens_while_switched_off(tmp_path: Path, registry: Registry) -> None:
    """With the switch off there is no request, no timer, no folder and an empty list."""
    async with full_mass_context(tmp_path) as mass:
        await _wait_for_start(mass)

        assert registry.requests == []
        assert _INDEX_CHECK not in mass._tracked_timers
        assert await mass.community_catalog.get_providers() == []
        assert await mass.community_catalog.refresh() == []

    assert registry.requests == []
    assert not (tmp_path / "data" / "community_providers").exists()


async def test_switching_on(
    mass: MusicAssistant, registry: Registry, caplog: pytest.LogCaptureFixture
) -> None:
    """Switching on fetches the index, schedules the daily check and lists what can be installed."""
    with patch.object(mass, "signal_event", wraps=mass.signal_event) as signal_event:
        await _switch(mass, enabled=True)

    assert registry.requests == [("nightly", None)]
    assert _events(signal_event) == 1
    delay = mass._tracked_timers[_INDEX_CHECK].when() - mass.loop.time()
    assert 24 * 3600 - 60 < delay <= 25 * 3600

    with caplog.at_level(logging.WARNING, logger=mass.community_catalog.logger.name):
        providers = await mass.community_catalog.get_providers()

    assert [provider.domain for provider in providers] == ["listed_one"]
    provider = providers[0]
    assert provider.state == CommunityProviderState.AVAILABLE
    assert provider.repository == "someone/listed_one"
    assert provider.maintainers == ["someone"]
    assert provider.icon == "data:image/svg+xml;base64,PHN2Zy8+"
    assert provider.latest_version == "1.2.0"
    assert provider.installed_version is None
    assert provider.added == ADDED
    assert provider.released == RELEASED
    assert provider.manifest.tier == ProviderTier.COMMUNITY
    assert provider.manifest.builtin is False
    assert provider.manifest.allow_disable is True
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "broken_one" in warnings[0]


async def test_switching_off(tmp_path: Path, mass: MusicAssistant, registry: Registry) -> None:
    """Switching off stops the daily check and empties the list, but keeps the files."""
    await _switch(mass, enabled=True)

    with patch.object(mass, "signal_event", wraps=mass.signal_event) as signal_event:
        await _switch(mass, enabled=False)

    assert _events(signal_event) == 1
    assert _INDEX_CHECK not in mass._tracked_timers
    assert mass.get_task(_INDEX_CHECK) is None
    assert await mass.community_catalog.get_providers() == []
    assert len(registry.requests) == 1
    folder = tmp_path / "data" / "community_providers"
    assert (folder / "index.json").exists()
    assert (folder / "state.json").exists()


async def test_refresh(mass: MusicAssistant, registry: Registry) -> None:
    """A refresh fetches now, and signals the list only when the index changed."""
    await _switch(mass, enabled=True)
    registry.document = catalog_index()
    del registry.document["providers"]["blocked_one"]["reason"]
    registry.document["providers"]["blocked_one"]["status"] = "listed"
    registry.etag = '"two"'

    with patch.object(mass, "signal_event", wraps=mass.signal_event) as signal_event:
        providers = await mass.community_catalog.refresh()
        assert [provider.domain for provider in providers] == ["listed_one", "blocked_one"]
        assert _events(signal_event) == 1

        await mass.community_catalog.refresh()
        assert _events(signal_event) == 1

    assert registry.requests[1:] == [("nightly", '"one"'), ("nightly", '"two"')]


async def test_kept_copy_after_a_restart(tmp_path: Path, registry: Registry) -> None:
    """A restarted server lists from its kept copy while the registry cannot be reached."""
    async with full_mass_context(tmp_path) as mass:
        await _switch(mass, enabled=True)
    registry.status = 503

    async with full_mass_context(tmp_path) as mass:
        await _wait_for_start(mass)

        assert len(registry.requests) == 2
        providers = await mass.community_catalog.get_providers()
        assert [provider.domain for provider in providers] == ["listed_one"]
        assert _INDEX_CHECK in mass._tracked_timers


async def test_safe_mode(tmp_path: Path, registry: Registry) -> None:
    """In safe mode the commands refuse, and switching on fetches nothing."""
    async with full_mass_context(tmp_path, safe_mode=True) as mass:
        await mass.config.save_core_config(DOMAIN, {"catalog_enabled": True})

        assert mass.get_task(_INDEX_CHECK) is None
        with pytest.raises(ActionUnavailable):
            await mass.community_catalog.get_providers()
        with pytest.raises(ActionUnavailable):
            await mass.community_catalog.refresh()

    assert registry.requests == []
    assert not (tmp_path / "data" / "community_providers").exists()


async def _switch(mass: MusicAssistant, *, enabled: bool) -> None:
    """Save the switch, as the app does, and wait for the check it starts."""
    await mass.config.save_core_config(DOMAIN, {"catalog_enabled": enabled})
    if (task := mass.get_task(_INDEX_CHECK)) is not None:
        await task


async def _wait_for_start(mass: MusicAssistant) -> None:
    """Wait until the start of the community catalog is done."""
    for task in list(mass._tracked_tasks.values()):
        if task.get_name() == "community_catalog_start":
            await task


def _events(signal_event: MagicMock) -> int:
    """Return how often the list of the community catalog was signalled as changed."""
    return [c.args[0] for c in signal_event.call_args_list].count(
        EventType.COMMUNITY_CATALOG_UPDATED
    )
