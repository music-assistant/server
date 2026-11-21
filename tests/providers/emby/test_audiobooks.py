"""Tests for Emby audiobook support."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.streamdetails import MultiPartPath

from music_assistant.providers.emby import SUPPORTED_FEATURES, EmbyProvider
from music_assistant.providers.emby.const import (
    ITEM_KEY_ID,
    ITEM_KEY_RUNTIME_TICKS,
    ITEM_KEY_TYPE,
    ITEMS,
)
from music_assistant.providers.emby.parsers import parse_audiobook

TICKS_PER_SECOND = 10_000_000


@pytest.fixture
def provider() -> EmbyProvider:
    """Create an Emby provider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "emby"
    config = Mock()
    config.instance_id = "emby--test123"
    config.name = "Emby Test"
    config.enabled = True
    config.get_value.return_value = "GLOBAL"
    result = EmbyProvider(mass, manifest, config, SUPPORTED_FEATURES)
    result._user_id = "user"
    result._token = "token"
    result._base_url = "http://emby.local:8096/"
    return result


def _album_item(**overrides: Any) -> dict[str, Any]:
    """Return an Emby audiobook in the 'audiobooks' library (album) shape."""
    item: dict[str, Any] = {
        ITEM_KEY_ID: "book-1",
        "Name": "Dune",
        ITEM_KEY_TYPE: "MusicAlbum",
        ITEM_KEY_RUNTIME_TICKS: 15 * TICKS_PER_SECOND,
        "AlbumArtists": [{"Name": "Frank Herbert", "Id": "author-1"}],
        "Artists": ["Scott Brick"],
        "Overview": "A desert planet saga.",
        "ProductionYear": 1984,
        "ImageTags": {"Primary": "abcdef"},
        "UserData": {
            "IsFavorite": True,
            "Played": False,
            "PlaybackPositionTicks": 60 * TICKS_PER_SECOND,
            "LastPlayedDate": "2026-11-01T10:00:00Z",
        },
    }
    item.update(overrides)
    return item


def _album_children() -> list[dict[str, Any]]:
    """Return the part files of the album-shape audiobook, out of order like Emby."""
    return [
        {
            ITEM_KEY_ID: "book-3",
            "Name": "Part 3",
            ITEM_KEY_TYPE: "Audio",
            ITEM_KEY_RUNTIME_TICKS: 3 * TICKS_PER_SECOND,
            "Container": "mp3",
            "MediaStreams": [
                {
                    "Type": "Audio",
                    "Codec": "mp3",
                    "SampleRate": 44100,
                    "Channels": 1,
                    "BitRate": 64000,
                }
            ],
        },
        {
            ITEM_KEY_ID: "book-1",
            "Name": "Part 1",
            ITEM_KEY_TYPE: "Audio",
            "IndexNumber": 1,
            ITEM_KEY_RUNTIME_TICKS: 7 * TICKS_PER_SECOND,
            "Container": "mp3",
            "MediaStreams": [
                {
                    "Type": "Audio",
                    "Codec": "mp3",
                    "SampleRate": 44100,
                    "Channels": 1,
                    "BitRate": 64000,
                }
            ],
        },
        {
            ITEM_KEY_ID: "book-2",
            "Name": "Part 2",
            ITEM_KEY_TYPE: "Audio",
            "IndexNumber": 2,
            ITEM_KEY_RUNTIME_TICKS: 5 * TICKS_PER_SECOND,
            "Container": "mp3",
            "MediaStreams": [
                {
                    "Type": "Audio",
                    "Codec": "mp3",
                    "SampleRate": 44100,
                    "Channels": 1,
                    "BitRate": 64000,
                }
            ],
        },
    ]


