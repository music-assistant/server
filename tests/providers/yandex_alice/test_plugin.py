"""Lifecycle tests for the Yandex Alice PluginProvider entrypoint."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from music_assistant.providers.yandex_alice.constants import (
    CONF_DIALOG_SKILL_ID,
    CONF_DIALOG_VOICE_CONTINUATION,
    CONF_DIALOG_WEBHOOK_SECRET,
    CONF_EXPOSED_PLAYERS,
    CONF_INSTANCE_NAME,
)
from music_assistant.providers.yandex_alice.plugin import YandexAlicePlugin
from music_assistant.providers.yandex_alice.skill_manifest_provider import SkillManifestProvider


def _plugin(tmp_path: Path, values: dict[str, object]) -> tuple[YandexAlicePlugin, MagicMock]:
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    mass.cache = MagicMock()
    mass.webserver.register_dynamic_route = MagicMock(return_value=MagicMock())
    config = MagicMock()
    config.get_value.side_effect = lambda key: values.get(key)
    manifest = MagicMock()
    manifest.domain = "yandex_alice"
    plugin = YandexAlicePlugin(mass, manifest, config, set())
    return plugin, mass


@pytest.mark.asyncio
async def test_handle_async_init_reads_runtime_configuration(tmp_path: Path) -> None:
    """Initialisation normalises names, exposed players and continuation state."""
    plugin, _mass = _plugin(
        tmp_path,
        {
            CONF_INSTANCE_NAME: "Alice Room",
            CONF_DIALOG_SKILL_ID: "skill-1",
            CONF_DIALOG_WEBHOOK_SECRET: "secret-1",
            CONF_EXPOSED_PLAYERS: ["p1", 2],
            CONF_DIALOG_VOICE_CONTINUATION: True,
        },
    )

    await plugin.handle_async_init()

    assert plugin._instance_name == "Alice Room"
    assert plugin._dialog_skill_id == "skill-1"
    assert plugin._dialog_webhook_secret == "secret-1"
    assert plugin._exposed_player_ids == {"p1", "2"}
    assert plugin._voice_continuation is True


@pytest.mark.asyncio
async def test_loaded_and_unload_register_exactly_one_route(tmp_path: Path) -> None:
    """The provider lifecycle creates the handler, registers it, then tears it down."""
    plugin, mass = _plugin(
        tmp_path,
        {
            CONF_DIALOG_SKILL_ID: "skill-1",
            CONF_DIALOG_WEBHOOK_SECRET: "secret-1",
        },
    )
    unregister = MagicMock()
    mass.webserver.register_dynamic_route.return_value = unregister
    await plugin.handle_async_init()

    await plugin.loaded_in_mass()

    assert plugin._dialogs_handler is not None
    mass.webserver.register_dynamic_route.assert_called_once()
    assert mass.webserver.register_dynamic_route.call_args.args[0] == (
        "/api/yandex_dialogs/webhook/secret-1"
    )

    await plugin.unload()

    unregister.assert_called_once()
    assert plugin._dialogs_handler is None


@pytest.mark.asyncio
async def test_loaded_without_credentials_does_not_register_route(tmp_path: Path) -> None:
    """Incomplete manual configuration keeps the runtime handler disabled."""
    plugin, mass = _plugin(tmp_path, {})
    await plugin.handle_async_init()

    await plugin.loaded_in_mass()

    assert plugin._dialogs_handler is None
    mass.webserver.register_dynamic_route.assert_not_called()


@pytest.mark.asyncio
async def test_replace_manifest_provider_updates_running_handler(tmp_path: Path) -> None:
    """A successful config action can activate its preloaded snapshot immediately."""
    plugin, _mass = _plugin(
        tmp_path,
        {
            CONF_DIALOG_SKILL_ID: "skill-1",
            CONF_DIALOG_WEBHOOK_SECRET: "secret-1",
        },
    )
    await plugin.handle_async_init()
    await plugin.loaded_in_mass()
    replacement = SkillManifestProvider(plugin.mass)

    plugin.replace_manifest_provider(replacement)

    assert plugin._dialogs_handler is not None
    assert plugin._dialogs_handler._manifest_provider is replacement
