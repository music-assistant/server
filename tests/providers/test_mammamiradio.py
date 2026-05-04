"""Tests for the mammamiradio music provider."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from music_assistant_models.enums import ContentType, MediaType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import Radio, SearchResults

from music_assistant.providers.mammamiradio import (
    CONF_MAMMAMIRADIO_URL,
    DEFAULT_URL,
    RADIO_ITEM_ID,
    RADIO_NAME,
    SUPPORTED_FEATURES,
    MammamiradioProvider,
    get_config_entries,
)


def _make_response_ctx(status: int = 200) -> MagicMock:
    """Build an async-context-manager mock that yields a response with `status`."""
    response = MagicMock()
    response.status = status
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _make_failing_ctx(exc: Exception) -> MagicMock:
    """Build an async-context-manager mock whose __aenter__ raises ``exc``."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(side_effect=exc)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


@pytest.fixture
def mass_mock() -> MagicMock:
    """Return a mock MusicAssistant instance with an http_session."""
    mass = MagicMock()
    mass.http_session = MagicMock()
    # default: every request succeeds with HTTP 200
    mass.http_session.get = MagicMock(return_value=_make_response_ctx(200))
    mass.http_session.head = MagicMock(return_value=_make_response_ctx(200))
    return mass


@pytest.fixture
def provider(mass_mock: MagicMock) -> MammamiradioProvider:
    """Return a configured MammamiradioProvider for unit testing."""
    manifest = MagicMock()
    manifest.domain = "mammamiradio"
    manifest.name = "mammamiradio"

    config = MagicMock()
    config.instance_id = "mammamiradio_test"

    def _get_value(key: str, default: Any = None) -> Any:
        if key == CONF_MAMMAMIRADIO_URL:
            return "http://localhost:8100"
        if key == "log_level":
            return "GLOBAL"
        return default

    config.get_value.side_effect = _get_value

    return MammamiradioProvider(mass_mock, manifest, config, SUPPORTED_FEATURES)


# ---------------------------------------------------------------------------
# Configuration entries
# ---------------------------------------------------------------------------


async def test_get_config_entries_returns_single_url_field() -> None:
    """get_config_entries must expose exactly one required URL string field."""
    entries = await get_config_entries(MagicMock())
    assert len(entries) == 1
    entry = entries[0]
    assert entry.key == CONF_MAMMAMIRADIO_URL
    assert entry.required is True
    assert entry.default_value == DEFAULT_URL
    assert entry.type.value == "string"


# ---------------------------------------------------------------------------
# Path 1 / 2: handle_async_init reachability
# ---------------------------------------------------------------------------


