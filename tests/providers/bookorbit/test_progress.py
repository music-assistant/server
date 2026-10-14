"""Tests for BookOrbit stream details and bidirectional audio progress."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientError
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.bookorbit import SUPPORTED_FEATURES, BookOrbitProvider
from music_assistant.providers.bookorbit.parsers import _audio_files
from music_assistant.providers.bookorbit.progress import (
    build_progress_update,
    parse_resume_position,
)


def _client(provider: BookOrbitProvider) -> Any:
    return cast("Any", provider._client)


@pytest.fixture
def provider(book_audiobook: dict[str, Any], book_multifile: dict[str, Any]) -> BookOrbitProvider:
    """Create a provider with a mocked client."""
    mass = Mock()
    mass.streams.base_url = "http://ma:8097"
    manifest = Mock()
    manifest.domain = "bookorbit"
    config = Mock()
    config.instance_id = "bookorbit--test"
    config.name = "BookOrbit Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {"log_level": "GLOBAL"}.get(
        key, default
    )
    prov = BookOrbitProvider(mass, manifest, config, SUPPORTED_FEATURES)
    prov.base_url = "http://bo"
    prov._catalog = {}
    prov._client = Mock()
    details = {book_audiobook["id"]: book_audiobook, book_multifile["id"]: book_multifile}
    prov._client.get_book = AsyncMock(side_effect=lambda book_id: details[book_id])
    prov._client.get_audio_progress = AsyncMock(return_value=None)
    prov._client.upsert_audio_progress = AsyncMock()
    return prov


def test_update_single_file(book_audiobook: dict[str, Any]) -> None:
    """A single file maps the position straight through."""
    files = _audio_files(book_audiobook)
    duration = float(files[0]["durationSeconds"])
    update = build_progress_update(files, position=duration / 4, fully_played=False)
    assert update is not None
    assert update.current_file_id == files[0]["id"]
    assert update.position_seconds == pytest.approx(duration / 4)
    assert update.percentage == pytest.approx(25.0)


def test_update_multi_file_maps_to_file_and_offset(book_multifile: dict[str, Any]) -> None:
    """Absolute position is translated to (file id, position inside that file)."""
    files = _audio_files(book_multifile)  # 100.5s + 200.5s
    update = build_progress_update(files, position=150.5, fully_played=False)
    assert update is not None
    assert update.current_file_id == 772
    assert update.position_seconds == pytest.approx(50.0)
    assert update.percentage == pytest.approx(150.5 / 301.0 * 100)


def test_update_fully_played_is_100_percent_on_last_file(book_multifile: dict[str, Any]) -> None:
    """Fully played pins the position to the end of the last file."""
    files = _audio_files(book_multifile)
    update = build_progress_update(files, position=10, fully_played=True)
    assert update is not None
    assert update.current_file_id == 772
    assert update.position_seconds == pytest.approx(200.5)
    assert update.percentage == 100.0


def test_update_position_beyond_end_is_clamped(book_multifile: dict[str, Any]) -> None:
    """A position past the end is clamped to the end of the last file."""
    files = _audio_files(book_multifile)
    update = build_progress_update(files, position=9999, fully_played=False)
    assert update is not None
    assert update.current_file_id == 772
    assert update.position_seconds == pytest.approx(200.5)
    assert update.percentage == 100.0


def test_update_without_files_is_none() -> None:
    """No audio files, no progress update."""
    assert build_progress_update([], position=5, fully_played=False) is None


def test_resume_multi_file_adds_file_offset(book_multifile: dict[str, Any]) -> None:
    """Resume position is offset by the durations of the preceding files, in ms."""
    files = _audio_files(book_multifile)
    progress = {
        "currentFileId": 772,
        "positionSeconds": 50.0,
        "percentage": 50.0,
        "updatedAt": "2026-10-14T12:00:00.000Z",
    }
    fully_played, position_ms, timestamp = parse_resume_position(files, progress)
    assert fully_played is False
    assert position_ms == 150_500
    assert timestamp is not None
    assert timestamp.isoformat().startswith("2026-10-14T12:00:00")


def test_resume_finished_book(book_multifile: dict[str, Any]) -> None:
    """100 percent is reported as fully played."""
    files = _audio_files(book_multifile)
    progress = {"currentFileId": 772, "positionSeconds": 200.5, "percentage": 100}
    fully_played, position_ms, timestamp = parse_resume_position(files, progress)
    assert fully_played is True
    assert position_ms == 301_000
    assert timestamp is None


def test_resume_naive_updated_at_is_utc_aware(book_multifile: dict[str, Any]) -> None:
    """Naive updatedAt timestamps are treated as UTC."""
    files = _audio_files(book_multifile)
    progress = {
        "currentFileId": 771,
        "positionSeconds": 0.0,
        "percentage": 0.0,
        "updatedAt": "2026-10-14T12:00:00",
    }
    _, _, timestamp = parse_resume_position(files, progress)
    assert timestamp is not None
    assert timestamp.tzinfo == UTC


def test_resume_unknown_file_falls_back_to_percentage(book_multifile: dict[str, Any]) -> None:
    """If the stored file is gone, the percentage is used."""
    files = _audio_files(book_multifile)
    progress = {"currentFileId": 9999, "positionSeconds": 12.0, "percentage": 50.0}
    _, position_ms, _ = parse_resume_position(files, progress)
    assert position_ms == 150_500


@pytest.mark.asyncio
async def test_stream_details_single_file(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """A single file is proxied through the MA stream route."""
    details = await provider.get_stream_details(str(book_audiobook["id"]), MediaType.AUDIOBOOK)
    file = _audio_files(book_audiobook)[0]
    assert details.stream_type == StreamType.HTTP
    assert details.audio_format.content_type == ContentType.try_parse(file["format"])
    assert details.can_seek is True
    assert details.allow_seek is True
    assert isinstance(details.path, str)
    assert details.path.startswith("http://ma:8097/bookorbit--test_part_stream?")
    assert f"book_id={book_audiobook['id']}" in details.path
    assert f"file_id={file['id']}" in details.path
    # no credentials in the url
    assert "token" not in details.path.lower()


@pytest.mark.asyncio
async def test_stream_details_multi_file(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Multiple files give ordered multi-part paths with durations (epub excluded)."""
    details = await provider.get_stream_details(str(book_multifile["id"]), MediaType.AUDIOBOOK)
    assert isinstance(details.path, list)
    assert [p.duration for p in details.path] == [100.5, 200.5]
    assert "file_id=771" in details.path[0].path
    assert "file_id=772" in details.path[1].path
    assert details.duration == 301


