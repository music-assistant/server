"""Tests for the mammamiradio music provider."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from multidict import CIMultiDict
from music_assistant_models.enums import ContentType, MediaType, ProviderFeature
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    SetupFailedError,
)
from music_assistant_models.media_items import Radio, SearchResults

from music_assistant.providers.mammamiradio import (
    CONF_MAMMAMIRADIO_URL,
    DEFAULT_URL,
    RADIO_ITEM_ID,
    RADIO_NAME,
    STREAM_METADATA_UPDATE_INTERVAL,
    SUPPORTED_FEATURES,
    MammamiradioProvider,
    _audio_format_from_contract,
    _host_display_names,
    _normalize_base_url,
    _stream_path_from_contract,
    _supports_v1_schema,
    _v1_to_stream_metadata,
    get_config_entries,
    setup,
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


def _make_v1_ctx(payload: Any, status: int = 200, etag: str | None = None) -> MagicMock:
    """Async-context-manager mock for the v1 endpoint, with case-insensitive headers (ETag)."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=payload)
    response.headers = CIMultiDict({"ETag": etag}) if etag else CIMultiDict()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _make_bad_json_ctx(status: int = 200, exc: Exception | None = None) -> MagicMock:
    """Async-context-manager mock whose body is not JSON (``.json()`` raises)."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(side_effect=exc or ValueError("not json"))
    response.headers = CIMultiDict()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# A representative v1 now-playing response (music segment) reused across tests.
_V1_AUDIO_FORMAT: dict[str, Any] = {
    "codec": "mp3",
    "mime_type": "audio/mpeg",
    "bitrate_kbps": 192,
    "sample_rate_hz": 48000,
    "channels": 2,
}
_V1_MUSIC: dict[str, Any] = {
    "schema_version": "1",
    "station": {
        "name": "mammamiradio",
        "hosts": [
            {"engine_host": "gianni", "display_name": "Gianni"},
            {"engine_host": "lucia", "display_name": "Lucia"},
        ],
    },
    "stream": {"relative_url": "/stream", "audio_format": _V1_AUDIO_FORMAT},
    "now_playing": {
        "segment_class": "music",
        "segment_type": "music",
        "title": "Volare",
        "artist": "Modugno",
        "artwork": "http://art/volare.jpg",
        "album": "Best Of",
    },
    "up_next": [
        {
            "segment_class": "voice",
            "segment_type": "banter",
            "title": "Chiacchiere",
            "predicted": False,
        }
    ],
    "session_state": "live",
    "changed_at": 100.0,
}


@pytest.fixture
def mass_mock() -> MagicMock:
    """Return a mock MusicAssistant instance with an http_session."""
    mass = MagicMock()
    mass.http_session = MagicMock()
    # default: every request answers with a healthy v1 now-playing payload
    mass.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC))
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
            return "http://localhost:8000"
        if key == "log_level":
            return "GLOBAL"
        return default

    config.get_value.side_effect = _get_value

    return MammamiradioProvider(mass_mock, manifest, config, SUPPORTED_FEATURES)


@pytest.fixture
async def initialized_provider(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> MammamiradioProvider:
    """Return a provider that completed handle_async_init against a healthy v1 mock."""
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC))
    await provider.handle_async_init()
    # Install a fresh mock so tests using side_effect sequences don't have their
    # first response consumed by the init probe.
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC))
    return provider


def _build_provider_with_url(
    mass_mock: MagicMock, configured_url: str | None
) -> MammamiradioProvider:
    """Build a fresh provider instance configured with ``configured_url``."""
    manifest = MagicMock()
    manifest.domain = "mammamiradio"
    manifest.name = "mammamiradio"
    config = MagicMock()
    config.instance_id = "mammamiradio_test"

    def _get_value(key: str, default: Any = None) -> Any:
        if key == CONF_MAMMAMIRADIO_URL:
            return configured_url
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


async def test_setup_returns_provider_instance(mass_mock: MagicMock) -> None:
    """The module-level setup() entrypoint constructs a MammamiradioProvider."""
    manifest = MagicMock()
    manifest.domain = "mammamiradio"
    manifest.name = "mammamiradio"
    config = MagicMock()
    config.instance_id = "mammamiradio_test"

    def _get_value(key: str, default: Any = None) -> Any:
        if key == CONF_MAMMAMIRADIO_URL:
            return "http://localhost:8000"
        if key == "log_level":
            return "GLOBAL"
        return default

    config.get_value.side_effect = _get_value
    prov = await setup(mass_mock, manifest, config)
    assert isinstance(prov, MammamiradioProvider)


# ---------------------------------------------------------------------------
# handle_async_init — the v1 now-playing contract probe
# ---------------------------------------------------------------------------


async def test_handle_async_init_v1_contract(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A reachable v1 now-playing endpoint caches the base URL, audio format and stream path."""
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"a"'))
    await provider.handle_async_init()
    assert provider._base_url == "http://localhost:8000"
    assert provider._audio_format_dict == _V1_AUDIO_FORMAT
    assert provider._stream_path == "/stream"
    called_url = mass_mock.http_session.get.call_args.args[0]
    assert called_url == "http://localhost:8000/api/integrations/v1/now-playing"


async def test_handle_async_init_none_config_uses_default_url(mass_mock: MagicMock) -> None:
    """An absent URL config value falls back to DEFAULT_URL for the probe."""
    prov = _build_provider_with_url(mass_mock, None)
    await prov.handle_async_init()
    called_url = mass_mock.http_session.get.call_args.args[0]
    assert called_url == f"{DEFAULT_URL}/api/integrations/v1/now-playing"


@pytest.mark.parametrize("status", [404, 405, 501])
async def test_handle_async_init_missing_endpoint_raises_addon_too_old(
    provider: MammamiradioProvider, mass_mock: MagicMock, status: int
) -> None:
    """A missing v1 endpoint (pre-2.13 addon) fails init with an actionable message."""
    mass_mock.http_session.get = MagicMock(return_value=_make_response_ctx(status))
    with pytest.raises(ProviderUnavailableError, match=r"2\.13"):
        await provider.handle_async_init()
    # One probe only — there is no legacy fallback endpoint to try.
    assert mass_mock.http_session.get.call_count == 1


