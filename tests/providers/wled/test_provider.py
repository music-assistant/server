"""Tests for the WLED provider's config-change handling."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from music_assistant.providers.wled.constants import (
    CONF_GAIN_DB,
    CONF_LATENCY_MS,
    CONF_SCALING_MODE,
    DEFAULT_GAIN_DB,
    DEFAULT_LATENCY_MS,
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
