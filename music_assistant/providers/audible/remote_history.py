"""
Read Audible Whispersync timestamps without touching Music Assistant's database.

The Audible provider decides which library items are relevant; the music
controller owns playlog writes and protects newer local progress.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

MAX_ASINS_PER_REQUEST = 25
MAX_ASINS_QUERY_LENGTH = 480
MAX_HISTORY_AGE = timedelta(days=180)


@dataclass(frozen=True, slots=True)
class AudibleRemotePlay:
    """Last known external progress for an Audible audiobook."""

    asin: str
    position_seconds: int
    updated_at: datetime


def asin_chunks(asins: Iterable[str]) -> Iterator[list[str]]:
    """Yield deduplicated batches satisfying Audible's ASIN endpoint limits."""
    batch: list[str] = []
    length = 0
    seen: set[str] = set()
    for raw_asin in asins:
        asin = raw_asin.strip()
        if not asin or asin in seen:
            continue
        seen.add(asin)
        if len(asin) > MAX_ASINS_QUERY_LENGTH:
            continue
        extra = len(asin) + (1 if batch else 0)
        if batch and (
            len(batch) >= MAX_ASINS_PER_REQUEST or length + extra > MAX_ASINS_QUERY_LENGTH
        ):
            yield batch
            batch = []
            length = 0
            extra = len(asin)
        batch.append(asin)
        length += extra
    if batch:
        yield batch


def parse_timestamp(raw: Any) -> datetime | None:
    """Parse an API timestamp into an aware UTC datetime (no invented timestamps)."""
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, (str, int, float)) and raw:
        try:
            if isinstance(raw, (int, float)):
                parsed = datetime.fromtimestamp(raw / 1000 if raw > 1e12 else raw, tz=UTC)
            else:
                value = raw.strip()
                if value.isdecimal():
                    return parse_timestamp(int(value))
                parsed = datetime.fromisoformat(value)
        except TypeError, ValueError, OverflowError, OSError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_lastposition(row: Mapping[str, Any]) -> tuple[int, datetime | None] | None:
    """Decode a last-heard annotation, preserving undated resume positions."""
    position = row.get("last_position_heard")
    if not isinstance(position, Mapping):
        return None
    try:
        position_ms = int(position.get("position_ms", 0))
    except TypeError, ValueError, OverflowError, OSError:
        return None
    if position_ms < 0:
        return None
    timestamp = next(
        (
            ts
            for field in ("last_updated", "reported_time", "last_updated_time", "timestamp")
            if (ts := parse_timestamp(position.get(field) or row.get(field))) is not None
        ),
        None,
    )
    return position_ms, timestamp


def parse_lastpositions(payload: Mapping[str, Any] | None) -> list[AudibleRemotePlay]:
    """Decode a batch response; skip records with no reliable time or position."""
    if not isinstance(payload, Mapping):
        return []
    rows = payload.get("asin_last_position_heard_annots")
    if not isinstance(rows, list):
        return []
    parsed: list[AudibleRemotePlay] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        position = row.get("last_position_heard")
        if not isinstance(position, Mapping):
            continue
        asin = row.get("asin") or position.get("asin")
        if not isinstance(asin, str) or not asin:
            continue
        annotation = parse_lastposition(row)
        if annotation is None:
            continue
        position_ms, timestamp = annotation
        if position_ms < 1000 or timestamp is None:
            continue
        parsed.append(AudibleRemotePlay(asin, position_ms // 1000, timestamp))
    return parsed


def eligible_external_plays(
    entries: Iterable[AudibleRemotePlay],
    watermarks: Mapping[str, str | None],
    *,
    now: datetime,
) -> list[AudibleRemotePlay]:
    """Exclude unchanged, future, and stale plays; oldest first for stable imports."""
    cutoff = now.astimezone(UTC) - MAX_HISTORY_AGE
    # The endpoint normally returns one row per ASIN; tolerate duplicate rows
    # without writing the same book multiple times during a single poll.
    latest: dict[str, AudibleRemotePlay] = {}
    for entry in entries:
        timestamp = entry.updated_at
        previous = parse_timestamp(watermarks.get(entry.asin))
        if timestamp < cutoff or timestamp > now or (previous and timestamp <= previous):
            continue
        if (current := latest.get(entry.asin)) is None or timestamp > current.updated_at:
            latest[entry.asin] = entry
    return sorted(latest.values(), key=lambda item: item.updated_at)
