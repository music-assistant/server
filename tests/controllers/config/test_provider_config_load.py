"""
Regression tests for the provider config save -> (re)load flow.

Covers three fixes for the Spotify auth failures caused by refresh-token rotation:
- Saving an enabled-but-unloaded provider triggers exactly one (re)load. A second,
  redundant load reused the same (already rotated and revoked) refresh token, which
  wiped the credentials right after a successful auth.
- A failed (re)load records ``last_error`` so the provider surfaces a clear status
  (e.g. auth_required) instead of appearing stuck loading (a spinning hourglass).
- A raw provider value stored with ``immediate=True`` is flushed straight away instead of
  on the debounced timer, so a rotated (and thus revoked) token is not lost on a crash.

Also covers the post-load dependent scan, which must neither resolve the option values of
every provider nor record its own failures against the provider that just loaded fine.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import ConfigEntry, ProviderConfig, ProviderError
from music_assistant_models.enums import ConfigEntryType, ProviderStatus, ProviderType
from music_assistant_models.errors import LoginFailed, SetupFailedError

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.controllers.config.helpers import _provider_status
from music_assistant.mass import MusicAssistant


def _prov_conf(instance_id: str) -> ProviderConfig:
    return ProviderConfig(
        values={},
        type=ProviderType.MUSIC,
        domain="spotify",
        instance_id=instance_id,
        enabled=True,
    )


async def test_save_enabled_unloaded_provider_loads_once(mass_minimal: MusicAssistant) -> None:
    """Saving config for an enabled provider that isn't loaded yet triggers a single load."""
    config = mass_minimal.config
    instance = "spotify--test"
    prov_conf = _prov_conf(instance)
    with (
        patch.object(config, "get_provider_config", AsyncMock(return_value=prov_conf)),
        patch.object(mass_minimal, "load_provider_config", AsyncMock()) as mock_load,
    ):
        await config.save_provider_config("spotify", {"enabled": True}, instance_id=instance)
    mock_load.assert_awaited_once()


async def test_failed_reload_records_auth_error(mass_minimal: MusicAssistant) -> None:
    """A load failure is persisted as last_error so the UI shows auth_required, not a spinner."""
    config = mass_minimal.config
    instance = "spotify--test"
    # the provider config must exist for last_error to be persisted (see #5728)
    config.set(
        f"{CONF_PROVIDERS}/{instance}",
        {"domain": "spotify", "type": "music", "instance_id": instance, "enabled": True},
    )
    with (
        patch.object(
            mass_minimal, "_load_provider", AsyncMock(side_effect=LoginFailed("bad token"))
        ),
        pytest.raises(LoginFailed),
    ):
        await mass_minimal.load_provider_config(_prov_conf(instance))

    stored = config.get(f"{CONF_PROVIDERS}/{instance}/last_error")
    assert stored is not None
    assert stored["error_code"] == LoginFailed.error_code
    prov_conf = _prov_conf(instance)
    prov_conf.last_error = ProviderError.from_dict(stored)
    assert _provider_status(prov_conf, is_loaded=False) == ProviderStatus.AUTH_REQUIRED


async def test_failed_post_load_step_unloads_the_provider(mass_minimal: MusicAssistant) -> None:
    """A provider that fails to finish loading is unloaded, so it can never read as loaded."""
    instance = "spotify--test"
    provider = MagicMock()
    provider.instance_id = instance
    provider.domain = "spotify"
    with (
        patch.object(
            mass_minimal,
            "_update_available_providers_cache",
            AsyncMock(side_effect=TimeoutError),
        ),
        patch.object(mass_minimal, "unload_provider", AsyncMock()) as mock_unload,
        pytest.raises(SetupFailedError, match="timed out while trying to finish loading"),
    ):
        await mass_minimal._register_loaded_provider(provider, _prov_conf(instance))

    mock_unload.assert_awaited_once_with(instance)


async def test_save_preserves_unknown_stored_values(mass_minimal: MusicAssistant) -> None:
    """Stored values without config entries in the current save context survive a save."""
    config = mass_minimal.config
    instance = "spotify--test"
    conf_key = f"{CONF_PROVIDERS}/{instance}"
    config.set(
        conf_key,
        {
            "domain": "spotify",
            "type": "music",
            "instance_id": instance,
            "enabled": True,
            "values": {"legacy_token": "keep-me"},
            "setup_data": {"token": "abc"},
        },
    )
    entry = ConfigEntry(key="region", type=ConfigEntryType.STRING, required=False)
    prov_conf = cast("ProviderConfig", ProviderConfig.parse([entry], config.get(conf_key)))
    with (
        patch.object(config, "get_provider_config", AsyncMock(return_value=prov_conf)),
        patch.object(mass_minimal, "load_provider_config", AsyncMock()),
    ):
        await config._update_provider_config(instance, {"region": "eu"})
    stored = config.get(conf_key)
    assert stored["values"]["region"] == "eu"
    # a value written outside the declared entries (e.g. by a provider at runtime)
    # is preserved instead of being dropped by the save rebuild
    assert stored["values"]["legacy_token"] == "keep-me"
    # setup_data travels along untouched
    assert stored["setup_data"] == {"token": "abc"}


async def test_dependent_scan_does_not_resolve_option_values(
    mass_minimal: MusicAssistant,
) -> None:
    """The post-load dependent scan reads the configs without resolving their option values."""
    instance = "spotify--test"
    with (
        patch.object(mass_minimal, "_load_provider", AsyncMock()),
        patch.object(
            mass_minimal.config, "get_provider_configs", AsyncMock(return_value=[])
        ) as mock_get,
    ):
        await mass_minimal.load_provider_config(_prov_conf(instance))

    # resolving values calls get_config_entries() on every loaded provider, which for some
    # (e.g. Home Assistant) means live network i/o - once per provider load
    assert mock_get.await_args is not None
    assert mock_get.await_args.kwargs.get("include_values") is not True
    assert True not in mock_get.await_args.args


async def test_dependent_scan_failure_is_not_blamed_on_loaded_provider(
    mass_minimal: MusicAssistant,
) -> None:
    """A provider that loaded fine keeps a clean status when the dependent scan fails."""
    config = mass_minimal.config
    instance = "spotify--test"
    config.set(
        f"{CONF_PROVIDERS}/{instance}",
        {"domain": "spotify", "type": "music", "instance_id": instance, "enabled": True},
    )
    with (
        patch.object(mass_minimal, "_load_provider", AsyncMock()),
        patch.object(config, "get_provider_configs", AsyncMock(side_effect=TimeoutError)),
    ):
        # the scan failure is swallowed: the provider itself loaded successfully
        await mass_minimal.load_provider_config(_prov_conf(instance))

    assert config.get(f"{CONF_PROVIDERS}/{instance}/last_error") is None
    assert _provider_status(_prov_conf(instance), is_loaded=True) == ProviderStatus.LOADED


async def test_immediate_flush_for_rotated_token(mass_minimal: MusicAssistant) -> None:
    """Storing a raw provider value with immediate=True flushes without waiting for the debounce."""
    config = mass_minimal.config
    instance = "spotify--test"
    config.set(
        f"{CONF_PROVIDERS}/{instance}",
        {"domain": "spotify", "type": "music", "instance_id": instance, "enabled": True},
    )
    with patch.object(config, "save", MagicMock()) as mock_save:
        config.set_raw_provider_config_value(
            instance, "refresh_token_global", "token_b", immediate=True
        )
    mock_save.assert_called_once_with(immediate=True)
