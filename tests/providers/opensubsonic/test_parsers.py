"""Test we can parse Open Subsonic models into Music Assistant models."""

import logging
import pathlib
from typing import TYPE_CHECKING

import aiofiles
import pytest
from libopensonic.media import (
    AlbumID3,
    AlbumInfo,
    ArtistID3,
    ArtistInfo2,
    Child,
    Lyrics,
    Playlist,
    PodcastChannel,
    PodcastEpisode,
    StructuredLyrics,
)

from music_assistant.providers.opensubsonic.parsers import (
    parse_album,
    parse_artist,
    parse_epsiode,
    parse_playlist,
    parse_podcast,
    parse_structured_lyrics,
    parse_track,
)

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
ARTIST_FIXTURES = list(FIXTURES_DIR.glob("artists/*.artist.json"))
ALBUM_FIXTURES = list(FIXTURES_DIR.glob("albums/*.album.json"))
PLAYLIST_FIXTURES = list(FIXTURES_DIR.glob("playlists/*.playlist.json"))
PODCAST_FIXTURES = list(FIXTURES_DIR.glob("podcasts/*.podcast.json"))
EPISODE_FIXTURES = list(FIXTURES_DIR.glob("episodes/*.episode.json"))
TRACK_FIXTURES = list(FIXTURES_DIR.glob("tracks/*.track.json"))
LYRICS_FIXTURES = list(FIXTURES_DIR.glob("lyrics/*.lyrics.json"))
STRUCTURED_LYRICS_FIXTURES = list(FIXTURES_DIR.glob("structured-lyrics/*.structured-lyrics.json"))

_LOGGER = logging.getLogger(__name__)


@pytest.mark.parametrize("example", ARTIST_FIXTURES, ids=lambda val: str(val.stem))
async def test_parse_artists(example: pathlib.Path, snapshot: SnapshotAssertion) -> None:
    """Test we can parse artists."""
    async with aiofiles.open(example, encoding="utf-8") as fp:
        artist = ArtistID3.from_json(await fp.read())

    parsed = parse_artist("xx-instance-id-xx", artist).to_dict()
    # sort external Ids to ensure they are always in the same order for snapshot testing
    parsed["external_ids"].sort()
    assert snapshot == parsed

    # Find the corresponding info file
    example_info = example.with_suffix("").with_suffix(".info.json")
    async with aiofiles.open(example_info, encoding="utf-8") as fp:
        artist_info = ArtistInfo2.from_json(await fp.read())

    parsed = parse_artist("xx-instance-id-xx", artist, artist_info).to_dict()
    # sort external Ids to ensure they are always in the same order for snapshot testing
    parsed["external_ids"].sort()
    assert snapshot == parsed


@pytest.mark.parametrize("example", ALBUM_FIXTURES, ids=lambda val: str(val.stem))
async def test_parse_albums(example: pathlib.Path, snapshot: SnapshotAssertion) -> None:
    """Test we can parse albums."""
    async with aiofiles.open(example, encoding="utf-8") as fp:
        album = AlbumID3.from_json(await fp.read())

    parsed = parse_album(_LOGGER, "xx-instance-id-xx", album).to_dict()
    # sort external Ids and genres to ensure they are always in the same order for snapshot testing
    parsed["external_ids"].sort()
    parsed["metadata"]["genres"].sort()
    assert snapshot == parsed

    # Find the corresponding info file
    example_info = example.with_suffix("").with_suffix(".info.json")
    async with aiofiles.open(example_info, encoding="utf-8") as fp:
        album_info = AlbumInfo.from_json(await fp.read())

    parsed = parse_album(_LOGGER, "xx-instance-id-xx", album, album_info).to_dict()
    # sort external Ids and genres to ensure they are always in the same order for snapshot testing
    parsed["external_ids"].sort()
    parsed["metadata"]["genres"].sort()
    assert snapshot == parsed


@pytest.mark.parametrize("example", PLAYLIST_FIXTURES, ids=lambda val: str(val.stem))
async def test_parse_playlist(example: pathlib.Path, snapshot: SnapshotAssertion) -> None:
    """Test we can parse Playlists."""
    async with aiofiles.open(example, encoding="utf-8") as fp:
        playlist = Playlist.from_json(await fp.read())

    parsed = parse_playlist("xx-instance-id-xx", playlist).to_dict()
    # sort external Ids to ensure they are always in the same order for snapshot testing
    parsed["external_ids"].sort()
    assert snapshot == parsed


