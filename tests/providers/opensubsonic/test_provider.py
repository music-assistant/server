"""Test the OpenSubsonic provider methods (internet radio support).

These exercise the provider's radio methods against a mocked py-opensonic
connection (provider.conn). The parser snapshot tests live in test_parsers.py;
here we test the provider's library/single-item/stream-detail wiring.
"""

from unittest.mock import AsyncMock, Mock

import pytest
from libopensonic.media.media_types import InternetRadioStation
from music_assistant_models.enums import MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Radio
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.opensubsonic import SUPPORTED_FEATURES
from music_assistant.providers.opensubsonic.sonic_provider import (
    CONF_ENABLE_RADIOS,
    OpenSonicProvider,
)


def _station(iid: str, name: str, url: str, *, cover: str | None = None) -> InternetRadioStation:
    """Build an InternetRadioStation as py-opensonic would return one."""
    payload: dict[str, str] = {"id": iid, "name": name, "streamUrl": url}
    if cover:
        payload["coverArt"] = cover
    return InternetRadioStation.from_dict(payload)


_STATIONS = [
    _station("1", "HBR1 Tronic", "http://hbr1.example/tronic.aac", cover="cv1"),
    _station(
        "v1.chan.streamer",
        "streamer (Twitch)",
        "http://ts.lan:4533/rest/stream.view?id=v1.chan.streamer",
    ),
]


def _make_provider(*, enable_radios: bool = True) -> OpenSonicProvider:
    """Build an OpenSonicProvider with a mocked connection (no network).

    The base provider reads config values at construction (notably log_level),
    so config.get_value must return real values, not bare Mocks. enable_radios
    drives the CONF_ENABLE_RADIOS toggle (mirrors the podcasts _enable_podcasts
    config-time gate).
    """
    mass = Mock()
    manifest = Mock()
    manifest.domain = "opensubsonic"
    config = Mock()
    config.instance_id = "xx-instance-id-xx"
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "INFO",
        "enable_radios": enable_radios,
    }.get(key, default)
    prov = OpenSonicProvider(mass, manifest, config, SUPPORTED_FEATURES)
    # instance_id is a read-only property derived from config (set above).
    prov.conn = Mock()
    # the toggle is normally set in handle_async_init; set it directly here so
    # the per-method early-out is exercised without standing up a live init.
    prov._enable_radios = enable_radios
    return prov


@pytest.fixture
def provider() -> OpenSonicProvider:
    """Return an OpenSonicProvider with radio enabled (the default case)."""
    return _make_provider(enable_radios=True)


# --- feature flag -----------------------------------------------------------


def test_radio_feature_declared() -> None:
    """LIBRARY_RADIOS must be advertised so MA surfaces the Radios browse node."""
    assert ProviderFeature.LIBRARY_RADIOS in SUPPORTED_FEATURES


def test_radio_config_key_wired() -> None:
    """Pin the CONF_ENABLE_RADIOS key string contract.

    This key is the contract between the config UI (the __init__.py ConfigEntry)
    and what handle_async_init reads. The toggle tests force-set _enable_radios
    and so can't catch a wrong key string; this pins it directly, mirroring
    CONF_ENABLE_PODCASTS == "enable_podcasts".
    """
    assert CONF_ENABLE_RADIOS == "enable_radios"


# --- get_library_radios -----------------------------------------------------


async def test_get_library_radios_yields_all(provider: OpenSonicProvider) -> None:
    """Every station from the server is yielded as a Radio."""
    provider.conn.get_internet_radio_stations = AsyncMock(return_value=_STATIONS)
    results = [r async for r in provider.get_library_radios()]
    assert len(results) == 2
    assert all(isinstance(r, Radio) for r in results)
    assert {r.item_id for r in results} == {"1", "v1.chan.streamer"}
    provider.conn.get_internet_radio_stations.assert_awaited_once()


async def test_get_library_radios_empty(provider: OpenSonicProvider) -> None:
    """A server with no stations yields nothing (no crash)."""
    provider.conn.get_internet_radio_stations = AsyncMock(return_value=[])
    results = [r async for r in provider.get_library_radios()]
    assert results == []