async def test_handle_async_init_5xx_raises(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A 5xx on the v1 probe fails init naming the HTTP status, not the 2.13 message."""
    mass_mock.http_session.get = MagicMock(return_value=_make_response_ctx(503))
    with pytest.raises(ProviderUnavailableError, match="HTTP 503") as excinfo:
        await provider.handle_async_init()
    assert "2.13" not in str(excinfo.value)
    assert mass_mock.http_session.get.call_count == 1


async def test_handle_async_init_non_json_body_raises_addon_too_old(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A 200 whose body is not valid JSON fails init with the 2.13 message."""
    mass_mock.http_session.get = MagicMock(return_value=_make_bad_json_ctx(200))
    with pytest.raises(ProviderUnavailableError, match=r"2\.13"):
        await provider.handle_async_init()
    assert mass_mock.http_session.get.call_count == 1


async def test_handle_async_init_array_payload_raises_addon_too_old(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A 200 whose JSON body is an array (not an object) fails init with the 2.13 message."""
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(["not", "an", "object"]))
    with pytest.raises(ProviderUnavailableError, match=r"2\.13"):
        await provider.handle_async_init()
    assert mass_mock.http_session.get.call_count == 1


async def test_handle_async_init_content_type_error_raises_addon_too_old(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    A non-JSON content-type on the v1 probe fails init with the 2.13 message.

    ``aiohttp.ContentTypeError`` subclasses ``ClientError``, so a 200 HTML page
    (e.g. from an ingress splash) must report "requires addon 2.13+" instead of
    the generic "unreachable" error.
    """
    content_type_error = aiohttp.ContentTypeError(request_info=MagicMock(), history=())
    mass_mock.http_session.get = MagicMock(return_value=_make_bad_json_ctx(exc=content_type_error))
    with pytest.raises(ProviderUnavailableError, match=r"2\.13"):
        await provider.handle_async_init()
    assert mass_mock.http_session.get.call_count == 1


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({k: v for k, v in _V1_MUSIC.items() if k != "schema_version"}, id="absent"),
        pytest.param({**_V1_MUSIC, "schema_version": None}, id="none"),
    ],
)
async def test_handle_async_init_missing_schema_version_raises_addon_too_old(
    provider: MammamiradioProvider, mass_mock: MagicMock, payload: dict[str, Any]
) -> None:
    """A payload without a usable schema_version fails init with the 2.13 message, not 'None'."""
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(payload))
    with pytest.raises(ProviderUnavailableError, match=r"2\.13") as excinfo:
        await provider.handle_async_init()
    assert "None" not in str(excinfo.value)


async def test_handle_async_init_unsupported_schema_names_version(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A reachable but incompatible now-playing schema fails init naming the version."""
    unsupported = {**_V1_MUSIC, "schema_version": "2"}
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(unsupported))
    with pytest.raises(ProviderUnavailableError) as excinfo:
        await provider.handle_async_init()
    assert "schema_version '2'" in str(excinfo.value)


async def test_handle_async_init_numeric_schema_version_accepted(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """schema_version arriving as the JSON number 1 is accepted at init."""
    payload = {**_V1_MUSIC, "schema_version": 1}
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(payload))
    await provider.handle_async_init()
    assert provider._audio_format_dict == _V1_AUDIO_FORMAT
    assert provider._stream_path == "/stream"


async def test_handle_async_init_bool_schema_version_raises_addon_too_old(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A bool schema_version is junk, not a version: init fails with the 2.13 message."""
    payload = {**_V1_MUSIC, "schema_version": True}
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(payload))
    with pytest.raises(ProviderUnavailableError, match=r"2\.13") as excinfo:
        await provider.handle_async_init()
    assert "True" not in str(excinfo.value)