async def test_handle_async_init_passes_when_reachable(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """Path 1 — reachable addon: init logs success and does not raise."""
    mass_mock.http_session.get = MagicMock(return_value=_make_response_ctx(200))
    await provider.handle_async_init()
    mass_mock.http_session.get.assert_called_once()
    called_url = mass_mock.http_session.get.call_args.args[0]
    assert called_url == "http://localhost:8100/api/capabilities"


async def test_handle_async_init_logs_but_does_not_raise_when_unreachable(
    provider: MammamiradioProvider,
    mass_mock: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Path 2 — unreachable addon: init logs a clean warning and does NOT raise."""
    mass_mock.http_session.get = MagicMock(
        return_value=_make_failing_ctx(aiohttp.ClientConnectionError("nope"))
    )
    # Must not raise even though the addon is offline.
    await provider.handle_async_init()
    assert any("unreachable" in rec.message.lower() for rec in caplog.records), (
        "expected a warning about the addon being unreachable"
    )


# ---------------------------------------------------------------------------
# Path 3 / 12: browse() returns a single Radio entry
# ---------------------------------------------------------------------------


async def test_browse_returns_single_radio_entry(
    provider: MammamiradioProvider,
) -> None:
    """Path 3 — browse() returns the one mammamiradio Radio object."""
    items = await provider.browse("mammamiradio://")
    assert len(items) == 1
    radio = items[0]
    assert isinstance(radio, Radio)
    assert radio.item_id == RADIO_ITEM_ID
    assert radio.name == RADIO_NAME
    # Single ProviderMapping wired to this provider instance.
    mappings = list(radio.provider_mappings)
    assert len(mappings) == 1
    assert mappings[0].provider_domain == "mammamiradio"
    assert mappings[0].available is True


# ---------------------------------------------------------------------------
# Path 4 / 5 / 6: search behaviour
# ---------------------------------------------------------------------------


async def test_search_exact_name_returns_entry(
    provider: MammamiradioProvider,
) -> None:
    """Path 4 — search('mammamiradio') returns the Radio entry."""
    results = await provider.search("mammamiradio", [MediaType.RADIO])
    assert isinstance(results, SearchResults)
    assert len(results.radio) == 1
    assert results.radio[0].item_id == RADIO_ITEM_ID


async def test_search_substring_returns_entry(
    provider: MammamiradioProvider,
) -> None:
    """Path 5 — search('mamma') matches the entry by substring (case-insensitive)."""
    results = await provider.search("MAMMA", [MediaType.RADIO])
    assert len(results.radio) == 1
    assert results.radio[0].item_id == RADIO_ITEM_ID


async def test_search_no_match_returns_empty(
    provider: MammamiradioProvider,
) -> None:
    """Path 6 — search('zzz') returns no results."""
    results = await provider.search("zzz", [MediaType.RADIO])
    assert results.radio == []


async def test_search_without_radio_media_type_returns_empty(
    provider: MammamiradioProvider,
) -> None:
    """search() respects the media_types filter — no Radio in filter, no results."""
    results = await provider.search("mammamiradio", [MediaType.TRACK])
    assert results.radio == []


# ---------------------------------------------------------------------------
# Path 7 / 8: get_radio
# ---------------------------------------------------------------------------


async def test_get_radio_with_valid_id_returns_radio(
    provider: MammamiradioProvider,
) -> None:
    """Path 7 — get_radio(valid_id) returns a fully-populated Radio."""
    radio = await provider.get_radio(RADIO_ITEM_ID)
    assert isinstance(radio, Radio)
    assert radio.item_id == RADIO_ITEM_ID
    assert radio.name == RADIO_NAME
    assert radio.metadata.description
    assert radio.metadata.genres
    assert radio.metadata.languages == ["it"]


async def test_get_radio_with_invalid_id_raises_media_not_found(
    provider: MammamiradioProvider,
) -> None:
    """Path 8 — get_radio('bogus') raises MediaNotFoundError."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_radio("does-not-exist")


# ---------------------------------------------------------------------------
# Path 9 / 10 / 11: get_stream_details
# ---------------------------------------------------------------------------


async def test_get_stream_details_returns_mp3_format(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """Path 9 — StreamDetails declares ContentType.MP3 with hard-coded 128k bitrate."""
    mass_mock.http_session.head = MagicMock(return_value=_make_response_ctx(200))
    details = await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.audio_format.content_type == ContentType.MP3
    assert details.audio_format.bit_rate == 128
    assert details.media_type == MediaType.RADIO


async def test_get_stream_details_uses_configured_url_with_stream_suffix(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """Path 10 — stream path is hard-coded as ``${url}/stream``."""
    mass_mock.http_session.head = MagicMock(return_value=_make_response_ctx(200))
    details = await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8100/stream"
    assert details.allow_seek is False
    assert details.can_seek is False


async def test_get_stream_details_raises_provider_unavailable_when_offline(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """Path 11 — addon offline at stream time raises ProviderUnavailableError."""
    mass_mock.http_session.head = MagicMock(
        return_value=_make_failing_ctx(aiohttp.ClientConnectionError("offline"))
    )
    with pytest.raises(ProviderUnavailableError):
        await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)


async def test_get_stream_details_raises_provider_unavailable_on_http_error(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """5xx from the addon raises ProviderUnavailableError too (sibling of Path 11)."""
    mass_mock.http_session.head = MagicMock(return_value=_make_response_ctx(503))
    with pytest.raises(ProviderUnavailableError):
        await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)


async def test_get_stream_details_with_invalid_id_raises_media_not_found(
    provider: MammamiradioProvider,
) -> None:
    """Unknown item id at stream time raises MediaNotFoundError, not unavailable."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("does-not-exist", MediaType.RADIO)


# ---------------------------------------------------------------------------
# URL hygiene + supported features (locks the contract for the discovery flow)
# ---------------------------------------------------------------------------


async def test_supported_features_are_browse_and_search_only() -> None:
    """SUPPORTED_FEATURES is exactly {BROWSE, SEARCH} (matches SomaFM)."""
    assert {ProviderFeature.BROWSE, ProviderFeature.SEARCH} == SUPPORTED_FEATURES


async def test_trailing_slash_in_configured_url_does_not_double_up(
    mass_mock: MagicMock,
) -> None:
    """A configured URL with a trailing slash should still produce ``${url}/stream``."""
    manifest = MagicMock()
    manifest.domain = "mammamiradio"
    manifest.name = "mammamiradio"
    config = MagicMock()
    config.instance_id = "mammamiradio_test"

    def _get_value(key: str, default: Any = None) -> Any:
        if key == CONF_MAMMAMIRADIO_URL:
            return "http://localhost:8100/"
        if key == "log_level":
            return "GLOBAL"
        return default

    config.get_value.side_effect = _get_value
    prov = MammamiradioProvider(mass_mock, manifest, config, SUPPORTED_FEATURES)
    mass_mock.http_session.head = MagicMock(return_value=_make_response_ctx(200))
    details = await prov.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8100/stream"
