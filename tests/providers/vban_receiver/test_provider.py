"""Tests for the VBAN receiver provider."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_ALLOW_AUDIO_ANALYSIS
from music_assistant.providers.vban_receiver.provider import VBANReceiverProvider


async def _provider(values: dict[str, Any]) -> VBANReceiverProvider:
    """Load the provider the way the server does: its config resolved against its entries."""
    mass = MagicMock()
    # nothing collected by the setup flow
    mass.config.get.return_value = {}
    manifest = MagicMock(domain="vban_receiver")
    manifest.name = "VBAN Receiver"
    raw = {"type": "plugin", "domain": "vban_receiver", "instance_id": "vban_receiver"}
    unresolved = cast("ProviderConfig", ProviderConfig.parse([], {**raw, "values": {}}))
    entries = await VBANReceiverProvider(mass, manifest, unresolved).get_config_entries()
    config = cast("ProviderConfig", ProviderConfig.parse(entries, {**raw, "values": values}))
    return VBANReceiverProvider(mass, manifest, config)


async def test_analysis_is_allowed_until_the_user_disables_it() -> None:
    """The input may be analysed by default, through a switch in the plugin's settings."""
    provider = await _provider({})
    entries = await provider.get_config_entries()
    entry = next((e for e in entries if e.key == CONF_ALLOW_AUDIO_ANALYSIS), None)
    assert entry is not None
    assert entry.type == ConfigEntryType.BOOLEAN
    assert entry.default_value is True
    assert [s.allow_audio_analysis for s in await provider.get_audio_sources()] == [True]


async def test_a_disabled_switch_keeps_analysis_off_the_input() -> None:
    """An input that should not be listened to, such as a microphone, is not offered for analysis."""
    provider = await _provider({CONF_ALLOW_AUDIO_ANALYSIS: False})
    assert [s.allow_audio_analysis for s in await provider.get_audio_sources()] == [False]