async def test_handle_async_init_without_stream_block_uses_defaults(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A contract without a stream block still initializes with the published defaults."""
    contract = {k: v for k, v in _V1_MUSIC.items() if k != "stream"}
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(contract))
    await provider.handle_async_init()
    details = await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8000/stream"
    assert details.audio_format.content_type == ContentType.MP3
    assert details.audio_format.bit_rate == 192
    assert details.audio_format.sample_rate == 48000
    assert details.audio_format.channels == 2


async def test_handle_async_init_raises_when_unreachable(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    An unreachable addon fails init with a clean "unreachable" error.

    The provider is the canonical place for liveness detection (matches
    RadioBrowser's pattern). Raising here prevents MA from loading a
    non-functional provider and surfaces a clean unavailable error to the
    user instead of letting the stream URL fail silently inside ffmpeg.
    """
    mass_mock.http_session.get = MagicMock(
        return_value=_make_failing_ctx(aiohttp.ClientConnectionError("nope"))
    )
    with pytest.raises(ProviderUnavailableError, match="unreachable"):
        await provider.handle_async_init()


async def test_handle_async_init_generic_client_error_still_raises(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A plain aiohttp.ClientError (true connection failure) must still fail init."""
    mass_mock.http_session.get = MagicMock(
        return_value=_make_failing_ctx(aiohttp.ClientError("boom"))
    )
    with pytest.raises(ProviderUnavailableError, match="unreachable"):
        await provider.handle_async_init()


async def test_handle_async_init_raises_on_timeout(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A timeout on the init probe surfaces as an "unreachable" ProviderUnavailableError."""
    mass_mock.http_session.get = MagicMock(return_value=_make_failing_ctx(TimeoutError("slow")))
    with pytest.raises(ProviderUnavailableError, match="unreachable"):
        await provider.handle_async_init()


# ---------------------------------------------------------------------------
# browse() returns a single Radio entry
# ---------------------------------------------------------------------------


async def test_browse_returns_single_radio_entry(
    initialized_provider: MammamiradioProvider,
) -> None:
    """browse() returns the one mammamiradio Radio object."""
    items = await initialized_provider.browse("mammamiradio://")
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
# search behaviour
# ---------------------------------------------------------------------------


async def test_search_exact_name_returns_entry(
    initialized_provider: MammamiradioProvider,
) -> None:
    """search('mammamiradio') returns the Radio entry."""
    results = await initialized_provider.search("mammamiradio", [MediaType.RADIO])
    assert isinstance(results, SearchResults)
    assert len(results.radio) == 1
    assert results.radio[0].item_id == RADIO_ITEM_ID


async def test_search_substring_returns_entry(
    initialized_provider: MammamiradioProvider,
) -> None:
    """search('mamma') matches the entry by substring (case-insensitive)."""
    results = await initialized_provider.search("MAMMA", [MediaType.RADIO])
    assert len(results.radio) == 1
    assert results.radio[0].item_id == RADIO_ITEM_ID


async def test_search_display_name_returns_entry(
    initialized_provider: MammamiradioProvider,
) -> None:
    """The full display name 'Mamma Mi Radio' (with spaces) matches the entry."""
    results = await initialized_provider.search("Mamma Mi Radio", [MediaType.RADIO])
    assert len(results.radio) == 1
    assert results.radio[0].item_id == RADIO_ITEM_ID


async def test_search_no_match_returns_empty(
    initialized_provider: MammamiradioProvider,
) -> None:
    """search('zzz') returns no results."""
    results = await initialized_provider.search("zzz", [MediaType.RADIO])
    assert results.radio == []


async def test_search_without_radio_media_type_returns_empty(
    initialized_provider: MammamiradioProvider,
) -> None:
    """search() respects the media_types filter — no Radio in filter, no results."""
    results = await initialized_provider.search("mammamiradio", [MediaType.TRACK])
    assert results.radio == []


async def test_search_empty_query_returns_empty(
    initialized_provider: MammamiradioProvider,
) -> None:
    """Empty search string must return no results (not match-all)."""
    results = await initialized_provider.search("", [MediaType.RADIO])
    assert results.radio == []


# ---------------------------------------------------------------------------
# get_radio
# ---------------------------------------------------------------------------


async def test_get_radio_with_valid_id_returns_radio(
    initialized_provider: MammamiradioProvider,
) -> None:
    """get_radio(valid_id) returns a fully-populated Radio."""
    radio = await initialized_provider.get_radio(RADIO_ITEM_ID)
    assert isinstance(radio, Radio)
    assert radio.item_id == RADIO_ITEM_ID
    assert radio.name == RADIO_NAME
    assert radio.metadata.description
    assert radio.metadata.genres
    assert radio.metadata.languages == ["it"]


async def test_get_radio_with_invalid_id_raises_media_not_found(
    initialized_provider: MammamiradioProvider,
) -> None:
    """get_radio('bogus') raises MediaNotFoundError."""
    with pytest.raises(MediaNotFoundError):
        await initialized_provider.get_radio("does-not-exist")


# ---------------------------------------------------------------------------
# get_stream_details
# ---------------------------------------------------------------------------


async def test_get_stream_details_returns_mp3_format(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    StreamDetails declares ContentType.MP3 at the addon's published bitrate.

    When the contract omits ``stream.audio_format`` the published defaults
    apply: MP3 @ 192 kbps / 48 kHz / stereo (matching the addon's AudioConfig
    default, not the old hard-coded 128).
    """
    contract = {**_V1_MUSIC, "stream": {"relative_url": "/stream"}}
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(contract))
    await provider.handle_async_init()
    details = await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.audio_format.content_type == ContentType.MP3
    assert details.audio_format.bit_rate == 192
    assert details.audio_format.sample_rate == 48000
    assert details.audio_format.channels == 2
    assert details.media_type == MediaType.RADIO


async def test_get_stream_details_uses_configured_url_with_stream_suffix(
    initialized_provider: MammamiradioProvider,
) -> None:
    """The stream path defaults to ``${url}/stream``."""
    details = await initialized_provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8000/stream"
    assert details.allow_seek is False
    assert details.can_seek is False


async def test_get_stream_details_uses_contract_relative_stream_path(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """The v1 consumer contract may publish the relative stream URL to expose."""
    contract = {
        **_V1_MUSIC,
        "stream": {"relative_url": "/radio/live.mp3", "audio_format": _V1_AUDIO_FORMAT},
    }
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(contract))
    await provider.handle_async_init()
    details = await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8000/radio/live.mp3"


async def test_get_stream_details_defaults_path_when_relative_url_absent(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A contract stream block without relative_url falls back to the default /stream path."""
    contract = {**_V1_MUSIC, "stream": {"audio_format": _V1_AUDIO_FORMAT}}
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(contract))
    await provider.handle_async_init()
    details = await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8000/stream"


async def test_get_stream_details_ignores_unsafe_contract_stream_path(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """An absolute contract URL must not turn the provider into a redirector."""
    contract = {
        **_V1_MUSIC,
        "stream": {"relative_url": "https://example.invalid/stream", "audio_format": {}},
    }
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(contract))
    await provider.handle_async_init()
    details = await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8000/stream"


async def test_get_stream_details_does_not_probe_at_stream_time(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    Stream-time has no HTTP probe; liveness is checked at init only.

    This is intentional per MA convention — NTS/RadioBrowser/ORF Radiothek
    all do the same. ``get_stream_details`` returns a passthrough
    ``StreamDetails``; failures at the actual stream URL surface naturally
    via MA's ffmpeg pipeline. Locks the contract that no http_session calls
    happen during stream-details resolution.

    Live metadata does not break this contract: ``get_stream_details`` only
    *wires* the ``stream_metadata_update_callback`` + interval; the HTTP poll
    happens later, inside the callback, never at stream-details time.
    """
    mass_mock.http_session.get = MagicMock()
    mass_mock.http_session.head = MagicMock()
    details = await initialized_provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8000/stream"
    mass_mock.http_session.get.assert_not_called()
    mass_mock.http_session.head.assert_not_called()
    # The live-metadata callback is wired, but not invoked here.
    assert details.stream_metadata_update_callback == initialized_provider._update_stream_metadata
    assert details.stream_metadata_update_interval == STREAM_METADATA_UPDATE_INTERVAL


async def test_get_stream_details_with_invalid_id_raises_media_not_found(
    initialized_provider: MammamiradioProvider,
) -> None:
    """Unknown item id at stream time raises MediaNotFoundError, not unavailable."""
    with pytest.raises(MediaNotFoundError):
        await initialized_provider.get_stream_details("does-not-exist", MediaType.RADIO)


async def test_get_stream_details_uses_contract_audio_format(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    After init against a v1 contract, get_stream_details reflects the contract's format.

    Locks the end-to-end plumbing with a NON-default format (the default test
    payload is bit-identical to the fallback defaults, which would hide a
    regression where _audio_format() ignored the cached contract).
    """
    contract = {
        **_V1_MUSIC,
        "stream": {
            "relative_url": "/stream",
            "audio_format": {
                "codec": "aac",
                "bitrate_kbps": 256,
                "sample_rate_hz": 44100,
                "channels": 1,
            },
        },
    }
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(contract, etag='W/"a"'))
    await provider.handle_async_init()
    details = await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.audio_format.content_type == ContentType.AAC
    assert details.audio_format.bit_rate == 256
    assert details.audio_format.sample_rate == 44100
    assert details.audio_format.channels == 1


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
    prov = _build_provider_with_url(mass_mock, "http://localhost:8000/")
    await prov.handle_async_init()
    probe_url = mass_mock.http_session.get.call_args.args[0]
    assert probe_url == "http://localhost:8000/api/integrations/v1/now-playing"
    details = await prov.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8000/stream"


async def test_query_string_in_configured_url_is_stripped(
    mass_mock: MagicMock,
) -> None:
    """A configured URL with a query string must not corrupt the probe/stream URLs."""
    prov = _build_provider_with_url(mass_mock, "http://localhost:8000?foo=bar")
    await prov.handle_async_init()
    probe_url = mass_mock.http_session.get.call_args.args[0]
    assert probe_url == "http://localhost:8000/api/integrations/v1/now-playing"
    details = await prov.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8000/stream"


async def test_credentials_in_configured_url_are_stripped(
    mass_mock: MagicMock,
) -> None:
    """A pasted token in URL userinfo must not reach the probe or stream URLs."""
    prov = _build_provider_with_url(
        mass_mock, "http://admin:secret-token@localhost:8000?admin_token=also-secret"
    )
    await prov.handle_async_init()
    probe_url = mass_mock.http_session.get.call_args.args[0]
    assert probe_url == "http://localhost:8000/api/integrations/v1/now-playing"
    assert "secret" not in probe_url
    assert "@" not in probe_url
    details = await prov.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8000/stream"


async def test_fragment_in_configured_url_is_stripped(
    mass_mock: MagicMock,
) -> None:
    """A configured URL with a fragment must not corrupt the probe/stream URLs."""
    prov = _build_provider_with_url(mass_mock, "http://localhost:8000#frag")
    await prov.handle_async_init()
    probe_url = mass_mock.http_session.get.call_args.args[0]
    assert probe_url == "http://localhost:8000/api/integrations/v1/now-playing"
    details = await prov.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8000/stream"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:8000", "http://localhost:8000"),
        ("https://radio.example.test", "https://radio.example.test"),
        ("https://radio.example.test/mamma/", "https://radio.example.test/mamma"),
        ("http://[::1]:8000/", "http://[::1]:8000"),
        ("http://user:secret@host:8000?token=x#frag", "http://host:8000"),
        ("  http://localhost:8000  ", "http://localhost:8000"),
        ("HTTP://localhost:8000", "http://localhost:8000"),
    ],
)
def test_normalize_base_url_accepts(raw: str, expected: str) -> None:
    """Valid http(s) base URLs normalize to a sanitized scheme://host[:port][/path]."""
    assert _normalize_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "localhost:8000",
        "//localhost:8000",
        "ftp://localhost:8000",
        "http:///stream",
        "",
        "   ",
        "http://[::1:8000",
        "http://localhost:99999",
        "http://localhost:notaport",
        "http://local host:8000",
        "http://local\thost:8000",
        "http://host\\evil:8000",
    ],
)
def test_normalize_base_url_rejects_bad_urls(raw: str) -> None:
    """Any string that is not a full http(s) URL with a hostname raises ValueError."""
    with pytest.raises(ValueError, match="base URL"):
        _normalize_base_url(raw)


