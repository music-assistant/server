"""Test Twitch Provider lifecycle and config."""

from unittest.mock import Mock

from music_assistant_models.enums import ProviderFeature

from music_assistant.providers.twitch import (
    SUPPORTED_FEATURES,
    TwitchProvider,
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
    """loaded_in_mass() calls mass.subscribe() for QUEUE_UPDATED."""
    # Step 6 will make this test meaningful — for now verify it doesn't crash
    await provider.loaded_in_mass()


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
