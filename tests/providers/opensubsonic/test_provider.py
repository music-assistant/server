"""Tests for OpenSonicProvider business logic."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from libopensonic.errors import DataNotFoundError
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.opensubsonic.parsers import EP_CHAN_SEP
from music_assistant.providers.opensubsonic.sonic_provider import OpenSonicProvider


def _make_task_capturer() -> tuple[list[asyncio.Future[Any]], Mock]:
    """Return (tasks, mock) that captures coroutines passed to mass.create_task."""
    tasks: list[asyncio.Future[Any]] = []

    # accept create_task's keyword-only options (task_id, abort_existing, ...) so the stub
    # keeps matching its signature
    def _schedule(coro: Any, *_args: Any, **_kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(coro)
        tasks.append(task)
        return task

    return tasks, Mock(side_effect=_schedule)


def _make_sonic_item(
    *,
    item_id: str = "tr-1",
    title: str = "Test Track",
    content_type: str | None = "audio/flac",
    transcoded_content_type: str | None = None,
    sampling_rate: int | None = None,
    bit_depth: int | None = None,
    channel_count: int | None = None,
    duration: int = 120,
    replay_gain: object = None,
) -> Mock:
    item = Mock()
    item.id = item_id
    item.title = title
    item.content_type = content_type
    item.transcoded_content_type = transcoded_content_type
    item.sampling_rate = sampling_rate
    item.bit_depth = bit_depth
    item.channel_count = channel_count
    item.duration = duration
    item.replay_gain = replay_gain
    return item


def _stub_conn(provider: OpenSonicProvider, item: Mock) -> None:
    provider.conn = Mock()
    provider.conn.get_song = AsyncMock(return_value=item)
    provider.conn.get_stream_url = Mock(return_value=("http://server/stream", {}))


# ---------------------------------------------------------------------------
# _set_loudness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_loudness_computes_lufs(provider: OpenSonicProvider) -> None:
    """track_gain and album_gain are each converted to LUFS via -18 - gain."""
    rg = Mock()
    rg.track_gain = 2.0
    rg.album_gain = -1.0
    item = _make_sonic_item(item_id="tr-1", replay_gain=rg)

    provider.mass.streams.audio_analysis.set_track_loudness = AsyncMock()  # type: ignore[method-assign]
    tasks, task_mock = _make_task_capturer()
    provider.mass.create_task = task_mock  # type: ignore[method-assign]

    provider._set_loudness(item)
    await asyncio.gather(*tasks)

    provider.mass.streams.audio_analysis.set_track_loudness.assert_awaited_once_with(
        "tr-1", provider.instance_id, -20.0, -17.0
    )


@pytest.mark.asyncio
async def test_set_loudness_no_album_gain_passes_none(provider: OpenSonicProvider) -> None:
    """album_loudness is None when the item carries no album_gain."""
    rg = Mock()
    rg.track_gain = 0.0
    rg.album_gain = None
    item = _make_sonic_item(item_id="tr-2", replay_gain=rg)

    provider.mass.streams.audio_analysis.set_track_loudness = AsyncMock()  # type: ignore[method-assign]
    tasks, task_mock = _make_task_capturer()
    provider.mass.create_task = task_mock  # type: ignore[method-assign]

    provider._set_loudness(item)
    await asyncio.gather(*tasks)

    provider.mass.streams.audio_analysis.set_track_loudness.assert_awaited_once_with(
        "tr-2", provider.instance_id, -18.0, None
    )


def test_set_loudness_no_track_gain_skips_task(provider: OpenSonicProvider) -> None:
    """No task is scheduled when track_gain is None."""
    rg = Mock()
    rg.track_gain = None
    item = _make_sonic_item(replay_gain=rg)

    provider.mass.create_task = Mock()  # type: ignore[method-assign]
    provider._set_loudness(item)

    provider.mass.create_task.assert_not_called()


def test_set_loudness_no_replay_gain_skips_task(provider: OpenSonicProvider) -> None:
    """No task is scheduled when the item has no replay_gain at all."""
    item = _make_sonic_item(replay_gain=None)

    provider.mass.create_task = Mock()  # type: ignore[method-assign]
    provider._set_loudness(item)

    provider.mass.create_task.assert_not_called()


# ---------------------------------------------------------------------------
# get_stream_details
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_details_audio_format_fallbacks(provider: OpenSonicProvider) -> None:
    """Missing sampling_rate, bit_depth, channel_count fall back to safe defaults."""
    item = _make_sonic_item(sampling_rate=None, bit_depth=None, channel_count=None)
    _stub_conn(provider, item)

    sd = await provider.get_stream_details("tr-1", MediaType.TRACK)

    assert sd.audio_format.sample_rate == 44100
    assert sd.audio_format.bit_depth == 16
    assert sd.audio_format.channels == 2


@pytest.mark.asyncio
async def test_stream_details_prefers_transcoded_content_type(
    provider: OpenSonicProvider,
) -> None:
    """transcoded_content_type takes precedence over content_type for the mime type."""
    item = _make_sonic_item(content_type="audio/flac", transcoded_content_type="audio/ogg")
    _stub_conn(provider, item)

    sd = await provider.get_stream_details("tr-1", MediaType.TRACK)

    assert sd.audio_format.content_type == ContentType.try_parse("audio/ogg")


@pytest.mark.asyncio
async def test_stream_details_falls_back_to_content_type(provider: OpenSonicProvider) -> None:
    """content_type is used when transcoded_content_type is absent."""
    item = _make_sonic_item(content_type="audio/flac", transcoded_content_type=None)
    _stub_conn(provider, item)

    sd = await provider.get_stream_details("tr-1", MediaType.TRACK)

    assert sd.audio_format.content_type == ContentType.try_parse("audio/flac")


@pytest.mark.asyncio
async def test_stream_details_stream_type_and_seek_flags(provider: OpenSonicProvider) -> None:
    """StreamDetails must expose HTTP stream type with seeking enabled."""
    item = _make_sonic_item()
    _stub_conn(provider, item)

    sd = await provider.get_stream_details("tr-1", MediaType.TRACK)

    assert sd.stream_type == StreamType.HTTP
    assert sd.can_seek is True
    assert sd.allow_seek is True


@pytest.mark.asyncio
async def test_stream_details_raises_media_not_found(provider: OpenSonicProvider) -> None:
    """DataNotFoundError from the server is translated to MediaNotFoundError."""
    provider.conn = Mock()
    provider.conn.get_song = AsyncMock(side_effect=DataNotFoundError("not found"))

    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("tr-missing", MediaType.TRACK)


@pytest.mark.asyncio
async def test_get_library_radios_returns_parsed_radios(provider: OpenSonicProvider) -> None:
    """Internet radio stations are pulled from the server and parsed into MA radios."""
    radio_station = Mock()
    radio_station.id = "radio-1"
    radio_station.name = "Sample Radio"
    radio_station.stream_url = "https://example.com/stream"
    radio_station.home_page_url = "https://example.com"
    radio_station.cover_art = "/cover.jpg"

    provider.conn = Mock()
    provider.conn.get_internet_radio_stations = AsyncMock(return_value=[radio_station])

    radios = [radio async for radio in provider.get_library_radios()]

    assert len(radios) == 1
    assert radios[0].item_id == "radio-1"
    assert radios[0].name == "Sample Radio"
    assert radios[0].uri == "https://example.com/stream"


@pytest.mark.asyncio
async def test_get_radio_returns_matching_station(provider: OpenSonicProvider) -> None:
    """Lookup by provider id resolves the requested radio station."""
    radio_station = Mock()
    radio_station.id = "radio-1"
    radio_station.name = "Sample Radio"
    radio_station.stream_url = "https://example.com/stream"
    radio_station.home_page_url = "https://example.com"
    radio_station.cover_art = None

    provider.conn = Mock()
    provider.conn.get_internet_radio_stations = AsyncMock(return_value=[radio_station])

    radio = await provider.get_radio("radio-1")

    assert radio.item_id == "radio-1"
    assert radio.name == "Sample Radio"


@pytest.mark.asyncio
async def test_stream_details_for_radio(provider: OpenSonicProvider) -> None:
    """Radio stream details point to the station stream URL and disable seeking."""
    radio_station = Mock()
    radio_station.id = "radio-1"
    radio_station.name = "Sample Radio"
    radio_station.stream_url = "https://example.com/stream"
    radio_station.home_page_url = "https://example.com"
    radio_station.cover_art = None

    provider.conn = Mock()
    provider.conn.get_internet_radio_stations = AsyncMock(return_value=[radio_station])

    sd = await provider.get_stream_details("radio-1", MediaType.RADIO)

    assert sd.stream_type == StreamType.HTTP
    assert sd.path == "https://example.com/stream"
    assert sd.can_seek is False
    assert sd.allow_seek is False


# ---------------------------------------------------------------------------
# on_played
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_played_non_podcast_makes_no_calls(provider: OpenSonicProvider) -> None:
    """on_played is a no-op for non-podcast media types."""
    provider.conn = Mock()
    provider.conn.delete_bookmark = AsyncMock()
    provider.conn.create_bookmark = AsyncMock()

    await provider.on_played(MediaType.TRACK, "tr-1", True, 60, Mock())

    provider.conn.delete_bookmark.assert_not_awaited()
    provider.conn.create_bookmark.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_played_fully_played_deletes_bookmark(provider: OpenSonicProvider) -> None:
    """Fully played podcast episode removes the server-side bookmark."""
    provider.conn = Mock()
    provider.conn.delete_bookmark = AsyncMock()

    await provider.on_played(MediaType.PODCAST_EPISODE, f"pd-1{EP_CHAN_SEP}ep-1", True, 0, Mock())

    provider.conn.delete_bookmark.assert_awaited_once_with(mid="ep-1")


@pytest.mark.asyncio
async def test_on_played_in_progress_stores_position_in_milliseconds(
    provider: OpenSonicProvider,
) -> None:
    """Resume position is stored as milliseconds (MA seconds * 1000)."""
    provider.conn = Mock()
    provider.conn.create_bookmark = AsyncMock()

    await provider.on_played(MediaType.PODCAST_EPISODE, f"pd-1{EP_CHAN_SEP}ep-1", False, 45, Mock())

    provider.conn.create_bookmark.assert_awaited_once_with(
        mid="ep-1",
        position=45000,
        comment="Music Assistant Bookmark",
    )


# ---------------------------------------------------------------------------
# get_resume_position
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_resume_position_non_podcast_raises(provider: OpenSonicProvider) -> None:
    """Non-podcast media type raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        await provider.get_resume_position("tr-1", MediaType.TRACK)


