"""Shared fixtures and stubs for KION Music provider tests."""

from __future__ import annotations

import logging

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping


class ProviderStub:
    """Minimal provider-like object for parser tests (no Mock).

    Provides the minimal interface needed by parse_* functions.
    """

    domain = "kion_music"
    instance_id = "kion_music_instance"

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


class StreamingProviderStub:
    """Minimal provider stub for streaming tests (no Mock).

    Provides the minimal interface needed by KionMusicStreamingManager.
    """

    domain = "kion_music"
    instance_id = "kion_music_instance"
    logger = logging.getLogger("kion_music_test_streaming")

    def __init__(self) -> None:
        """Initialize stub with minimal client."""
        self.client = type("ClientStub", (), {"user_id": 12345})()
        self.mass = type("MassStub", (), {})()
        self._warning_count = 0

    def _count_warning(self, *args: object, **kwargs: object) -> None:
        """Track warning calls for test assertions."""
        self._warning_count += 1


class TrackingLogger:
    """Logger that tracks calls for test assertions without using Mock."""

    def __init__(self) -> None:
        """Initialize with empty call counters."""
        self._debug_count = 0
        self._info_count = 0
        self._warning_count = 0
        self._error_count = 0

    def debug(self, *args: object, **kwargs: object) -> None:
        """Track debug calls."""
        self._debug_count += 1

    def info(self, *args: object, **kwargs: object) -> None:
        """Track info calls."""
        self._info_count += 1

    def warning(self, *args: object, **kwargs: object) -> None:
        """Track warning calls."""
        self._warning_count += 1

    def error(self, *args: object, **kwargs: object) -> None:
        """Track error calls."""
        self._error_count += 1


class StreamingProviderStubWithTracking:
    """Provider stub with tracking logger for assertions.

    Use this when you need to verify logging behavior.
    """

    domain = "kion_music"
    instance_id = "kion_music_instance"

    def __init__(self) -> None:
        """Initialize stub with tracking logger."""
        self.client = type("ClientStub", (), {"user_id": 12345})()
        self.mass = type("MassStub", (), {})()
        self.logger = TrackingLogger()


# Minimal client-like object for kion_music de_json (library requires client, not None)
DE_JSON_CLIENT = type("ClientStub", (), {"report_unknown_fields": False})()


@pytest.fixture
def provider_stub() -> ProviderStub:
    """Return a real provider stub (no Mock)."""
    return ProviderStub()


@pytest.fixture
def streaming_provider_stub() -> StreamingProviderStub:
    """Return a streaming provider stub (no Mock)."""
    return StreamingProviderStub()


@pytest.fixture
def streaming_provider_stub_with_tracking() -> StreamingProviderStubWithTracking:
    """Return a streaming provider stub with tracking logger."""
    return StreamingProviderStubWithTracking()
