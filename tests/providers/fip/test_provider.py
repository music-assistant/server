"""Tests for the FIP music provider."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError, UnplayableMediaError

from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.providers.fip import SUPPORTED_FEATURES
from music_assistant.providers.fip.constants import (
    FIP_STATIONS,
    OVERDUE_UPDATE_INTERVAL,
    STREAM_DELAY,
    STREAM_METADATA_UPDATE_INTERVAL,
)
from music_assistant.providers.fip.helpers import LiveDisplay, LiveItem
from music_assistant.providers.fip.provider import LIVE_DISPLAY_KEY, FipProvider

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict[str, Any]:
    """Return a recorded answer of the now-playing service."""
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


def _song_now(start: float, length: int = 200) -> dict[str, Any]:
    """Return an answer whose current song started at the given time."""
    return {
        "now": {
            "firstLine": "Song",
            "secondLine": "Artist",
            "cover": "cover",
            "startTime": int(start),
            "endTime": int(start) + length,
            "songUuid": "song",
        },
        "next": [],
    }


@pytest.fixture
def provider() -> FipProvider:
    """Return a FipProvider wired to mocks for unit testing."""
    manifest = MagicMock()
    manifest.domain = "fip"
    manifest.name = "FIP"

    config = MagicMock()
    config.instance_id = "fip_test"
    config.values = {}

    def _get_value(key: str, default: Any = None) -> Any:
        if key == "log_level":
            return "GLOBAL"
        return default

    config.get_value.side_effect = _get_value
    provider = FipProvider(MagicMock(), manifest, config, SUPPORTED_FEATURES)
    provider._get_pull = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return provider


async def test_get_radio_and_unknown_station(provider: FipProvider) -> None:
    """A known station resolves with its own picture and text, an unknown one raises."""
    provider._get_live = AsyncMock()  # type: ignore[method-assign]
    radio = await provider.get_radio("fip_jazz")
    assert radio.name == "FIP Jazz"
    assert radio.metadata.description == FIP_STATIONS["fip_jazz"]["description"]
    assert radio.metadata.images
    assert FIP_STATIONS["fip_jazz"]["image_id"] in radio.metadata.images[0].path
    provider._get_live.assert_not_awaited()
    with pytest.raises(MediaNotFoundError):
        await provider.get_radio("france_inter")


async def test_search_matches_station_names(provider: FipProvider) -> None:
    """Search finds stations by name, for radio only."""
    results = await provider.search("metal", [MediaType.RADIO])
    assert [radio.item_id for radio in results.radio] == ["fip_metal"]
    assert (await provider.search("fip", [MediaType.RADIO], limit=3)).radio
    assert len((await provider.search("fip", [MediaType.RADIO], limit=3)).radio) == 3
    assert not (await provider.search("metal", [MediaType.TRACK])).radio


async def test_browse_lists_all_thirteen_stations(provider: FipProvider) -> None:
    """Browsing lists FIP and its twelve webradios."""
    items = await provider.browse("fip://")
    assert len(items) == 13
    assert items[0].name == "FIP"
    assert [item.item_id for item in items] == list(FIP_STATIONS)


async def test_a_stream_on_the_placeholder_shows_the_older_services_song(
    provider: FipProvider,
) -> None:
    """While the service answers with its placeholder, the older service's song stands in."""
    provider._get_live = AsyncMock(return_value=_fixture("webradio_placeholder"))  # type: ignore[method-assign]
    provider._get_pull = AsyncMock(return_value=_fixture("rock_pull"))  # type: ignore[method-assign]
    with patch("music_assistant.providers.fip.provider.time.time", return_value=1792838425):
        details = await provider.get_stream_details("fip_rock", MediaType.RADIO)
    assert details.stream_metadata is not None
    assert (details.stream_metadata.title, details.stream_metadata.artist) == (
        "Rise",
        "Public Image Limited",
    )


async def test_stream_details_wire_the_live_metadata(provider: FipProvider) -> None:
    """The stream plays the station's Icecast mount and starts with the live song."""
    provider._get_live = AsyncMock(  # type: ignore[method-assign]
        return_value=_song_now(time.time())
    )
    details = await provider.get_stream_details("fip_sacre_francais", MediaType.RADIO)

    assert details.path == "https://icecast.radiofrance.fr/fipsacrefrancais-hifi.aac"
    assert details.stream_type == StreamType.HTTP
    assert details.audio_format.content_type == ContentType.AAC
    assert details.stream_metadata_update_callback == provider._update_stream_metadata
    assert details.stream_metadata_update_interval == STREAM_METADATA_UPDATE_INTERVAL
    assert details.stream_metadata is not None
    assert details.stream_metadata.title == "Song"
    provider._get_live.assert_awaited_once_with(96)