@pytest.mark.asyncio
async def test_get_resume_position_returns_matching_bookmark(
    provider: OpenSonicProvider,
) -> None:
    """When a bookmark exists for the episode, its position and timestamp are returned."""
    bookmark = Mock()
    bookmark.entry.id = "ep-1"
    bookmark.position = 90000
    bookmark.created = "2026-07-25T10:00:00"

    provider.conn = Mock()
    provider.conn.get_bookmarks = AsyncMock(return_value=[bookmark])

    fully_played, position, created = await provider.get_resume_position(
        f"pd-1{EP_CHAN_SEP}ep-1", MediaType.PODCAST_EPISODE
    )

    assert not fully_played
    assert position == 90000
    assert isinstance(created, datetime)


@pytest.mark.asyncio
async def test_get_resume_position_no_bookmark_returns_zero(
    provider: OpenSonicProvider,
) -> None:
    """When no bookmark exists, position is 0 and created is None."""
    provider.conn = Mock()
    provider.conn.get_bookmarks = AsyncMock(return_value=[])

    fully_played, position, created = await provider.get_resume_position(
        f"pd-1{EP_CHAN_SEP}ep-1", MediaType.PODCAST_EPISODE
    )

    assert not fully_played
    assert position == 0
    assert created is None


# ---------------------------------------------------------------------------
# get_playlist_tracks
# ---------------------------------------------------------------------------


