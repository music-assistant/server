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

    artist = getattr(album, "artist_str", "")
    year = getattr(album, "year", None)
    # Build footer: "Artist · 2024" or just one
    footer: str | None = (
        f"{artist} · {year}" if artist and year else (artist or (str(year) if year else None))
    )
    url = f"{prefix}/msx/albums/{album.item_id}/tracks.json?provider={album.provider}"
    return MsxItem(
        title=album.name,
        title_footer=footer,
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
    owner = getattr(playlist, "owner", None)
    prov = getattr(playlist, "provider", None)
    footer: str | None = f"{owner} · {prov}" if owner and prov else (owner or prov or None)
    url = f"{prefix}/msx/playlists/{playlist.item_id}/tracks.json"
    return MsxItem(
        title=playlist.name,
        title_footer=footer,
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
    artist = getattr(track, "artist_str", "")
    image_url = get_image_url(track, provider)

    # Build footer: "Artist · 3:42" or just one of them
    footer: str | None = (
        f"{artist} · {duration_str}"
        if artist and duration_str
        else (artist or duration_str or None)
    )

    if playlist_url:
        # playlist: (not auto:) loads the playlist and executes the content
        # root's action ("player:play") which starts playback AND shows the
        # player in foreground.  playlist:auto: plays in background.
        # Items are rotated so the desired track is at index 0.
        action = f"playlist:{playlist_url}"
    else:
        audio_url = f"{prefix}/msx/audio/{player_id}.mp3?uri={quote(track.uri, safe='')}"
        action = f"audio:{append_device_param(audio_url, device_param)}"

    return MsxItem(
        title_header="{txt:msx-white:" + track.name + "}",
        title_footer=footer,
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

    # Rotate items so the desired track is at index 0.
    # playlist:auto: always starts from index 0, so rotation ensures
    # the clicked track plays first. Next/prev wrap naturally.
    if start_index > 0 and start_index < len(msx_items):
        msx_items = msx_items[start_index:] + msx_items[:start_index]

    return MsxContent(
        type="list",
        template=MsxTemplate(
            type="control",
            layout="0,0,12,1",
            image_filler="default",
        ),
        items=msx_items,
        action="player:play",
    )
