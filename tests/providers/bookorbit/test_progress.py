"""Tests for BookOrbit stream details and bidirectional audio progress."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import ClientError, ClientResponseError, web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import LoginFailed, MediaNotFoundError, ProviderUnavailableError
from yarl import URL

from music_assistant.providers.bookorbit import BookOrbitProvider
from music_assistant.providers.bookorbit.constants import STREAMDETAILS_EXPIRATION_S
from music_assistant.providers.bookorbit.parsers import _audio_files
from music_assistant.providers.bookorbit.progress import (
    build_playback_state_update,
    build_progress_update,
    parse_resume_position,
    parse_resume_position_from_playback_state,
)
from tests.providers.bookorbit.conftest import manifest_from_book


def _client(provider: BookOrbitProvider) -> Any:
    return cast("Any", provider._client)


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


def test_update_unknown_duration_raises(book_multifile: dict[str, Any]) -> None:
    """Legacy progress must not invent offsets when a part duration is missing."""
    files = _audio_files(book_multifile)
    files[0]["durationSeconds"] = None
    with pytest.raises(ValueError, match="unknown duration"):
        build_progress_update(files, position=10, fully_played=False)


def test_update_single_file_unknown_duration_complete_and_reset(
    book_audiobook: dict[str, Any],
) -> None:
    """Single-file legacy updates can complete or clear without a measured duration."""
    files = _audio_files(book_audiobook)
    files[0]["durationSeconds"] = None
    done = build_progress_update(files, position=12.5, fully_played=True)
    assert done is not None
    assert done.percentage == 100.0
    assert done.current_file_id == int(files[0]["id"])
    assert done.position_seconds == pytest.approx(12.5)
    reset = build_progress_update(files, position=0, fully_played=False)
    assert reset is not None
    assert reset.percentage == 0.0
    assert reset.position_seconds == 0.0


def test_resume_unknown_preceding_duration_raises(book_multifile: dict[str, Any]) -> None:
    """Legacy resume must not invent an absolute position across unknown parts."""
    files = _audio_files(book_multifile)
    files[0]["durationSeconds"] = None
    progress = {"currentFileId": 772, "positionSeconds": 50.0, "percentage": 50.0}
    with pytest.raises(ValueError, match="unknown duration"):
        parse_resume_position(files, progress)


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


def test_resume_unknown_file_raises(book_multifile: dict[str, Any]) -> None:
    """If the stored file is gone, fall back to MA resume instead of inventing a seek."""
    files = _audio_files(book_multifile)
    progress = {"currentFileId": 9999, "positionSeconds": 12.0, "percentage": 50.0}
    with pytest.raises(ValueError, match="unknown file"):
        parse_resume_position(files, progress)


@pytest.mark.asyncio
async def test_stream_details_single_file(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """A single asset is proxied through the MA stream route."""
    details = await provider.get_stream_details(str(book_audiobook["id"]), MediaType.AUDIOBOOK)
    asset = manifest_from_book(book_audiobook)["assets"][0]
    assert details.stream_type == StreamType.HTTP
    assert details.audio_format.content_type == ContentType.try_parse(asset["format"])
    assert details.can_seek is True
    assert details.allow_seek is True
    assert isinstance(details.path, str)
    assert details.path.startswith("http://ma:8097/bookorbit--test_part_stream?")
    assert f"book_id={book_audiobook['id']}" in details.path
    assert f"asset_id={asset['assetId']}" in details.path
    assert "session_id=" in details.path
    assert isinstance(details.data, dict)
    assert details.data.get("session_id")
    # no credentials in the url
    assert "token" not in details.path.lower()


@pytest.mark.asyncio
async def test_stream_details_multi_file(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Multiple assets give ordered multi-part paths with durations (epub excluded)."""
    details = await provider.get_stream_details(str(book_multifile["id"]), MediaType.AUDIOBOOK)
    assets = manifest_from_book(book_multifile)["assets"]
    assert isinstance(details.path, list)
    assert [p.duration for p in details.path] == [100.5, 200.5]
    assert f"asset_id={assets[0]['assetId']}" in details.path[0].path
    assert f"asset_id={assets[1]['assetId']}" in details.path[1].path
    assert "session_id=" in details.path[0].path
    assert details.duration == 301