async def test_get_library_radios_disabled_yields_nothing() -> None:
    """Disabling CONF_ENABLE_RADIOS short-circuits before touching the server.

    Mirrors the podcasts _enable_podcasts config-time gate.
    """
    prov = _make_provider(enable_radios=False)
    prov.conn.get_internet_radio_stations = AsyncMock(return_value=_STATIONS)
    results = [r async for r in prov.get_library_radios()]
    assert results == []
    # disabled means no server call at all (not "fetch then drop").
    prov.conn.get_internet_radio_stations.assert_not_awaited()


# --- get_radio (single item) ------------------------------------------------


async def test_get_radio_found(provider: OpenSonicProvider) -> None:
    """A known station id resolves to its Radio.

    Targets the SECOND station (not _STATIONS[0]) so a "return the first
    station" stub can't pass by accident — the id must actually be matched.
    """
    provider.conn.get_internet_radio_stations = AsyncMock(return_value=_STATIONS)
    radio = await provider.get_radio("v1.chan.streamer")
    assert isinstance(radio, Radio)
    assert radio.item_id == "v1.chan.streamer"
    assert radio.name == "streamer (Twitch)"
    # the list was fetched + filtered (no singular getter exists), not guessed.
    provider.conn.get_internet_radio_stations.assert_awaited_once()


async def test_get_radio_not_found(provider: OpenSonicProvider) -> None:
    """An unknown station id raises MediaNotFoundError (not a silent None)."""
    provider.conn.get_internet_radio_stations = AsyncMock(return_value=_STATIONS)
    with pytest.raises(MediaNotFoundError):
        await provider.get_radio("does-not-exist")


# --- get_stream_details(RADIO) ----------------------------------------------


async def test_get_stream_details_radio(provider: OpenSonicProvider) -> None:
    """A radio item streams as a direct HTTP URL, unseekable, no proxying.

    NOTE on false-confidence: StreamDetails defaults media_type=TRACK,
    stream_type=CUSTOM, path=None, allow_seek=False, can_seek=False. So the
    load-bearing assertions are the ones that DIFFER from the defaults
    (media_type=RADIO, stream_type=HTTP, path=<url>) — those prove the radio
    branch actually ran. allow_seek/can_seek==False match the defaults, so on
    their own they'd pass even against an unset object; we still assert them as
    a behavioral statement, but they are NOT the discriminating checks.
    """
    provider.conn.get_internet_radio_stations = AsyncMock(return_value=_STATIONS)
    # target the SECOND station so a "return _STATIONS[0]" stub can't pass by
    # position rather than by matching the requested id.
    sd = await provider.get_stream_details("v1.chan.streamer", MediaType.RADIO)
    assert isinstance(sd, StreamDetails)
    # discriminating (non-default) — these fail if the radio branch didn't run:
    assert sd.media_type == MediaType.RADIO  # default is TRACK
    assert sd.stream_type == StreamType.HTTP  # default is CUSTOM
    # the SECOND station's streamUrl, proving id-match not position:
    assert sd.path == "http://ts.lan:4533/rest/stream.view?id=v1.chan.streamer"
    assert sd.provider == "xx-instance-id-xx"
    assert sd.item_id == "v1.chan.streamer"
    # behavioral: radio is a live, unseekable stream. allow_seek=False also
    # discriminates against a "reuse the TRACK builder" impl, since the TRACK
    # path sets allow_seek=True (sonic_provider.py:687).
    assert sd.allow_seek is False
    assert sd.can_seek is False
    # the list was fetched + filtered (no singular getter), not guessed:
    provider.conn.get_internet_radio_stations.assert_awaited_once()


async def test_get_stream_details_radio_not_found(provider: OpenSonicProvider) -> None:
    """An unknown radio id in get_stream_details raises MediaNotFoundError."""
    provider.conn.get_internet_radio_stations = AsyncMock(return_value=_STATIONS)
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("nope", MediaType.RADIO)


async def test_get_stream_details_track_unchanged(provider: OpenSonicProvider) -> None:
    """Adding the radio branch must not disturb the existing TRACK stream path.

    A CUSTOM stream type is still returned for tracks.
    """
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
    provider._seek_support = False
    sd = await provider.get_stream_details("t1", MediaType.TRACK)
    assert sd.media_type == MediaType.TRACK
    assert sd.stream_type == StreamType.CUSTOM
