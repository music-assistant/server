"""Additional catalogue links preserve parser identity and privacy boundaries."""

from unittest.mock import Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ProviderMapping

from music_assistant.controllers.music.sharing import public_provider_url
from music_assistant.controllers.music.sharing_catalogue import catalogue_url
from music_assistant.providers.music247e.parsers import parse_artist
from music_assistant.providers.musicme.provider import MusicMeProvider
from music_assistant.providers.phishin.helpers import show_to_album
from tests.providers.musicme.test_parsers import ALBUM_OBJ, ARTIST_OBJ, TRACK_OBJ


def mapping(domain: str, item_id: str, url: str | None = None) -> ProviderMapping:
    """Create a provider mapping without account metadata."""
    return ProviderMapping(
        provider_domain=domain, provider_instance="account", item_id=item_id, url=url
    )


def test_telmore_parser() -> None:
    """The inherited parser retains the Telmore public artist identity."""
    artist = parse_artist(
        Mock(domain="telmore", instance_id="account"), {"id": "53082396", "title": "APHACA"}
    )
    assert catalogue_url(artist.media_type, next(iter(artist.provider_mappings))) == (
        "https://musik.telmore.dk/kunstner/53082396"
    )


def test_musicme_parsers() -> None:
    """Existing API fixtures provide public artist and barcode-bound album URLs."""
    provider = Mock(domain="musicme", instance_id="account")
    provider._parse_artist.side_effect = lambda obj: MusicMeProvider._parse_artist(provider, obj)
    artist = MusicMeProvider._parse_artist(provider, ARTIST_OBJ)
    album = MusicMeProvider._parse_album(provider, ALBUM_OBJ)
    track = MusicMeProvider._parse_track(provider, TRACK_OBJ)
    for item, expected in (
        (artist, "https://www.musicme.com/Daft-Punk/"),
        (album, "https://www.musicme.com/Daft-Punk/albums/Veridis-Quo-5034644330297.html"),
        (track, None),
    ):
        assert catalogue_url(item.media_type, next(iter(item.provider_mappings))) == expected


def test_phishin_parser() -> None:
    """Show albums use the date rather than an audio URL or venue metadata."""
    album = show_to_album(
        Mock(domain="phishin", instance_id="account"),
        {"date": "1997-11-17", "venue": {"name": "McNichols Arena"}},
    )
    assert catalogue_url(album.media_type, next(iter(album.provider_mappings))) == (
        "https://phish.in/1997-11-17"
    )


@pytest.mark.parametrize(
    ("domain", "kind", "item_id", "url"),
    [
        ("telmore", MediaType.ARTIST, "53082396", None),
        ("musicme", MediaType.ARTIST, "6726", "https://www.musicme.com/Daft-Punk/"),
        ("phishin", MediaType.ALBUM, "1997-11-17", None),
    ],
)
def test_public_resolver_dispatch(
    domain: str, kind: MediaType, item_id: str, url: str | None
) -> None:
    """The main sharing policy dispatches audited catalogue providers."""
    provider_mapping = mapping(domain, item_id, url)
    assert public_provider_url(kind, provider_mapping) == catalogue_url(kind, provider_mapping)
    assert public_provider_url(kind, provider_mapping) is not None


@pytest.mark.parametrize("domain", ["telmore", "musicme"])
@pytest.mark.parametrize(
    "item_id", ["", "123\n", "\uff11\uff12\uff13", "../123", "123?token=x", "123/456"]
)
def test_invalid_numeric_identity(domain: str, item_id: str) -> None:
    """Only ASCII numeric catalogue identities are accepted."""
    assert (
        catalogue_url(
            MediaType.ARTIST, mapping(domain, item_id, "https://www.musicme.com/Daft-Punk/")
        )
        is None
    )


@pytest.mark.parametrize(
    "item_id",
    [
        "1997-11-31",
        "1997-02-29",
        "0000-01-01",
        "19971117",
        "1997-1-17",
        "1997-11-17\n",
        "\uff11\uff19\uff19\uff17-11-17",
        "1997-11-17/stream",
    ],
)
def test_invalid_show_date(item_id: str) -> None:
    """Show dates require both a strict route shape and a real calendar date."""
    assert catalogue_url(MediaType.ALBUM, mapping("phishin", item_id)) is None


@pytest.mark.parametrize(
    "url",
    [
        None,
        "https://www.musicme.com/Daft-Punk/?token=secret",
        "https://www.musicme.com/Daft-Punk/#secret",
        "https://www.musicme.com/Daft-Punk/\n",
        "http://www.musicme.com/Daft-Punk/",
        "https://www.musicme.com:443/Daft-Punk/",
        "https://secret@www.musicme.com/Daft-Punk/",
        "https://www.musicme.com.evil.test/Daft-Punk/",
        "https://localhost/Daft-Punk/",
        "https://www.musicme.com/../",
        "https://www.musicme.com/%44aft-Punk/",
        "https://www.musicme.com/Daft-Punk/audio.mp3",
    ],
)
def test_musicme_artist_url_boundary(url: str | None) -> None:
    """Artist URLs must already be clean public catalogue paths."""
    assert catalogue_url(MediaType.ARTIST, mapping("musicme", "6726", url)) is None


@pytest.mark.parametrize(
    "suffix",
    [
        "Veridis-Quo-999.html",
        "5034644330297.html",
        "Veridis-Quo-5034644330297.html?key=x",
        "Veridis-Quo-5034644330297.html#x",
        "Veridis-Quo-5034644330297.html/stream",
    ],
)
def test_musicme_album_identity(suffix: str) -> None:
    """Album routes must terminate in the exact mapped barcode."""
    assert (
        catalogue_url(
            MediaType.ALBUM,
            mapping(
                "musicme", "5034644330297", f"https://www.musicme.com/Daft-Punk/albums/{suffix}"
            ),
        )
        is None
    )


@pytest.mark.parametrize(
    "domain", ["telmore", "musicme", "phishin", "pandora", "kion_music", "yousee", "music247e"]
)
@pytest.mark.parametrize("kind", [MediaType.PLAYLIST, MediaType.TRACK, MediaType.RADIO])
def test_unsupported_and_private_items(domain: str, kind: MediaType) -> None:
    """Unverified types never pass through mapping URLs, including playlists and audio."""
    assert (
        catalogue_url(kind, mapping(domain, "123", "https://localhost/audio?token=secret")) is None
    )