@pytest.mark.parametrize("raw", [123, True, None, ["http://localhost:8000"]])
def test_normalize_base_url_rejects_non_strings(raw: Any) -> None:
    """Provider-visible non-string values raise TypeError instead of being coerced."""
    with pytest.raises(TypeError, match="base URL"):
        _normalize_base_url(raw)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        (1, True),
        (True, False),
        (1.0, False),
        ("2", False),
        (None, False),
    ],
)
def test_supports_v1_schema(value: Any, expected: bool) -> None:
    """Only a supported version as a string or int counts; bools and floats never do."""
    assert _supports_v1_schema(value) is expected


@pytest.mark.parametrize(
    "value",
    ["//evil/stream", "radio/live.mp3", "/", None, "", "//["],
)
def test_stream_path_from_contract_rejects_unsafe_values(value: Any) -> None:
    """Protocol-relative, schemeless-relative, or unparsable values fall back to /stream."""
    assert _stream_path_from_contract(value) == "/stream"


@pytest.mark.parametrize(
    ("hosts", "expected"),
    [
        ("Gianni", None),
        ({"display_name": "Gianni"}, None),
        ([{"engine_host": "gianni"}], "gianni"),
    ],
)
def test_host_display_names_variants(hosts: Any, expected: str | None) -> None:
    """Non-list hosts yield no byline; a dict host without display_name uses engine_host."""
    assert _host_display_names(hosts) == expected


async def test_invalid_base_url_fails_setup_before_http(mass_mock: MagicMock) -> None:
    """A schemeless URL raises a provider-localized SetupFailedError before any request."""
    prov = _build_provider_with_url(mass_mock, "localhost:8000")
    with pytest.raises(SetupFailedError) as excinfo:
        await prov.handle_async_init()
    assert excinfo.value.translation_key == "invalid_base_url"
    assert excinfo.value.translation_owner == "provider.mammamiradio"
    mass_mock.http_session.get.assert_not_called()


async def test_cached_base_url_survives_config_replacement(mass_mock: MagicMock) -> None:
    """
    The base URL is bound at init; a later config swap does not affect polling.

    Base ``Provider.update_config`` replaces ``self.config`` immediately and
    schedules the reload later; an already-resolved stream must keep polling the
    URL it was initialized with instead of raising through the callback.
    """
    prov = _build_provider_with_url(mass_mock, "http://radio.example.test:8000")
    await prov.handle_async_init()
    details = await prov.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://radio.example.test:8000/stream"

    bad_config = MagicMock()
    bad_config.get_value.return_value = "not a url"
    prov.config = bad_config
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC))
    await prov._update_stream_metadata(details, 0)  # must not raise
    called_url = mass_mock.http_session.get.call_args.args[0]
    assert called_url == "http://radio.example.test:8000/api/integrations/v1/now-playing"


# ---------------------------------------------------------------------------
# v1 now-playing contract — audio format
# ---------------------------------------------------------------------------


def test_audio_format_from_contract_reads_published_format() -> None:
    """The audio format comes from the v1 contract: MP3 / 192 kbps / 48 kHz / stereo."""
    fmt = _audio_format_from_contract(_V1_AUDIO_FORMAT)
    assert fmt.content_type == ContentType.MP3
    assert fmt.bit_rate == 192
    assert fmt.sample_rate == 48000
    assert fmt.channels == 2


def test_audio_format_from_contract_defaults_when_absent() -> None:
    """Missing/None contract falls back to the addon's published defaults."""
    fmt = _audio_format_from_contract(None)
    assert fmt.content_type == ContentType.MP3
    assert fmt.bit_rate == 192
    assert fmt.sample_rate == 48000
    assert fmt.channels == 2


def test_audio_format_from_contract_unknown_codec_defaults_to_mp3() -> None:
    """An unrecognized codec degrades to MP3 rather than ContentType.UNKNOWN."""
    fmt = _audio_format_from_contract({"codec": "weird", "bitrate_kbps": 96})
    assert fmt.content_type == ContentType.MP3
    assert fmt.bit_rate == 96


