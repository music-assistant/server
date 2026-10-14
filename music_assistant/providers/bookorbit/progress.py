"""
Conversion between Music Assistant positions and BookOrbit audio progress.

BookOrbit stores progress as the current audio file, the position *inside* that file and a
percentage (0-100) of the whole book. Music Assistant works with one absolute position
over the whole (multi-file) audiobook.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

FULLY_PLAYED_PERCENTAGE = 100.0


@dataclass(frozen=True)
class ProgressUpdate:
    """Body of a BookOrbit audio-progress PATCH."""

    percentage: float
    current_file_id: int
    position_seconds: float


def _duration(file: dict[str, Any]) -> float:
    return float(file.get("durationSeconds") or 0)


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
    if updated_at := progress.get("updatedAt"):
        with suppress(TypeError, ValueError):
            timestamp = datetime.fromisoformat(str(updated_at))
    return fully_played, int(position * 1000), timestamp
