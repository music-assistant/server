"""Public share links exercised through provider parsers and hostile mapping inputs."""

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Podcast
from niconico.objects.video import Owner

from music_assistant.controllers.music.sharing import public_provider_url
from music_assistant.providers.internet_archive.parsers import doc_to_album
from music_assistant.providers.internet_archive.provider import InternetArchiveProvider
from music_assistant.providers.neteasecloudmusic import NeteaseCloudMusicProvider
from music_assistant.providers.qqmusic.parsers import parse_artist
from music_assistant.providers.spotify import parsers as spotify
from music_assistant.providers.yandex_music.parsers import parse_artist as parse_yandex_artist
from music_assistant.providers.ytmusic import YoutubeMusicProvider
from music_assistant.providers.zvuk_music.parsers import parse_artist as parse_zvuk_artist
from tests.controllers.music.test_sharing import SPOTIFY_ID, mapping
from tests.providers.nicovideo.helpers import create_converter_manager
from tests.providers.nicovideo.test_activity_converter import _build_video_activity
from tests.providers.yandex_music.test_parsers import _artist_from_fixture
from tests.providers.zvuk_music.test_parsers import _create_mock_artist


@pytest.mark.parametrize(
    ("parser", "route"),
    [
        (spotify.parse_podcast, "show"),
        (spotify.parse_podcast_episode, "episode"),
        (spotify.parse_audiobook, "audiobook"),
    ],
)
def test_spotify_parser(parser: Callable[..., Any], route: str) -> None:
    """Spotify parser mappings use distinct spoken-audio website routes."""
    provider = Mock(instance_id="account", domain="spotify")
    item = parser(
        {
            "id": SPOTIFY_ID,
            "name": "Example",
            "external_urls": {"spotify": f"https://open.spotify.com/{route}/{SPOTIFY_ID}"},
        },
        provider,
    )
    assert public_provider_url(item.media_type, next(iter(item.provider_mappings))) == (
        f"https://open.spotify.com/{route}/{SPOTIFY_ID}"
    )


def test_youtube_episode_parser() -> None:
    """A composite episode identifier and a watch URL cannot establish public visibility."""
    provider = Mock(instance_id="account", domain="ytmusic")
    podcast = Podcast(
        item_id="fixture-podcast", provider="account", name="Example", provider_mappings=set()
    )
    episode = YoutubeMusicProvider._parse_podcast_episode(
        provider, {"videoId": "meuR1l4j-xs", "title": "Episode"}, podcast
    )
    assert public_provider_url(episode.media_type, next(iter(episode.provider_mappings))) is None


def test_nicovideo_converter() -> None:
    """The actual activity converter supplies the video catalog identity."""
    track = create_converter_manager().track.convert_by_activity(_build_video_activity())
    assert track is not None
    assert public_provider_url(track.media_type, next(iter(track.provider_mappings))) == (
        "https://www.nicovideo.jp/watch/sm123"
    )


def test_archive_parser() -> None:
    """Archive album identity produces a details page independently of mapping URLs."""
    album = doc_to_album(
        {"identifier": "fixture-identifier", "title": "Example"},
        "internet_archive",
        "account",
        lambda _: "https://private.local/secret",
    )
    assert album is not None
    assert public_provider_url(album.media_type, next(iter(album.provider_mappings))) == (
        "https://archive.org/details/fixture-identifier"
    )


def test_qqmusic_parser() -> None:
    """QQMusic uses SDK mids rather than numeric API identifiers."""
    artist = parse_artist({"mid": "003Nz2So3XXYek", "name": "Example"}, "qqmusic", "account")
    assert public_provider_url(artist.media_type, next(iter(artist.provider_mappings))) == (
        "https://y.qq.com/n/ryqq/singer/003Nz2So3XXYek"
    )


@pytest.mark.parametrize(
    "domain",
    ["nicovideo", "neteasecloudmusic", "qqmusic", "yandex_music", "zvuk_music", "internet_archive"],
)
@pytest.mark.parametrize(
    "item_id",
    [
        "",
        "../123",
        "123/stream",
        "123?key=secret",
        "123\n",
        "123#secret",
        "https://localhost/audio",
        "\uff11\uff12\uff13",
        "123%2f456",
    ],
)
def test_hostile_identifiers(domain: str, item_id: str) -> None:
    """Catalog identity cannot inject addresses, credentials or URL syntax."""
    if domain == "internet_archive" and item_id == "123#secret":
        assert public_provider_url(MediaType.TRACK, mapping(domain, item_id)) == (
            "https://archive.org/details/123"
        )
    else:
        assert public_provider_url(MediaType.TRACK, mapping(domain, item_id)) is None