def test_audio_format_from_contract_honors_alternate_codec() -> None:
    """A real alternate codec is parsed (forward-compat for a future addon encoder)."""
    fmt = _audio_format_from_contract(
        {"codec": "aac", "bitrate_kbps": 256, "sample_rate_hz": 44100, "channels": 1}
    )
    assert fmt.content_type == ContentType.AAC
    assert fmt.bit_rate == 256
    assert fmt.sample_rate == 44100
    assert fmt.channels == 1


@pytest.mark.parametrize("bitrate", [True, 0, -5])
def test_audio_format_from_contract_non_positive_bitrate_defaults(bitrate: Any) -> None:
    """A bool / zero / negative bitrate_kbps falls back to the published 192 default."""
    fmt = _audio_format_from_contract({"codec": "mp3", "bitrate_kbps": bitrate})
    assert fmt.bit_rate == 192


# ---------------------------------------------------------------------------
# v1 now-playing contract — `_v1_to_stream_metadata` (pure mapping)
# ---------------------------------------------------------------------------


def _v1_payload(now_playing: Any, *, up_next: Any = None, station: Any = None) -> dict[str, Any]:
    """Build a minimal v1 response around a ``now_playing`` block."""
    return {
        "schema_version": "1",
        "station": station if station is not None else {"name": "mammamiradio", "hosts": []},
        "stream": {"relative_url": "/stream", "audio_format": _V1_AUDIO_FORMAT},
        "now_playing": now_playing,
        "up_next": up_next if up_next is not None else [],
        "session_state": "live" if now_playing is not None else "empty_queue",
        "changed_at": 1.0,
    }


def test_v1_music_maps_title_artist_artwork_album() -> None:
    """A music segment surfaces title / artist / artwork / album from the contract."""
    sm = _v1_to_stream_metadata(_V1_MUSIC, show_upcoming=False)
    assert sm.title == "Volare"
    assert sm.artist == "Modugno"
    assert sm.image_url == "http://art/volare.jpg"
    assert sm.album == "Best Of"


def test_v1_music_without_album_has_no_album() -> None:
    """A music segment without an album stays album-less (no station-name fallback)."""
    now = {k: v for k, v in _V1_MUSIC["now_playing"].items() if k != "album"}
    sm = _v1_to_stream_metadata(_v1_payload(now), show_upcoming=False)
    assert sm.title == "Volare"
    assert sm.album is None


def test_v1_music_non_string_fields_coerced() -> None:
    """Non-string artist / artwork / album from untrusted JSON coerce to None, not garbage."""
    now = {
        "segment_class": "music",
        "segment_type": "music",
        "title": "X",
        "artist": 42,
        "artwork": ["not", "a", "url"],
        "album": {"not": "a string"},
    }
    sm = _v1_to_stream_metadata(_v1_payload(now), show_upcoming=False)
    assert sm.title == "X"
    assert sm.artist is None
    assert sm.image_url is None
    assert sm.album is None


@pytest.mark.parametrize(
    ("artwork", "expected"),
    [
        ("javascript:alert(1)", None),
        ("file:///etc/passwd", None),
        ("relative/art.jpg", None),
        ("http://", None),
        ("http:///pathonly.jpg", None),
        ("http://[bad/art.jpg", None),
        ("http://art/ok.jpg", "http://art/ok.jpg"),
        ("https://art/ok.jpg", "https://art/ok.jpg"),
        ("HTTPS://art/ok.jpg", "HTTPS://art/ok.jpg"),
    ],
)
def test_v1_artwork_non_http_scheme_dropped(artwork: str, expected: str | None) -> None:
    """Only http(s) artwork URLs may reach MA media surfaces."""
    now = {**_V1_MUSIC["now_playing"], "artwork": artwork}
    sm = _v1_to_stream_metadata(_v1_payload(now), show_upcoming=False)
    assert sm.image_url == expected


def test_v1_voice_uses_now_playing_host() -> None:
    """A voice segment uses the contract's top-level ``host`` byline as the artist."""
    now = {
        "segment_class": "voice",
        "segment_type": "banter",
        "title": "Host banter",
        "host": "Gianni",
    }
    sm = _v1_to_stream_metadata(_v1_payload(now), show_upcoming=False)
    assert sm.title == "Host banter"
    assert sm.artist == "Gianni"


def test_v1_voice_host_string_wins_over_station_hosts() -> None:
    """
    A populated now_playing.host takes precedence over station.hosts.

    Mirrors the live news_flash case (host="Giulia") where the byline must be the
    single reading host, not the full station roster.
    """
    now = {
        "segment_class": "voice",
        "segment_type": "news_flash",
        "title": "Notizie",
        "host": "Giulia",
    }
    station = {
        "name": "mammamiradio",
        "hosts": [
            {"engine_host": "m", "display_name": "Marco"},
            {"engine_host": "l", "display_name": "Lucia"},
        ],
    }
    sm = _v1_to_stream_metadata(_v1_payload(now, station=station), show_upcoming=False)
    assert sm.artist == "Giulia"


def test_v1_voice_without_host_falls_back_to_station_display_names() -> None:
    """The real banter fix: station hosts are display_name dicts, not strings."""
    now = {"segment_class": "voice", "segment_type": "banter", "title": None, "host": None}
    station = {
        "name": "mammamiradio",
        "hosts": [
            {"engine_host": "g", "display_name": "Gianni"},
            {"engine_host": "l", "display_name": "Lucia"},
        ],
    }
    sm = _v1_to_stream_metadata(_v1_payload(now, station=station), show_upcoming=False)
    assert sm.title == "Host banter"
    assert sm.artist == "Gianni, Lucia"


def test_v1_banter_string_hosts_join() -> None:
    """station.hosts given as plain strings still joins the names (hardening)."""
    now = {"segment_class": "voice", "segment_type": "banter", "title": None, "host": None}
    station = {"name": "mammamiradio", "hosts": ["Gianni", "Lucia"]}
    sm = _v1_to_stream_metadata(_v1_payload(now, station=station), show_upcoming=False)
    assert sm.artist == "Gianni, Lucia"


def test_v1_voice_empty_hosts_falls_back_to_station_name() -> None:
    """A voice segment with an empty hosts roster and no host byline uses the station name."""
    now = {"segment_class": "voice", "segment_type": "banter", "title": None}
    sm = _v1_to_stream_metadata(_v1_payload(now), show_upcoming=False)
    assert sm.title == "Host banter"
    assert sm.artist == "mammamiradio"