@pytest.mark.asyncio
async def test_stream_details_unknown_duration_when_part_missing(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """A partial measured total must not become the audiobook duration."""
    incomplete = manifest_from_book(book_multifile)
    incomplete["assets"][1]["durationMs"] = None
    # BookOrbit sums only measured parts when another duration is unknown.
    incomplete["totalDurationMs"] = incomplete["assets"][0]["durationMs"]
    incomplete_book = {
        **book_multifile,
        "audioMetadata": {**(book_multifile.get("audioMetadata") or {}), "durationSeconds": None},
        "files": [
            {**book_multifile["files"][0]},
            {**book_multifile["files"][1], "durationSeconds": None},
            *book_multifile["files"][2:],
        ],
    }
    _client(provider).get_audiobook_manifest = AsyncMock(return_value=incomplete)
    _client(provider).get_book = AsyncMock(return_value=incomplete_book)
    details = await provider.get_stream_details(str(book_multifile["id"]), MediaType.AUDIOBOOK)
    assert details.duration is None


@pytest.mark.asyncio
async def test_stream_details_unknown_duration_book_fetch_error_is_ma_error(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Duration fallback book fetch failures must be MA errors for multi-mapping fallback."""
    incomplete = manifest_from_book(book_multifile)
    incomplete["assets"][1]["durationMs"] = None
    incomplete["totalDurationMs"] = incomplete["assets"][0]["durationMs"]
    _client(provider).get_audiobook_manifest = AsyncMock(return_value=incomplete)
    _client(provider).get_book = AsyncMock(side_effect=ClientError("connection reset"))
    with pytest.raises(ProviderUnavailableError):
        await provider.get_stream_details(str(book_multifile["id"]), MediaType.AUDIOBOOK)


@pytest.mark.asyncio
async def test_stream_session_survives_on_streamed_for_seek_reuse(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """on_streamed must not revoke grants: seek/stop-resume reuses the same part URLs."""
    details = await provider.get_stream_details(str(book_audiobook["id"]), MediaType.AUDIOBOOK)
    assert isinstance(details.data, dict)
    session_id = details.data["session_id"]
    asset = manifest_from_book(book_audiobook)["assets"][0]
    book_id = str(book_audiobook["id"])
    assert provider._stream_session_allows(session_id, book_id, asset["assetId"])
    await provider.on_stream_started(details)
    assert session_id in provider._active_stream_sessions
    await provider.on_streamed(details)
    assert session_id not in provider._active_stream_sessions
    # Grant stays so the reused buffer can open the next part.
    assert provider._stream_session_allows(session_id, book_id, asset["assetId"])


@pytest.mark.asyncio
async def test_stream_session_idle_timeout_revokes_grant(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """Inactive grants expire after STREAMDETAILS_EXPIRATION_S without part access."""
    details = await provider.get_stream_details(str(book_audiobook["id"]), MediaType.AUDIOBOOK)
    assert isinstance(details.data, dict)
    session_id = details.data["session_id"]
    asset = manifest_from_book(book_audiobook)["assets"][0]
    book_id = str(book_audiobook["id"])
    await provider.on_stream_started(details)
    await provider.on_streamed(details)
    grant = provider._stream_sessions[session_id]
    grant.last_access_at -= STREAMDETAILS_EXPIRATION_S + 1
    assert not provider._stream_session_allows(session_id, book_id, asset["assetId"])


@pytest.mark.asyncio
async def test_stream_session_survives_idle_while_buffer_reusable(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """After on_streamed, a reused buffer must still open the next part past idle TTL."""
    details = await provider.get_stream_details(str(book_multifile["id"]), MediaType.AUDIOBOOK)
    assert isinstance(details.data, dict)
    session_id = details.data["session_id"]
    assets = manifest_from_book(book_multifile)["assets"]
    book_id = str(book_multifile["id"])
    # Core attaches a PCM buffer and may reuse it after on_streamed without
    # calling on_stream_started again (seconds_streamed already set).
    details.buffer = Mock(cancelled=False)
    await provider.on_stream_started(details)
    await provider.on_streamed(details)
    assert session_id not in provider._active_stream_sessions
    grant = provider._stream_sessions[session_id]
    grant.last_access_at -= STREAMDETAILS_EXPIRATION_S + 1
    assert provider._stream_session_allows(session_id, book_id, assets[1]["assetId"])


@pytest.mark.asyncio
async def test_stream_session_transfer_blocks_idle_prune(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """In-flight part transfers must not be pruned even when inactive and idle."""
    details = await provider.get_stream_details(str(book_audiobook["id"]), MediaType.AUDIOBOOK)
    assert isinstance(details.data, dict)
    session_id = details.data["session_id"]
    asset = manifest_from_book(book_audiobook)["assets"][0]
    book_id = str(book_audiobook["id"])
    await provider.on_stream_started(details)
    await provider.on_streamed(details)
    grant = provider._begin_stream_transfer(session_id, book_id, asset["assetId"])
    assert grant is not None
    try:
        grant.last_access_at -= STREAMDETAILS_EXPIRATION_S + 1
        assert provider._stream_session_allows(session_id, book_id, asset["assetId"])
    finally:
        provider._end_stream_transfer(grant)


@pytest.mark.asyncio
async def test_stream_details_rejects_other_media(provider: BookOrbitProvider) -> None:
    """Only audiobooks can be streamed."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("1", MediaType.TRACK)


@pytest.mark.asyncio
async def test_stream_details_http_error_is_provider_unavailable(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """Non-404 manifest HTTP errors must be MA errors so core can try another mapping."""
    _client(provider).get_audiobook_manifest.side_effect = ClientResponseError(
        request_info=Mock(real_url=URL("http://bo/x")),
        history=(),
        status=503,
        message="unavailable",
    )
    with pytest.raises(ProviderUnavailableError):
        await provider.get_stream_details(str(book_audiobook["id"]), MediaType.AUDIOBOOK)


@pytest.mark.asyncio
async def test_stream_details_auth_error_is_login_failed(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """Manifest auth failures must surface as LoginFailed for multi-mapping fallback."""
    _client(provider).get_audiobook_manifest.side_effect = LoginFailed("login rejected")
    with pytest.raises(LoginFailed):
        await provider.get_stream_details(str(book_audiobook["id"]), MediaType.AUDIOBOOK)


@pytest.mark.asyncio
async def test_stream_details_connection_error_is_provider_unavailable(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """Transport failures must surface as ProviderUnavailableError."""
    _client(provider).get_audiobook_manifest.side_effect = ClientError("connection reset")
    with pytest.raises(ProviderUnavailableError):
        await provider.get_stream_details(str(book_audiobook["id"]), MediaType.AUDIOBOOK)


def test_playback_state_update_maps_absolute_position(book_multifile: dict[str, Any]) -> None:
    """Absolute seconds become assetId + in-asset positionMs."""
    manifest = manifest_from_book(book_multifile)
    update = build_playback_state_update(manifest, position=150.5, fully_played=False)
    assert update is not None
    assert update.asset_id == manifest["assets"][1]["assetId"]
    assert update.position_ms == 50_000
    assert update.percentage == pytest.approx(150.5 / 301.0 * 100)


def test_playback_state_update_declines_missing_asset_duration(
    book_multifile: dict[str, Any],
) -> None:
    """Manifest updates are skipped when any asset duration is unknown."""
    manifest = manifest_from_book(book_multifile)
    manifest["assets"][1]["durationMs"] = None
    assert build_playback_state_update(manifest, position=150.5, fully_played=False) is None


def test_playback_state_update_single_asset_without_duration(
    book_audiobook: dict[str, Any],
) -> None:
    """Ordinary single-asset seeks send positionMs; complete/reset fall back to legacy."""
    manifest = manifest_from_book(book_audiobook)
    manifest["assets"][0]["durationMs"] = None
    manifest["totalDurationMs"] = None
    mid = build_playback_state_update(manifest, position=42.5, fully_played=False)
    assert mid is not None
    assert mid.asset_id == manifest["assets"][0]["assetId"]
    assert mid.position_ms == 42_500
    assert build_playback_state_update(manifest, position=42.5, fully_played=True) is None
    assert build_playback_state_update(manifest, position=0, fully_played=False) is None


def test_parse_resume_from_playback_state(book_multifile: dict[str, Any]) -> None:
    """Playback-state resume reconstructs the absolute position."""
    manifest = manifest_from_book(book_multifile)
    state = {
        "assetId": manifest["assets"][1]["assetId"],
        "positionMs": 10_000,
        "percentage": 40.0,
        "completed": False,
        "capturedAt": "2026-10-14T20:00:00.000Z",
        "revision": 3,
    }
    fully_played, position_ms, timestamp = parse_resume_position_from_playback_state(
        manifest, state
    )
    assert fully_played is False
    assert position_ms == 110_500
    assert timestamp is not None


def test_parse_resume_from_playback_state_rejects_unknown_preceding_duration(
    book_multifile: dict[str, Any],
) -> None:
    """Unknown preceding asset durations must not produce a wrong absolute position."""
    manifest = manifest_from_book(book_multifile)
    manifest["assets"][0]["durationMs"] = None
    state = {
        "assetId": manifest["assets"][1]["assetId"],
        "positionMs": 10_000,
        "percentage": 40.0,
        "completed": False,
    }
    with pytest.raises(ValueError, match="unknown duration"):
        parse_resume_position_from_playback_state(manifest, state)


def test_parse_resume_from_playback_state_rejects_unknown_asset(
    book_multifile: dict[str, Any],
) -> None:
    """Missing asset ids must not invent a seek from percentage."""
    manifest = manifest_from_book(book_multifile)
    state = {
        "assetId": "aud_missing",
        "positionMs": 10_000,
        "percentage": 40.0,
        "completed": False,
    }
    with pytest.raises(ValueError, match="unknown asset"):
        parse_resume_position_from_playback_state(manifest, state)


@pytest.mark.asyncio
async def test_get_resume_position_reads_playback_state(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Resume prefers GET playback-state over legacy audio-progress."""
    manifest = manifest_from_book(book_multifile)
    _client(provider).get_playback_state.return_value = {
        "assetId": manifest["assets"][1]["assetId"],
        "positionMs": 10_000,
        "percentage": 40.0,
        "completed": False,
        "revision": 2,
    }
    result = await provider.get_resume_position(str(book_multifile["id"]), MediaType.AUDIOBOOK)
    assert result[0] is False
    assert result[1] == 110_500
    _client(provider).get_playback_state.assert_awaited_once_with(book_multifile["id"])
    _client(provider).get_audio_progress.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_resume_position_client_error_falls_back(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Progress fetch errors defer to MA's own position."""
    _client(provider).get_audiobook_manifest.side_effect = ClientError("connection reset")
    with pytest.raises(NotImplementedError):
        await provider.get_resume_position(str(book_multifile["id"]), MediaType.AUDIOBOOK)


@pytest.mark.asyncio
async def test_get_resume_position_auth_error_falls_back(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Auth failures during resume must defer to MA, not abort playback prep."""
    _client(provider).get_audiobook_manifest.side_effect = LoginFailed("login rejected")
    with pytest.raises(NotImplementedError):
        await provider.get_resume_position(str(book_multifile["id"]), MediaType.AUDIOBOOK)


@pytest.mark.asyncio
async def test_get_resume_position_timeout_falls_back(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Total HTTP timeouts must also defer to MA's saved position."""
    _client(provider).get_audiobook_manifest.side_effect = TimeoutError("total timeout")
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
async def test_get_resume_position_legacy_fetches_fresh_book(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Legacy audio-progress resume must not use a stale in-memory catalog book."""
    files = _audio_files(book_multifile)
    _client(provider).get_playback_state.return_value = None
    _client(provider).get_audio_progress.return_value = {
        "currentFileId": files[1]["id"],
        "positionSeconds": 10.0,
        "percentage": 40.0,
        "isFinished": False,
    }
    with patch.object(provider, "_get_book", new_callable=AsyncMock) as get_book:
        get_book.return_value = book_multifile
        result = await provider.get_resume_position(str(book_multifile["id"]), MediaType.AUDIOBOOK)
    get_book.assert_awaited_once_with(str(book_multifile["id"]), fresh=True)
    assert result[0] is False
    assert result[1] == 110_500


@pytest.mark.asyncio
async def test_get_resume_position_legacy_unknown_file_falls_back(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Unknown currentFileId must raise NotImplementedError for MA's saved position."""
    _client(provider).get_playback_state.return_value = None
    _client(provider).get_audio_progress.return_value = {
        "currentFileId": 9999,
        "positionSeconds": 12.0,
        "percentage": 50.0,
    }
    with patch.object(provider, "_get_book", new_callable=AsyncMock) as get_book:
        get_book.return_value = book_multifile
        with pytest.raises(NotImplementedError):
            await provider.get_resume_position(str(book_multifile["id"]), MediaType.AUDIOBOOK)


@pytest.mark.asyncio
async def test_get_resume_position_legacy_unknown_duration_falls_back(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Unknown preceding duration must raise NotImplementedError for MA's saved position."""
    files = _audio_files(book_multifile)
    broken = {
        **book_multifile,
        "files": [
            {**file, "durationSeconds": None} if file.get("id") == files[0]["id"] else file
            for file in book_multifile["files"]
        ],
    }
    _client(provider).get_playback_state.return_value = None
    _client(provider).get_audio_progress.return_value = {
        "currentFileId": files[1]["id"],
        "positionSeconds": 10.0,
        "percentage": 40.0,
    }
    with patch.object(provider, "_get_book", new_callable=AsyncMock) as get_book:
        get_book.return_value = broken
        with pytest.raises(NotImplementedError):
            await provider.get_resume_position(str(book_multifile["id"]), MediaType.AUDIOBOOK)


@pytest.mark.asyncio
async def test_on_played_puts_playback_state(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Playing reports via PUT playback-state with asset id and in-asset ms."""
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
    _client(provider).put_playback_state.assert_awaited_once()
    kwargs = _client(provider).put_playback_state.await_args.kwargs
    manifest = manifest_from_book(book_multifile)
    assert kwargs["asset_id"] == manifest["assets"][1]["assetId"]
    assert kwargs["position_ms"] == 49_500
    assert kwargs["base_revision"] == 0
    assert kwargs["manifest_revision"] == manifest["revision"]
    _client(provider).upsert_audio_progress.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_played_retries_playback_state_conflict(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """A 409 revision conflict reloads baseRevision and retries."""
    conflict = ClientResponseError(
        request_info=Mock(real_url=URL("http://bo/x")),
        history=(),
        status=409,
        message="conflict",
    )
    client = _client(provider)
    client.get_playback_state = AsyncMock(side_effect=[None, {"revision": 4}, {"revision": 5}])
    client.put_playback_state = AsyncMock(side_effect=[conflict, {"revision": 5}])
    await provider.on_played(
        MediaType.AUDIOBOOK,
        str(book_multifile["id"]),
        fully_played=False,
        position=10,
        media_item=Mock(),
        is_playing=False,
    )
    assert client.put_playback_state.await_count == 2
    assert client.put_playback_state.await_args.kwargs["base_revision"] == 4


@pytest.mark.asyncio
async def test_on_played_discards_when_remote_captured_at_newer_after_409(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """After 409, do not retry with a stale position if remote capturedAt is newer."""
    conflict = ClientResponseError(
        request_info=Mock(real_url=URL("http://bo/x")),
        history=(),
        status=409,
        message="conflict",
    )
    report_time = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)
    client = _client(provider)
    client.get_playback_state = AsyncMock(
        side_effect=[
            {"revision": 1, "capturedAt": "2026-10-27T11:59:00.000Z"},
            # Newer than the patched report stamp (and than "now" if the patch misses).
            {"revision": 2, "capturedAt": "2026-10-27T12:00:30.000Z"},
        ]
    )
    client.put_playback_state = AsyncMock(side_effect=[conflict])
    with patch("music_assistant.providers.bookorbit.utc", return_value=report_time):
        await provider.on_played(
            MediaType.AUDIOBOOK,
            str(book_multifile["id"]),
            fully_played=False,
            position=10,
            media_item=Mock(),
            is_playing=False,
        )
    assert client.put_playback_state.await_count == 1
    assert client.put_playback_state.await_args.kwargs["captured_at"] == (
        report_time.isoformat().replace("+00:00", "Z")
    )
    assert client.get_playback_state.await_count == 2


@pytest.mark.asyncio
async def test_on_played_refetches_manifest_on_412(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """A 412 manifest mismatch reloads the manifest before retrying."""
    stale = ClientResponseError(
        request_info=Mock(real_url=URL("http://bo/x")),
        history=(),
        status=412,
        message="precondition failed",
    )
    client = _client(provider)
    first_manifest = manifest_from_book(book_multifile)
    second_manifest = {
        **first_manifest,
        "revision": "rev-2",
        "assets": [
            {**first_manifest["assets"][0], "durationMs": 100_000},
            {**first_manifest["assets"][1], "durationMs": 200_000},
        ],
        "totalDurationMs": 300_000,
    }
    client.get_audiobook_manifest = AsyncMock(side_effect=[first_manifest, second_manifest])
    client.get_playback_state = AsyncMock(side_effect=[None, {"revision": 1}])
    client.put_playback_state = AsyncMock(side_effect=[stale, {"revision": 2}])
    await provider.on_played(
        MediaType.AUDIOBOOK,
        str(book_multifile["id"]),
        fully_played=False,
        position=10,
        media_item=Mock(),
        is_playing=False,
    )
    assert client.get_audiobook_manifest.await_count == 2
    assert client.put_playback_state.await_count == 2
    assert client.put_playback_state.await_args.kwargs["manifest_revision"] == "rev-2"


@pytest.mark.asyncio
async def test_on_played_ignores_other_media(provider: BookOrbitProvider) -> None:
    """Non-audiobook callbacks are ignored."""
    await provider.on_played(MediaType.TRACK, "1", False, 5, Mock())
    _client(provider).put_playback_state.assert_not_awaited()
    _client(provider).upsert_audio_progress.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_played_complete_without_duration_uses_legacy(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """Mark-played with an unknown single-asset duration must set legacy percentage 100."""
    client = _client(provider)
    manifest = manifest_from_book(book_audiobook)
    manifest["assets"][0]["durationMs"] = None
    manifest["totalDurationMs"] = None
    book = dict(book_audiobook)
    book["files"] = [{**_audio_files(book)[0], "durationSeconds": None}]
    client.get_audiobook_manifest = AsyncMock(return_value=manifest)
    client.get_book = AsyncMock(return_value=book)
    client.get_playback_state = AsyncMock(return_value=None)
    await provider.on_played(
        MediaType.AUDIOBOOK,
        str(book_audiobook["id"]),
        fully_played=True,
        position=12,
        media_item=Mock(),
        is_playing=False,
    )
    client.put_playback_state.assert_not_awaited()
    client.upsert_audio_progress.assert_awaited_once()
    assert client.upsert_audio_progress.await_args.kwargs["percentage"] == 100.0


@pytest.mark.asyncio
async def test_legacy_progress_skips_multipart_when_manifest_unavailable(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Legacy multipart writes must not use unordered BookDetail file order."""
    _client(provider).get_book = AsyncMock(return_value=book_multifile)
    _client(provider).get_audiobook_manifest = AsyncMock(
        side_effect=ClientResponseError(
            request_info=Mock(real_url=URL("http://bo/manifest")),
            history=(),
            status=503,
            message="unavailable",
        )
    )
    await provider._sync_audio_progress_legacy(
        book_multifile["id"], position=150, fully_played=False
    )
    _client(provider).upsert_audio_progress.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_played_swallows_timeout(
    provider: BookOrbitProvider, book_multifile: dict[str, Any]
) -> None:
    """Progress timeouts are logged, not raised out of the play callback."""
    _client(provider).get_audiobook_manifest = AsyncMock(side_effect=TimeoutError("slow"))
    await provider.on_played(
        MediaType.AUDIOBOOK,
        str(book_multifile["id"]),
        fully_played=False,
        position=10,
        media_item=Mock(),
    )
    _client(provider).put_playback_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_part_request_only_serves_assets_of_the_book(
    provider: BookOrbitProvider, book_multifile: dict[str, Any], book_audiobook: dict[str, Any]
) -> None:
    """The stream proxy refuses asset ids that do not belong to a resolved session."""
    client = _client(provider)
    client.open_file = AsyncMock(side_effect=ClientError("unreachable"))
    book_id = book_multifile["id"]
    assets = manifest_from_book(book_multifile)["assets"]
    other_asset = manifest_from_book(book_audiobook)["assets"][0]["assetId"]

    async def status(query: str) -> int:
        request = make_mocked_request("GET", f"/bookorbit--test_part_stream?{query}")
        try:
            response = await provider._handle_part_request(request)
        except web.HTTPException as err:
            return err.status
        return response.status

    assert (
        await status(f"session_id=missing&book_id={book_id}&asset_id={assets[0]['assetId']}") == 404
    )
    assert await status(f"book_id={book_id}&asset_id={assets[0]['assetId']}") == 400
    details = await provider.get_stream_details(str(book_id), MediaType.AUDIOBOOK)
    assert isinstance(details.data, dict)
    session_id = details.data["session_id"]
    assert await status(f"session_id={session_id}&book_id={book_id}&asset_id={other_asset}") == 404
    assert (
        await status(f"session_id={session_id}&book_id={book_id}&asset_id=aud_not-a-real-asset")
        == 404
    )
    client.open_file.assert_not_awaited()
    assert (
        await status(f"session_id={session_id}&book_id={book_id}&asset_id={assets[0]['assetId']}")
        == 502
    )
    client.open_file.assert_awaited_once()
    await provider.on_streamed(details)
    # Grant survives on_streamed so seek/resume can still fetch parts.
    assert (
        await status(f"session_id={session_id}&book_id={book_id}&asset_id={assets[0]['assetId']}")
        == 502
    )
