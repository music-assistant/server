"""Tests for the rate limit state of the Wikipedia metadata provider."""

from unittest.mock import MagicMock

from music_assistant.providers.wikipedia import SUPPORTED_FEATURES, WikipediaMetadataProvider


async def test_rate_limited_follows_the_throttler_cooldown() -> None:
    """The provider reports a rate limit exactly while its throttler holds requests back."""
    manifest = MagicMock()
    manifest.domain = "wikipedia"
    config = MagicMock()
    config.get_value.return_value = None
    provider = WikipediaMetadataProvider(MagicMock(), manifest, config, SUPPORTED_FEATURES)
    await provider.handle_async_init()

    assert not provider.rate_limited

    provider.throttler.set_cooldown(60)

    assert provider.rate_limited
