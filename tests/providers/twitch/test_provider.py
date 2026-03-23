"""Test Twitch Provider lifecycle and config."""

from unittest.mock import Mock

from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from music_assistant.providers.twitch import (
    CONF_AD_HANDLING,
    CONF_AUTO_RAID,
    SUPPORTED_FEATURES,
    TwitchProvider,
    get_config_entries,
    setup,
)

# --- Provider Loading ---


async def test_setup_returns_provider_instance(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """setup() returns a TwitchProvider instance."""
    provider = await setup(mass_mock, manifest_mock, config_mock)
    assert isinstance(provider, TwitchProvider)


async def test_provider_is_streaming_provider(provider: TwitchProvider) -> None:
    """is_streaming_provider property returns True."""
    assert provider.is_streaming_provider is True


async def test_supported_features_declared() -> None:
    """SUPPORTED_FEATURES includes BROWSE, SEARCH, LIBRARY_RADIOS."""
    assert ProviderFeature.BROWSE in SUPPORTED_FEATURES
    assert ProviderFeature.SEARCH in SUPPORTED_FEATURES
    assert ProviderFeature.LIBRARY_RADIOS in SUPPORTED_FEATURES


async def test_supported_features_no_edit() -> None:
    """SUPPORTED_FEATURES does not include library edit (Twitch removed follow API)."""
    assert ProviderFeature.LIBRARY_RADIOS_EDIT not in SUPPORTED_FEATURES


async def test_loaded_in_mass_subscribes_to_events(provider: TwitchProvider) -> None:
    """loaded_in_mass() calls mass.subscribe()."""
    await provider.loaded_in_mass()
    provider.mass.subscribe.assert_called_once()  # type: ignore[attr-defined]


async def test_unload_cleans_up(provider: TwitchProvider) -> None:
    """unload() succeeds and cleans up resources."""
    await provider.loaded_in_mass()
    await provider.unload()


async def test_unload_with_no_active_resources(provider: TwitchProvider) -> None:
    """unload() succeeds when nothing is running (fresh provider, no playback)."""
    await provider.unload()


async def test_provider_domain(provider: TwitchProvider) -> None:
    """Provider domain matches manifest."""
    assert provider.domain == "twitch"


async def test_provider_instance_id(provider: TwitchProvider) -> None:
    """Provider instance_id comes from config."""
    assert provider.instance_id == "twitch_test"


# --- Config Entries ---


async def test_config_entries_includes_ad_handling(mass_mock: Mock) -> None:
    """get_config_entries() includes ad_handling config entry."""
    entries = await get_config_entries(mass_mock)
    keys = {e.key for e in entries}
    assert CONF_AD_HANDLING in keys


async def test_config_entries_includes_auto_raid(mass_mock: Mock) -> None:
    """get_config_entries() includes auto_raid config entry."""
    entries = await get_config_entries(mass_mock)
    keys = {e.key for e in entries}
    assert CONF_AUTO_RAID in keys


async def test_ad_handling_is_string_with_options(mass_mock: Mock) -> None:
    """ad_handling config entry is STRING type with options."""
    entries = await get_config_entries(mass_mock)
    entry = next(e for e in entries if e.key == CONF_AD_HANDLING)
    assert entry.type == ConfigEntryType.STRING
    assert len(entry.options) >= 2


async def test_auto_raid_is_boolean(mass_mock: Mock) -> None:
    """auto_raid config entry is BOOLEAN type."""
    entries = await get_config_entries(mass_mock)
    entry = next(e for e in entries if e.key == CONF_AUTO_RAID)
    assert entry.type == ConfigEntryType.BOOLEAN