def test_v1_interstitial_titles_with_station_artist() -> None:
    """An interstitial (ad / station id) carries its label and the station as artist."""
    now = {"segment_class": "interstitial", "segment_type": "ad", "title": "Ad break"}
    sm = _v1_to_stream_metadata(_v1_payload(now), show_upcoming=False)
    assert sm.title == "Ad break"
    assert sm.artist == "mammamiradio"


def test_v1_unavailable_renders_idle_station_frame() -> None:
    """An 'unavailable' segment renders the station name and suppresses the description."""
    now = {"segment_class": "unavailable", "segment_type": "skipping", "title": None}
    sm = _v1_to_stream_metadata(_v1_payload(now, up_next=[{"title": "Next"}]), show_upcoming=True)
    assert sm.title == "mammamiradio"
    assert sm.description is None


def test_v1_no_now_playing_is_idle() -> None:
    """session_state stopped/empty_queue (now_playing null) renders the station name."""
    sm = _v1_to_stream_metadata(_v1_payload(None), show_upcoming=True)
    assert sm.title == "mammamiradio"
    assert sm.description is None


def test_v1_unknown_segment_class_renders_idle_not_leak() -> None:
    """A future additive segment_class degrades to the idle station frame, not a leak."""
    now = {"segment_class": "future_thing", "segment_type": "promo", "title": "Promo X"}
    sm = _v1_to_stream_metadata(_v1_payload(now), show_upcoming=False)
    assert sm.title == "mammamiradio"


def test_v1_up_next_description_when_show_upcoming() -> None:
    """The 'Up next' frame surfaces the next item's title."""
    sm = _v1_to_stream_metadata(_V1_MUSIC, show_upcoming=True)
    assert sm.description == "Up next: Chiacchiere"


def test_v1_no_up_next_description_on_now_frame() -> None:
    """The 'Now' frame carries no description (no 'A casa' in the v1 contract)."""
    sm = _v1_to_stream_metadata(_V1_MUSIC, show_upcoming=False)
    assert sm.description is None


def test_v1_up_next_non_dict_entry_ignored() -> None:
    """A non-dict first up_next entry is skipped without raising; no 'Up next' line."""
    sm = _v1_to_stream_metadata(
        _v1_payload(_V1_MUSIC["now_playing"], up_next=["just-a-string"]), show_upcoming=True
    )
    assert sm.title == "Volare"
    assert sm.description is None


def test_v1_up_next_unavailable_entry_suppressed() -> None:
    """An idle 'unavailable' up-next entry never renders as an 'Up next' line."""
    up_next = [{"segment_class": "unavailable", "segment_type": "skipping", "title": "Nothing"}]
    sm = _v1_to_stream_metadata(
        _v1_payload(_V1_MUSIC["now_playing"], up_next=up_next), show_upcoming=True
    )
    assert sm.title == "Volare"
    assert sm.description is None


@pytest.mark.parametrize("up_title", [None, ""])
def test_v1_up_next_missing_title_suppresses_description(up_title: str | None) -> None:
    """An up-next entry without a usable title renders no 'Up next' line."""
    up_next = [{"segment_class": "music", "segment_type": "music", "title": up_title}]
    sm = _v1_to_stream_metadata(
        _v1_payload(_V1_MUSIC["now_playing"], up_next=up_next), show_upcoming=True
    )
    assert sm.description is None


def test_v1_mapper_is_total_against_malformed_payload() -> None:
    """Non-dict now_playing / non-list up_next / non-dict station never raise."""
    payload = {"station": ["x"], "now_playing": ["not", "a", "dict"], "up_next": {"bad": 1}}
    sm = _v1_to_stream_metadata(payload, show_upcoming=True)
    assert sm.title == "Mamma Mi Radio"
    assert sm.description is None


@pytest.mark.parametrize("seg_class", [[], {}, 7, None, True])
def test_v1_mapper_non_string_segment_class_never_raises(seg_class: Any) -> None:
    """A non-str segment_class renders the station frame (unhashable values must not raise)."""
    payload = {
        "station": {"name": "Mamma Mi Radio"},
        "now_playing": {"segment_class": seg_class, "title": "x"},
        "up_next": [{"segment_class": "music", "title": "Next"}],
    }
    sm = _v1_to_stream_metadata(payload, show_upcoming=True)
    assert sm.title == "Mamma Mi Radio"
    assert sm.description is None


# ---------------------------------------------------------------------------
# v1 now-playing contract — `_update_stream_metadata` callback (stateful)
# ---------------------------------------------------------------------------


async def _details_for(provider: MammamiradioProvider) -> Any:
    """Resolve a StreamDetails object to drive the metadata callback against."""
    return await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)


