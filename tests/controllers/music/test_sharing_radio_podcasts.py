"""Audited radio and podcast destinations from real provider parsers."""

import json
from unittest.mock import Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ProviderMapping

from music_assistant.controllers.music.sharing import public_provider_url
from music_assistant.providers.iheartradio.parsers import parse_live_station
from music_assistant.providers.itunes_podcasts import ITunesPodcastsProvider
from music_assistant.providers.itunes_podcasts.schema import PodcastSearchResult
from music_assistant.providers.nts import NTSProvider
from music_assistant.providers.podcast_index.helpers import (
    parse_episode_from_data,
    parse_podcast_from_feed,
)
from music_assistant.providers.somafm import SomaFMProvider

FEED = "https://user:password@private.example/feed?token=secret"
ENCLOSURE = "https://private.example/audio.mp3?token=secret"


def mapping(domain: str, item_id: str, url: str | None = None) -> ProviderMapping:
    """Create an account mapping with potentially sensitive website metadata."""
    return ProviderMapping(
        provider_domain=domain, provider_instance="account", item_id=item_id, url=url
    )


@pytest.mark.parametrize("channel", ["1", "2"])
def test_nts_parser(channel: str) -> None:
    """The live channel builder supplies the exact supported identity."""
    provider = Mock(instance_id="account", domain="nts")
    provider._build_radio.side_effect = lambda **kwargs: NTSProvider._build_radio(
        provider, **kwargs
    )
    radio = NTSProvider._build_channel_radio(provider, channel, None)
    assert public_provider_url(radio.media_type, next(iter(radio.provider_mappings))) == (
        f"https://www.nts.live/{channel}"
    )


def test_somafm_parser() -> None:
    """Channel codes produce website pages without retaining stream metadata."""
    radio = SomaFMProvider._parse_channel(
        Mock(instance_id="account", domain="somafm"),
        {"id": "groovesalad", "title": "Groove Salad", "playlists": [{"url": ENCLOSURE}]},
    )
    assert public_provider_url(radio.media_type, next(iter(radio.provider_mappings))) == (
        "https://somafm.com/groovesalad/"
    )


def test_iheart_parser() -> None:
    """Only a station website with the matching numeric identity is shared."""
    radio = parse_live_station(
        {"id": 193, "name": "KOST", "website": "https://www.iheart.com/live/kost-1035-193/"},
        "account",
        "iheartradio",
    )
    assert radio is not None
    assert public_provider_url(radio.media_type, next(iter(radio.provider_mappings))) == (
        "https://www.iheart.com/live/kost-1035-193/"
    )


def test_itunes_parser() -> None:
    """The search converter retains the directory ID separately from its private feed."""
    provider = Mock(instance_id="account", domain="itunes_podcasts", _search_result_details={})
    podcast = ITunesPodcastsProvider._get_podcast_list(
        provider,
        [PodcastSearchResult(collection_id=1200361736, feed_url=FEED, track_name="Example")],
    )[0]
    provider_mapping = next(iter(podcast.provider_mappings))
    before = provider_mapping.to_dict()
    assert provider_mapping.item_id == FEED
    assert public_provider_url(podcast.media_type, provider_mapping) == (
        "https://podcasts.apple.com/podcast/id1200361736"
    )
    assert provider_mapping.to_dict() == before


def test_podcast_index_parsers() -> None:
    """Directory podcasts share numeric pages; episode enclosures remain private."""
    podcast = parse_podcast_from_feed(
        {"id": 920666, "url": FEED, "title": "Example"}, "account", "podcast_index"
    )
    assert podcast is not None
    assert public_provider_url(podcast.media_type, next(iter(podcast.provider_mappings))) == (
        "https://podcastindex.org/podcast/920666"
    )
    episode = parse_episode_from_data(
        {"id": 123, "enclosureUrl": ENCLOSURE}, "920666", "account", "podcast_index"
    )
    assert episode is not None
    assert public_provider_url(episode.media_type, next(iter(episode.provider_mappings))) is None


