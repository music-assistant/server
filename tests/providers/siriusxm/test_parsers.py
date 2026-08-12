"""Test parsing SiriusXM models into Music Assistant models."""

from __future__ import annotations

from aiosxm import NowPlaying
from music_assistant_models.enums import ImageType

from music_assistant.providers.siriusxm.parsers import parse_radio, parse_stream_metadata

from .conftest import make_channel

INSTANCE_ID = "siriusxm--test123"
DOMAIN = "siriusxm"


def test_parse_radio() -> None:
    """A channel becomes a Radio item with artwork and metadata."""
    radio = parse_radio(make_channel(), INSTANCE_ID, DOMAIN)

    assert radio.item_id == make_channel().id
    assert radio.name == "SiriusXM Hits 1"
    assert radio.provider == INSTANCE_ID
    assert radio.metadata.description == "Today's hits"
    assert radio.metadata.genres == {"Pop"}

    mapping = next(iter(radio.provider_mappings))
    assert mapping.provider_domain == DOMAIN
    assert mapping.provider_instance == INSTANCE_ID
    assert mapping.available is True

    image_types = {image.type for image in radio.metadata.images or []}
    assert image_types == {
        ImageType.THUMB,
        ImageType.LOGO,
        ImageType.BANNER,
        ImageType.LANDSCAPE,
    }
    # SiriusXM's image service is https-only, so no upgrade step is needed.
    assert all((image.path or "").startswith("https://") for image in radio.metadata.images or [])


def test_parse_radio_unavailable_when_off_air() -> None:
    """An off-air or unentitled channel is mapped as unavailable."""
    off_air = parse_radio(make_channel(off_air=True), INSTANCE_ID, DOMAIN)
    assert next(iter(off_air.provider_mappings)).available is False

    unentitled = parse_radio(make_channel(unentitled=True), INSTANCE_ID, DOMAIN)
    assert next(iter(unentitled.provider_mappings)).available is False


def test_parse_stream_metadata() -> None:
    """Now-playing data becomes StreamMetadata with the show as the album."""
    metadata = parse_stream_metadata(
        NowPlaying(
            channel_id="1",
            title="Le Freak",
            artist="Chic",
            show="70s Hits",
            image_key="art-key",
        )
    )

    assert metadata is not None
    assert metadata.title == "Le Freak"
    assert metadata.artist == "Chic"
    assert metadata.album == "70s Hits"
    assert metadata.image_url is not None


def test_parse_stream_metadata_falls_back_to_channel_art() -> None:
    """A track without artwork falls back to the channel logo."""
    metadata = parse_stream_metadata(
        NowPlaying(channel_id="1", title="Le Freak", artist="Chic"),
        fallback_image="https://example.com/logo.png",
    )

    assert metadata is not None
    assert metadata.image_url == "https://example.com/logo.png"


def test_parse_stream_metadata_skips_ads() -> None:
    """Ad breaks produce no metadata, so the station name stays on screen."""
    assert (
        parse_stream_metadata(
            NowPlaying(channel_id="1", title="Some Advert", artist="Brand", is_ad=True)
        )
        is None
    )


def test_parse_stream_metadata_requires_title() -> None:
    """An entry without a title yields nothing."""
    assert parse_stream_metadata(NowPlaying(channel_id="1")) is None
