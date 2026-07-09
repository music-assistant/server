"""Regression test for the provider config save -> (re)load flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderType

from music_assistant.mass import MusicAssistant


async def test_save_enabled_unloaded_provider_loads_once(mass_minimal: MusicAssistant) -> None:
    """
    Saving an enabled provider that isn't loaded yet triggers exactly one (re)load.

    A redundant second load reuses the same refresh token, which Spotify rotates and
    revokes on first use, wiping the credentials right after a successful auth.
    """
    config = mass_minimal.config
    instance_id = "spotify--test"
    prov_conf = ProviderConfig(
        values={},
        type=ProviderType.MUSIC,
        domain="spotify",
        instance_id=instance_id,
        enabled=True,
    )
    with (
        patch.object(config, "get_provider_config", AsyncMock(return_value=prov_conf)),
        patch.object(mass_minimal, "load_provider_config", AsyncMock()) as mock_load,
    ):
        await config.save_provider_config("spotify", {"enabled": True}, instance_id=instance_id)
    mock_load.assert_awaited_once()