@pytest.mark.parametrize("domain", ["nts", "somafm", "iheartradio", "podcast_index"])
@pytest.mark.parametrize(
    "item_id",
    [
        "",
        "-1",
        "../123",
        "123/stream",
        "123?token=x",
        "123\n",
        "\uff11\uff12\uff13",
        FEED,
        "123%2f4",
    ],
)
def test_invalid_ids(domain: str, item_id: str) -> None:
    """Reject malformed or address-bearing identities."""
    kind = MediaType.PODCAST if domain == "podcast_index" else MediaType.RADIO
    assert public_provider_url(kind, mapping(domain, item_id, ENCLOSURE)) is None


@pytest.mark.parametrize(
    "url",
    [
        None,
        ENCLOSURE,
        "http://www.iheart.com/live/kost-193/",
        "https://iheart.com/live/kost-193/",
        "https://www.iheart.com.evil.example/live/kost-193/",
        "https://user:password@www.iheart.com/live/kost-193/",
        "https://www.iheart.com:443/live/kost-193/",
        "https://www.iheart.com/live/kost-194/",
        "https://www.iheart.com/live/../kost-193/",
        "https://www.iheart.com/live/%2e%2e-193/",
        "https://www.iheart.com/live/kost-193/stream",
        "https://www.iheart.com/live/kost-193/\n",
        "https://www.iheart.com/live/kost-193/?token=\tsecret",
        "https://www.iheart.com\\@evil.example/live/kost-193/",
    ],
)
def test_iheart_invalid_urls(url: str | None) -> None:
    """Reject credentials, other hosts, transport URLs, and ambiguous paths."""
    assert public_provider_url(MediaType.RADIO, mapping("iheartradio", "193", url)) is None


def test_iheart_clean_url() -> None:
    """Strip query and fragment metadata from an otherwise verified station page."""
    assert (
        public_provider_url(
            MediaType.RADIO,
            mapping(
                "iheartradio", "193", "https://www.iheart.com/live/kost-193?token=secret#private"
            ),
        )
        == "https://www.iheart.com/live/kost-193/"
    )


@pytest.mark.parametrize(
    "details",
    [None, "", "{", "[]", "null", "true", "1", '{"itunes_id":NaN}']
    + [json.dumps({"itunes_id": value}) for value in [None, True, False, 0, -1, 1.5, "123", FEED]],
)
def test_itunes_invalid_details(details: str | None) -> None:
    """Malformed metadata never falls back to a private feed URL."""
    provider_mapping = mapping("itunes_podcasts", FEED, ENCLOSURE)
    provider_mapping.details = details
    assert public_provider_url(MediaType.PODCAST, provider_mapping) is None


@pytest.mark.parametrize(
    "domain", ["nts", "somafm", "iheartradio", "itunes_podcasts", "podcast_index"]
)
@pytest.mark.parametrize("kind", [MediaType.PLAYLIST, MediaType.PODCAST_EPISODE, MediaType.TRACK])
def test_unverified_types(domain: str, kind: MediaType) -> None:
    """No generic URL fallback exposes feeds or audio for unsupported types."""
    assert public_provider_url(kind, mapping(domain, "193", ENCLOSURE)) is None


def test_nts_mixtape() -> None:
    """Mixtape identities do not establish an audited website route."""
    assert (
        public_provider_url(MediaType.RADIO, mapping("nts", "nts_mixtape_slow-focus", ENCLOSURE))
        is None
    )


@pytest.mark.parametrize("item_id", ["0", "00", "01"])
def test_nonpositive_or_noncanonical_directory_ids(item_id: str) -> None:
    """Directory and station identities require canonical positive decimal IDs."""
    assert public_provider_url(MediaType.PODCAST, mapping("podcast_index", item_id)) is None
    assert (
        public_provider_url(
            MediaType.RADIO,
            mapping("iheartradio", item_id, f"https://www.iheart.com/live/kost-{item_id}/"),
        )
        is None
    )


def test_iheart_stream_parser() -> None:
    """A parsed station with an audio website cannot expose its stream URL."""
    radio = parse_live_station({"id": 193, "website": ENCLOSURE}, "account", "iheartradio")
    assert radio is not None
    assert public_provider_url(radio.media_type, next(iter(radio.provider_mappings))) is None