def _parse_track_stub(*_args: Any, **_kwargs: Any) -> Mock:
    """Return a lightweight stand-in Track (parse_track is covered by test_parsers)."""
    return Mock(position=0)


@pytest.mark.asyncio
async def test_get_playlist_tracks_avoids_per_track_metadata_fetch(
    provider: OpenSonicProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enqueuing a playlist must not fetch album or lyrics per track (avoids N+1 requests)."""
    sonic_playlist = Mock()
    sonic_playlist.entry = [_make_sonic_item(item_id=f"tr-{i}") for i in range(3)]

    provider.conn = Mock()
    provider.conn.get_playlist = AsyncMock(return_value=sonic_playlist)
    provider.conn.get_album = AsyncMock()
    provider.conn.get_album_info2 = AsyncMock()
    provider.conn.get_lyrics = AsyncMock()
    provider.conn.get_lyrics_by_song_id = AsyncMock()

    # force a cache miss so the real method body runs, and capture the background
    # store task so its coroutine is awaited rather than left pending
    tasks, task_mock = _make_task_capturer()
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]
    provider.mass.create_task = task_mock  # type: ignore[method-assign]

    # parse_track has its own coverage in test_parsers; isolate the fetch behaviour here
    monkeypatch.setattr(
        "music_assistant.providers.opensubsonic.sonic_provider.parse_track",
        _parse_track_stub,
    )

    result = await provider.get_playlist_tracks("pl-1")
    await asyncio.gather(*tasks)

    assert len(result) == 3
    assert [track.position for track in result] == [1, 2, 3]
    provider.conn.get_playlist.assert_awaited_once_with("pl-1")
    provider.conn.get_album.assert_not_awaited()
    provider.conn.get_album_info2.assert_not_awaited()
    provider.conn.get_lyrics.assert_not_awaited()
    provider.conn.get_lyrics_by_song_id.assert_not_awaited()
