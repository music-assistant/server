"""Mappers for converting Music Assistant objects to MSX models."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .models import MsxContent, MsxItem, MsxTemplate

if TYPE_CHECKING:
    from .provider import MSXBridgeProvider

logger = logging.getLogger(__name__)


def append_device_param(url: str, device_param: str) -> str:
    """Append device_id to URL if present."""
    if not device_param:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{device_param}"


def get_image_url(item: Any, provider: MSXBridgeProvider) -> str | None:
    """Get an image URL for a media item."""
    if hasattr(item, "image") and item.image:
        return provider.mass.metadata.get_image_url(item.image)
    return None


async def get_album_image_fallback(album: Any, provider: MSXBridgeProvider) -> str | None:
    """Get album image from its first track (albums often lack metadata images)."""
    try:
        tracks = await provider.mass.music.albums.tracks(album.item_id, album.provider)
        for track in tracks:
            if hasattr(track, "image") and track.image:
                return provider.mass.metadata.get_image_url(track.image)
    except Exception:
        logger.debug("Failed to fetch album image fallback for %s", album.item_id)
    return None


async def map_album_to_msx(
    album: Any, prefix: str, provider: MSXBridgeProvider, device_param: str = ""
) -> MsxItem:
    """Map a MA Album to an MSX Item."""
    image = get_image_url(album, provider)
    if not image:
        image = await get_album_image_fallback(album, provider)

    url = f"{prefix}/msx/albums/{album.item_id}/tracks.json?provider={album.provider}"
    return MsxItem(
        title=album.name,
        label=getattr(album, "artist_str", ""),
        image=image,
        action=f"content:{append_device_param(url, device_param)}",
    )


def map_artist_to_msx(
    artist: Any, prefix: str, provider: MSXBridgeProvider, device_param: str = ""
) -> MsxItem:
    """Map a MA Artist to an MSX Item."""
    url = f"{prefix}/msx/artists/{artist.item_id}/albums.json"
    return MsxItem(
        title=artist.name,
        image=get_image_url(artist, provider),
        action=f"content:{append_device_param(url, device_param)}",
    )


def map_playlist_to_msx(
    playlist: Any, prefix: str, provider: MSXBridgeProvider, device_param: str = ""
) -> MsxItem:
    """Map a MA Playlist to an MSX Item."""
    url = f"{prefix}/msx/playlists/{playlist.item_id}/tracks.json"
    return MsxItem(
        title=playlist.name,
        image=get_image_url(playlist, provider),
        action=f"content:{append_device_param(url, device_param)}",
    )


def map_track_to_msx(
    track: Any,
    prefix: str,
    player_id: str,
    provider: MSXBridgeProvider,
    device_param: str = "",
    playlist_url: str | None = None,
) -> MsxItem:
    """Map a MA Track to an MSX Item."""
    duration = getattr(track, "duration", 0) or 0
    duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else ""
    label = getattr(track, "artist_str", "")
    if duration_str:
        label = f"{label} · {duration_str}" if label else duration_str
    image_url = get_image_url(track, provider)

    if playlist_url:
        action = f"playlist:auto:{playlist_url}"
    else:
        audio_url = f"{prefix}/msx/audio/{player_id}.mp3?uri={quote(track.uri, safe='')}"
        action = f"audio:{append_device_param(audio_url, device_param)}"

    return MsxItem(
        title=track.name,
        label=label,
        player_label=track.name,
        duration=duration,
        image=image_url,
        background=image_url,
        action=action,
    )


def map_tracks_to_msx_playlist(
    tracks: list[Any],
    start_index: int,
    prefix: str,
    player_id: str,
    provider: MSXBridgeProvider,
    device_param: str = "",
) -> MsxContent:
    """Map a list of MA Track objects to an MSX Content page for playlist playback.

    MSX ``playlist:{URL}`` loads a standard Content Root Object.
    Each item uses ``action: "audio:{URL}"`` so MSX can play them sequentially.
    The page-level ``action`` auto-starts playback at the requested track index.
    """
    msx_items = []
    for track in tracks:
        duration = getattr(track, "duration", 0) or 0
        duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else ""
        artist = getattr(track, "artist_str", "")
        label = f"{artist} · {duration_str}" if artist and duration_str else artist or duration_str
        image_url = get_image_url(track, provider)

        encoded_uri = quote(track.uri, safe="")
        audio_url = f"{prefix}/msx/audio/{player_id}.mp3?uri={encoded_uri}&from_playlist=1"
        audio_url = append_device_param(audio_url, device_param)

        msx_items.append(
            MsxItem(
                title=track.name,
                label=label,
                player_label=track.name,
                image=image_url,
                background=image_url,
                duration=duration,
                action=f"audio:{audio_url}",
            )
        )

    # Auto-start playback at the requested track index
    auto_action = "player:play"
    if start_index > 0:
        auto_action = f"player:goto:index:{start_index}"

    return MsxContent(
        type="list",
        template=MsxTemplate(
            type="control",
            layout="0,0,12,1",
            image_filler="default",
        ),
        items=msx_items,
        action=auto_action,
    )
