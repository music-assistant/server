"""Tests for the Sunshine Live music provider."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError, UnplayableMediaError
from music_assistant_models.media_items import Radio

from music_assistant.providers.sunshine_live import SUPPORTED_FEATURES
from music_assistant.providers.sunshine_live.constants import CHANNELS
from music_assistant.providers.sunshine_live.provider import SunshineLiveProvider


@pytest.fixture
def provider() -> SunshineLiveProvider:
    """Return a provider wired to mocks."""
    manifest = MagicMock()
    manifest.domain = "sunshine_live"
    manifest.name = "Sunshine Live"

    config = MagicMock()
    config.instance_id = "sunshine_live_test"
    config.values = {}

    def _get_value(key: str, default: Any = None) -> Any:
        return "GLOBAL" if key == "log_level" else default

    config.get_value.side_effect = _get_value
    return SunshineLiveProvider(MagicMock(), manifest, config, SUPPORTED_FEATURES)


def test_channels_do_not_share_a_name_or_a_stream() -> None:
    """Two channels with the same name or stream would look like one in the library."""
    assert len({channel["name"] for channel in CHANNELS.values()}) == len(CHANNELS)
    assert len({channel["path"] for channel in CHANNELS.values()}) == len(CHANNELS)


def test_channel_images_are_on_the_image_server_of_the_station() -> None:
    """A channel image comes from the station, without a fixed size."""
    for channel in CHANNELS.values():
        assert channel["image"].startswith("https://images.sunshine-live.de/files/")
        assert "?" not in channel["image"]


async def test_browse_shows_all_channels_with_the_main_program_first(
    provider: SunshineLiveProvider,
) -> None:
    """Every channel is listed with its picture, and the main program leads."""
    items = await provider.browse("sunshine_live://")

    assert [item.item_id for item in items] == list(CHANNELS)
    assert items[0].name == "Sunshine Live"
    assert all(isinstance(item, Radio) and item.metadata.images for item in items)


async def test_get_radio_returns_the_channel(provider: SunshineLiveProvider) -> None:
    """A known channel has its own name, picture and mapping on this instance."""
    radio = await provider.get_radio("classics")

    assert radio.name == "Sunshine Live - Classics"
    assert radio.provider == "sunshine_live_test"
    assert radio.metadata.images
    assert radio.metadata.images[0].path == CHANNELS["classics"]["image"]
    assert radio.metadata.images[0].remotely_accessible
    mapping = next(iter(radio.provider_mappings))
    assert mapping.item_id == "classics"
    assert mapping.provider_domain == "sunshine_live"
    assert mapping.provider_instance == "sunshine_live_test"


async def test_get_radio_refuses_an_unknown_channel(provider: SunshineLiveProvider) -> None:
    """A channel that left the list is reported as not found."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_radio("unknown")


async def test_search_finds_a_channel_by_part_of_its_name(
    provider: SunshineLiveProvider,
) -> None:
    """Case and spaces around the search text do not matter."""
    results = await provider.search("  MAYDAY ", [MediaType.RADIO])

    assert [radio.item_id for radio in results.radio] == ["mayday"]


async def test_search_keeps_to_the_limit(provider: SunshineLiveProvider) -> None:
    """All channels hold the station name, so the limit has to apply."""
    results = await provider.search("sunshine", [MediaType.RADIO], limit=3)

    assert [radio.item_id for radio in results.radio] == list(CHANNELS)[:3]


@pytest.mark.parametrize(
    ("query", "media_types"),
    [("mayday", [MediaType.TRACK]), ("   ", [MediaType.RADIO]), ("nothing", [MediaType.RADIO])],
    ids=["other-media-type", "empty-query", "no-match"],
)
async def test_search_without_a_result(
    provider: SunshineLiveProvider, query: str, media_types: list[MediaType]
) -> None:
    """A search that cannot match returns nothing."""
    results = await provider.search(query, media_types)

    assert list(results.radio) == []


async def test_stream_details_use_the_best_stream_of_the_channel(
    provider: SunshineLiveProvider,
) -> None:
    """The stream server knows a channel by its own name, which can differ from the id."""
    details = await provider.get_stream_details("chillout", MediaType.RADIO)

    assert details.path == "https://stream.sunshine-live.de/sp8/mp3-192/stream.sunshine-live.de/"
    assert details.provider == "sunshine_live_test"
    assert details.media_type == MediaType.RADIO
    assert details.stream_type == StreamType.HTTP
    assert details.audio_format.content_type == ContentType.MP3
    assert details.audio_format.bit_rate == 192
    assert details.allow_seek is False
    assert details.can_seek is False


async def test_stream_details_keep_the_case_of_the_stream_path(
    provider: SunshineLiveProvider,
) -> None:
    """Some channels have a capital letter on the stream server."""
    details = await provider.get_stream_details("melodic_beats", MediaType.RADIO)

    assert "/MelodicB/" in str(details.path)


async def test_stream_details_refuse_other_requests(provider: SunshineLiveProvider) -> None:
    """Only the known channels can play, and only as radio."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("unknown", MediaType.RADIO)
    with pytest.raises(UnplayableMediaError):
        await provider.get_stream_details("classics", MediaType.TRACK)
