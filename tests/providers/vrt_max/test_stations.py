"""Tests for VRT MAX live radio stations."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Radio

from music_assistant.providers.vrt_max import VrtMaxProvider
from music_assistant.providers.vrt_max.helpers import STATIONS, STATIONS_BY_ID


def test_stations_table_integrity() -> None:
    """STATIONS is non-empty, has unique ids, and every entry has a stream url."""
    assert STATIONS
    ids = [station.id for station in STATIONS]
    assert len(ids) == len(set(ids))
    for station in STATIONS:
        assert station.stream_url
        assert STATIONS_BY_ID[station.id] is station


def test_radio_item_with_logo_and_tagline(provider: VrtMaxProvider) -> None:
    """A station with a logo and tagline maps to a Radio item carrying both."""
    station = STATIONS_BY_ID["radio1"]
    assert station.logo_url
    assert station.tagline

    radio = provider._radio_item(station)

    assert isinstance(radio, Radio)
    assert radio.item_id == station.id
    assert radio.provider == provider.instance_id
    assert len(radio.provider_mappings) == 1
    mapping = next(iter(radio.provider_mappings))
    assert mapping.provider_domain == provider.domain
    assert mapping.provider_instance == provider.instance_id
    assert radio.metadata.images is not None
    assert len(radio.metadata.images) == 1
    assert radio.metadata.description == station.tagline


def test_radio_item_without_tagline(provider: VrtMaxProvider) -> None:
    """A station without a tagline leaves the description unset, but keeps its logo."""
    station = STATIONS_BY_ID["radio-bene"]
    assert station.tagline is None
    assert station.logo_url

    radio = provider._radio_item(station)

    assert radio.metadata.description is None
    assert radio.metadata.images is not None
    assert len(radio.metadata.images) == 1


async def test_get_radio_known(provider: VrtMaxProvider) -> None:
    """get_radio returns the matching station's Radio item."""
    station = STATIONS_BY_ID["radio1"]

    radio = await provider.get_radio(station.id)

    assert radio.item_id == station.id


async def test_get_radio_unknown(provider: VrtMaxProvider) -> None:
    """get_radio raises MediaNotFoundError for an unknown station id."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_radio("does-not-exist")


async def test_get_stream_details_known(provider: VrtMaxProvider) -> None:
    """get_stream_details resolves a live, unseekable HTTP stream for a known station."""
    station = STATIONS_BY_ID["radio1"]

    details = await provider.get_stream_details(station.id, MediaType.RADIO)

    assert details.stream_type == StreamType.HTTP
    assert details.path == station.stream_url
    assert details.can_seek is False


async def test_get_stream_details_unknown(provider: VrtMaxProvider) -> None:
    """get_stream_details raises MediaNotFoundError for an unknown station id."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("does-not-exist", MediaType.RADIO)
