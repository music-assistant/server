"""Tests for the Digitally Imported provider."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ProviderType

from music_assistant.models.music_provider import ProviderStreamLimitError
from music_assistant.providers.digitally_incorporated import (
    SUPPORTED_FEATURES,
    DigitallyImportedProvider,
)


def _make_provider() -> DigitallyImportedProvider:
    """Construct the provider without loading it."""
    manifest = MagicMock()
    manifest.type = ProviderType.MUSIC
    manifest.domain = "digitally_incorporated"
    manifest.name = "Digitally Imported"
    config = MagicMock()
    config.name = "Digitally Imported"
    config.instance_id = "digitally_incorporated"
    config.get_value.return_value = "GLOBAL"
    return DigitallyImportedProvider(MagicMock(), manifest, config, SUPPORTED_FEATURES)


async def test_one_listen_key_plays_one_stream_at_a_time() -> None:
    """A second stream on the same subscription waits for the first one to end."""
    provider = _make_provider()

    assert provider.max_concurrent_streams == 1
    async with provider.acquire_stream_slot(0):
        assert not provider.has_available_stream_slot
        with pytest.raises(ProviderStreamLimitError):
            async with provider.acquire_stream_slot(0):
                pytest.fail("A second stream was started on the same subscription")
    assert provider.has_available_stream_slot