@pytest.mark.parametrize(
    ("domain", "kind", "item_id", "expected"),
    [
        ("neteasecloudmusic", MediaType.TRACK, "123", "https://music.163.com/#/song?id=123"),
        ("yandex_music", MediaType.PODCAST, "123", "https://music.yandex.ru/album/123"),
        ("yandex_music", MediaType.PODCAST_EPISODE, "123", "https://music.yandex.ru/track/123"),
        ("yandex_music", MediaType.AUDIOBOOK, "123", "https://music.yandex.ru/album/123"),
        ("zvuk_music", MediaType.ALBUM, "123", "https://zvuk.com/release/123"),
        ("nicovideo", MediaType.ALBUM, "123", "https://www.nicovideo.jp/series/123"),
        (
            "internet_archive",
            MediaType.TRACK,
            "item#01 Song.mp3",
            "https://archive.org/details/item",
        ),
    ],
)
def test_catalog_routes(domain: str, kind: MediaType, item_id: str, expected: str) -> None:
    """Construct verified pages without passing through mapping addresses."""
    assert (
        public_provider_url(kind, mapping(domain, item_id, "https://localhost/secret")) == expected
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/user/123",
        "https://www.nicovideo.jp/user/456",
        "https://www.nicovideo.jp:443/user/123",
        "https://secret@www.nicovideo.jp/user/123",
        "https://www.nicovideo.jp/user/123\n",
        "https://www.nicovideo.jp/user/123?token=secret",
        "https://www.nicovideo.jp/user/123#secret",
    ],
)
def test_nicovideo_artist_identity(url: str) -> None:
    """Artist routes require both the audited host and exact mapped identity."""
    assert public_provider_url(MediaType.ARTIST, mapping("nicovideo", "123", url)) is None


@pytest.mark.parametrize(
    "item_id",
    [
        "a#",
        "a#../secret.mp3",
        "a#x/y.mp3",
        "a#x?secret",
        "a#x#y",
        "a#x%2fsecret",
        "a#x\ny.mp3",
        "a" * 101,
    ],
)
def test_archive_file_boundary(item_id: str) -> None:
    """File identities must have one safe filename boundary."""
    assert public_provider_url(MediaType.TRACK, mapping("internet_archive", item_id)) is None


@pytest.mark.parametrize(
    "domain",
    [
        "spotify",
        "ytmusic",
        "nicovideo",
        "neteasecloudmusic",
        "qqmusic",
        "yandex_music",
        "zvuk_music",
        "internet_archive",
    ],
)
def test_playlists_remain_text(domain: str) -> None:
    """Provider parsers do not establish playlist public visibility."""
    assert public_provider_url(MediaType.PLAYLIST, mapping(domain, "123")) is None


def test_netease_parser() -> None:
    """NetEase numeric parser identity maps to the official song catalog."""
    provider = Mock(instance_id="account", domain="neteasecloudmusic")
    artist = NeteaseCloudMusicProvider._parse_artist(provider, {"id": 123, "name": "Example"})
    assert public_provider_url(artist.media_type, next(iter(artist.provider_mappings))) == (
        "https://music.163.com/#/artist?id=123"
    )


def test_zvuk_parser() -> None:
    """Zvuk artist SDK identity generates a public catalog page."""
    artist = parse_zvuk_artist(
        Mock(instance_id="account", domain="zvuk_music"), _create_mock_artist()
    )
    assert public_provider_url(artist.media_type, next(iter(artist.provider_mappings))) == (
        "https://zvuk.com/artist/123"
    )


def test_yandex_parser() -> None:
    """Deserialize an existing Yandex SDK fixture before resolving its identity."""
    obj = _artist_from_fixture(Path("tests/providers/yandex_music/fixtures/artists/minimal.json"))
    assert obj is not None
    artist = parse_yandex_artist(Mock(instance_id="account", domain="yandex_music"), obj)
    assert public_provider_url(artist.media_type, next(iter(artist.provider_mappings))) == (
        f"https://music.yandex.ru/artist/{obj.id}"
    )


