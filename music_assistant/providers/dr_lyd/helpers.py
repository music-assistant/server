"""Helper functions for the DR LYD provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import MediaNotFoundError

from music_assistant.helpers.datetime import from_iso_string

from .constants import ID_PATTERN, IMAGE_URL
from .models import NowPlaying

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .models import Channel, Programme, Series, Track


def clean_text(value: Any) -> str | None:
    """
    Return the value stripped, if it is text with something in it.

    :param value: Any value of a document of DR.
    """
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def parse_time(value: Any) -> float | None:
    """
    Return the epoch seconds of an ISO 8601 time that carries a UTC offset, else None.

    :param value: Any value of a document of DR.
    """
    try:
        moment = from_iso_string(value)
    except TypeError, ValueError:
        return None
    return moment.timestamp() if moment.tzinfo else None


def parse_id(value: Any) -> str | None:
    """
    Return the value if it is a production number of DR, which is made of digits, else None.

    :param value: Any value of a document of DR.
    """
    return value if isinstance(value, str) and ID_PATTERN.fullmatch(value) else None


def require_id(item_id: str, kind: str) -> str:
    """
    Return the id, or raise when it cannot be the production number of an item of DR.

    :param item_id: The id to check.
    :param kind: What the id is meant to identify, for the message of the error.
    :raises MediaNotFoundError: When the id is no production number.
    """
    if parse_id(item_id) is None:
        raise MediaNotFoundError(f"{item_id!r} is not the id of a DR {kind}")
    return item_id


def get_items(document: Any) -> list[Any]:
    """
    Return the items of a list document of DR, or none when it is not one.

    :param document: A page of a list, as DR serves it.
    """
    items = document.get("items") if isinstance(document, dict) else None
    return items if isinstance(items, list) else []


def get_image_url(assets: Any, *targets: str) -> str | None:
    """
    Return the url of the square picture among the image assets, if any.

    A picture whose target is named earlier in targets wins, the others follow ordered by id.

    :param assets: The image assets of an item of DR, each with an id, a target and a ratio.
    :param targets: The targets of the preferred pictures, the first one is the most wanted.
    """
    candidates = []
    for asset in assets if isinstance(assets, list) else []:
        if not isinstance(asset, dict) or asset.get("ratio") != "1:1":
            continue
        if image_id := clean_text(asset.get("id")):
            target = asset.get("target")
            rank = targets.index(target) if target in targets else len(targets)
            candidates.append((rank, image_id))
    return IMAGE_URL.format(min(candidates)[1]) if candidates else None


def search_series(series: Iterable[Series], query: str, limit: int) -> list[Series]:
    """
    Return the series matching all words of the query in title or description.

    Matching is case-insensitive. Titles starting with the query come first, then other
    title matches, then description matches; each group is ordered by title.

    :param series: The series to search.
    :param query: The words to find.
    :param limit: The maximum number of series to return.
    """
    folded_query = " ".join(query.casefold().split())
    words = folded_query.split()
    if not words:
        return []
    ranked: list[tuple[int, str, Series]] = []
    for entry in series:
        title = entry.title.casefold()
        if all(word in title for word in words):
            rank = 0 if title.startswith(folded_query) else 1
        elif all(word in f"{title}\n{(entry.description or '').casefold()}" for word in words):
            rank = 2
        else:
            continue
        ranked.append((rank, title, entry))
    ranked.sort(key=lambda item: item[:2])
    return [entry for _, _, entry in ranked[:limit]]


def recently_updated(series: Iterable[Series], limit: int) -> list[Series]:
    """
    Return the series whose latest episode is the newest, the newest series first.

    :param series: The series to pick from.
    :param limit: The maximum number of series to return.
    """
    published = [entry for entry in series if entry.episodes > 0 and entry.latest is not None]
    published.sort(key=lambda entry: entry.latest or 0.0, reverse=True)
    return published[:limit]


def search_channels(channels: Iterable[Channel], query: str, limit: int) -> list[Channel]:
    """
    Return the channels whose name holds every word of the query, ignoring case.

    :param channels: The channels to search.
    :param query: The words to find.
    :param limit: The maximum number of channels to return.
    """
    words = query.casefold().split()
    if not words:
        return []
    matches = [
        channel for channel in channels if all(word in channel.name.casefold() for word in words)
    ]
    return matches[:limit]


def find_track(tracks: Iterable[Track], at: float) -> Track | None:
    """
    Return the track playing at the time, which is the latest one to have started.

    :param tracks: The tracks DR has played on a channel.
    :param at: The time to look at, in epoch seconds.
    """
    playing = [track for track in tracks if track.start <= at < track.end]
    return max(playing, key=lambda track: track.start, default=None)


def find_programme(
    programmes: Iterable[Programme], channel_slug: str, at: float
) -> Programme | None:
    """
    Return the programme on air on the channel at the time, none when it is over.

    :param programmes: The programmes on air now, one for every channel.
    :param channel_slug: The slug of the channel.
    :param at: The time to look at, in epoch seconds.
    """
    programme = next((entry for entry in programmes if entry.channel_slug == channel_slug), None)
    return programme if programme is not None and programme.end > at else None


def get_now_playing(
    channel: Channel, tracks: Iterable[Track], programmes: Iterable[Programme], at: float
) -> NowPlaying:
    """
    Return the track the channel plays at the time, else its programme, else the channel.

    :param channel: The channel to look at.
    :param tracks: The tracks DR has played on the channel.
    :param programmes: The programmes on air now, one for every channel.
    :param at: The time to look at, in epoch seconds.
    """
    programme = find_programme(programmes, channel.slug, at)
    picture = programme.image_url if programme else None
    if track := find_track(tracks, at):
        return NowPlaying(track.title, track.artist, picture)
    if programme:
        return NowPlaying(programme.title, None, picture)
    return NowPlaying(channel.name)
