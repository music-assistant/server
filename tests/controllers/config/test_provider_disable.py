"""Tests for disabling a provider through the provider config save command."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.mass import MusicAssistant

DOMAIN = "test_plugin"
INSTANCE = f"{DOMAIN}--test"
CONF_KEY = f"{CONF_PROVIDERS}/{INSTANCE}"


def _setup_provider(mass: MusicAssistant, allow_disable: bool) -> ProviderConfig:
    """Store an enabled provider config and register its manifest."""
    mass._provider_manifests[DOMAIN] = ProviderManifest(
        type=ProviderType.PLUGIN,
        domain=DOMAIN,
        name="Test Plugin",
        description="",
        codeowners=[],
        allow_disable=allow_disable,
    )
    mass.config.set(
        CONF_KEY,
        {"domain": DOMAIN, "type": "plugin", "instance_id": INSTANCE, "enabled": True},
    )
    return cast("ProviderConfig", ProviderConfig.parse([], mass.config.get(CONF_KEY)))


async def test_disable_rejected_when_not_allowed(mass_minimal: MusicAssistant) -> None:
    """A provider that cannot be disabled rejects the request and keeps its stored config."""
    config = mass_minimal.config
    prov_conf = _setup_provider(mass_minimal, allow_disable=False)
    with (
        patch.object(config, "get_provider_config", AsyncMock(return_value=prov_conf)),
        patch.object(mass_minimal, "unload_provider", AsyncMock()) as mock_unload,
        pytest.raises(InvalidDataError) as exc_info,
    ):
        await config.save_provider_config(DOMAIN, {"enabled": False}, instance_id=INSTANCE)

    assert exc_info.value.translation_key == "provider_can_not_be_disabled"
    assert config.get(CONF_KEY)["enabled"] is True
    mock_unload.assert_not_awaited()


async def test_disable_stores_and_unloads_when_allowed(mass_minimal: MusicAssistant) -> None:
    """A provider that may be disabled is stored as disabled and unloaded."""
    config = mass_minimal.config
    prov_conf = _setup_provider(mass_minimal, allow_disable=True)
    with (
        patch.object(config, "get_provider_config", AsyncMock(return_value=prov_conf)),
        patch.object(mass_minimal, "unload_provider", AsyncMock()) as mock_unload,
    ):
        await config.save_provider_config(DOMAIN, {"enabled": False}, instance_id=INSTANCE)

    assert config.get(CONF_KEY)["enabled"] is False
    mock_unload.assert_awaited_once_with(INSTANCE)
