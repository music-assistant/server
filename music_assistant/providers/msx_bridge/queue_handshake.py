"""Queue ↔ MSX native playlist handshake."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from music_assistant_models.errors import (
    InvalidDataError,
    InvalidProviderURI,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import Track

from music_assistant.helpers.uri import parse_uri

from .mappers import PlaylistTrack

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant

    from .player import MSXPlayer
    from .provider import MSXBridgeProvider

logger = logging.getLogger(__name__)


async def is_media_item_uri(uri: str) -> bool:
    """Check whether a URI names a non-builtin media item."""
    if "://" not in uri:
        return False
    try:
        _, provider_instance_id_or_domain, _ = await parse_uri(uri)
    except InvalidProviderURI:
        return False
    return provider_instance_id_or_domain != "builtin"


def find_uri_in_active_queue(
    mass: MusicAssistant,
    player_id: str,
    uri: str,
    queue_item_id: str | None = None,
) -> tuple[str, str] | None:
    """Return the active queue and item IDs matching the request."""
    queue = mass.player_queues.get_active_queue(player_id)
    if queue is None:
        return None
    items = mass.player_queues.items(queue.queue_id, limit=queue.items)
    for item in items:
        if item.uri == uri and (queue_item_id is None or item.queue_item_id == queue_item_id):
            return queue.queue_id, item.queue_item_id
    return None


def current_media_matches_uri(
    mass: MusicAssistant,
    player: MSXPlayer,
    track_uri: str,
    queue_item_id: str | None = None,
) -> bool:
    """Check whether current media matches the requested queue item."""
    media = player.current_media
    if not media or not media.source_id or not media.queue_item_id:
        return False
    if queue_item_id is not None and media.queue_item_id != queue_item_id:
        return False
    queue_item = mass.player_queues.get_item(media.source_id, media.queue_item_id)
    return queue_item is not None and queue_item.uri == track_uri


def queue_items_to_tracks(queue_items: Sequence[QueueItem]) -> list[PlaylistTrack]:
    """Adapt MA queue items into native-playlist rendering metadata."""
    tracks: list[PlaylistTrack] = []
    for qi in queue_items:
        mi = qi.media_item
        tracks.append(
            PlaylistTrack(
                name=mi.name if mi else qi.name,
                uri=qi.uri,
                duration=(mi.duration if mi else qi.duration) or 0,
                artist=mi.artist_str if isinstance(mi, Track) else "",
                image=qi.image,
                queue_item_id=qi.queue_item_id,
            )
        )
    return tracks


async def prepare_msx_audio(
    provider: MSXBridgeProvider,
    player: MSXPlayer,
    uri: str,
    *,
    from_playlist: bool,
    queue_item_id: str | None,
) -> PlayerMedia:
    """
    Resolve the PlayerMedia MSX should stream for this URI.

    Selects a queued item (or reuses current media) so MA-driven play
    and MSX-driven /msx/audio share one implementation.
    """
    provider.on_player_activity(player.player_id)
    async with player._prepare_lock:
        return await _prepare_msx_audio_locked(
            provider,
            player,
            uri,
            from_playlist=from_playlist,
            queue_item_id=queue_item_id,
        )


async def _prepare_msx_audio_locked(
    provider: MSXBridgeProvider,
    player: MSXPlayer,
    uri: str,
    *,
    from_playlist: bool,
    queue_item_id: str | None,
) -> PlayerMedia:
    """Select the queued item under the per-player lock."""
    queue_item = find_uri_in_active_queue(provider.mass, player.player_id, uri, queue_item_id)
    if queue_item is None:
        raise InvalidDataError("Invalid uri parameter")

    if from_playlist and current_media_matches_uri(provider.mass, player, uri, queue_item_id):
        logger.debug("Queue-driven: using current_media for %s", uri)
        media = player.current_media
        if media is None:
            raise ResourceTemporarilyUnavailable("Playback setup timeout")
        return media

    player.expect_new_media()

    if from_playlist:
        with player.suppress_ws_notify():
            await provider.mass.player_queues.play_index(*queue_item)
    else:
        await provider.mass.player_queues.play_index(*queue_item)

    media = await player.wait_for_media(timeout=10.0)
    if not media:
        raise ResourceTemporarilyUnavailable("Playback setup timeout")
    if media.source_id:
        player.mark_queue_playback(media.source_id)
    return media