@pytest.mark.parametrize("example", PODCAST_FIXTURES, ids=lambda val: str(val.stem))
async def test_parse_podcast(example: pathlib.Path, snapshot: SnapshotAssertion) -> None:
    """Test we can parse Podcasts."""
    async with aiofiles.open(example, encoding="utf-8") as fp:
        podcast = PodcastChannel.from_json(await fp.read())

    parsed = parse_podcast("xx-instance-id-xx", podcast).to_dict()
    # sort external Ids to ensure they are always in the same order for snapshot testing
    parsed["external_ids"].sort()
    assert snapshot == parsed


@pytest.mark.parametrize("example", EPISODE_FIXTURES, ids=lambda val: str(val.stem))
async def test_parse_episode(example: pathlib.Path, snapshot: SnapshotAssertion) -> None:
    """Test we can parse Podcast Episodes."""
    async with aiofiles.open(example, encoding="utf-8") as fp:
        episode = PodcastEpisode.from_json(await fp.read())

    example_channel = example.with_suffix("").with_suffix(".podcast.json")
    async with aiofiles.open(example_channel, encoding="utf-8") as fp:
        channel = PodcastChannel.from_json(await fp.read())

    parsed = parse_epsiode("xx-instance-id-xx", episode, channel).to_dict()
    # sort external Ids to ensure they are always in the same order for snapshot testing
    parsed["external_ids"].sort()
    assert snapshot == parsed


@pytest.mark.parametrize("example", TRACK_FIXTURES, ids=lambda val: str(val.stem))
async def test_parse_track(example: pathlib.Path, snapshot: SnapshotAssertion) -> None:
    """Test we can parse Tracks."""
    async with aiofiles.open(example, encoding="utf-8") as fp:
        song = Child.from_json(await fp.read())

    parsed = parse_track(_LOGGER, "xx-instance-id-xx", song).to_dict()
    # sort external Ids, genres, and performers to ensure they are always in the same
    # order for snapshot testing
    parsed["external_ids"].sort()
    parsed["metadata"]["genres"].sort()
    parsed["metadata"]["performers"].sort()
    assert snapshot == parsed

    example_album = example.with_suffix("").with_suffix(".album.json")
    async with aiofiles.open(example_album, encoding="utf-8") as fp:
        album = AlbumID3.from_json(await fp.read())

    parsed = parse_track(
        _LOGGER, "xx-instance-id-xx", song, parse_album(_LOGGER, "xx-instance-id-xx", album)
    ).to_dict()
    # sort external Ids, genres, and performers to ensure they are always in the same
    # order for snapshot testing
    parsed["external_ids"].sort()
    parsed["metadata"]["genres"].sort()
    parsed["metadata"]["performers"].sort()
    if parsed.get("album"):
        parsed["album"]["external_ids"].sort()
        parsed["album"]["metadata"]["genres"].sort()
    assert snapshot == parsed


@pytest.mark.parametrize("example", LYRICS_FIXTURES, ids=lambda val: str(val.stem))
async def test_lyrics(example: pathlib.Path, snapshot: SnapshotAssertion) -> None:
    """Test that we can handle unstructured lyrics."""
    async with aiofiles.open(example, encoding="utf-8") as fp:
        lyrics = Lyrics.from_json(await fp.read())

    example_track = example.with_suffix("").with_suffix(".track.json")
    async with aiofiles.open(example_track, encoding="utf-8") as fp:
        track = Child.from_json(await fp.read())

    parsed = parse_track(_LOGGER, "xx-instance-id-xx", track, None, (lyrics.value, False)).to_dict()
    parsed["external_ids"].sort()
    parsed["metadata"]["genres"].sort()
    parsed["metadata"]["performers"].sort()
    assert snapshot == parsed


@pytest.mark.parametrize("example", STRUCTURED_LYRICS_FIXTURES, ids=lambda val: str(val.stem))
async def test_structured_lyrics(example: pathlib.Path, snapshot: SnapshotAssertion) -> None:
    """Test that we can handle structured lyrics."""
    async with aiofiles.open(example, encoding="utf-8") as fp:
        lyrics = StructuredLyrics.from_json(await fp.read())

    parsed, _ = parse_structured_lyrics(lyrics)
    assert snapshot == parsed
