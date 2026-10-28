"""Conversion between Music Assistant positions and BookOrbit progress payloads."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

FULLY_PLAYED_PERCENTAGE = 100.0


@dataclass(frozen=True)
class ProgressUpdate:
    """Body of a BookOrbit legacy audio-progress PATCH."""

    percentage: float
    current_file_id: int
    position_seconds: float


@dataclass(frozen=True)
class PlaybackStateUpdate:
    """Body fields for BookOrbit audiobook playback-state updates."""

    asset_id: str
    position_ms: int
    percentage: float
    manifest_revision: str


def _duration(file: dict[str, Any]) -> float:
    """Return a file's duration in seconds."""
    duration = file.get("durationSeconds")
    if duration is None:
        raise ValueError("BookOrbit audio file has unknown duration")
    return float(duration)


def _require_known_file_durations(audio_files: list[dict[str, Any]]) -> None:
    """Raise when any audio part duration is unknown (unsafe for absolute offsets)."""
    if any(file.get("durationSeconds") is None for file in audio_files):
        raise ValueError("BookOrbit audio file has unknown duration")


def _manifest_assets(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return manifest assets sorted by sequence."""
    return sorted(
        (a for a in (manifest.get("assets") or []) if a.get("assetId")),
        key=lambda asset: int(asset.get("sequence") or 0),
    )


def build_progress_update(
    audio_files: list[dict[str, Any]], *, position: float, fully_played: bool
) -> ProgressUpdate | None:
    """
    Translate an absolute position into a legacy BookOrbit progress update.

    :param audio_files: Ordered audio files for the book.
    :param position: Absolute position in seconds across the whole audiobook.
    :param fully_played: Whether playback is complete.
    :returns: Progress payload, or ``None`` when there are no audio files.
    """
    if not audio_files:
        return None
    position = max(float(position), 0.0)
    # Single-file books can complete/reset via percentage without a measured duration.
    if any(file.get("durationSeconds") is None for file in audio_files):
        if len(audio_files) != 1:
            raise ValueError("BookOrbit audio file has unknown duration")
        only = audio_files[0]
        return ProgressUpdate(
            percentage=FULLY_PLAYED_PERCENTAGE if fully_played else 0.0,
            current_file_id=int(only["id"]),
            position_seconds=position,
        )
    total = sum(_duration(f) for f in audio_files)
    if fully_played or (total and position >= total):
        last = audio_files[-1]
        return ProgressUpdate(
            percentage=FULLY_PLAYED_PERCENTAGE,
            current_file_id=int(last["id"]),
            position_seconds=_duration(last) if total else position,
        )
    start = 0.0
    chosen, in_file = audio_files[0], position
    for file in audio_files:
        end = start + _duration(file)
        chosen, in_file = file, position - start
        if position < end:
            break
        start = end
    return ProgressUpdate(
        percentage=min(position / total * 100, 100.0) if total else 0.0,
        current_file_id=int(chosen["id"]),
        position_seconds=max(in_file, 0.0),
    )


def build_playback_state_update(
    manifest: dict[str, Any], *, position: float, fully_played: bool
) -> PlaybackStateUpdate | None:
    """
    Translate an absolute position into a BookOrbit playback-state update.

    :param manifest: Audiobook playback manifest.
    :param position: Absolute position in seconds across the whole audiobook.
    :param fully_played: Whether playback is complete.
    :returns: Playback-state payload, or ``None`` when assets/revision are missing.
    """
    assets = _manifest_assets(manifest)
    revision = manifest.get("revision")
    if not assets or not revision:
        return None
    position_ms = max(int(float(position) * 1000), 0)
    if len(assets) == 1 and assets[0].get("durationMs") is None:
        # PUT playback-state has no completion flag, so mark-played / reset must use
        # the legacy percentage endpoint. Ordinary mid-book seeks can still send positionMs.
        if fully_played or position_ms <= 0:
            return None
        return PlaybackStateUpdate(
            asset_id=str(assets[0]["assetId"]),
            position_ms=position_ms,
            percentage=0.0,
            manifest_revision=str(revision),
        )
    # Multipart positions require known durations to locate the current asset.
    if any(asset.get("durationMs") is None for asset in assets):
        return None
    total_ms = manifest.get("totalDurationMs")
    if total_ms is None:
        total_ms = sum(int(a["durationMs"]) for a in assets)
    if fully_played or (total_ms and position_ms >= int(total_ms)):
        last = assets[-1]
        return PlaybackStateUpdate(
            asset_id=str(last["assetId"]),
            position_ms=int(last["durationMs"]),
            percentage=FULLY_PLAYED_PERCENTAGE,
            manifest_revision=str(revision),
        )
    elapsed = 0
    chosen = assets[0]
    in_asset_ms = position_ms
    for asset in assets:
        duration_ms = int(asset["durationMs"])
        end = elapsed + duration_ms
        chosen = asset
        in_asset_ms = position_ms - elapsed
        if position_ms < end:
            break
        elapsed = end
    in_asset_ms = min(max(in_asset_ms, 0), int(chosen["durationMs"]))
    percentage = min(position_ms / float(total_ms) * 100, 100.0) if total_ms else 0.0
    return PlaybackStateUpdate(
        asset_id=str(chosen["assetId"]),
        position_ms=int(in_asset_ms),
        percentage=percentage,
        manifest_revision=str(revision),
    )


def parse_resume_position(
    audio_files: list[dict[str, Any]], progress: dict[str, Any]
) -> tuple[bool, int, datetime | None]:
    """
    Translate legacy BookOrbit progress into a Music Assistant resume position.

    :param audio_files: Ordered audio files for the book.
    :param progress: Legacy audio-progress payload.
    :returns: Fully played flag, absolute position in ms, and optional timestamp.
    """
    percentage = float(progress.get("percentage") or 0)
    fully_played = percentage >= FULLY_PLAYED_PERCENTAGE
    _require_known_file_durations(audio_files)

    position = None
    offset = 0.0
    for file in audio_files:
        if file.get("id") == progress.get("currentFileId"):
            position = offset + float(progress.get("positionSeconds") or 0)
            break
        offset += _duration(file)
    if position is None:
        # Do not invent an absolute seek from percentage when the file is gone.
        raise ValueError("BookOrbit progress references an unknown file")

    timestamp: datetime | None = None
    if updated_at := progress.get("updatedAt") or progress.get("capturedAt"):
        with suppress(TypeError, ValueError):
            timestamp = datetime.fromisoformat(str(updated_at))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
    return fully_played, int(position * 1000), timestamp


def parse_resume_position_from_playback_state(
    manifest: dict[str, Any], state: dict[str, Any]
) -> tuple[bool, int, datetime | None]:
    """
    Translate BookOrbit playback-state into a Music Assistant resume position.

    :param manifest: Audiobook playback manifest.
    :param state: Playback-state payload.
    :returns: Fully played flag, absolute position in ms, and optional timestamp.
    """
    percentage = float(state.get("percentage") or 0)
    fully_played = bool(state.get("completed")) or percentage >= FULLY_PLAYED_PERCENTAGE
    asset_id = state.get("assetId")
    position_in_asset_ms = int(state.get("positionMs") or 0)
    absolute_ms = 0
    found = False
    for asset in _manifest_assets(manifest):
        if asset.get("assetId") == asset_id:
            absolute_ms += position_in_asset_ms
            found = True
            break
        if asset.get("durationMs") is None:
            raise ValueError("BookOrbit manifest has an asset with unknown duration")
        absolute_ms += int(asset["durationMs"])
    if not found:
        # Do not invent an absolute seek from percentage when the asset is gone.
        raise ValueError("BookOrbit playback state references an unknown asset")
    timestamp: datetime | None = None
    if captured_at := state.get("capturedAt"):
        with suppress(TypeError, ValueError):
            timestamp = datetime.fromisoformat(str(captured_at))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
    return fully_played, absolute_ms, timestamp