@pytest.mark.asyncio
async def test_stream_details_rejects_other_media(provider: BookOrbitProvider) -> None:
    """Only audiobooks can be streamed."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("1", MediaType.TRACK)


@pytest.mark.asyncio
async def test_get_resume_position_reads_progress(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Resume comes from GET audio-progress."""
    _client(provider).get_audio_progress.return_value = {
        "currentFileId": 772,
        "positionSeconds": 10.0,
        "percentage": 40.0,
    }
    result = await provider.get_resume_position(str(book_multifile["id"]), MediaType.AUDIOBOOK)
    assert result == (False, 110_500, None)
    _client(provider).get_audio_progress.assert_awaited_once_with(book_multifile["id"])


@pytest.mark.asyncio
async def test_get_resume_position_client_error_falls_back(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Progress fetch errors defer to MA's own position."""
    _client(provider).get_audio_progress.side_effect = ClientError("connection reset")
    with pytest.raises(NotImplementedError):
        await provider.get_resume_position(str(book_multifile["id"]), MediaType.AUDIOBOOK)


@pytest.mark.asyncio
async def test_get_resume_position_without_progress_falls_back(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """No progress on the server defers to MA's own position."""
    with pytest.raises(NotImplementedError):
        await provider.get_resume_position(str(book_multifile["id"]), MediaType.AUDIOBOOK)


@pytest.mark.asyncio
async def test_on_played_patches_progress(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Playing reports percentage, file and in-file position."""
    media_item = Mock()
    media_item.duration = 301
    await provider.on_played(
        MediaType.AUDIOBOOK,
        str(book_multifile["id"]),
        fully_played=False,
        position=150,
        media_item=media_item,
        is_playing=True,
    )
    _client(provider).upsert_audio_progress.assert_awaited_once()
    args, kwargs = _client(provider).upsert_audio_progress.await_args
    assert args == (book_multifile["id"],)
    assert kwargs["current_file_id"] == 772
    assert kwargs["position_seconds"] == pytest.approx(49.5)
    assert kwargs["percentage"] == pytest.approx(150 / 301.0 * 100)


@pytest.mark.asyncio
async def test_on_played_skips_multi_file_without_durations(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Multi-file books with unknown file durations must not PATCH bogus percentages."""
    book = deepcopy(book_multifile)
    book["files"][0]["durationSeconds"] = None
    _client(provider).get_book = AsyncMock(return_value=book)
    await provider.on_played(
        MediaType.AUDIOBOOK,
        str(book["id"]),
        fully_played=False,
        position=150,
        media_item=Mock(),
        is_playing=True,
    )
    _client(provider).upsert_audio_progress.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_played_ignores_other_media(provider: BookOrbitProvider) -> None:
    """Non-audiobook callbacks are ignored."""
    await provider.on_played(MediaType.TRACK, "1", False, 5, Mock())
    _client(provider).upsert_audio_progress.assert_not_awaited()