async def test_v1_callback_populates_from_contract(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """The v1 callback polls the contract endpoint and sets stream_metadata."""
    details = await _details_for(initialized_provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"v1"'))
    await initialized_provider._update_stream_metadata(details, 0)
    assert details.stream_metadata.title == "Volare"
    assert details.stream_metadata.artist == "Modugno"
    assert mass_mock.http_session.get.call_args.args[0].endswith("/api/integrations/v1/now-playing")


async def test_v1_callback_alternates_now_then_upnext(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """Call 1 renders the Now frame; call 2 (same segment) flips to the Up-next frame."""
    details = await _details_for(initialized_provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"v1"'))
    await initialized_provider._update_stream_metadata(details, 0)
    desc_now = details.stream_metadata.description
    assert desc_now is None
    await initialized_provider._update_stream_metadata(details, 0)
    desc_next = details.stream_metadata.description
    assert desc_next == "Up next: Chiacchiere"
    # Per-stream state lives under its own namespace in StreamDetails.data so it
    # can never collide with keys MA core stashes there (e.g. HLS bookkeeping).
    state = details.data["mammamiradio"]
    assert {"v1_segment", "show_upcoming", "v1_etag", "v1_last"} <= set(state)


async def test_v1_callback_304_reuses_cache_and_keeps_alternating(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A 304 reuses the cached payload (conditional poll) and still flips the view."""
    details = await _details_for(initialized_provider)
    mass_mock.http_session.get = MagicMock(
        side_effect=[
            _make_v1_ctx(_V1_MUSIC, status=200, etag='W/"v1"'),
            _make_v1_ctx(None, status=304),
        ]
    )
    await initialized_provider._update_stream_metadata(details, 0)  # 200 -> Now frame
    desc_now = details.stream_metadata.description
    assert desc_now is None
    await initialized_provider._update_stream_metadata(details, 0)  # 304 -> Up-next from cache
    desc_next = details.stream_metadata.description
    assert desc_next == "Up next: Chiacchiere"
    # The second request was conditional on the stored ETag.
    second_call = mass_mock.http_session.get.call_args_list[1]
    assert second_call.kwargs["headers"]["If-None-Match"] == 'W/"v1"'


async def test_v1_callback_swallows_unreachable_keeps_prior(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A mid-stream connection failure must not raise, keeps the prior frame, drops the ETag."""
    details = await _details_for(initialized_provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"v1"'))
    await initialized_provider._update_stream_metadata(details, 0)
    prior = details.stream_metadata
    assert details.data["mammamiradio"]["v1_etag"] == 'W/"v1"'
    mass_mock.http_session.get = MagicMock(
        return_value=_make_failing_ctx(aiohttp.ClientConnectionError("nope"))
    )
    await initialized_provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior
    # The stored validator is dropped on the ClientError leg too (mirrors the
    # poisoned-ETag recovery on the ValueError leg).
    assert "v1_etag" not in details.data["mammamiradio"]


async def test_v1_callback_swallows_timeout_keeps_prior(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A mid-stream poll timeout must not raise and keeps the prior frame."""
    details = await _details_for(initialized_provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"v1"'))
    await initialized_provider._update_stream_metadata(details, 0)
    prior = details.stream_metadata
    mass_mock.http_session.get = MagicMock(return_value=_make_failing_ctx(TimeoutError("slow")))
    await initialized_provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior


async def test_v1_callback_http_error_keeps_prior(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A mid-stream 5xx from the v1 endpoint must not raise and keeps the prior frame."""
    details = await _details_for(initialized_provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"v1"'))
    await initialized_provider._update_stream_metadata(details, 0)
    prior = details.stream_metadata
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(None, status=503))
    await initialized_provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior


async def test_v1_callback_non_dict_payload_keeps_prior(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A JSON array from the v1 endpoint mid-stream is ignored, prior frame kept."""
    details = await _details_for(initialized_provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"v1"'))
    await initialized_provider._update_stream_metadata(details, 0)
    prior = details.stream_metadata
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(["unexpected", "array"]))
    await initialized_provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior


async def test_v1_callback_bad_json_keeps_prior(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A non-JSON body from the v1 endpoint mid-stream is ignored, prior frame kept."""
    details = await _details_for(initialized_provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"v1"'))
    await initialized_provider._update_stream_metadata(details, 0)
    prior = details.stream_metadata
    mass_mock.http_session.get = MagicMock(return_value=_make_bad_json_ctx())
    await initialized_provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior


async def test_v1_callback_poisoned_etag_cleared_and_recovers(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    A stored ETag that fails at request time is dropped so the next tick recovers.

    A poisoned validator (e.g. control characters from a broken proxy) raises on
    every conditional request; the provider must pop it instead of freezing the
    metadata forever.
    """
    details = await _details_for(initialized_provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag="bad\r\netag"))
    await initialized_provider._update_stream_metadata(details, 0)
    prior = details.stream_metadata
    assert details.data["mammamiradio"]["v1_etag"] == "bad\r\netag"

    mass_mock.http_session.get = MagicMock(side_effect=ValueError("invalid header value"))
    await initialized_provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior
    assert "v1_etag" not in details.data["mammamiradio"]

    recovered = {**_V1_MUSIC, "now_playing": {**_V1_MUSIC["now_playing"], "title": "Recovered"}}
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(recovered))
    await initialized_provider._update_stream_metadata(details, 0)
    assert details.stream_metadata.title == "Recovered"
    # The recovery request went out unconditionally (no stored validator left).
    assert "If-None-Match" not in mass_mock.http_session.get.call_args.kwargs["headers"]


async def test_v1_callback_mapper_exception_propagates(
    initialized_provider: MammamiradioProvider,
    mass_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A mapper raise escapes the callback instead of being swallowed.

    The mapper is total by construction, so any raise is a genuine bug. The
    raise escapes into MA's fire-and-forget callback task, where asyncio
    reports an unretrieved task exception at the default log level and nothing
    crashes. The show_upcoming flip is skipped so the next successful tick
    renders the frame the failed tick would have.
    """
    details = await _details_for(initialized_provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"a"'))
    await initialized_provider._update_stream_metadata(details, 0)
    prior = details.stream_metadata
    flip_before = details.data["mammamiradio"]["show_upcoming"]

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("v1 mapper blew up")

    monkeypatch.setattr("music_assistant.providers.mammamiradio._v1_to_stream_metadata", _boom)
    with pytest.raises(RuntimeError, match="v1 mapper blew up"):
        await initialized_provider._update_stream_metadata(details, 0)
    assert details.stream_metadata is prior
    assert details.data["mammamiradio"]["show_upcoming"] == flip_before


async def test_v1_callback_resets_alternation_on_segment_change(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    A new segment resets the alternation back to the Now frame.

    Keyed on segment identity (segment_type/title/started_at), not the addon's
    ``changed_at`` clock, so a mid-segment queue-append does not snap to Now.
    """
    details = await _details_for(initialized_provider)
    other = {**_V1_MUSIC, "now_playing": {**_V1_MUSIC["now_playing"], "title": "OtherSong"}}
    mass_mock.http_session.get = MagicMock(
        side_effect=[
            _make_v1_ctx(_V1_MUSIC, etag='W/"a"'),
            _make_v1_ctx(_V1_MUSIC, etag='W/"a"'),
            _make_v1_ctx(_V1_MUSIC, etag='W/"a"'),
            _make_v1_ctx(other, etag='W/"b"'),
        ]
    )
    await initialized_provider._update_stream_metadata(details, 0)  # Now
    d1 = details.stream_metadata.description
    await initialized_provider._update_stream_metadata(details, 0)  # Up-next
    d2 = details.stream_metadata.description
    await initialized_provider._update_stream_metadata(details, 0)  # Now again
    d3 = details.stream_metadata.description
    # Segment change while show_upcoming is True — only the reset logic can
    # produce a "Now" frame here; without it this call would render "Up next".
    await initialized_provider._update_stream_metadata(details, 0)
    d4 = details.stream_metadata.description
    assert d1 is None
    assert d2 == "Up next: Chiacchiere"
    assert d3 is None
    assert d4 is None
    assert details.stream_metadata.title == "OtherSong"


async def test_v1_callback_cold_cache_304_keeps_prior(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A 304 with no cached payload (cold cache) is a no-op, not a crash."""
    details = await _details_for(initialized_provider)
    assert details.stream_metadata is None
    mass_mock.http_session.get = MagicMock(
        return_value=_make_v1_ctx(None, status=304, etag='W/"x"')
    )
    await initialized_provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is None


async def test_v1_callback_idle_no_now_playing(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A live response with session_state empty_queue / now_playing=null renders idle, no raise."""
    details = await _details_for(initialized_provider)
    idle: dict[str, Any] = {
        "schema_version": "1",
        "station": {"name": "mammamiradio", "hosts": []},
        "stream": {"relative_url": "/stream", "audio_format": _V1_AUDIO_FORMAT},
        "now_playing": None,
        "up_next": [],
        "session_state": "empty_queue",
        "changed_at": 0.0,
    }
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(idle, etag='W/"i"'))
    await initialized_provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata.title == "mammamiradio"
    assert details.stream_metadata.description is None


async def test_v1_callback_without_etag_polls_unconditionally(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    If the addon omits the ETag header, the provider degrades gracefully.

    Each tick is a fresh 200 with no If-None-Match sent; polling keeps working
    without the 304 optimization.
    """
    details = await _details_for(initialized_provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC))  # no etag
    await initialized_provider._update_stream_metadata(details, 0)
    assert details.stream_metadata.title == "Volare"
    await initialized_provider._update_stream_metadata(details, 0)
    second = mass_mock.http_session.get.call_args_list[1]
    assert "If-None-Match" not in second.kwargs.get("headers", {})


async def test_v1_callback_resets_on_artist_change_same_title(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    Segment identity includes artist/host, not just type+title.

    ``started_at`` is None when the addon does not know the segment start, so
    two consecutive segments sharing type and title (e.g. a same-title cover)
    must still be told apart by the other contract fields.
    """
    details = await _details_for(initialized_provider)
    cover = {**_V1_MUSIC, "now_playing": {**_V1_MUSIC["now_playing"], "artist": "Cover Band"}}
    mass_mock.http_session.get = MagicMock(
        side_effect=[
            _make_v1_ctx(_V1_MUSIC, etag='W/"a"'),
            _make_v1_ctx(cover, etag='W/"b"'),
        ]
    )
    await initialized_provider._update_stream_metadata(details, 0)  # Now (show_upcoming -> True)
    await initialized_provider._update_stream_metadata(details, 0)  # new artist -> reset
    desc = details.stream_metadata.description
    assert desc is None
    assert details.stream_metadata.artist == "Cover Band"


async def test_v1_callback_drops_stale_etag_when_header_disappears(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A 200 without an ETag clears the stored validator; polling becomes unconditional."""
    details = await _details_for(initialized_provider)
    mass_mock.http_session.get = MagicMock(
        side_effect=[
            _make_v1_ctx(_V1_MUSIC, etag='W/"a"'),
            _make_v1_ctx(_V1_MUSIC),  # ETag header disappears
            _make_v1_ctx(_V1_MUSIC),
        ]
    )
    await initialized_provider._update_stream_metadata(details, 0)
    await initialized_provider._update_stream_metadata(details, 0)
    second = mass_mock.http_session.get.call_args_list[1]
    assert second.kwargs["headers"]["If-None-Match"] == 'W/"a"'
    await initialized_provider._update_stream_metadata(details, 0)
    third = mass_mock.http_session.get.call_args_list[2]
    assert "If-None-Match" not in third.kwargs.get("headers", {})


async def test_v1_callback_unsupported_schema_keeps_prior(
    initialized_provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    A drifted now-playing schema is ignored mid-stream instead of mapped loosely.

    Init raises on an unsupported schema; the mid-stream callback instead
    ignores the response and keeps the prior frame.
    """
    details = await _details_for(initialized_provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC))
    await initialized_provider._update_stream_metadata(details, 0)
    prior = details.stream_metadata
    unsupported = {**_V1_MUSIC, "schema_version": "2"}
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(unsupported))
    await initialized_provider._update_stream_metadata(details, 0)
    assert details.stream_metadata is prior


# ---------------------------------------------------------------------------
# Shared contract fixture (cross-repo golden copy)
# ---------------------------------------------------------------------------


def test_golden_fixture_maps_cleanly() -> None:
    """
    The cross-repo golden fixture renders through the v1 mapper.

    The same bytes live in the addon repo (tests/integrations/golden/); the
    addon's contract-drift CI checksum-compares the two copies, so this test is
    the provider-side half of the shared contract fixture.
    """
    fixture = Path(__file__).parent / "fixtures" / "v1_now_playing.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    assert _supports_v1_schema(payload["schema_version"])

    now_frame = _v1_to_stream_metadata(payload, show_upcoming=False)
    assert now_frame.title == "Volare"
    assert now_frame.artist == "Domenico Modugno"
    assert now_frame.album == "Mr Volare"
    assert now_frame.image_url == "https://example.test/art.jpg"
    assert now_frame.description is None

    upcoming_frame = _v1_to_stream_metadata(payload, show_upcoming=True)
    assert upcoming_frame.description == "Up next: Sapore di Sale — Gino Paoli"


# ---------------------------------------------------------------------------
# Live integration smoke (opt-in via MAMMAMIRADIO_LIVE_URL)
# ---------------------------------------------------------------------------


async def test_live_stream_smoke() -> None:
    """
    Live smoke test against a running mammamiradio addon. Skipped by default.

    Opt in by setting ``MAMMAMIRADIO_LIVE_URL`` (e.g. ``http://localhost:8000``).
    Verifies the init probe, browse, stream-details resolution, and the
    metadata fetch/mapping against a live addon; the audio path itself is not
    exercised.
    """
    live_url = os.environ.get("MAMMAMIRADIO_LIVE_URL")
    if not live_url:
        pytest.skip("MAMMAMIRADIO_LIVE_URL not set")

    async with aiohttp.ClientSession() as session:
        mass = MagicMock()
        mass.http_session = session
        prov = _build_provider_with_url(mass, live_url)

        # Init validates the v1 now-playing contract; it raises when the addon
        # is unreachable or older than 2.13 (no legacy fallback).
        await prov.handle_async_init()
        # Browse returns exactly one Radio entry.
        items = await prov.browse("mammamiradio://")
        assert len(items) == 1
        assert isinstance(items[0], Radio)
        # Stream details succeed against the live addon.
        details = await prov.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
        assert isinstance(details.path, str)
        assert details.path.endswith("/stream")
        assert details.audio_format.content_type == ContentType.MP3
        # The live-metadata callback always polls the v1 now-playing endpoint.
        await prov._update_stream_metadata(details, 0)
        assert details.stream_metadata is not None
        assert details.stream_metadata.title
