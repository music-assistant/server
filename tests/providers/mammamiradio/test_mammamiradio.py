"""Tests for the mammamiradio music provider."""

from __future__ import annotations

import os
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
    _normalize_base_url,
    _segment_to_stream_metadata,
    _v1_to_stream_metadata,
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


def _make_json_ctx(payload: Any, status: int = 200) -> MagicMock:
    """Async-context-manager mock yielding a response with `status` and async `.json()`."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=payload)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
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


def _route_get(mapping: dict[str, MagicMock], default_status: int = 404) -> Any:
    """Return a ``http_session.get`` side_effect that dispatches by URL substring."""

    def _get(url: str, *_args: Any, **_kwargs: Any) -> MagicMock:
        for needle, ctx in mapping.items():
            if needle in url:
                return ctx
        return _make_response_ctx(default_status)

    return _get


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
    # default: every request succeeds with HTTP 200
    mass.http_session.get = MagicMock(return_value=_make_response_ctx(200))
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


async def test_handle_async_init_v1_contract(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """Path 1 — a reachable v1 now-playing endpoint selects v1 mode and caches the format."""
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"a"'))
    await provider.handle_async_init()
    assert provider._use_v1 is True
    assert provider._audio_format_dict == _V1_AUDIO_FORMAT
    assert provider._stream_path == "/stream"
    called_url = mass_mock.http_session.get.call_args.args[0]
    assert called_url == "http://localhost:8000/api/integrations/v1/now-playing"


async def test_handle_async_init_falls_back_to_healthz_on_404(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """An older addon (no v1 endpoint -> 404) falls back to the /healthz liveness probe."""
    mass_mock.http_session.get = MagicMock(
        side_effect=_route_get(
            {
                "/api/integrations/v1/now-playing": _make_response_ctx(404),
                "/healthz": _make_response_ctx(200),
            }
        )
    )
    await provider.handle_async_init()
    assert provider._use_v1 is False
    called_urls = [c.args[0] for c in mass_mock.http_session.get.call_args_list]
    assert any(u.endswith("/api/integrations/v1/now-playing") for u in called_urls)
    assert any(u.endswith("/healthz") for u in called_urls)


async def test_handle_async_init_raises_when_unreachable(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    Path 2 — unreachable addon: init raises ProviderUnavailableError.

    The provider is the canonical place for liveness detection (matches
    RadioBrowser's pattern). Raising here prevents MA from loading a
    non-functional provider and surfaces a clean unavailable error to the
    user instead of letting the stream URL fail silently inside ffmpeg.
    """
    mass_mock.http_session.get = MagicMock(
        return_value=_make_failing_ctx(aiohttp.ClientConnectionError("nope"))
    )
    with pytest.raises(ProviderUnavailableError):
        await provider.handle_async_init()


