"""Test we can parse Yandex Music API objects into Music Assistant models."""

import json
import pathlib
from typing import Any, cast

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping
from yandex_music import Album as YandexAlbum
from yandex_music import Artist as YandexArtist
from yandex_music import Playlist as YandexPlaylist
from yandex_music import Track as YandexTrack

from music_assistant.providers.yandex_music.parsers import (
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
)
from music_assistant.providers.yandex_music.provider import YandexMusicProvider

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
ARTIST_FIXTURES = list(FIXTURES_DIR.glob("artists/*.json"))
ALBUM_FIXTURES = list(FIXTURES_DIR.glob("albums/*.json"))
TRACK_FIXTURES = list(FIXTURES_DIR.glob("tracks/*.json"))
PLAYLIST_FIXTURES = list(FIXTURES_DIR.glob("playlists/*.json"))

# Minimal client-like object for yandex_music de_json (library requires client, not None)
_DE_JSON_CLIENT = type("ClientStub", (), {"report_unknown_fields": False})()


class ProviderStub:
    """Minimal provider-like object for parser tests (no Mock)."""

    domain = "yandex_music"
    instance_id = "yandex_music_instance"

    def __init__(self) -> None:
        """Initialize stub with minimal client."""
        self.client = type("ClientStub", (), {"user_id": 12345})()

    def get_item_mapping(self, media_type: MediaType | str, key: str, name: str) -> ItemMapping:
        """Return ItemMapping for the given media type, key and name."""
        return ItemMapping(
            media_type=MediaType(media_type) if isinstance(media_type, str) else media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    """Load JSON fixture."""
    with open(path) as f:
        return cast("dict[str, Any]", json.load(f))


def _artist_from_fixture(path: pathlib.Path) -> YandexArtist | None:
    """Deserialize Yandex Artist from fixture JSON."""
    data = _load_json(path)
    return YandexArtist.de_json(data, _DE_JSON_CLIENT)


def _album_from_fixture(path: pathlib.Path) -> YandexAlbum | None:
    """Deserialize Yandex Album from fixture JSON."""
    data = _load_json(path)
    return YandexAlbum.de_json(data, _DE_JSON_CLIENT)


def _track_from_fixture(path: pathlib.Path) -> YandexTrack | None:
    """Deserialize Yandex Track from fixture JSON."""
    data = _load_json(path)
    return YandexTrack.de_json(data, _DE_JSON_CLIENT)


def _playlist_from_fixture(path: pathlib.Path) -> YandexPlaylist | None:
    """Deserialize Yandex Playlist from fixture JSON."""
    data = _load_json(path)
    return YandexPlaylist.de_json(data, _DE_JSON_CLIENT)


@pytest.fixture
def provider_stub() -> ProviderStub:
    """Return a real provider stub (no Mock)."""
    return ProviderStub()


@pytest.mark.parametrize("example", ARTIST_FIXTURES, ids=lambda val: val.stem)
def test_parse_artist(example: pathlib.Path, provider_stub: ProviderStub) -> None:
    """Test we can parse artists from fixture JSON."""
    artist_obj = _artist_from_fixture(example)
    assert artist_obj is not None
    result = parse_artist(cast("YandexMusicProvider", provider_stub), artist_obj)
    assert result.item_id == str(artist_obj.id)
    assert result.name == (artist_obj.name or "Unknown Artist")
    assert result.provider == provider_stub.instance_id
    assert len(result.provider_mappings) == 1
    mapping = next(iter(result.provider_mappings))
    assert f"music.yandex.ru/artist/{artist_obj.id}" in (mapping.url or "")


def test_parse_artist_with_cover(provider_stub: ProviderStub) -> None:
    """Test parsing artist with cover image."""
    path = FIXTURES_DIR / "artists" / "with_cover.json"
    artist_obj = _artist_from_fixture(path)
    assert artist_obj is not None
    result = parse_artist(cast("YandexMusicProvider", provider_stub), artist_obj)
    assert result.item_id == "200"
    assert result.name == "Artist With Cover"
    if artist_obj.cover and artist_obj.cover.uri:
        assert result.metadata.images is not None
        assert len(result.metadata.images) == 1
        assert "avatars.yandex.net" in (result.metadata.images[0].path or "")


@pytest.mark.parametrize("example", ALBUM_FIXTURES, ids=lambda val: val.stem)
def test_parse_album(example: pathlib.Path, provider_stub: ProviderStub) -> None:
    """Test we can parse albums from fixture JSON."""
    album_obj = _album_from_fixture(example)
    assert album_obj is not None
    result = parse_album(cast("YandexMusicProvider", provider_stub), album_obj)
    assert result.item_id == str(album_obj.id)
    assert result.name
    assert result.provider == provider_stub.instance_id
    mapping = next(iter(result.provider_mappings))
    assert f"music.yandex.ru/album/{album_obj.id}" in (mapping.url or "")
    if album_obj.year:
        assert result.year == album_obj.year


@pytest.mark.parametrize("example", TRACK_FIXTURES, ids=lambda val: val.stem)
def test_parse_track(example: pathlib.Path, provider_stub: ProviderStub) -> None:
    """Test we can parse tracks from fixture JSON."""
    track_obj = _track_from_fixture(example)
    assert track_obj is not None
    result = parse_track(cast("YandexMusicProvider", provider_stub), track_obj)
    assert result.item_id == str(track_obj.id)
    assert result.name
    assert result.duration == (track_obj.duration_ms or 0) // 1000
    mapping = next(iter(result.provider_mappings))
    assert f"music.yandex.ru/track/{track_obj.id}" in (mapping.url or "")


def test_parse_track_with_artist_and_album(provider_stub: ProviderStub) -> None:
    """Test parsing track with artist and album."""
    path = FIXTURES_DIR / "tracks" / "with_artist_and_album.json"
    track_obj = _track_from_fixture(path)
    assert track_obj is not None
    result = parse_track(cast("YandexMusicProvider", provider_stub), track_obj)
    assert result.item_id == "500"
    if track_obj.artists:
        assert len(result.artists) >= 1
        assert result.artists[0].name == "Track Artist"
    if track_obj.albums:
        assert result.album is not None
        assert result.album.item_id == "20"
        assert result.album.name == "Track Album"


@pytest.mark.parametrize("example", PLAYLIST_FIXTURES, ids=lambda val: val.stem)
def test_parse_playlist(example: pathlib.Path, provider_stub: ProviderStub) -> None:
    """Test we can parse playlists from fixture JSON."""
    playlist_obj = _playlist_from_fixture(example)
    assert playlist_obj is not None
    result = parse_playlist(cast("YandexMusicProvider", provider_stub), playlist_obj)
    owner_id = (
        str(playlist_obj.owner.uid) if playlist_obj.owner else str(provider_stub.client.user_id)
    )
    kind = str(playlist_obj.kind)
    assert result.item_id == f"{owner_id}:{kind}"
    assert result.name == (playlist_obj.title or "Unknown Playlist")
    mapping = next(iter(result.provider_mappings))
    assert f"music.yandex.ru/users/{owner_id}/playlists/{kind}" in (mapping.url or "")


def test_parse_playlist_editable(provider_stub: ProviderStub) -> None:
    """Test parsing own playlist (editable)."""
    path = FIXTURES_DIR / "playlists" / "minimal.json"
    playlist_obj = _playlist_from_fixture(path)
    assert playlist_obj is not None
    result = parse_playlist(cast("YandexMusicProvider", provider_stub), playlist_obj)
    assert result.owner == "Me"
    assert result.is_editable is True


def test_parse_playlist_other_user(provider_stub: ProviderStub) -> None:
    """Test parsing playlist owned by another user."""
    path = FIXTURES_DIR / "playlists" / "other_user.json"
    playlist_obj = _playlist_from_fixture(path)
    assert playlist_obj is not None
    result = parse_playlist(cast("YandexMusicProvider", provider_stub), playlist_obj)
    assert result.item_id == "99999:1"
    assert result.name == "Shared Playlist"
    assert result.owner == "Other User"
    assert result.is_editable is False
    assert result.metadata.description == "A shared playlist"
