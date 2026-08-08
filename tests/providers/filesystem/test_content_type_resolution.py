"""
Regression tests for the content type as resolved through the real config machinery (#6019).

Mirroring the setup-flow owned content type as a LABEL lost it for every share left on the
default: "music" is not in stored values either, since a value equal to its default is never
persisted. It resolved to None, which disabled the sync and hid playlists from the browser.

test_content_type_default.py covers the constructor read against a mocked config; these
drive the load path the server uses, which is what the options page resolves against.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
from music_assistant_models.enums import ConfigEntryType, ProviderType
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_PATH, CONF_PROVIDERS
from music_assistant.controllers.config.providers import DEFAULT_PROVIDER_CONFIG_ENTRIES
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import (
    CONF_CONTENT_TYPE,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

INSTANCE_ID = "filesystem_local--test"

MANIFEST = ProviderManifest(
    type=ProviderType.MUSIC,
    domain="filesystem_local",
    name="Local files",
    description="",
    codeowners=[],
)


async def _load_provider(
    mass: MusicAssistant,
    setup_data: dict[str, str],
    values: dict[str, str] | None = None,
) -> LocalFileSystemProvider:
    """
    Load a filesystem provider the way the server does, and return the instance.

    :param mass: The MusicAssistant instance to load into.
    :param setup_data: The provider's stored setup_data.
    :param values: The provider's stored (raw) config values.
    """
    # the load-time ordering matters here: the config carries the server defaults only
    # until rehydrate_provider_config re-parses it against the provider's own entries
    raw = {
        "type": ProviderType.MUSIC.value,
        "domain": "filesystem_local",
        "instance_id": INSTANCE_ID,
        "default_name": "Local files",
        "enabled": True,
        "values": values or {},
        "setup_data": {CONF_PATH: "/media", **setup_data},
    }
    mass.config.set(f"{CONF_PROVIDERS}/{INSTANCE_ID}", raw)
    config = cast("ProviderConfig", ProviderConfig.parse(DEFAULT_PROVIDER_CONFIG_ENTRIES, raw))
    mass.config.seed_stored_config_values(config)
    provider = LocalFileSystemProvider(mass, MANIFEST, config)
    await mass.config.rehydrate_provider_config(provider)
    return provider


async def test_music_share_without_a_stored_content_type(mass_minimal: MusicAssistant) -> None:
    """An install that never changed the content type reads back as music, not None."""
    # what an install upgraded from before the setup flows has on disk: "music" equals
    # the entry default, so it was never persisted and there is nothing to migrate
    provider = await _load_provider(mass_minimal, setup_data={})

    assert provider.media_content_type == "music"
    assert provider.config.get_value(CONF_CONTENT_TYPE) == "music"


async def test_setup_data_content_type_wins(mass_minimal: MusicAssistant) -> None:
    """A content type collected by the setup flow is used instead of the default."""
    provider = await _load_provider(mass_minimal, setup_data={CONF_CONTENT_TYPE: "podcasts"})

    assert provider.media_content_type == "podcasts"
    assert provider.config.get_value(CONF_CONTENT_TYPE) == "podcasts"


async def test_legacy_stored_content_type_is_read_through(mass_minimal: MusicAssistant) -> None:
    """A non-default content type still in `values` survives until it is migrated."""
    provider = await _load_provider(
        mass_minimal, setup_data={}, values={CONF_CONTENT_TYPE: "audiobooks"}
    )

    assert provider.media_content_type == "audiobooks"


async def test_sync_options_resolve_their_dependency(mass_minimal: MusicAssistant) -> None:
    """The sync options gate on the mirrored content type, so it must hold a real value."""
    provider = await _load_provider(mass_minimal, setup_data={})
    entries = list(provider.config.values.values())

    playlists = provider.config.values[CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS.key]
    podcasts = provider.config.values[CONF_ENTRY_LIBRARY_SYNC_PODCASTS.key]
    assert playlists.dependency_met(entries) is True
    assert podcasts.dependency_met(entries) is False
    # the toggles keep their own (default) value, which drives sync_library
    assert provider.config.get_value(CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS.key) is True


async def test_content_type_mirror_is_never_persisted(mass_minimal: MusicAssistant) -> None:
    """The options-page mirror is display state and must not leak into stored values."""
    provider = await _load_provider(mass_minimal, setup_data={CONF_CONTENT_TYPE: "podcasts"})

    assert CONF_CONTENT_TYPE not in provider.config.to_raw()["values"]


async def test_display_only_entry_does_not_shadow_the_default(
    mass_minimal: MusicAssistant,
) -> None:
    """A UI-only entry holds no value, so reading its key must fall back to the default."""
    provider = await _load_provider(mass_minimal, setup_data={})
    provider.config.values["banner"] = ConfigEntry(
        key="banner", type=ConfigEntryType.LABEL, value="music"
    )

    assert provider.get_config_value("banner", "fallback") == "fallback"
    assert provider.get_setup_value("banner", "fallback") == "fallback"
