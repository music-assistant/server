"""Test we can parse Yandex Music API objects into Music Assistant models."""

from __future__ import annotations

import json
import pathlib
from typing import TYPE_CHECKING, Any, cast

import pytest
from yandex_music import Album as YandexAlbum
from yandex_music import Artist as YandexArtist
from yandex_music import Playlist as YandexPlaylist
from yandex_music import Track as YandexTrack

from music_assistant.providers.yandex_music.parsers import (
    classify_album,
    parse_album,
    parse_artist,
    parse_audiobook,
    parse_playlist,
    parse_podcast,
    parse_podcast_episode,
    parse_track,
)
from music_assistant.providers.yandex_music.provider import YandexMusicProvider

from .conftest import DE_JSON_CLIENT

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

    from .conftest import ProviderStub

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
ARTIST_FIXTURES = list(FIXTURES_DIR.glob("artists/*.json"))
ALBUM_FIXTURES = list(FIXTURES_DIR.glob("albums/*.json"))
TRACK_FIXTURES = list(FIXTURES_DIR.glob("tracks/*.json"))
PLAYLIST_FIXTURES = list(FIXTURES_DIR.glob("playlists/*.json"))
PODCAST_FIXTURES = list(FIXTURES_DIR.glob("podcasts/*.json"))
AUDIOBOOK_FIXTURES = list(FIXTURES_DIR.glob("audiobooks/*.json"))


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    """Load JSON fixture."""
    with open(path) as f:
        return cast("dict[str, Any]", json.load(f))


def _artist_from_fixture(path: pathlib.Path) -> YandexArtist | None:
    """Deserialize Yandex Artist from fixture JSON."""
    data = _load_json(path)
    return YandexArtist.de_json(data, DE_JSON_CLIENT)


def _album_from_fixture(path: pathlib.Path) -> YandexAlbum | None:
    """Deserialize Yandex Album from fixture JSON."""
    data = _load_json(path)
    return YandexAlbum.de_json(data, DE_JSON_CLIENT)


def _track_from_fixture(path: pathlib.Path) -> YandexTrack | None:
    """Deserialize Yandex Track from fixture JSON."""
    data = _load_json(path)
    return YandexTrack.de_json(data, DE_JSON_CLIENT)


def _playlist_from_fixture(path: pathlib.Path) -> YandexPlaylist | None:
    """Deserialize Yandex Playlist from fixture JSON."""
    data = _load_json(path)
    return YandexPlaylist.de_json(data, DE_JSON_CLIENT)


# provider_stub fixture is provided by conftest.py


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


def test_parse_artist_with_about(provider_stub: ProviderStub) -> None:
    """parse_artist enriches description and popularity from ArtistAbout."""
    artist_obj = _artist_from_fixture(FIXTURES_DIR / "artists" / "with_cover.json")
    assert artist_obj is not None

    about = type(
        "ArtistAbout",
        (),
        {
            "description": "Singer-songwriter from somewhere.",
            "stats": type("Stats", (), {"last_month_listeners": 250_000})(),
        },
    )()

    result = parse_artist(cast("YandexMusicProvider", provider_stub), artist_obj, about=about)
    assert result.metadata.description == "Singer-songwriter from somewhere."
    # 250000 // 10000 == 25
    assert result.metadata.popularity == 25


def test_parse_artist_about_missing_fields(provider_stub: ProviderStub) -> None:
    """parse_artist tolerates ArtistAbout with missing description/stats."""
    artist_obj = _artist_from_fixture(FIXTURES_DIR / "artists" / "with_cover.json")
    assert artist_obj is not None

    about = type("ArtistAbout", (), {"description": None, "stats": None})()

    result = parse_artist(cast("YandexMusicProvider", provider_stub), artist_obj, about=about)
    assert result.metadata.description is None
    assert result.metadata.popularity is None


def test_parse_artist_about_clamps_popularity(provider_stub: ProviderStub) -> None:
    """parse_artist caps very large monthly listeners at popularity 100."""
    artist_obj = _artist_from_fixture(FIXTURES_DIR / "artists" / "with_cover.json")
    assert artist_obj is not None

    about = type(
        "ArtistAbout",
        (),
        {
            "description": "",
            "stats": type("Stats", (), {"last_month_listeners": 50_000_000})(),
        },
    )()

    result = parse_artist(cast("YandexMusicProvider", provider_stub), artist_obj, about=about)
    assert result.metadata.popularity == 100


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


# --- Snapshot tests ---


