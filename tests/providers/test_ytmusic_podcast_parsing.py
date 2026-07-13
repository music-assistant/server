"""Tests for parsing YTM podcasts from browse/search results."""

from unittest.mock import MagicMock

from music_assistant_models.media_items import Podcast

from music_assistant.providers.ytmusic import YoutubeMusicProvider


def _make_provider() -> MagicMock:
    """Create a mock YoutubeMusicProvider with the fields used by the parsers."""
    prov = MagicMock()
    prov.instance_id = "ytmusic_test"
    prov.domain = "ytmusic"
    prov._parse_thumbnails = MagicMock(return_value=[])
    prov._parse_podcast = lambda obj: YoutubeMusicProvider._parse_podcast(prov, obj)
    return prov


def test_browse_podcast_with_mpsp_browse_id_is_parsed() -> None:
    """A home/search item with an MPSP browseId must parse into a Podcast."""
    prov = _make_provider()
    item = {
        "title": "Couple Of",
        "browseId": "MPSPPLtest123",
        "podcastId": None,
        "channel": {"id": "UC123", "name": "Some Channel"},
    }

    podcast = YoutubeMusicProvider._parse_browse_podcast(prov, item)

    assert isinstance(podcast, Podcast)
    assert podcast.item_id == "PLtest123"
    assert podcast.name == "Couple Of"
    assert podcast.publisher == "Some Channel"


def test_browse_podcast_prefers_explicit_podcast_id() -> None:
    """The item's podcastId must be preferred over the derived browseId."""
    prov = _make_provider()
    item = {
        "title": "Couple Of",
        "browseId": "MPSPPLtest123",
        "podcastId": "PLexplicit456",
    }

    podcast = YoutubeMusicProvider._parse_browse_podcast(prov, item)

    assert isinstance(podcast, Podcast)
    assert podcast.item_id == "PLexplicit456"


def test_browse_album_is_not_parsed_as_podcast() -> None:
    """An album item (MPRE browseId) must not be parsed as a podcast."""
    prov = _make_provider()
    item = {"title": "Some Album", "browseId": "MPREb_test123"}

    assert YoutubeMusicProvider._parse_browse_podcast(prov, item) is None


def test_browse_item_without_browse_id_is_not_parsed_as_podcast() -> None:
    """An item without browseId must not be parsed as a podcast."""
    prov = _make_provider()

    assert YoutubeMusicProvider._parse_browse_podcast(prov, {"title": "X"}) is None
