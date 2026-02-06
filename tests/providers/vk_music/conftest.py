"""Shared fixtures and stubs for VK Music provider tests."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping


class ProviderStub:
    """Minimal provider-like object for parser tests (no Mock).

    Provides the minimal interface needed by parse_* functions.
    """

    domain = "vk_music"
    instance_id = "vk_music_test_instance"

    def __init__(self) -> None:
        """Initialize stub with minimal client."""
        self.client = type("ClientStub", (), {"user_id": 12345})()

    def get_item_mapping(self, media_type: MediaType | str, key: str, name: str) -> ItemMapping:
        """Return ItemMapping for the given media type, key and name."""
        return ItemMapping(
            media_type=MediaType(media_type) if isinstance(media_type, str) else media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )


@pytest.fixture
def provider_stub() -> ProviderStub:
    """Return a real provider stub (no Mock)."""
    return ProviderStub()
