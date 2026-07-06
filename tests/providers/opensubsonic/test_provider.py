"""
Test the OpenSubsonic provider methods (internet radio support).

These exercise the provider's radio methods against a mocked py-opensonic
connection (provider.conn). The parser snapshot tests live in test_parsers.py;
here we test the provider's library/single-item/stream-detail wiring.
"""

from unittest.mock import AsyncMock, Mock

import pytest
from libopensonic.media.media_types import InternetRadioStation
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import ProviderMapping, Radio
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.opensubsonic import SUPPORTED_FEATURES
from music_assistant.providers.opensubsonic.sonic_provider import OpenSonicProvider


def _station(iid: str, name: str, url: str, *, cover: str | None = None) -> InternetRadioStation:
    """Build an InternetRadioStation as py-opensonic would return one."""
    payload: dict[str, str] = {"id": iid, "name": name, "streamUrl": url}
    if cover:
        payload["coverArt"] = cover
    return InternetRadioStation.from_dict(payload)


_STATIONS = [
    _station("1", "HBR1 Tronic", "http://hbr1.example/tronic.aac", cover="cv1"),
    _station(
        "station-2",
        "Some Station",
        "http://radio.example.com/rest/stream.view?id=station-2",
    ),
]


def _make_provider() -> OpenSonicProvider:
    """
    Build an OpenSonicProvider with a mocked connection (no network).

    The base provider reads config values at construction (notably log_level),
    so config.get_value must return real values, not bare Mocks.
    """
    mass = Mock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    manifest = Mock()
    manifest.domain = "opensubsonic"
    config = Mock()
    config.instance_id = "xx-instance-id-xx"
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "INFO",
    }.get(key, default)
    prov = OpenSonicProvider(mass, manifest, config, SUPPORTED_FEATURES)
    prov.conn = Mock()
    return prov


@pytest.fixture
def provider() -> OpenSonicProvider:
    """Return an OpenSonicProvider with a mocked connection."""
    return _make_provider()


# --- get_library_radios -----------------------------------------------------


async def test_get_library_radios_yields_all(provider: OpenSonicProvider) -> None:
    """Every station from the server is yielded as a Radio."""
    provider.conn.get_internet_radio_stations = AsyncMock(return_value=_STATIONS)
    results = [r async for r in provider.get_library_radios()]
    assert len(results) == 2
    assert all(isinstance(r, Radio) for r in results)
    assert {r.item_id for r in results} == {"1", "station-2"}
    provider.conn.get_internet_radio_stations.assert_awaited_once()


# --- get_radio (single item) ------------------------------------------------


async def test_get_radio_found(provider: OpenSonicProvider) -> None:
    """A known station id resolves to its Radio."""
    provider.conn.get_internet_radio_stations = AsyncMock(return_value=_STATIONS)
    radio = await provider.get_radio("station-2")
    assert isinstance(radio, Radio)
    assert radio.item_id == "station-2"
    assert radio.name == "Some Station"
    provider.conn.get_internet_radio_stations.assert_awaited_once()


async def test_get_radio_not_found(provider: OpenSonicProvider) -> None:
    """An unknown station id raises MediaNotFoundError (not a silent None)."""
    provider.conn.get_internet_radio_stations = AsyncMock(return_value=_STATIONS)
    with pytest.raises(MediaNotFoundError):
        await provider.get_radio("does-not-exist")


# --- get_stream_details(RADIO) ----------------------------------------------


def _library_radio(item_id: str, instance_id: str, stream_url: str | None) -> Radio:
    """Build a library Radio whose provider mapping carries the stream URL in details."""
    return Radio(
        item_id=item_id,
        name="in-library",
        provider="opensubsonic",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="opensubsonic",
                provider_instance=instance_id,
                details=stream_url,
            )
        },
    )


def _set_library_lookup(provider: OpenSonicProvider, return_value: Radio | None) -> AsyncMock:
    """Wire provider.mass.music.radio.get_library_item_by_prov_id to a controlled result."""
    lookup = AsyncMock(return_value=return_value)
    provider.mass.music.radio.get_library_item_by_prov_id = lookup  # type: ignore[method-assign]
    return lookup


async def test_get_stream_details_radio_from_library(provider: OpenSonicProvider) -> None:
    """Library-first: read the stream URL from the synced item, no list re-fetch."""
    url = "http://radio.example.com/rest/stream.view?id=station-2"
    lookup = _set_library_lookup(provider, _library_radio("station-2", "xx-instance-id-xx", url))
    provider.conn.get_internet_radio_stations = AsyncMock(return_value=_STATIONS)

    sd = await provider.get_stream_details("station-2", MediaType.RADIO)

    assert isinstance(sd, StreamDetails)
    assert sd.media_type == MediaType.RADIO
    assert sd.stream_type == StreamType.HTTP
    assert sd.path == url
    assert sd.provider == "xx-instance-id-xx"
    assert sd.item_id == "station-2"
    assert sd.allow_seek is False
    assert sd.can_seek is False
    lookup.assert_awaited_once_with("station-2", "xx-instance-id-xx")
    provider.conn.get_internet_radio_stations.assert_not_awaited()


async def test_get_stream_details_radio_not_in_library_raises(
    provider: OpenSonicProvider,
) -> None:
    """A radio id absent from the library raises MediaNotFoundError with no server re-fetch."""
    _set_library_lookup(provider, None)  # not in library
    provider.conn.get_internet_radio_stations = AsyncMock(return_value=_STATIONS)

    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("station-2", MediaType.RADIO)

    provider.conn.get_internet_radio_stations.assert_not_awaited()


async def test_get_stream_details_track_unchanged(provider: OpenSonicProvider) -> None:
    """Adding the radio branch must not disturb the existing TRACK stream path."""
    song = Mock()
    song.id = "t1"
    song.title = "A Track"
    song.transcoded_content_type = None
    song.content_type = "audio/mpeg"
    song.sampling_rate = 44100
    song.bit_depth = 16
    song.channel_count = 2
    song.duration = 180
    provider.conn.get_song = AsyncMock(return_value=song)
    provider._raw_file = False
    # the TRACK path resolves the stream URL + POST params through get_stream_url;
    # it returns a (url, params) tuple that get_stream_details unpacks.
    provider.conn.get_stream_url = Mock(
        return_value=("http://music.example.com/rest/stream.view", {"id": "t1", "u": "user"})
    )
    sd = await provider.get_stream_details("t1", MediaType.TRACK)
    assert sd.media_type == MediaType.TRACK
    assert sd.stream_type == StreamType.HTTP
    assert sd.allow_seek is True
    assert sd.can_seek is True
    assert sd.path == "http://music.example.com/rest/stream.view"
    provider.conn.get_stream_url.assert_called_once()
