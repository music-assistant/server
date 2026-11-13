"""Batched library lookups of the field values the media item models do not carry."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType

from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user

from .fields import AUDIO_QUALITIES, quality_rank
from .models import ItemKind

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# the ids of one lookup; each id is a bound parameter of the query
CHUNK_SIZE = 500


async def load_values(
    mass: MusicAssistant, kind: ItemKind, item_ids: list[int], names: frozenset[str]
) -> dict[int, dict[str, Any]]:
    """
    Return the asked values of library items, a few queries for all of them.

    Every id gets an entry. A play count and a track count are 0 when there is no row;
    a last played and a date favorited are None when the item was never played or is
    not a favorite of the user the call serves. The plays and the audio quality of an
    album include those of its library tracks.

    :param mass: The Music Assistant instance.
    :param kind: The kind of the items.
    :param item_ids: The library ids of the items.
    :param names: The names of the fields to load.
    """
    result: dict[int, dict[str, Any]] = {item_id: {} for item_id in item_ids}
    for start in range(0, len(item_ids), CHUNK_SIZE):
        chunk = item_ids[start : start + CHUNK_SIZE]
        if names & {"play_count", "last_played"}:
            await _load_play_stats(mass, kind, chunk, result)
        if "audio_quality" in names and kind == ItemKind.ALBUM:
            await _load_album_qualities(mass, chunk, result)
        if "track_count" in names and kind == ItemKind.ALBUM:
            await _load_track_counts(mass, chunk, result)
        if "date_favorited" in names:
            await _load_favorite_dates(mass, kind, chunk, result)
    return result


async def _load_play_stats(
    mass: MusicAssistant, kind: ItemKind, item_ids: list[int], result: dict[int, dict[str, Any]]
) -> None:
    """Add the play count and the last played moment of all users; an album adds its tracks."""
    controller = mass.music.get_controller(MediaType(kind.value))
    stats = await controller.get_library_play_stats(item_ids)
    for item_id in item_ids:
        play_count, last_played = stats.get(item_id, (0, 0))
        result[item_id].update(play_count=play_count, last_played=_moment(last_played))


async def _load_track_counts(
    mass: MusicAssistant, item_ids: list[int], result: dict[int, dict[str, Any]]
) -> None:
    """Add the number of library tracks of albums."""
    counts = await mass.music.albums.get_library_track_counts(item_ids)
    for item_id in item_ids:
        result[item_id]["track_count"] = counts.get(item_id, 0)


async def _load_album_qualities(
    mass: MusicAssistant, item_ids: list[int], result: dict[int, dict[str, Any]]
) -> None:
    """Add the best quality of the available mappings of albums and of their library tracks."""
    formats = await mass.music.albums.get_library_audio_formats(item_ids)
    for item_id in item_ids:
        ranks = [
            rank
            for audio_format in formats.get(item_id, [])
            if (
                rank := quality_rank(
                    audio_format.content_type,
                    audio_format.codec_type,
                    audio_format.bit_depth,
                    audio_format.sample_rate,
                )
            )
            is not None
        ]
        result[item_id]["audio_quality"] = [(AUDIO_QUALITIES[max(ranks)],)] if ranks else []


async def _load_favorite_dates(
    mass: MusicAssistant, kind: ItemKind, item_ids: list[int], result: dict[int, dict[str, Any]]
) -> None:
    """Add the moment the user the call serves liked the items, like their favorite flag."""
    for item_id in item_ids:
        result[item_id]["date_favorited"] = None
    user = get_current_user()
    if user is None:
        return
    liked = await mass.music.favorites.liked_timestamps(
        user.user_id, MediaType(kind.value), item_ids
    )
    for item_id, timestamp in liked.items():
        if (values := result.get(item_id)) is not None:
            values["date_favorited"] = _moment(timestamp)


def _moment(stamp: int) -> datetime | None:
    """Return a stored unix timestamp as a moment, or None when it is not set."""
    return datetime.fromtimestamp(stamp, UTC) if stamp else None