def test_parse_album_audiobook_with_parts_and_chapters() -> None:
    """Parts (already ordered by the provider) become chapters with cumulative offsets."""
    children = _album_children()
    ordered_parts = [children[1], children[2], children[0]]  # Part 1, Part 2, Part 3
    audiobook = parse_audiobook("emby--test123", Mock(domain="emby"), _album_item(), ordered_parts)

    assert audiobook.media_type == MediaType.AUDIOBOOK
    assert audiobook.authors == ["Frank Herbert"]
    assert audiobook.narrators == ["Scott Brick"]
    assert audiobook.duration == 15
    assert audiobook.favorite is True
    assert audiobook.resume_position_ms == 60 * 1000
    assert audiobook.resume_position_timestamp is not None
    assert audiobook.resume_position_timestamp.year == 2026
    chapters = audiobook.metadata.chapters
    assert chapters is not None
    assert [chapter.name for chapter in chapters] == ["Part 1", "Part 2", "Part 3"]
    assert chapters[0].start == 0
    assert chapters[1].start == 7
    assert chapters[2].end == 15


def test_parse_album_audiobook_listing_without_parts() -> None:
    """A listing item without parts is a valid summary (no synthetic chapters)."""
    audiobook = parse_audiobook("emby--test123", Mock(domain="emby"), _album_item())

    assert audiobook.metadata.chapters in (None, [])
    assert audiobook.duration == 15


async def test_get_audiobook_orders_children(provider: EmbyProvider) -> None:
    """Child parts arrive unordered from Emby and must be ordered by track number."""
    provider._get = AsyncMock(return_value={ITEMS: _album_children()})  # type: ignore[method-assign]

    audiobook = await provider._parse_audiobook_item(
        _album_item(),
    )

    chapters = audiobook.metadata.chapters
    assert chapters is not None
    assert [chapter.name for chapter in chapters] == ["Part 1", "Part 2", "Part 3"]


async def test_get_audiobook_orders_parts_by_disc_first(provider: EmbyProvider) -> None:
    """Track numbers restart per disc, so the disc number must order ahead of them."""

    def part(part_id: str, disc: int, track: int) -> dict[str, Any]:
        return {
            ITEM_KEY_ID: part_id,
            "Name": f"Part {disc}-{track}",
            ITEM_KEY_TYPE: "Audio",
            "IndexNumber": track,
            "ParentIndexNumber": disc,
            ITEM_KEY_RUNTIME_TICKS: TICKS_PER_SECOND,
            "Container": "mp3",
            "MediaStreams": [{"Type": "Audio", "Codec": "mp3"}],
        }

    # Emby returns the children unordered, with track numbers restarting on disc 2
    children = [part("d2t1", 2, 1), part("d1t2", 1, 2), part("d2t2", 2, 2), part("d1t1", 1, 1)]
    provider._get = AsyncMock(return_value={ITEMS: children})  # type: ignore[method-assign]

    ordered = [item[ITEM_KEY_ID] for item in await provider._get_audiobook_part_items("book-1")]

    assert ordered == ["d1t1", "d1t2", "d2t1", "d2t2"]


async def test_get_stream_details_multi_part(provider: EmbyProvider) -> None:
    """A multi-file audiobook streams as a list of part URLs."""
    provider._get = AsyncMock(return_value={ITEMS: _album_children()})  # type: ignore[method-assign]

    stream_details = await provider.get_stream_details("book-1", MediaType.AUDIOBOOK)

    assert stream_details.duration == 15
    assert stream_details.media_type == MediaType.AUDIOBOOK
    assert isinstance(stream_details.path, list)
    assert all(isinstance(part, MultiPartPath) for part in stream_details.path)
    assert [part.duration for part in stream_details.path] == [7, 5, 3]
    paths = [part.path for part in stream_details.path]
    assert "Audio/book-1/universal" in paths[0]
    assert "Audio/book-3/universal" in paths[2]


async def test_part_durations_keep_sub_second_precision(provider: EmbyProvider) -> None:
    """Truncating part durations to whole seconds drifts the seek/chapter mapping."""
    children = _album_children()
    # 7.4s and 5.2s, as they occur on real files; Emby returns them unordered
    children[1][ITEM_KEY_RUNTIME_TICKS] = int(7.4 * TICKS_PER_SECOND)
    children[2][ITEM_KEY_RUNTIME_TICKS] = int(5.2 * TICKS_PER_SECOND)
    provider._get = AsyncMock(return_value={ITEMS: children})  # type: ignore[method-assign]

    audiobook = await provider._parse_audiobook_item(_album_item())
    stream_details = await provider.get_stream_details("book-1", MediaType.AUDIOBOOK)

    assert isinstance(stream_details.path, list)
    assert [part.duration for part in stream_details.path] == [7.4, 5.2, 3.0]
    chapters = audiobook.metadata.chapters
    assert chapters is not None
    assert chapters[1].start == pytest.approx(7.4)
    assert chapters[2].start == pytest.approx(12.6)
    assert chapters[2].end == pytest.approx(15.6)


