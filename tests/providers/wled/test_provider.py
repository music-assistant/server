"""Tests for the WLED provider's config-change handling and duplicate-port guard."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import SetupFailedError

import music_assistant.providers.wled.provider as provider_module
from music_assistant.providers.wled.constants import (
    CONF_GAIN_DB,
    CONF_LATENCY_MS,
    CONF_PORT,
    CONF_SCALING_MODE,
    DEFAULT_GAIN_DB,
    DEFAULT_LATENCY_MS,
    DEFAULT_PORT,
)
from music_assistant.providers.wled.provider import WledProvider


def _make_provider(values: dict[str, Any] | None = None) -> tuple[WledProvider, MagicMock]:
    """Return a WledProvider with a mocked bridge manager, backed by mocked MA infra."""
    values = values or {}
    mass = MagicMock()
    manifest = MagicMock(domain="wled")
    config = MagicMock()
    config.instance_id = "wled_test"
    config.get_value = MagicMock(side_effect=lambda key, default=None: values.get(key, default))
    provider = WledProvider(mass, manifest, config, set())
    provider._bridge_manager = MagicMock()
    return provider, provider._bridge_manager


def _fake_sibling(instance_id: str, name: str = "WLED") -> MagicMock:
    """Build a minimal fake sibling ProviderConfig for the duplicate-port scan."""
    sibling = MagicMock()
    sibling.instance_id = instance_id
    sibling.name = name
    return sibling


def _provider_with_siblings(
    own_port: int,
    siblings: list[MagicMock],
    raw_ports: dict[str, int] | None = None,
    setup_ports: dict[str, int] | None = None,
) -> WledProvider:
    """
    Return a provider whose sibling scan sees the given instances and ports.

    :param own_port: The port this instance itself resolves to.
    :param siblings: The configs get_provider_configs() hands back (mass includes the
        instance's own config in that listing, so pass it in where that matters).
    :param raw_ports: instance_id -> port stored under "values" (an options-UI override).
    :param setup_ports: instance_id -> port stored in "setup_data", readable even for a
        sibling that is not loaded (see _port_from_config).
    """
    raw_ports = dict(raw_ports or {})
    setup_ports = setup_ports or {}
    raw_ports.setdefault("wled_test", own_port)
    provider, _ = _make_provider()
    provider.mass.config.get_provider_configs = AsyncMock(  # type: ignore[method-assign]
        return_value=siblings
    )
    provider.mass.config.get_raw_provider_config_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda instance_id, key, default=None: (
            raw_ports.get(instance_id, default) if key == CONF_PORT else default
        )
    )
    provider.mass.config.get_provider_setup_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda instance_id, key, default=None: (
            setup_ports.get(instance_id, default) if key == CONF_PORT else default
        )
    )
    return provider


class TestLoadedInMassAvailability:
    """
    Tests that `available` always reflects whether a bridge is actually running.

    Mass registers a provider as available *before* it runs loaded_in_mass, and only
    logs an exception raised from that post-load hook (see mass._register_loaded_provider
    / _on_provider_loaded), so this hook has to clear availability itself rather than
    relying on a failure being noticed for it.
    """

    async def _run_loaded_in_mass(self, start: AsyncMock) -> WledProvider:
        """Run loaded_in_mass with a mocked bridge manager whose start() is given."""
        provider, _ = _make_provider()
        provider.available = True  # as mass leaves it before the post-load hook runs
        provider.mass.config.get_raw_provider_config_value = MagicMock(  # type: ignore[method-assign]
            return_value=DEFAULT_PORT
        )
        manager = MagicMock()
        manager.start = start
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                provider_module, "WledBridgeManager", MagicMock(return_value=manager)
            )
            await provider.loaded_in_mass()
        return provider

    async def test_available_when_the_bridge_came_up(self) -> None:
        """A started bridge is what makes this instance available."""
        provider = await self._run_loaded_in_mass(AsyncMock(return_value=True))
        assert provider.available is True

    async def test_not_available_when_sendspin_is_missing(self) -> None:
        """start() returning False (no Sendspin yet) must not read as healthy."""
        provider = await self._run_loaded_in_mass(AsyncMock(return_value=False))
        assert provider.available is False

    async def test_not_available_when_the_bridge_fails_to_start(self) -> None:
        """A raised startup error (e.g. the UDP port is taken) must clear availability."""
        provider, _ = _make_provider()
        provider.available = True
        provider.mass.config.get_raw_provider_config_value = MagicMock(  # type: ignore[method-assign]
            return_value=DEFAULT_PORT
        )
        manager = MagicMock()
        manager.start = AsyncMock(side_effect=OSError("address already in use"))
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                provider_module, "WledBridgeManager", MagicMock(return_value=manager)
            )
            with pytest.raises(OSError, match="address already in use"):
                await provider.loaded_in_mass()

        assert provider.available is False


class TestDuplicatePortGuard:
    """
    Tests for handle_async_init's one-zone-per-port rule.

    Two instances sharing a port would derive the same Sendspin client_id and evict
    each other's registration instead of failing loudly, so the second one has to be
    rejected at load time -- see the method's own docstring.
    """

    async def test_rejects_a_sibling_already_using_the_same_port(self) -> None:
        """A second instance on a taken port must fail to load, naming the conflict."""
        provider = _provider_with_siblings(
            own_port=DEFAULT_PORT,
            siblings=[_fake_sibling("wled_other", name="Living Room")],
            raw_ports={"wled_other": DEFAULT_PORT},
        )

        with pytest.raises(SetupFailedError, match="Living Room"):
            await provider.handle_async_init()

    async def test_allows_a_sibling_on_a_different_port(self) -> None:
        """Zones on their own ports are the supported multi-instance setup."""
        provider = _provider_with_siblings(
            own_port=DEFAULT_PORT,
            siblings=[_fake_sibling("wled_other")],
            raw_ports={"wled_other": DEFAULT_PORT + 1},
        )

        await provider.handle_async_init()  # must not raise

    async def test_does_not_reject_itself(self) -> None:
        """The scan includes this instance's own config, which is never a conflict."""
        provider = _provider_with_siblings(
            own_port=DEFAULT_PORT,
            siblings=[_fake_sibling("wled_test")],
        )

        await provider.handle_async_init()  # must not raise

    async def test_detects_an_unloaded_sibling_whose_port_is_only_in_setup_data(self) -> None:
        """
        A sibling that failed to load still holds its port and must still be detected.

        Its config resolves no CONF_PORT entry (mass only injects the server-side
        defaults for an unloaded instance), so the port is only readable from
        setup_data -- see _port_from_config.
        """
        provider = _provider_with_siblings(
            own_port=DEFAULT_PORT,
            siblings=[_fake_sibling("wled_unloaded")],
            setup_ports={"wled_unloaded": DEFAULT_PORT},
        )

        with pytest.raises(SetupFailedError, match=str(DEFAULT_PORT)):
            await provider.handle_async_init()


class TestUpdateConfigImmediateApply:
    """
    Tests for which config changes get applied without a full provider reload.

    Regression coverage: Config.get_changed_values() (and Provider.update_config's own
    changed_keys check) reports changed keys namespaced as "values/<key>", not the bare
    key -- see Provider.update_config's `k.startswith("values/")` check and the Hue
    Entertainment provider's equivalent immediate-apply set. Comparing against bare keys
    means the condition can never match, so every latency/gain/scaling-mode edit would
    silently fall through to a full reload instead of the cheap in-place update.
    """

    async def test_namespaced_keys_trigger_the_in_place_update(self) -> None:
        """The real (namespaced) changed_keys format must hit update_settings, not reload."""
        provider, bridge_manager = _make_provider(
            {CONF_LATENCY_MS: 250, CONF_GAIN_DB: 12.0, CONF_SCALING_MODE: "linear"}
        )
        changed_keys = {f"values/{CONF_LATENCY_MS}", f"values/{CONF_GAIN_DB}"}

        await provider.update_config(provider.config, changed_keys)

        bridge_manager.update_settings.assert_called_once_with(
            latency_ms=250, gain_db=12.0, scaling_mode="linear"
        )

    async def test_a_configured_zero_latency_and_gain_are_preserved(self) -> None:
        """0ms latency and 0dB gain are valid settings and must not fall back to the default."""
        provider, bridge_manager = _make_provider({CONF_LATENCY_MS: 0, CONF_GAIN_DB: 0.0})
        changed_keys = {f"values/{CONF_LATENCY_MS}", f"values/{CONF_GAIN_DB}"}

        await provider.update_config(provider.config, changed_keys)

        bridge_manager.update_settings.assert_called_once()
        kwargs = bridge_manager.update_settings.call_args.kwargs
        assert kwargs["latency_ms"] == 0
        assert kwargs["gain_db"] == 0.0

    async def test_missing_values_fall_back_to_the_documented_defaults(self) -> None:
        """An unset latency/gain still resolves to the provider's documented defaults."""
        provider, bridge_manager = _make_provider({})
        changed_keys = {f"values/{CONF_LATENCY_MS}"}

        await provider.update_config(provider.config, changed_keys)

        kwargs = bridge_manager.update_settings.call_args.kwargs
        assert kwargs["latency_ms"] == DEFAULT_LATENCY_MS
        assert kwargs["gain_db"] == DEFAULT_GAIN_DB