async def test_handle_async_init_raises_when_both_endpoints_unhealthy(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    A 5xx on the v1 probe falls back to /healthz; if that is also 5xx, init raises.

    The addon process responds but reports unhealthy on both the v1 contract and
    /healthz, so the provider surfaces ProviderUnavailableError.
    """
    mass_mock.http_session.get = MagicMock(return_value=_make_response_ctx(503))
    with pytest.raises(ProviderUnavailableError):
        await provider.handle_async_init()


async def test_handle_async_init_v1_5xx_falls_back_to_healthy_healthz(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    A transient 5xx on the v1 endpoint must NOT fail load when /healthz is healthy.

    Regression guard: a momentary ingress/proxy 5xx during an addon restart used
    to be fatal (the v1 probe raised on any >=400). Now it falls back to legacy
    mode and the provider still loads.
    """
    mass_mock.http_session.get = MagicMock(
        side_effect=_route_get(
            {
                "/api/integrations/v1/now-playing": _make_response_ctx(503),
                "/healthz": _make_response_ctx(200),
            }
        )
    )
    await provider.handle_async_init()  # must not raise
    assert provider._use_v1 is False


async def test_handle_async_init_404_then_healthz_5xx_raises(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """An older addon (v1 404) whose /healthz is unhealthy raises ProviderUnavailableError."""
    mass_mock.http_session.get = MagicMock(
        side_effect=_route_get(
            {
                "/api/integrations/v1/now-playing": _make_response_ctx(404),
                "/healthz": _make_response_ctx(503),
            }
        )
    )
    with pytest.raises(ProviderUnavailableError):
        await provider.handle_async_init()


async def test_handle_async_init_v1_non_json_falls_back_to_healthz(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A 200 with a non-JSON body demotes to legacy mode (no crash) when /healthz is up."""
    mass_mock.http_session.get = MagicMock(
        side_effect=_route_get(
            {
                "/api/integrations/v1/now-playing": _make_bad_json_ctx(200),
                "/healthz": _make_response_ctx(200),
            }
        )
    )
    await provider.handle_async_init()  # must not raise
    assert provider._use_v1 is False


async def test_handle_async_init_healthz_connection_error_raises(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """An older addon (v1 404) whose /healthz probe hits a connection error raises."""
    mass_mock.http_session.get = MagicMock(
        side_effect=_route_get(
            {
                "/api/integrations/v1/now-playing": _make_response_ctx(404),
                "/healthz": _make_failing_ctx(aiohttp.ClientConnectionError("nope")),
            }
        )
    )
    with pytest.raises(ProviderUnavailableError):
        await provider.handle_async_init()


async def test_handle_async_init_v1_array_payload_falls_back_to_healthz(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A 200 whose JSON body is an array (not an object) demotes to legacy mode."""
    mass_mock.http_session.get = MagicMock(
        side_effect=_route_get(
            {
                "/api/integrations/v1/now-playing": _make_v1_ctx(["not", "an", "object"]),
                "/healthz": _make_response_ctx(200),
            }
        )
    )
    await provider.handle_async_init()  # must not raise
    assert provider._use_v1 is False


async def test_handle_async_init_v1_content_type_error_falls_back_to_healthz(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    A non-JSON content-type on the v1 probe demotes to legacy mode.

    Regression guard: ``aiohttp.ContentTypeError`` subclasses ``ClientError``, so
    a 200 HTML page (e.g. from an ingress splash) used to raise
    ProviderUnavailableError instead of falling back to /healthz.
    """
    content_type_error = aiohttp.ContentTypeError(request_info=MagicMock(), history=())
    mass_mock.http_session.get = MagicMock(
        side_effect=_route_get(
            {
                "/api/integrations/v1/now-playing": _make_bad_json_ctx(exc=content_type_error),
                "/healthz": _make_response_ctx(200),
            }
        )
    )
    await provider.handle_async_init()  # must not raise
    assert provider._use_v1 is False


async def test_handle_async_init_generic_client_error_still_raises(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A plain aiohttp.ClientError (true connection failure) must still fail init."""
    mass_mock.http_session.get = MagicMock(
        return_value=_make_failing_ctx(aiohttp.ClientError("boom"))
    )
    with pytest.raises(ProviderUnavailableError):
        await provider.handle_async_init()


async def test_handle_async_init_unsupported_v1_schema_falls_back_to_healthz(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A reachable but incompatible now-playing schema demotes to legacy mode."""
    unsupported = {**_V1_MUSIC, "schema_version": "2"}
    mass_mock.http_session.get = MagicMock(
        side_effect=_route_get(
            {
                "/api/integrations/v1/now-playing": _make_v1_ctx(unsupported),
                "/healthz": _make_response_ctx(200),
            }
        )
    )
    await provider.handle_async_init()
    assert provider._use_v1 is False
    assert provider._stream_path == "/stream"


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


async def test_search_display_name_returns_entry(
    provider: MammamiradioProvider,
) -> None:
    """The full display name 'Mamma Mi Radio' (with spaces) matches the entry."""
    results = await provider.search("Mamma Mi Radio", [MediaType.RADIO])
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
    """
    Path 9 — StreamDetails declares ContentType.MP3 at the addon's published bitrate.

    Without a cached v1 contract (no init in this test) the published defaults
    apply: MP3 @ 192 kbps (matching the addon's AudioConfig default, not the old
    hard-coded 128).
    """
    mass_mock.http_session.get = MagicMock(return_value=_make_response_ctx(200))
    details = await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.audio_format.content_type == ContentType.MP3
    assert details.audio_format.bit_rate == 192
    assert details.media_type == MediaType.RADIO


async def test_get_stream_details_uses_configured_url_with_stream_suffix(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """Path 10 — stream path defaults to ``${url}/stream``."""
    mass_mock.http_session.get = MagicMock(return_value=_make_response_ctx(200))
    details = await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
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
    provider: MammamiradioProvider, mass_mock: MagicMock
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
    details = await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8000/stream"
    mass_mock.http_session.get.assert_not_called()
    mass_mock.http_session.head.assert_not_called()
    # The live-metadata callback is wired, but not invoked here.
    assert details.stream_metadata_update_callback == provider._update_stream_metadata
    assert details.stream_metadata_update_interval == STREAM_METADATA_UPDATE_INTERVAL


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


def _build_provider_with_url(mass_mock: MagicMock, configured_url: str) -> MammamiradioProvider:
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


async def test_trailing_slash_in_configured_url_does_not_double_up(
    mass_mock: MagicMock,
) -> None:
    """A configured URL with a trailing slash should still produce ``${url}/stream``."""
    prov = _build_provider_with_url(mass_mock, "http://localhost:8000/")
    mass_mock.http_session.get = MagicMock(return_value=_make_response_ctx(200))
    details = await prov.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8000/stream"


async def test_query_string_in_configured_url_is_stripped(
    mass_mock: MagicMock,
) -> None:
    """A configured URL with a query string must not corrupt the stream URL."""
    prov = _build_provider_with_url(mass_mock, "http://localhost:8000?foo=bar")
    mass_mock.http_session.get = MagicMock(return_value=_make_response_ctx(200))
    details = await prov.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8000/stream"


async def test_credentials_in_configured_url_are_stripped(
    mass_mock: MagicMock,
) -> None:
    """A pasted token in URL userinfo must not reach stream/probe URLs."""
    prov = _build_provider_with_url(
        mass_mock, "http://admin:secret-token@localhost:8000?admin_token=also-secret"
    )
    details = await prov.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8000/stream"


async def test_fragment_in_configured_url_is_stripped(
    mass_mock: MagicMock,
) -> None:
    """A configured URL with a fragment must not corrupt the stream URL."""
    prov = _build_provider_with_url(mass_mock, "http://localhost:8000#frag")
    mass_mock.http_session.get = MagicMock(return_value=_make_response_ctx(200))
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
    A bound metadata callback keeps its resolved root after ``self.config`` is swapped.

    Base ``Provider.update_config`` replaces ``self.config`` immediately and
    schedules the reload later; an already-resolved stream must keep polling the
    root it was created with instead of raising through the callback.
    """
    prov = _build_provider_with_url(mass_mock, "http://radio.example.test:8000")
    details = await prov.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://radio.example.test:8000/stream"

    bad_config = MagicMock()
    bad_config.get_value.return_value = "not a url"
    prov.config = bad_config
    payload = {
        "now_streaming": {"type": "music", "label": "A", "metadata": {"title": "A"}},
        "upcoming": [],
        "brand": _BRAND,
    }
    mass_mock.http_session.get = MagicMock(return_value=_make_json_ctx(payload))
    await prov._update_stream_metadata(details, 0)  # must not raise
    called_url = mass_mock.http_session.get.call_args.args[0]
    assert called_url == "http://radio.example.test:8000/public-status"


def test_legacy_album_only_on_music_segments() -> None:
    """The legacy mapper mirrors v1: album is the station name for music, None otherwise."""
    music = _segment_to_stream_metadata(
        {"type": "music", "label": "A", "metadata": {"title": "A"}},
        [],
        {},
        _BRAND,
        show_upcoming=False,
    )
    assert music.album == "mammamiradio"
    for now in (
        {"type": "banter", "label": "B"},
        {"type": "ad", "label": "C"},
        {"type": "news_flash", "label": "D"},
        {"type": "station_id", "label": "E"},
        {"type": "stopped"},
        {"type": "mystery", "label": "F"},
    ):
        sm = _segment_to_stream_metadata(now, [], {}, _BRAND, show_upcoming=False)
        assert sm.album is None, f"album leaked for segment type {now['type']}"


async def test_search_empty_query_returns_empty(
    provider: MammamiradioProvider,
) -> None:
    """Empty search string must return no results (not match-all)."""
    results = await provider.search("", [MediaType.RADIO])
    assert results.radio == []


# ---------------------------------------------------------------------------
# Live typed-segment metadata — `_segment_to_stream_metadata` (pure mapping)
#
# The invariant under test everywhere: the helper is TOTAL — every input
# produces a StreamMetadata with a non-empty `title` (a mandatory str field).
# ---------------------------------------------------------------------------

_BRAND = {"station_name": "mammamiradio", "hosts": ["Gianni", "Lucia"]}


def test_segment_music_maps_title_artist_image() -> None:
    """A music segment surfaces title / artist / album art."""
    now = {
        "type": "music",
        "label": "Volare — Modugno",
        "metadata": {
            "title_only": "Volare",
            "title": "Volare — Modugno",
            "artist": "Domenico Modugno",
            "album_art": "http://art/volare.jpg",
        },
    }
    sm = _segment_to_stream_metadata(now, [], {}, _BRAND, show_upcoming=False)
    assert sm.title == "Volare"
    assert sm.artist == "Domenico Modugno"
    assert sm.image_url == "http://art/volare.jpg"
    assert sm.album == "mammamiradio"


def test_segment_banter_uses_host_names() -> None:
    """A banter segment titles as 'Host banter' with the hosts as artist."""
    sm = _segment_to_stream_metadata(
        {"type": "banter", "label": ""}, [], {}, _BRAND, show_upcoming=False
    )
    assert sm.title == "Host banter"
    assert sm.artist == "Gianni, Lucia"


def test_segment_ad_is_pubblicita() -> None:
    """An ad segment titles as 'Ad break' with artist 'Pubblicità'."""
    sm = _segment_to_stream_metadata(
        {"type": "ad", "label": ""}, [], {}, _BRAND, show_upcoming=False
    )
    assert sm.title == "Ad break"
    assert sm.artist == "Pubblicità"


def test_segment_news_flash_uses_host() -> None:
    """A news_flash segment carries the reporting host as artist."""
    now = {"type": "news_flash", "label": "Notizie flash", "metadata": {"host": "Gianni"}}
    sm = _segment_to_stream_metadata(now, [], {}, _BRAND, show_upcoming=False)
    assert sm.title == "Notizie flash"
    assert sm.artist == "Gianni"


def test_segment_sweeper_is_total_and_titled() -> None:
    """
    A sweeper segment (real SegmentType, no dedicated branch) still yields a title.

    Regression for the unhandled-type crash: SWEEPER is in mammamiradio's
    SegmentType enum; the helper must produce a valid StreamMetadata.
    """
    sm = _segment_to_stream_metadata(
        {"type": "sweeper", "label": "Stazione radio"}, [], {}, _BRAND, show_upcoming=False
    )
    assert sm.title == "Stazione radio"
    assert sm.artist == "mammamiradio"


def test_segment_unknown_type_falls_through_to_station_name() -> None:
    """An unrecognized segment type hits the catch-all and still has a title."""
    sm = _segment_to_stream_metadata(
        {"type": "future_segment_kind", "label": ""}, [], {}, _BRAND, show_upcoming=False
    )
    assert sm.title == "mammamiradio"


def test_segment_music_with_none_metadata_clamps_title() -> None:
    """Music payload with metadata=None must not raise; title clamps to the label."""
    sm = _segment_to_stream_metadata(
        {"type": "music", "label": "Brano 5", "metadata": None},
        [],
        {},
        _BRAND,
        show_upcoming=False,
    )
    assert sm.title == "Brano 5"


def test_segment_music_with_empty_metadata_and_label_clamps_to_station() -> None:
    """Music payload with metadata={} and no label clamps to the station name."""
    sm = _segment_to_stream_metadata(
        {"type": "music", "label": "", "metadata": {}}, [], {}, _BRAND, show_upcoming=False
    )
    assert sm.title == "mammamiradio"


def test_segment_music_placeholder_title_clamps() -> None:
    """A music 'unknown' placeholder title is treated as no title."""
    now = {"type": "music", "label": "Brano misterioso", "metadata": {"title": "unknown"}}
    sm = _segment_to_stream_metadata(now, [], {}, _BRAND, show_upcoming=False)
    assert sm.title == "Brano misterioso"


def test_segment_empty_label_fallbacks_per_type() -> None:
    """news_flash / station_id / time_check with empty labels never produce empty titles."""
    for seg_type, expected in (
        ("news_flash", "News flash"),
        ("station_id", "mammamiradio"),
        ("time_check", "mammamiradio"),
    ):
        sm = _segment_to_stream_metadata(
            {"type": seg_type, "label": ""}, [], {}, _BRAND, show_upcoming=False
        )
        assert sm.title == expected, seg_type


def test_segment_empty_now_streaming_is_idle() -> None:
    """An empty now_streaming dict falls to the idle branch with the station name."""
    sm = _segment_to_stream_metadata({}, [], {}, _BRAND, show_upcoming=False)
    assert sm.title == "mammamiradio"
    assert sm.description is None


def test_segment_missing_brand_defaults_station_name() -> None:
    """A missing brand still yields a non-empty title via the literal fallback."""
    sm = _segment_to_stream_metadata({"type": "stopped"}, [], {}, {}, show_upcoming=False)
    assert sm.title == "Mamma Mi Radio"


def test_segment_malformed_upcoming_does_not_raise() -> None:
    """Upcoming entries missing a 'label' key must not raise a KeyError."""
    now = {"type": "music", "label": "X", "metadata": {"title": "X"}}
    # missing-label dict, then empty list — both must be tolerated
    sm1 = _segment_to_stream_metadata(now, [{}], {}, _BRAND, show_upcoming=True)
    sm2 = _segment_to_stream_metadata(now, [], {}, _BRAND, show_upcoming=True)
    assert sm1.title == "X"
    assert sm2.title == "X"


def test_segment_description_combines_upnext_and_casa() -> None:
    """When both are present, description carries 'Up next' AND 'A casa' together."""
    now = {"type": "music", "label": "X", "metadata": {"title": "X"}}
    upcoming = [{"type": "banter", "label": "Chiacchiere"}]
    ha = {"mood": "cena in famiglia"}
    sm = _segment_to_stream_metadata(now, upcoming, ha, _BRAND, show_upcoming=True)
    assert sm.description is not None
    assert "Up next: Chiacchiere" in sm.description
    assert "A casa: cena in famiglia" in sm.description


def test_segment_description_casa_only_when_not_show_upcoming() -> None:
    """On the 'Now' frame the HA line still renders even though 'Up next' is hidden."""
    now = {"type": "music", "label": "X", "metadata": {"title": "X"}}
    upcoming = [{"type": "banter", "label": "Chiacchiere"}]
    ha = {"mood": "cena in famiglia"}
    sm = _segment_to_stream_metadata(now, upcoming, ha, _BRAND, show_upcoming=False)
    assert sm.description == "A casa: cena in famiglia"


def test_segment_idle_suppresses_description() -> None:
    """Stopped / skipping segments push no description to MA media surfaces."""
    ha = {"mood": "cena in famiglia"}
    for seg_type in ("stopped", "skipping"):
        sm = _segment_to_stream_metadata(
            {"type": seg_type}, [{"label": "Next"}], ha, _BRAND, show_upcoming=True
        )
        assert sm.description is None, seg_type


def test_segment_description_weather_fallback_when_no_mood() -> None:
    """The 'A casa' line falls back to ha_moments.weather when no mood is present."""
    now = {"type": "music", "label": "X", "metadata": {"title": "X"}}
    sm = _segment_to_stream_metadata(
        now, [], {"weather": "soleggiato"}, _BRAND, show_upcoming=False
    )
    assert sm.description == "A casa: soleggiato"


def test_segment_banter_without_hosts_falls_back_to_station() -> None:
    """A banter segment with an empty hosts list uses the station name as artist."""
    sm = _segment_to_stream_metadata(
        {"type": "banter", "label": "Chiacchiere"},
        [],
        {},
        {"station_name": "mammamiradio", "hosts": []},
        show_upcoming=False,
    )
    assert sm.artist == "mammamiradio"


def test_segment_news_flash_without_host_falls_back_to_station() -> None:
    """A news_flash segment with no host metadata uses the station name as artist."""
    now = {"type": "news_flash", "label": "Notizie", "metadata": {}}
    sm = _segment_to_stream_metadata(now, [], {}, _BRAND, show_upcoming=False)
    assert sm.artist == "mammamiradio"


@pytest.mark.parametrize("placeholder", ["untitled", "unknown title", "UNTITLED", "  unknown  "])
def test_segment_music_all_placeholder_titles_clamp(placeholder: str) -> None:
    """Every placeholder title (case/space-insensitive) is treated as no title."""
    now = {"type": "music", "label": "Brano", "metadata": {"title": placeholder}}
    sm = _segment_to_stream_metadata(now, [], {}, _BRAND, show_upcoming=False)
    assert sm.title == "Brano"


def test_segment_non_string_metadata_values_are_dropped() -> None:
    """Non-string artist / album_art from untrusted JSON coerce to None, not garbage."""
    now = {
        "type": "music",
        "label": "X",
        "metadata": {"title": "X", "artist": 42, "album_art": ["not", "a", "url"]},
    }
    sm = _segment_to_stream_metadata(now, [], {}, _BRAND, show_upcoming=False)
    assert sm.title == "X"
    assert sm.artist is None
    assert sm.image_url is None


def test_segment_non_string_title_does_not_leak_repr() -> None:
    """A non-string title_only is coerced away, not rendered as its repr."""
    now = {"type": "music", "label": "Brano", "metadata": {"title_only": ["a", "b"]}}
    sm = _segment_to_stream_metadata(now, [], {}, _BRAND, show_upcoming=False)
    assert sm.title == "Brano"  # clamps to the label, never "['a', 'b']"


def test_segment_non_string_label_does_not_raise() -> None:
    """A non-string label is coerced so the terminal clamp never calls int.strip()."""
    now = {"type": "music", "label": 42, "metadata": {}}
    sm = _segment_to_stream_metadata(now, [], {}, _BRAND, show_upcoming=False)
    assert sm.title == "mammamiradio"  # no usable label or title -> station name


def test_segment_banter_string_hosts_falls_back_to_station() -> None:
    """A bare-string hosts value must not be iterated character-by-character."""
    sm = _segment_to_stream_metadata(
        {"type": "banter", "label": "Chiacchiere"},
        [],
        {},
        {"station_name": "mammamiradio", "hosts": "Gianni"},
        show_upcoming=False,
    )
    assert sm.artist == "mammamiradio"


def test_segment_non_string_casa_is_dropped() -> None:
    """A non-string ha mood/weather is dropped, not rendered as its repr."""
    now = {"type": "music", "label": "X", "metadata": {"title": "X"}}
    sm = _segment_to_stream_metadata(now, [], {"mood": 22}, _BRAND, show_upcoming=False)
    assert sm.description is None


def test_segment_non_dict_upcoming_entry_does_not_raise() -> None:
    """A non-dict first upcoming entry must not raise; the 'Up next' line is skipped."""
    now = {"type": "music", "label": "X", "metadata": {"title": "X"}}
    sm = _segment_to_stream_metadata(now, ["just-a-string"], {}, _BRAND, show_upcoming=True)
    assert sm.title == "X"
    assert sm.description is None


# ---------------------------------------------------------------------------
# Live typed-segment metadata — `_update_stream_metadata` callback (stateful)
# ---------------------------------------------------------------------------


async def _details_for(provider: MammamiradioProvider) -> Any:
    """Resolve a StreamDetails object to drive the metadata callback against."""
    return await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)


async def test_callback_populates_stream_metadata_from_public_status(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """The callback polls /public-status and sets stream_metadata."""
    details = await _details_for(provider)
    payload = {
        "now_streaming": {
            "type": "music",
            "label": "Volare",
            "started": 100,
            "metadata": {"title_only": "Volare", "artist": "Modugno"},
        },
        "upcoming": [],
        "brand": _BRAND,
    }
    mass_mock.http_session.get = MagicMock(return_value=_make_json_ctx(payload))
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata is not None
    assert details.stream_metadata.title == "Volare"
    assert details.stream_metadata.artist == "Modugno"
    # The poll hits /public-status, not /stream or /healthz.
    assert mass_mock.http_session.get.call_args.args[0].endswith("/public-status")


async def test_callback_alternates_now_then_upnext(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """Call 1 shows 'Now'; call 2 flips to 'Up next'; a segment change resets to 'Now'."""
    details = await _details_for(provider)
    payload = {
        "now_streaming": {"type": "music", "label": "A", "started": 1, "metadata": {"title": "A"}},
        "upcoming": [{"type": "banter", "label": "Chiacchiere"}],
        "brand": _BRAND,
    }
    mass_mock.http_session.get = MagicMock(return_value=_make_json_ctx(payload))

    # Call 1 — first frame of the segment is the "Now" view, no "Up next".
    await provider._update_stream_metadata(details, 0)
    desc1 = details.stream_metadata.description

    # Call 2 — same segment, alternates to "Up next".
    await provider._update_stream_metadata(details, 0)
    desc2 = details.stream_metadata.description

    # Call 3 — same segment, back to "Now" (show_upcoming is now True again).
    await provider._update_stream_metadata(details, 0)
    desc3 = details.stream_metadata.description

    # Segment change while show_upcoming is True — only the reset logic can
    # produce a "Now" frame here; without it this call would render "Up next".
    payload2 = {
        "now_streaming": {"type": "music", "label": "B", "started": 2, "metadata": {"title": "B"}},
        "upcoming": [{"type": "banter", "label": "Chiacchiere"}],
        "brand": _BRAND,
    }
    mass_mock.http_session.get = MagicMock(return_value=_make_json_ctx(payload2))
    await provider._update_stream_metadata(details, 0)
    desc4 = details.stream_metadata.description

    assert desc1 is None
    assert desc2 == "Up next: Chiacchiere"
    assert desc3 is None
    assert desc4 is None


async def test_callback_offline_public_status_leaves_prior_metadata(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A failing /public-status mid-stream must not raise and must not clobber metadata."""
    details = await _details_for(provider)
    good = {
        "now_streaming": {"type": "music", "label": "A", "started": 1, "metadata": {"title": "A"}},
        "upcoming": [],
        "brand": _BRAND,
    }
    mass_mock.http_session.get = MagicMock(return_value=_make_json_ctx(good))
    await provider._update_stream_metadata(details, 0)
    prior = details.stream_metadata
    assert prior is not None

    # Now the addon goes unreachable — callback must swallow it.
    mass_mock.http_session.get = MagicMock(
        return_value=_make_failing_ctx(aiohttp.ClientConnectionError("nope"))
    )
    await provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior

    # A 5xx is treated the same way.
    mass_mock.http_session.get = MagicMock(return_value=_make_json_ctx({}, status=503))
    await provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior


async def test_callback_handles_stopped_session(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A stopped session yields an idle StreamMetadata with no description."""
    details = await _details_for(provider)
    payload = {"now_streaming": {"type": "stopped"}, "upcoming": [], "brand": _BRAND}
    mass_mock.http_session.get = MagicMock(return_value=_make_json_ctx(payload))
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata.title == "mammamiradio"
    assert details.stream_metadata.description is None


async def test_callback_handles_null_now_streaming(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A payload with now_streaming=null falls to the idle branch without raising."""
    details = await _details_for(provider)
    mass_mock.http_session.get = MagicMock(
        return_value=_make_json_ctx({"now_streaming": None, "brand": _BRAND})
    )
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata.title == "mammamiradio"


# ---------------------------------------------------------------------------
# Live typed-segment metadata — error-swallowing / hardening
#
# The contract under test: neither the init probe nor the metadata callback may
# ever let a malformed addon response crash MA. handle_async_init RAISES a clean
# ProviderUnavailableError; the mid-stream callback SWALLOWS and keeps the prior
# frame so playback is never disturbed.
# ---------------------------------------------------------------------------


async def test_handle_async_init_raises_on_timeout(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A timeout on the init probe (v1 endpoint) surfaces as ProviderUnavailableError."""
    mass_mock.http_session.get = MagicMock(return_value=_make_failing_ctx(TimeoutError("slow")))
    with pytest.raises(ProviderUnavailableError):
        await provider.handle_async_init()


async def _seed_prior_metadata(
    provider: MammamiradioProvider, mass_mock: MagicMock, details: Any
) -> Any:
    """Drive one good callback so a prior stream_metadata frame exists, and return it."""
    good = {
        "now_streaming": {"type": "music", "label": "A", "started": 1, "metadata": {"title": "A"}},
        "upcoming": [],
        "brand": _BRAND,
    }
    mass_mock.http_session.get = MagicMock(return_value=_make_json_ctx(good))
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata is not None
    return details.stream_metadata


async def test_callback_swallows_timeout_and_keeps_prior(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A /public-status timeout mid-stream must not raise and must keep prior metadata."""
    details = await _details_for(provider)
    prior = await _seed_prior_metadata(provider, mass_mock, details)
    mass_mock.http_session.get = MagicMock(return_value=_make_failing_ctx(TimeoutError("slow")))
    await provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior


async def test_callback_swallows_bad_json_and_keeps_prior(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A /public-status body that is not valid JSON must be swallowed, prior kept."""
    details = await _details_for(provider)
    prior = await _seed_prior_metadata(provider, mass_mock, details)

    response = MagicMock()
    response.status = 200
    response.json = AsyncMock(side_effect=ValueError("not json"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    mass_mock.http_session.get = MagicMock(return_value=ctx)
    await provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior


async def test_callback_ignores_non_dict_payload(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A JSON array (not an object) from /public-status is ignored, prior kept."""
    details = await _details_for(provider)
    prior = await _seed_prior_metadata(provider, mass_mock, details)
    mass_mock.http_session.get = MagicMock(return_value=_make_json_ctx(["unexpected", "array"]))
    await provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior


async def test_callback_tolerates_malformed_segment_metadata(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    A segment whose ``metadata`` is the wrong type is coerced, not dropped.

    ``metadata`` arriving as a list (not an object) is treated as empty metadata;
    the mapper is total, so it yields a valid frame (title clamps to the label)
    instead of raising and being swallowed.
    """
    details = await _details_for(provider)
    await _seed_prior_metadata(provider, mass_mock, details)
    bad = {
        "now_streaming": {"type": "music", "label": "B", "started": 2, "metadata": ["wrong"]},
        "upcoming": [],
        "brand": _BRAND,
    }
    mass_mock.http_session.get = MagicMock(return_value=_make_json_ctx(bad))
    await provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata.title == "B"


async def test_callback_tolerates_non_dict_now_streaming(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    A non-dict now_streaming must not raise out of the callback (no-raise contract).

    Regression: ``seg_key`` is computed from ``now.get(...)`` OUTSIDE the mapper's
    try/except, so a truthy non-dict ``now_streaming`` previously escaped into MA's
    metadata-update task. The field is now type-guarded before that line.
    """
    details = await _details_for(provider)
    payload = {"now_streaming": ["unexpected", "list"], "upcoming": [], "brand": _BRAND}
    mass_mock.http_session.get = MagicMock(return_value=_make_json_ctx(payload))
    await provider._update_stream_metadata(details, 0)  # must not raise
    # Falls through to the idle branch with the station name.
    assert details.stream_metadata.title == "mammamiradio"


async def test_callback_swallows_mapper_exception(
    provider: MammamiradioProvider,
    mass_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The callback try/except is a backstop: if the mapper raises, prior frame is kept.

    The mapper is total by construction, so this monkeypatches it to raise and
    asserts the documented no-raise contract still holds for any future regression.
    """
    details = await _details_for(provider)
    prior = await _seed_prior_metadata(provider, mass_mock, details)

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("mapper blew up")

    monkeypatch.setattr("music_assistant.providers.mammamiradio._segment_to_stream_metadata", _boom)
    good_again = {
        "now_streaming": {"type": "music", "label": "C", "started": 3, "metadata": {"title": "C"}},
        "upcoming": [],
        "brand": _BRAND,
    }
    mass_mock.http_session.get = MagicMock(return_value=_make_json_ctx(good_again))
    await provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior


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


def test_v1_mapper_is_total_against_malformed_payload() -> None:
    """Non-dict now_playing / non-list up_next / non-dict station never raise."""
    payload = {"station": ["x"], "now_playing": ["not", "a", "dict"], "up_next": {"bad": 1}}
    sm = _v1_to_stream_metadata(payload, show_upcoming=True)
    assert sm.title == "Mamma Mi Radio"
    assert sm.description is None


# ---------------------------------------------------------------------------
# v1 now-playing contract — `_update_from_v1` callback (stateful)
# ---------------------------------------------------------------------------


async def test_v1_callback_populates_from_contract(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """The v1 callback polls the contract endpoint and sets stream_metadata."""
    provider._use_v1 = True
    details = await _details_for(provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"v1"'))
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata.title == "Volare"
    assert details.stream_metadata.artist == "Modugno"
    assert mass_mock.http_session.get.call_args.args[0].endswith("/api/integrations/v1/now-playing")


async def test_v1_callback_alternates_now_then_upnext(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """Call 1 renders the Now frame; call 2 (same segment) flips to the Up-next frame."""
    provider._use_v1 = True
    details = await _details_for(provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"v1"'))
    await provider._update_stream_metadata(details, 0)
    desc_now = details.stream_metadata.description
    assert desc_now is None
    await provider._update_stream_metadata(details, 0)
    desc_next = details.stream_metadata.description
    assert desc_next == "Up next: Chiacchiere"


async def test_v1_callback_304_reuses_cache_and_keeps_alternating(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A 304 reuses the cached payload (conditional poll) and still flips the view."""
    provider._use_v1 = True
    details = await _details_for(provider)
    mass_mock.http_session.get = MagicMock(
        side_effect=[
            _make_v1_ctx(_V1_MUSIC, status=200, etag='W/"v1"'),
            _make_v1_ctx(None, status=304),
        ]
    )
    await provider._update_stream_metadata(details, 0)  # 200 -> Now frame
    desc_now = details.stream_metadata.description
    assert desc_now is None
    await provider._update_stream_metadata(details, 0)  # 304 -> Up-next from cache
    desc_next = details.stream_metadata.description
    assert desc_next == "Up next: Chiacchiere"
    # The second request was conditional on the stored ETag.
    second_call = mass_mock.http_session.get.call_args_list[1]
    assert second_call.kwargs["headers"]["If-None-Match"] == 'W/"v1"'


async def test_v1_callback_swallows_unreachable_keeps_prior(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A mid-stream connection failure must not raise and keeps the prior frame."""
    provider._use_v1 = True
    details = await _details_for(provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"v1"'))
    await provider._update_stream_metadata(details, 0)
    prior = details.stream_metadata
    mass_mock.http_session.get = MagicMock(
        return_value=_make_failing_ctx(aiohttp.ClientConnectionError("nope"))
    )
    await provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior


async def test_v1_callback_http_error_keeps_prior(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A mid-stream 5xx from the v1 endpoint must not raise and keeps the prior frame."""
    provider._use_v1 = True
    details = await _details_for(provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"v1"'))
    await provider._update_stream_metadata(details, 0)
    prior = details.stream_metadata
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(None, status=503))
    await provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior


async def test_v1_callback_non_dict_payload_keeps_prior(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A JSON array from the v1 endpoint mid-stream is ignored, prior frame kept."""
    provider._use_v1 = True
    details = await _details_for(provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"v1"'))
    await provider._update_stream_metadata(details, 0)
    prior = details.stream_metadata
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(["unexpected", "array"]))
    await provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior


async def test_v1_callback_bad_json_keeps_prior(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A non-JSON body from the v1 endpoint mid-stream is ignored, prior frame kept."""
    provider._use_v1 = True
    details = await _details_for(provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"v1"'))
    await provider._update_stream_metadata(details, 0)
    prior = details.stream_metadata
    mass_mock.http_session.get = MagicMock(return_value=_make_bad_json_ctx())
    await provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior


async def test_v1_callback_resets_alternation_on_segment_change(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    A new segment resets the alternation back to the Now frame.

    Keyed on segment identity (segment_type/title/started_at), not the addon's
    ``changed_at`` clock, so a mid-segment queue-append does not snap to Now.
    """
    provider._use_v1 = True
    details = await _details_for(provider)
    other = {**_V1_MUSIC, "now_playing": {**_V1_MUSIC["now_playing"], "title": "OtherSong"}}
    mass_mock.http_session.get = MagicMock(
        side_effect=[
            _make_v1_ctx(_V1_MUSIC, etag='W/"a"'),
            _make_v1_ctx(_V1_MUSIC, etag='W/"a"'),
            _make_v1_ctx(_V1_MUSIC, etag='W/"a"'),
            _make_v1_ctx(other, etag='W/"b"'),
        ]
    )
    await provider._update_stream_metadata(details, 0)  # Now
    d1 = details.stream_metadata.description
    await provider._update_stream_metadata(details, 0)  # Up-next
    d2 = details.stream_metadata.description
    await provider._update_stream_metadata(details, 0)  # Now again (show_upcoming -> True)
    d3 = details.stream_metadata.description
    # Segment change while show_upcoming is True — only the reset logic can
    # produce a "Now" frame here; without it this call would render "Up next".
    await provider._update_stream_metadata(details, 0)
    d4 = details.stream_metadata.description
    assert d1 is None
    assert d2 == "Up next: Chiacchiere"
    assert d3 is None
    assert d4 is None
    assert details.stream_metadata.title == "OtherSong"


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


async def test_v1_callback_swallows_mapper_exception(
    provider: MammamiradioProvider,
    mass_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The v1 callback's try/except backstop keeps the prior frame if the mapper raises."""
    provider._use_v1 = True
    details = await _details_for(provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC, etag='W/"a"'))
    await provider._update_stream_metadata(details, 0)
    prior = details.stream_metadata

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("v1 mapper blew up")

    monkeypatch.setattr("music_assistant.providers.mammamiradio._v1_to_stream_metadata", _boom)
    other = {**_V1_MUSIC, "now_playing": {**_V1_MUSIC["now_playing"], "title": "X"}}
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(other, etag='W/"b"'))
    await provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior


async def test_v1_callback_cold_cache_304_keeps_prior(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A 304 with no cached payload (cold cache) is a no-op, not a crash."""
    provider._use_v1 = True
    details = await _details_for(provider)
    assert details.stream_metadata is None
    mass_mock.http_session.get = MagicMock(
        return_value=_make_v1_ctx(None, status=304, etag='W/"x"')
    )
    await provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is None


async def test_v1_callback_idle_no_now_playing(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A live response with session_state empty_queue / now_playing=null renders idle, no raise."""
    provider._use_v1 = True
    details = await _details_for(provider)
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
    await provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata.title == "mammamiradio"
    assert details.stream_metadata.description is None


async def test_v1_callback_without_etag_polls_unconditionally(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    If the addon omits the ETag header, the provider degrades gracefully.

    Each tick is a fresh 200 with no If-None-Match sent — the 304 optimization is
    simply unavailable, not a failure.
    """
    provider._use_v1 = True
    details = await _details_for(provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC))  # no etag
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata.title == "Volare"
    await provider._update_stream_metadata(details, 0)
    second = mass_mock.http_session.get.call_args_list[1]
    assert "If-None-Match" not in second.kwargs.get("headers", {})


async def test_v1_callback_resets_on_artist_change_same_title(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """
    Segment identity includes artist/host, not just type+title.

    The v1 payload carries no ``started_at``, so two consecutive segments sharing
    type and title (e.g. a same-title cover) must still be told apart by the
    other contract fields.
    """
    provider._use_v1 = True
    details = await _details_for(provider)
    cover = {**_V1_MUSIC, "now_playing": {**_V1_MUSIC["now_playing"], "artist": "Cover Band"}}
    mass_mock.http_session.get = MagicMock(
        side_effect=[
            _make_v1_ctx(_V1_MUSIC, etag='W/"a"'),
            _make_v1_ctx(cover, etag='W/"b"'),
        ]
    )
    await provider._update_stream_metadata(details, 0)  # Now (show_upcoming -> True)
    await provider._update_stream_metadata(details, 0)  # same title, new artist -> reset
    desc = details.stream_metadata.description
    assert desc is None
    assert details.stream_metadata.artist == "Cover Band"


async def test_v1_callback_drops_stale_etag_when_header_disappears(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A 200 without an ETag clears the stored validator; polling becomes unconditional."""
    provider._use_v1 = True
    details = await _details_for(provider)
    mass_mock.http_session.get = MagicMock(
        side_effect=[
            _make_v1_ctx(_V1_MUSIC, etag='W/"a"'),
            _make_v1_ctx(_V1_MUSIC),  # ETag header disappears
            _make_v1_ctx(_V1_MUSIC),
        ]
    )
    await provider._update_stream_metadata(details, 0)
    await provider._update_stream_metadata(details, 0)
    second = mass_mock.http_session.get.call_args_list[1]
    assert second.kwargs["headers"]["If-None-Match"] == 'W/"a"'
    await provider._update_stream_metadata(details, 0)
    third = mass_mock.http_session.get.call_args_list[2]
    assert "If-None-Match" not in third.kwargs.get("headers", {})


async def test_v1_callback_unsupported_schema_keeps_prior(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A drifted now-playing schema is ignored mid-stream instead of mapped loosely."""
    provider._use_v1 = True
    details = await _details_for(provider)
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(_V1_MUSIC))
    await provider._update_stream_metadata(details, 0)
    prior = details.stream_metadata
    unsupported = {**_V1_MUSIC, "schema_version": "2"}
    mass_mock.http_session.get = MagicMock(return_value=_make_v1_ctx(unsupported))
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata is prior


def test_legacy_banter_dict_hosts_use_display_name() -> None:
    """The /public-status fallback path also fixes the host-dict bug (real addon shape)."""
    brand = {
        "station_name": "mammamiradio",
        "hosts": [
            {"engine_host": "g", "display_name": "Gianni"},
            {"engine_host": "l", "display_name": "Lucia"},
        ],
    }
    sm = _segment_to_stream_metadata(
        {"type": "banter", "label": ""}, [], {}, brand, show_upcoming=False
    )
    assert sm.artist == "Gianni, Lucia"


# ---------------------------------------------------------------------------
# Live integration smoke (opt-in via MAMMAMIRADIO_LIVE_URL)
# ---------------------------------------------------------------------------


async def test_live_stream_smoke() -> None:
    """
    Live smoke test against a running mammamiradio addon. Skipped by default.

    Opt in by setting ``MAMMAMIRADIO_LIVE_URL`` (e.g. ``http://localhost:8000``).
    Verifies handle_async_init, browse, and get_stream_details against a real
    Icecast endpoint.
    """
    live_url = os.environ.get("MAMMAMIRADIO_LIVE_URL")
    if not live_url:
        pytest.skip("MAMMAMIRADIO_LIVE_URL not set")

    async with aiohttp.ClientSession() as session:
        mass = MagicMock()
        mass.http_session = session
        prov = _build_provider_with_url(mass, live_url)

        # Init probes the v1 now-playing contract first and falls back to the
        # legacy /healthz + /public-status pair on older addons; raises only if
        # the addon is unreachable or unhealthy.
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
        # The live-metadata callback polls the endpoint selected at init
        # (v1 now-playing on current addons, /public-status on older ones).
        await prov._update_stream_metadata(details, 0)
        assert details.stream_metadata is not None
        assert details.stream_metadata.title
