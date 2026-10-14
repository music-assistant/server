"""
Conversion between Music Assistant positions and BookOrbit audio progress.

BookOrbit 3.4 stores playback in the audiobook playback-state API (assetId + positionMs).
The legacy ``/books/{id}/audio-progress`` endpoint writes the same row but does not carry
manifest revision / operation ids the BookOrbit player expects.
Music Assistant works with one absolute position over the whole (multi-file) audiobook.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

FULLY_PLAYED_PERCENTAGE = 100.0


@dataclass(frozen=True)
class ProgressUpdate:
    """Body of a BookOrbit audio-progress PATCH (legacy)."""

    percentage: float
    current_file_id: int
    position_seconds: float


@dataclass(frozen=True)
class PlaybackStateUpdate:
    """Body fields for BookOrbit ``PUT /audiobooks/{id}/playback-state``."""

    asset_id: str
    position_ms: int
    percentage: float
    manifest_revision: str


def _duration(file: dict[str, Any]) -> float:
    return float(file.get("durationSeconds") or 0)


def _manifest_assets(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        (a for a in (manifest.get("assets") or []) if a.get("assetId")),
        key=lambda asset: int(asset.get("sequence") or 0),
    )


def build_progress_update(
    audio_files: list[dict[str, Any]], *, position: float, fully_played: bool
) -> ProgressUpdate | None:
    """Translate an absolute position (seconds) into a BookOrbit progress update."""
    if not audio_files:
        return None
    total = sum(_duration(f) for f in audio_files)
    position = max(float(position), 0.0)
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
    """Translate an absolute position (seconds) into a BookOrbit playback-state update."""
    assets = _manifest_assets(manifest)
    revision = manifest.get("revision")
    if not assets or not revision:
        return None
    position_ms = max(int(float(position) * 1000), 0)
    total_ms = manifest.get("totalDurationMs")
    if total_ms is None:
        total_ms = sum(int(a["durationMs"]) for a in assets if a.get("durationMs") is not None)
    if fully_played or (total_ms and position_ms >= int(total_ms)):
        last = assets[-1]
        last_dur = int(last["durationMs"]) if last.get("durationMs") is not None else 0
        return PlaybackStateUpdate(
            asset_id=str(last["assetId"]),
            position_ms=last_dur,
            percentage=FULLY_PLAYED_PERCENTAGE,
            manifest_revision=str(revision),
        )
    elapsed = 0
    chosen = assets[0]
    in_asset_ms = position_ms
    for asset in assets:
        duration_ms = int(asset["durationMs"]) if asset.get("durationMs") is not None else 0
        end = elapsed + duration_ms
        chosen = asset
        in_asset_ms = position_ms - elapsed
        if position_ms < end or duration_ms == 0:
            break
        elapsed = end
    if chosen.get("durationMs") is not None:
        in_asset_ms = min(max(in_asset_ms, 0), int(chosen["durationMs"]))
    else:
        in_asset_ms = max(in_asset_ms, 0)
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
    """Translate BookOrbit audio progress to (fully played, position in ms, timestamp)."""
    percentage = float(progress.get("percentage") or 0)
    fully_played = percentage >= FULLY_PLAYED_PERCENTAGE
    total = sum(_duration(f) for f in audio_files)

    position = None
    offset = 0.0
    for file in audio_files:
        if file.get("id") == progress.get("currentFileId"):
            position = offset + float(progress.get("positionSeconds") or 0)
            break
        offset += _duration(file)
    if position is None:  # stored file is gone; best effort via the percentage
        position = percentage / 100 * total

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
    """Translate BookOrbit playback-state to (fully played, absolute position in ms, timestamp)."""
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
        absolute_ms += int(asset["durationMs"]) if asset.get("durationMs") is not None else 0
    if not found:
        total_ms = int(manifest.get("totalDurationMs") or 0)
        absolute_ms = int(percentage / 100 * total_ms) if total_ms else 0
    timestamp: datetime | None = None
    if captured_at := state.get("capturedAt"):
        with suppress(TypeError, ValueError):
            timestamp = datetime.fromisoformat(str(captured_at))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
    return fully_played, absolute_ms, timestamp