@pytest.mark.parametrize("route", ["user", "channel"])
def test_nicovideo_artist_converter(route: str) -> None:
    """The converter disambiguates the same artist ID by owner type."""
    owner = Owner.model_validate(
        {
            "id": "123",
            "name": "Example",
            "iconUrl": None,
            "ownerType": route,
            "type": route,
            "visibility": "visible",
        }
    )
    artist = create_converter_manager().artist.convert_by_owner_or_user(owner)
    assert public_provider_url(artist.media_type, next(iter(artist.provider_mappings))) == (
        f"https://www.nicovideo.jp/{route}/123"
    )


@pytest.mark.parametrize(
    "item_id",
    [
        "123",
        "123|abc",
        "123|meuR1l4j-xs|other",
        "../123|meuR1l4j-xs",
        "123|meuR1l4j-xs?token=secret",
    ],
)
def test_youtube_episode_invalid_composites(item_id: str) -> None:
    """Episode IDs contain exactly one safe podcast and video boundary."""
    assert (
        public_provider_url(
            MediaType.PODCAST_EPISODE, mapping("ytmusic", item_id), publicly_visible=True
        )
        is None
    )


def test_archive_artist_and_kion_fallback() -> None:
    """Creator names and the distinct KION catalog do not establish public identity."""
    assert public_provider_url(MediaType.ARTIST, mapping("internet_archive", "Example")) is None
    assert public_provider_url(MediaType.TRACK, mapping("kion_music", "123")) is None


async def test_archive_file_parsers() -> None:
    """Track and episode parsers retain download URLs that must never be shared."""
    provider = Mock(instance_id="account", domain="internet_archive")
    provider._get_metadata = AsyncMock(return_value={"metadata": {"title": "Example"}})
    provider._get_audio_files = AsyncMock(return_value=[{"name": "01 Song.mp3"}])
    provider._extract_track_number.return_value = 1
    provider.client.get_download_url.return_value = (
        "https://archive.org/download/fixture-identifier/01%20Song.mp3?token=secret"
    )
    provider.client.get_item_url.return_value = "https://archive.org/details/fixture-identifier"
    tracks = await InternetArchiveProvider.get_album_tracks(provider, "fixture-identifier")
    episodes = [
        episode
        async for episode in InternetArchiveProvider.get_podcast_episodes(
            provider, "fixture-identifier"
        )
    ]
    for item in [*tracks, *episodes]:
        provider_mapping = next(iter(item.provider_mappings))
        assert provider_mapping.item_id == "fixture-identifier#01 Song.mp3"
        assert provider_mapping.url
        assert "/download/" in provider_mapping.url
        assert public_provider_url(item.media_type, provider_mapping) == (
            "https://archive.org/details/fixture-identifier"
        )


@pytest.mark.parametrize("kind", [MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK])
@pytest.mark.parametrize("domain", ["neteasecloudmusic", "qqmusic", "yandex_music", "zvuk_music"])
def test_regional_catalog_types(domain: str, kind: MediaType) -> None:
    """All three catalog types use provider-specific public route names."""
    routes = {
        "neteasecloudmusic": ("https://music.163.com/#/", ["artist", "album", "song"], "?id="),
        "qqmusic": ("https://y.qq.com/n/ryqq/", ["singer", "albumDetail", "songDetail"], "/"),
        "yandex_music": ("https://music.yandex.ru/", ["artist", "album", "track"], "/"),
        "zvuk_music": ("https://zvuk.com/", ["artist", "release", "track"], "/"),
    }
    host, kinds, separator = routes[domain]
    index = [MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK].index(kind)
    item_id = "003Nz2So3XXYek" if domain == "qqmusic" else "123"
    assert public_provider_url(kind, mapping(domain, item_id)) == (
        f"{host}{kinds[index]}{separator}{item_id}"
    )


@pytest.mark.parametrize(
    "kind", [MediaType.PODCAST, MediaType.PODCAST_EPISODE, MediaType.AUDIOBOOK]
)
def test_spotify_spoken_audio_without_url(kind: MediaType) -> None:
    """Spoken audio routes work without requiring website metadata."""
    assert public_provider_url(kind, mapping("spotify", SPOTIFY_ID)) is not None


def test_qqmusic_numeric_id_fallback() -> None:
    """A numeric API ID does not establish a QQMusic website MID."""
    assert public_provider_url(MediaType.TRACK, mapping("qqmusic", "123")) is None
