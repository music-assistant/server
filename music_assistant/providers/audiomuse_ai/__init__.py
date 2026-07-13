"""
AudioMuse-AI plugin.

A thin HTTP client over an external AudioMuse-AI server
(https://github.com/NeptuneHub/AudioMuse-AI). Where the ``sonic_similarity``
plugin computes similarity locally from ``sonic_analysis`` data, this plugin
delegates all similarity, recommendation, and free-text search to a running
AudioMuse-AI instance via its REST API.

The two id spaces are bridged by a single fact: AudioMuse-AI keys tracks on the
media server's native item id, which is the same id Music Assistant stores in
that server's provider mapping. The ``media_provider`` config entry names which
Music Assistant provider to map against, so results round-trip back to real
Tracks via ``mass.music.tracks.get(item_id, media_provider)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from music_assistant.providers.audiomuse_ai.constants import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_ENABLE_DISCOVER_ROW,
    CONF_ENABLE_TEXT_SEARCH,
    CONF_LABEL_STATUS,
    CONF_MEDIA_PROVIDER,
    LIBRARY_DOMAIN,
    SUPPORTED_FEATURES,
)
from music_assistant.providers.audiomuse_ai.provider import AudioMuseAiPlugin

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    features = SUPPORTED_FEATURES.copy()
    if bool(config.get_value(CONF_ENABLE_TEXT_SEARCH)):
        features.add(ProviderFeature.SEARCH)
    return AudioMuseAiPlugin(mass, manifest, config, features)


async def _status_label(mass: MusicAssistant, instance_id: str | None) -> str:
    """
    Return a single-line connection-status string for the config page.

    Degrades gracefully before the provider is loaded and never raises.

    :param mass: MusicAssistant instance used to look up the loaded provider.
    :param instance_id: Provider instance id to inspect, or None on first setup.
    """
    if not instance_id:
        return "Not yet configured"
    provider = mass.get_provider(instance_id)
    if not isinstance(provider, AudioMuseAiPlugin):
        return "Not yet loaded"
    status = await provider._handle_status()
    base_url = status.get("base_url") or "(no URL)"
    if not status.get("reachable"):
        return f"Cannot reach AudioMuse-AI server at {base_url}"
    parts = [f"Connected to {base_url}"]
    clap = status.get("clap") or {}
    if isinstance(clap.get("num_embeddings"), int):
        parts.append(f"{clap['num_embeddings']:,} CLAP embeddings")
    return " · ".join(parts)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    :param mass: MusicAssistant instance.
    :param instance_id: id of an existing provider instance (None if new instance setup).
    :param action: action key called from config entries UI.
    :param values: the (intermediate) raw values for config entries sent with the action.
    """
    from music_assistant_models.config_entries import (  # noqa: PLC0415
        ConfigEntry,
        ConfigValueOption,
    )
    from music_assistant_models.enums import (  # noqa: PLC0415
        ConfigEntryType,
        ProviderType,
    )

    # The media-server provider whose item ids match AudioMuse-AI's. The library
    # aggregator is excluded — AA ids are the streaming/file provider's ids.
    provider_options = [
        ConfigValueOption(title=prov.name, value=prov.instance_id)
        for prov in mass.get_providers(ProviderType.MUSIC)
        if prov.domain != LIBRARY_DOMAIN
    ]

    return (
        ConfigEntry(
            key=CONF_BASE_URL,
            type=ConfigEntryType.STRING,
            required=True,
        ),
        ConfigEntry(
            key=CONF_API_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            required=False,
        ),
        ConfigEntry(
            key=CONF_MEDIA_PROVIDER,
            type=ConfigEntryType.STRING,
            options=provider_options,
            required=True,
        ),
        ConfigEntry(
            key=CONF_ENABLE_TEXT_SEARCH,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
        ),
        ConfigEntry(
            key=CONF_ENABLE_DISCOVER_ROW,
            type=ConfigEntryType.BOOLEAN,
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_LABEL_STATUS,
            type=ConfigEntryType.LABEL,
            label=await _status_label(mass, instance_id),
            category="status",
        ),
    )
