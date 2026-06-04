"""Tests for the mammamiradio music provider."""

from __future__ import annotations

import os
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
    STREAM_METADATA_UPDATE_INTERVAL,
    SUPPORTED_FEATURES,
    MammamiradioProvider,
    _segment_to_stream_metadata,
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


async def test_handle_async_init_passes_when_reachable(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """Path 1 — reachable addon: init logs success and does not raise."""
    mass_mock.http_session.get = MagicMock(return_value=_make_response_ctx(200))
    await provider.handle_async_init()
    mass_mock.http_session.get.assert_called_once()
    called_url = mass_mock.http_session.get.call_args.args[0]
    assert called_url == "http://localhost:8000/healthz"


async def test_handle_async_init_raises_when_unreachable(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """Path 2 — unreachable addon: init raises ProviderUnavailableError.

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


async def test_handle_async_init_raises_on_http_error(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """5xx (or any >=400) from /healthz raises ProviderUnavailableError.

    Distinct from the unreachable case: the addon process is up enough to
    respond, but reports unhealthy. Surfaces as ProviderUnavailableError
    same as the unreachable case so MA treats both consistently.
    """
    mass_mock.http_session.get = MagicMock(return_value=_make_response_ctx(503))
    with pytest.raises(ProviderUnavailableError):
        await provider.handle_async_init()


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
    mass_mock.http_session.get = MagicMock(return_value=_make_response_ctx(200))
    details = await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.audio_format.content_type == ContentType.MP3
    assert details.audio_format.bit_rate == 128
    assert details.media_type == MediaType.RADIO


async def test_get_stream_details_uses_configured_url_with_stream_suffix(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """Path 10 — stream path is hard-coded as ``${url}/stream``."""
    mass_mock.http_session.get = MagicMock(return_value=_make_response_ctx(200))
    details = await provider.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8000/stream"
    assert details.allow_seek is False
    assert details.can_seek is False


async def test_get_stream_details_does_not_probe_at_stream_time(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """Stream-time has no HTTP probe; liveness is checked at init only.

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


async def test_fragment_in_configured_url_is_stripped(
    mass_mock: MagicMock,
) -> None:
    """A configured URL with a fragment must not corrupt the stream URL."""
    prov = _build_provider_with_url(mass_mock, "http://localhost:8000#frag")
    mass_mock.http_session.get = MagicMock(return_value=_make_response_ctx(200))
    details = await prov.get_stream_details(RADIO_ITEM_ID, MediaType.RADIO)
    assert details.path == "http://localhost:8000/stream"


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
    """A sweeper segment (real SegmentType, no dedicated branch) still yields a title.

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
    assert sm.title == "mammamiradio"


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
    assert desc1 is None or "Up next:" not in desc1

    # Call 2 — same segment, alternates to "Up next".
    await provider._update_stream_metadata(details, 0)
    assert "Up next: Chiacchiere" in details.stream_metadata.description

    # Segment change — resets the alternation back to "Now".
    payload2 = {
        "now_streaming": {"type": "music", "label": "B", "started": 2, "metadata": {"title": "B"}},
        "upcoming": [{"type": "banter", "label": "Chiacchiere"}],
        "brand": _BRAND,
    }
    mass_mock.http_session.get = MagicMock(return_value=_make_json_ctx(payload2))
    await provider._update_stream_metadata(details, 0)
    desc3 = details.stream_metadata.description
    assert desc3 is None or "Up next:" not in desc3


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
    """A timeout probing /healthz surfaces as ProviderUnavailableError."""
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


async def test_callback_swallows_malformed_segment(
    provider: MammamiradioProvider, mass_mock: MagicMock
) -> None:
    """A structurally valid payload whose segment metadata is the wrong type is swallowed.

    ``metadata`` arriving as a list (not an object) makes the mapper raise; the
    callback must catch it so the metadata-update task never crashes playback.
    """
    details = await _details_for(provider)
    prior = await _seed_prior_metadata(provider, mass_mock, details)
    bad = {
        "now_streaming": {"type": "music", "label": "B", "started": 2, "metadata": ["wrong"]},
        "upcoming": [],
        "brand": _BRAND,
    }
    mass_mock.http_session.get = MagicMock(return_value=_make_json_ctx(bad))
    await provider._update_stream_metadata(details, 0)  # must not raise
    assert details.stream_metadata is prior


# ---------------------------------------------------------------------------
# Live integration smoke (opt-in via MAMMAMIRADIO_LIVE_URL)
# ---------------------------------------------------------------------------


async def test_live_stream_smoke() -> None:
    """Live smoke test against a running mammamiradio addon. Skipped by default.

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

        # Init raises if /healthz is absent or unhealthy.
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
        # The live-metadata callback fetches a real /public-status payload.
        await prov._update_stream_metadata(details, 0)
        assert details.stream_metadata is not None
        assert details.stream_metadata.title