async def test_get_stream_details_single_part(provider: EmbyProvider) -> None:
    """A single-file audiobook streams straight from its only part."""
    single_child = [
        {
            ITEM_KEY_ID: "book-1",
            "Name": "Dune",
            ITEM_KEY_TYPE: "Audio",
            ITEM_KEY_RUNTIME_TICKS: 15 * TICKS_PER_SECOND,
            "Container": "mp3",
            "MediaStreams": [{"Type": "Audio", "Codec": "mp3", "SampleRate": 44100, "Channels": 1}],
        }
    ]
    provider._get = AsyncMock(return_value={ITEMS: single_child})  # type: ignore[method-assign]

    stream_details = await provider.get_stream_details("book-1", MediaType.AUDIOBOOK)

    assert isinstance(stream_details.path, str)
    assert stream_details.path.startswith("http://emby.local:8096/Audio/book-1/universal")
    assert stream_details.duration == 15


async def test_on_played_stopped_audiobook_persists_position(provider: EmbyProvider) -> None:
    """The final stop report of an unfinished audiobook must save its position."""
    provider._post = AsyncMock()  # type: ignore[method-assign]

    await provider.on_played(
        MediaType.AUDIOBOOK,
        "book-1",
        fully_played=False,
        position=300,
        media_item=Mock(),
        is_playing=False,
    )

    provider._post.assert_awaited_once_with(
        "/Users/user/Items/book-1/UserData",
        json={"PlaybackPositionTicks": 300 * 10_000_000},
    )


async def test_on_played_finished_track_on_stop_saves_nothing(provider: EmbyProvider) -> None:
    """Tracks keep their existing semantics: no position write outside heartbeats."""
    provider._post = AsyncMock()  # type: ignore[method-assign]

    await provider.on_played(
        MediaType.TRACK,
        "track-1",
        fully_played=False,
        position=100,
        media_item=Mock(),
        is_playing=False,
    )

    provider._post.assert_not_awaited()


async def test_on_played_finished_audiobook_marks_played_without_position(
    provider: EmbyProvider,
) -> None:
    """A finished book is reported at its end position, which must not be resumed from."""
    provider._post = AsyncMock()  # type: ignore[method-assign]

    await provider.on_played(
        MediaType.AUDIOBOOK,
        "book-1",
        fully_played=True,
        position=900,
        media_item=Mock(),
        is_playing=False,
    )

    provider._post.assert_awaited_once_with("Users/user/PlayedItems/book-1")


async def test_get_resume_position(provider: EmbyProvider) -> None:
    """The resume point is read from the book's user data."""
    provider._get = AsyncMock(return_value=_album_item())  # type: ignore[method-assign]

    fully_played, position_ms, timestamp = await provider.get_resume_position(
        "book-1", MediaType.AUDIOBOOK
    )

    assert fully_played is False
    assert position_ms == 60 * 1000
    assert timestamp is not None
    assert timestamp.year == 2026


async def test_get_library_audiobooks_lists_books_collections(provider: EmbyProvider) -> None:
    """Books are listed from the audiobook collections with album and part types."""
    provider._get_audiobook_libraries = AsyncMock(  # type: ignore[method-assign]
        return_value=[{ITEM_KEY_ID: "ab-lib"}]
    )
    provider._get = AsyncMock(  # type: ignore[method-assign]
        side_effect=[{ITEMS: [_album_item()]}, {ITEMS: []}]
    )

    audiobooks = [item async for item in provider.get_library_audiobooks()]

    assert len(audiobooks) == 1
    assert audiobooks[0].item_id == "book-1"
    listing_params = provider._get.call_args_list[0].kwargs["params"]
    assert listing_params["IncludeItemTypes"] == "MusicAlbum"
    assert listing_params["ParentId"] == "ab-lib"
