"""Mappers for converting Music Assistant objects to MSX models."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItem,
    Playlist,
    Track,
)

from .models import MsxContent, MsxItem, MsxTemplate

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from music_assistant_models.media_items import (
        MediaItemImage,
        PlayableMediaItemType,
    )

    from .provider import MSXBridgeProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlaylistTrack:
    """Playback metadata needed to render one MSX native playlist item."""

    name: str
    uri: str
    duration: int
    artist: str
    image: MediaItemImage | None
    queue_item_id: str | None = None


def playlist_tracks_from_media_items(
    items: Iterable[PlayableMediaItemType | ItemMapping],
) -> list[PlaylistTrack]:
    """Adapt playable MA media items for MSX native playlist rendering."""
    tracks: list[PlaylistTrack] = []
    for item in items:
        if item.uri is None:
            continue
        tracks.append(
            PlaylistTrack(
                name=item.name,
                uri=item.uri,
                duration=0 if isinstance(item, ItemMapping) else item.duration or 0,
                artist=item.artist_str if isinstance(item, Track) else "",
                image=item.image,
            )
        )
    return tracks


def queue_nav_properties(player_id: str, prefix: str = "") -> dict[str, str]:
    """MSX next/prev/complete must go through the MA queue, not the native list."""
    next_action = f"execute:{prefix}/api/next/{player_id}"
    prev_action = f"execute:{prefix}/api/previous/{player_id}"
    return {
        "button:next:icon": "default",
        "button:next:action": next_action,
        "button:prev:icon": "default",
        "button:prev:action": prev_action,
        "trigger:complete": next_action,
    }


def append_device_param(url: str, device_param: str) -> str:
    """Append device_id to URL if present."""
    if not device_param:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{device_param}"


def container_uri(kind: str, item_id: str, provider: str = "library") -> str:
    """Build a MA media URI for an album, playlist, or similar container."""
    domain = "library" if provider in {"", "library"} else provider
    return f"{domain}://{kind}/{item_id}"


def play_context_action(
    prefix: str,
    player_id: str,
    context_uri: str,
    start: int,
    device_param: str = "",
    track_uri: str | None = None,
) -> str:
    """Build a request that enqueues a container/track into the MA queue."""
    url = f"{prefix}/api/play-context/{player_id}?uri={quote(context_uri, safe='')}&start={start}"
    if track_uri:
        url += f"&track={quote(track_uri, safe='')}"
    return f"execute:{append_device_param(url, device_param)}"


def sort_album_tracks(tracks: list[Track]) -> list[Track]:
    """
    Sort album tracks deterministically.

    Include stable track identity so separate display and playlist requests
    agree even when disc, track number, and title are identical.
    """
    return sorted(
        tracks,
        key=lambda t: (
            t.disc_number,
            t.track_number,
            t.name,
            t.uri or t.item_id,
        ),
    )


def dump_msx(content: MsxContent) -> dict[str, Any]:
    """Serialize an MSX content page for an HTTP JSON response."""
    return content.model_dump(by_alias=True, exclude_none=True)


def msx_list_page(
    headline: str,
    items: Sequence[MsxItem],
    *,
    empty_title: str,
    layout: str,
    template_type: str = "separate",
    color: str = "msx-glass",
    image_width: float | None = None,
) -> MsxContent:
    """Build a standard MSX list page from already-mapped items."""
    template_kwargs: dict[str, Any] = {
        "type": template_type,
        "layout": layout,
        "color": color,
    }
    if image_width is not None:
        template_kwargs["image_width"] = image_width
    return MsxContent(
        headline=headline,
        template=MsxTemplate(**template_kwargs),
        items=list(items) if items else [MsxItem(title=empty_title)],
    )


def get_image_url(
    item: MediaItem | ItemMapping | PlaylistTrack,
    provider: MSXBridgeProvider,
    prefer_proxy: bool = False,
) -> str | None:
    """
    Get an image URL for a media item.

    :param prefer_proxy: Route the image through the MA imageproxy so the URL
        points at the MA server (rather than a remote CDN). Needed for the
        party QR-cover compositor, which only accepts MA-hosted sources.
    """
    if item.image:
        return provider.mass.metadata.get_image_url(item.image, prefer_proxy=prefer_proxy)
    return None


async def get_album_image_fallback(album: Album, provider: MSXBridgeProvider) -> str | None:
    """Get album image from its first track (albums often lack metadata images)."""
    try:
        tracks = await provider.mass.music.albums.tracks(album.item_id, album.provider)
        for track in tracks:
            if track.image:
                return provider.mass.metadata.get_image_url(track.image)
    except MusicAssistantError, TimeoutError:
        logger.debug("Failed to fetch album image fallback for %s", album.item_id)
    return None


async def map_album_to_msx(
    album: Album | ItemMapping, prefix: str, provider: MSXBridgeProvider, device_param: str = ""
) -> MsxItem:
    """Map a MA Album to an MSX Item."""
    image = get_image_url(album, provider)
    if not image and isinstance(album, Album):
        image = await get_album_image_fallback(album, provider)

    artist = album.artist_str if isinstance(album, Album) else ""
    year = album.year
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
    artist: Artist | ItemMapping, prefix: str, provider: MSXBridgeProvider, device_param: str = ""
) -> MsxItem:
    """Map a MA Artist to an MSX Item."""
    url = (
        f"{prefix}/msx/artists/{artist.item_id}/albums.json?"
        f"provider={quote(str(artist.provider), safe='')}"
    )
    return MsxItem(
        title=artist.name,
        image=get_image_url(artist, provider),
        action=f"content:{append_device_param(url, device_param)}",
    )


def map_playlist_to_msx(
    playlist: Playlist | ItemMapping,
    prefix: str,
    provider: MSXBridgeProvider,
    device_param: str = "",
) -> MsxItem:
    """Map a MA Playlist to an MSX Item."""
    owner = playlist.owner if isinstance(playlist, Playlist) else ""
    prov = playlist.provider
    footer: str | None = f"{owner} · {prov}" if owner and prov else (owner or prov or None)
    url = f"{prefix}/msx/playlists/{playlist.item_id}/tracks.json"
    return MsxItem(
        title=playlist.name,
        title_footer=footer,
        image=get_image_url(playlist, provider),
        action=f"content:{append_device_param(url, device_param)}",
    )


def _build_audio_action(
    prefix: str,
    player_id: str,
    track_uri: str,
    token: str,
    device_param: str = "",
    from_playlist: bool = False,
    queue_item_id: str | None = None,
) -> str:
    """Build audio action URL for MSX playback."""
    # Standard HTTP streaming mode
    audio_url = f"{prefix}/msx/audio/{player_id}?uri={quote(track_uri, safe='')}&token={token}"
    if from_playlist:
        audio_url += "&from_playlist=1"
    if queue_item_id is not None:
        audio_url += f"&queue_item_id={quote(queue_item_id, safe='')}"
    audio_url = append_device_param(audio_url, device_param)
    return f"audio:{audio_url}"


def map_track_to_msx(
    track: PlayableMediaItemType | ItemMapping,
    prefix: str,
    player_id: str,
    provider: MSXBridgeProvider,
    device_param: str = "",
    *,
    context_uri: str,
    context_start: int = 0,
) -> MsxItem:
    """Map a MA Track to an MSX Item."""
    duration = track.duration if isinstance(track, Track) else 0
    duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else ""
    artist = track.artist_str if isinstance(track, Track) else ""
    image_url = get_image_url(track, provider)

    footer: str | None = (
        f"{artist} · {duration_str}"
        if artist and duration_str
        else (artist or duration_str or None)
    )

    nav = queue_nav_properties(player_id, prefix)
    action = play_context_action(
        prefix,
        player_id,
        context_uri,
        context_start,
        device_param,
        track_uri=track.uri,
    )

    return MsxItem(
        title_header="{txt:msx-white:" + track.name + "}",
        title_footer=footer,
        player_label=track.name,
        duration=duration,
        image=image_url,
        background=image_url,
        action=action,
        next_action=nav["button:next:action"],
        prev_action=nav["button:prev:action"],
        properties=nav,
    )


def map_tracks_to_msx_playlist(
    tracks: Sequence[PlaylistTrack],
    start_index: int,
    prefix: str,
    player_id: str,
    provider: MSXBridgeProvider,
    device_param: str = "",
    qr_cover_base: str | None = None,
) -> MsxContent:
    """
    Map tracks to an MSX Content page for playlist playback.

    MSX ``playlist:{URL}`` loads a standard Content Root Object.
    Each item uses ``action: "audio:{URL}"`` so MSX can play them sequentially.
    The page-level ``action`` auto-starts playback at the requested track index.

    :param qr_cover_base: When set (active party), item backgrounds are routed
        through this QR-compositing endpoint so the join QR shows on covers.
    """
    token = provider.get_stream_token(player_id)
    msx_items = []
    for track in tracks:
        duration = track.duration
        duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else ""
        artist = track.artist
        label = (
            f"{artist} · {duration_str}"
            if artist and duration_str
            else (artist or duration_str or None)
        )
        image_url = get_image_url(track, provider)
        background = image_url
        if qr_cover_base:
            # The compositor only accepts MA-hosted images; a remote CDN cover
            # would be rejected (400) and vanish. Route it through the MA
            # imageproxy so the QR-stamped background actually loads.
            proxied = get_image_url(track, provider, prefer_proxy=True)
            if proxied:
                background = f"{qr_cover_base}?image={quote(proxied, safe='')}"

        action = _build_audio_action(
            prefix=prefix,
            player_id=player_id,
            track_uri=track.uri,
            token=token,
            device_param=device_param,
            from_playlist=True,
            queue_item_id=track.queue_item_id,
        )
        nav = queue_nav_properties(player_id, prefix)

        msx_items.append(
            MsxItem(
                title=track.name,
                label=label,
                player_label=track.name,
                image=image_url,
                background=background,
                duration=duration,
                action=action,
                next_action=nav["button:next:action"],
                prev_action=nav["button:prev:action"],
                properties=nav,
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