def _sort_for_snapshot(parsed: dict[str, Any]) -> dict[str, Any]:
    """Sort lists in parsed dict for deterministic snapshot comparison."""
    if parsed.get("external_ids"):
        parsed["external_ids"] = sorted(parsed["external_ids"])
    if "metadata" in parsed and isinstance(parsed["metadata"], dict):
        if parsed["metadata"].get("genres"):
            parsed["metadata"]["genres"] = sorted(parsed["metadata"]["genres"])
    return parsed


@pytest.mark.parametrize("example", ARTIST_FIXTURES, ids=lambda val: val.stem)
def test_parse_artist_snapshot(
    example: pathlib.Path,
    provider_stub: ProviderStub,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot test for artist parsing."""
    artist_obj = _artist_from_fixture(example)
    assert artist_obj is not None
    result = parse_artist(cast("YandexMusicProvider", provider_stub), artist_obj)
    parsed = _sort_for_snapshot(result.to_dict())
    assert snapshot == parsed


@pytest.mark.parametrize("example", ALBUM_FIXTURES, ids=lambda val: val.stem)
def test_parse_album_snapshot(
    example: pathlib.Path,
    provider_stub: ProviderStub,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot test for album parsing."""
    album_obj = _album_from_fixture(example)
    assert album_obj is not None
    result = parse_album(cast("YandexMusicProvider", provider_stub), album_obj)
    parsed = _sort_for_snapshot(result.to_dict())
    assert snapshot == parsed


@pytest.mark.parametrize("example", TRACK_FIXTURES, ids=lambda val: val.stem)
def test_parse_track_snapshot(
    example: pathlib.Path,
    provider_stub: ProviderStub,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot test for track parsing."""
    track_obj = _track_from_fixture(example)
    assert track_obj is not None
    result = parse_track(cast("YandexMusicProvider", provider_stub), track_obj)
    parsed = _sort_for_snapshot(result.to_dict())
    assert snapshot == parsed


@pytest.mark.parametrize("example", PLAYLIST_FIXTURES, ids=lambda val: val.stem)
def test_parse_playlist_snapshot(
    example: pathlib.Path,
    provider_stub: ProviderStub,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot test for playlist parsing."""
    playlist_obj = _playlist_from_fixture(example)
    assert playlist_obj is not None
    result = parse_playlist(cast("YandexMusicProvider", provider_stub), playlist_obj)
    parsed = _sort_for_snapshot(result.to_dict())
    assert snapshot == parsed


# --- classify_album ---


@pytest.mark.parametrize(
    ("meta_type", "type_", "expected"),
    [
        ("podcast", None, "podcast"),
        (None, "podcast", "podcast"),
        ("Podcast", None, "podcast"),
        ("podcast_episode", None, "podcast"),
        ("audiobook", None, "audiobook"),
        (None, "audiobook", "audiobook"),
        ("AUDIOBOOK", None, "audiobook"),
        # audiobook wins over podcast on any field — empirically observed:
        # Yandex tags audiobooks as meta_type="podcast" + type="audiobook"
        ("podcast", "audiobook", "audiobook"),
        ("audiobook", "podcast", "audiobook"),
        ("audiobook", "music", "audiobook"),
        # plain music
        (None, None, "music"),
        ("music", "album", "music"),
        ("", "", "music"),
    ],
)
def test_classify_album(
    meta_type: str | None,
    type_: str | None,
    expected: str,
) -> None:
    """classify_album maps meta_type/type variants to music/podcast/audiobook."""
    album_obj = YandexAlbum.de_json(
        {"id": 1, "title": "x", "meta_type": meta_type, "type": type_},
        DE_JSON_CLIENT,
    )
    assert album_obj is not None
    assert classify_album(album_obj) == expected


# --- Podcast / Audiobook / PodcastEpisode parsers ---


@pytest.mark.parametrize("example", PODCAST_FIXTURES, ids=lambda val: val.stem)
def test_parse_podcast(example: pathlib.Path, provider_stub: ProviderStub) -> None:
    """parse_podcast extracts basic fields from a podcast-typed album fixture."""
    album_obj = _album_from_fixture(example)
    assert album_obj is not None
    result = parse_podcast(cast("YandexMusicProvider", provider_stub), album_obj)
    assert result.item_id == str(album_obj.id)
    assert result.name
    assert result.provider == provider_stub.instance_id
    mapping = next(iter(result.provider_mappings))
    assert f"music.yandex.ru/album/{album_obj.id}" in (mapping.url or "")
    # publisher resolves from labels[0].name when present
    if album_obj.labels:
        first = album_obj.labels[0]
        label_name = first if isinstance(first, str) else getattr(first, "name", None)
        if label_name:
            assert result.publisher == label_name
    if album_obj.track_count is not None:
        assert result.total_episodes == album_obj.track_count


@pytest.mark.parametrize("example", AUDIOBOOK_FIXTURES, ids=lambda val: val.stem)
def test_parse_audiobook(example: pathlib.Path, provider_stub: ProviderStub) -> None:
    """parse_audiobook extracts authors from artists and publisher from labels."""
    album_obj = _album_from_fixture(example)
    assert album_obj is not None
    result = parse_audiobook(cast("YandexMusicProvider", provider_stub), album_obj)
    assert result.item_id == str(album_obj.id)
    assert result.name
    assert result.duration == 0  # filled in later by get_audiobook()
    # authors come from album artists
    expected_authors = [a.name for a in (album_obj.artists or []) if a.name]
    assert list(result.authors) == expected_authors
    assert list(result.narrators) == []


def test_parse_audiobook_fully_played_true(provider_stub: ProviderStub) -> None:
    """parse_audiobook propagates album.listening_finished=True to fully_played."""
    album_obj = _album_from_fixture(FIXTURES_DIR / "audiobooks" / "basic.json")
    assert album_obj is not None
    album_obj.listening_finished = True
    result = parse_audiobook(cast("YandexMusicProvider", provider_stub), album_obj)
    assert result.fully_played is True


def test_parse_audiobook_fully_played_false(provider_stub: ProviderStub) -> None:
    """parse_audiobook propagates album.listening_finished=False to fully_played."""
    album_obj = _album_from_fixture(FIXTURES_DIR / "audiobooks" / "basic.json")
    assert album_obj is not None
    album_obj.listening_finished = False
    result = parse_audiobook(cast("YandexMusicProvider", provider_stub), album_obj)
    assert result.fully_played is False


def test_parse_audiobook_fully_played_none(provider_stub: ProviderStub) -> None:
    """parse_audiobook leaves fully_played=None when the flag is missing."""
    album_obj = _album_from_fixture(FIXTURES_DIR / "audiobooks" / "basic.json")
    assert album_obj is not None
    album_obj.listening_finished = None
    result = parse_audiobook(cast("YandexMusicProvider", provider_stub), album_obj)
    assert result.fully_played is None


def test_parse_podcast_episode(provider_stub: ProviderStub) -> None:
    """parse_podcast_episode links episode to its parent podcast."""
    podcast_album = _album_from_fixture(FIXTURES_DIR / "podcasts" / "basic.json")
    assert podcast_album is not None
    podcast = parse_podcast(cast("YandexMusicProvider", provider_stub), podcast_album)

    track_obj = _track_from_fixture(FIXTURES_DIR / "podcast_episodes" / "basic.json")
    assert track_obj is not None
    episode = parse_podcast_episode(
        cast("YandexMusicProvider", provider_stub), track_obj, podcast, position=1
    )
    assert episode.item_id == str(track_obj.id)
    assert episode.name == track_obj.title
    assert episode.position == 1
    assert episode.duration == (track_obj.duration_ms or 0) // 1000
    assert episode.podcast is podcast
    mapping = next(iter(episode.provider_mappings))
    assert f"music.yandex.ru/track/{track_obj.id}" in (mapping.url or "")


def test_parse_podcast_episode_inherits_podcast_image(provider_stub: ProviderStub) -> None:
    """Episode image falls back to parent podcast image when track has none."""
    podcast_album = _album_from_fixture(FIXTURES_DIR / "podcasts" / "basic.json")
    assert podcast_album is not None
    podcast = parse_podcast(cast("YandexMusicProvider", provider_stub), podcast_album)
    # strip cover on the track so the fallback kicks in
    track_obj = _track_from_fixture(FIXTURES_DIR / "podcast_episodes" / "basic.json")
    assert track_obj is not None
    track_obj.cover_uri = None
    track_obj.og_image = None
    episode = parse_podcast_episode(
        cast("YandexMusicProvider", provider_stub), track_obj, podcast, position=1
    )
    assert episode.metadata.images is not None
    assert episode.metadata.images == podcast.metadata.images
    # Must be a separate list — mutating one shouldn't affect the other.
    assert episode.metadata.images is not podcast.metadata.images