async def test_get_live_reads_the_service(provider: FipProvider) -> None:
    """The live answer comes from the service, and a failed request gives None."""
    provider._throttler = Throttler(rate_limit=5, period=1.0)
    response = MagicMock(status=200)
    response.json = AsyncMock(return_value=_fixture("webradio_song"))
    request = MagicMock()
    request.__aenter__ = AsyncMock(return_value=response)
    request.__aexit__ = AsyncMock(return_value=None)
    session = cast("MagicMock", provider.mass.http_session)
    session.get.return_value = request

    data = await provider._get_live(65)
    assert data == _fixture("webradio_song")
    url = session.get.call_args.args[0]
    assert url == "https://api.radiofrance.fr/livemeta/live/65/webrf_webradio_player"

    response.status = 503
    assert await provider._get_live(66) is None
    request.__aenter__.side_effect = TimeoutError
    assert await provider._get_live(64) is None


async def test_stream_details_refuse_other_media(provider: FipProvider) -> None:
    """Only radio items of this provider can be streamed."""
    with pytest.raises(UnplayableMediaError):
        await provider.get_stream_details("fip", MediaType.TRACK)
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("france_inter", MediaType.RADIO)


async def test_update_asks_more_often_while_overdue(provider: FipProvider) -> None:
    """The callback shortens its interval once the song should be over, and restores it."""
    provider._get_live = AsyncMock(  # type: ignore[method-assign]
        return_value=_song_now(time.time() - 300)
    )
    details = await provider.get_stream_details("fip", MediaType.RADIO)
    assert details.stream_metadata_update_interval == OVERDUE_UPDATE_INTERVAL

    provider._get_live.return_value = _song_now(time.time())
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata_update_interval == STREAM_METADATA_UPDATE_INTERVAL


async def test_a_failed_request_keeps_the_display(provider: FipProvider) -> None:
    """When the service does not answer, the metadata on display stays."""
    provider._get_live = AsyncMock(  # type: ignore[method-assign]
        return_value=_fixture("webradio_song")
    )
    details = await provider.get_stream_details("fip_jazz", MediaType.RADIO)
    shown = details.stream_metadata

    provider._get_live.return_value = None
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata is shown
    assert details.data[LIVE_DISPLAY_KEY].shown is not None


async def test_an_announced_song_takes_over_while_the_service_is_down(
    provider: FipProvider,
) -> None:
    """A song announced before the service stopped answering is still shown when it is due."""
    provider._get_live = AsyncMock(return_value=None)  # type: ignore[method-assign]
    details = await provider.get_stream_details("fip_jazz", MediaType.RADIO)
    now = time.time()
    song = LiveItem("Next", "Artist", "cover", int(now) - STREAM_DELAY - 1, int(now) + 200, "b")
    details.data[LIVE_DISPLAY_KEY] = LiveDisplay(
        shown=LiveItem("Song", "Artist", "cover", int(now) - 300, song.start, "a"),
        announced=song,
    )
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata is not None
    assert details.stream_metadata.title == "Next"


async def test_the_older_service_names_the_song_the_listener_hears(
    provider: FipProvider,
) -> None:
    """Just after a song started in the studio, the listener still hears the one before it."""
    provider._get_live = AsyncMock(return_value=_fixture("webradio_placeholder"))  # type: ignore[method-assign]
    provider._get_pull = AsyncMock(return_value=_fixture("rock_pull"))  # type: ignore[method-assign]
    # "Rise" starts at 1792838295 in the studio
    with patch("music_assistant.providers.fip.provider.time.time", return_value=1792838296):
        details = await provider.get_stream_details("fip_rock", MediaType.RADIO)
    assert details.stream_metadata is not None
    assert details.stream_metadata.title == "Thinking of you"


def test_stations_count_as_radio_for_searches(provider: FipProvider) -> None:
    """The stations take part in search-based lookups such as matching library radios."""
    assert provider.supported_media_types == {MediaType.RADIO}


async def test_a_placeholder_shows_the_station(provider: FipProvider) -> None:
    """A station on its placeholder gives no metadata, so the player shows the station."""
    provider._get_live = AsyncMock(  # type: ignore[method-assign]
        return_value=_fixture("webradio_placeholder")
    )
    details = await provider.get_stream_details("fip_groove", MediaType.RADIO)
    assert details.stream_metadata is None
