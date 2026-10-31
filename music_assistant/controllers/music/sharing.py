"""Server-owned policy for sharing public item pages and human-readable metadata."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import SplitResult, parse_qs, urlsplit

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.helpers.util import isolate_task_scope

from .constants import CACHE_CATEGORY_SHARE_VISIBILITY
from .sharing_catalogue import catalogue_url

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from music_assistant_models.media_items import MediaItemType, ProviderMapping

    from music_assistant.controllers.cache import CacheController


class ItemShareLink(TypedDict):
    """A public provider website destination."""

    provider_domain: str
    provider_instance: str
    url: str


class ItemShareInfo(TypedDict):
    """On-demand sharing data, independent of playback and library models."""

    title: str
    text: str | None
    links: list[ItemShareLink]


SHARE_MEDIA_TYPES = frozenset(
    {
        MediaType.ARTIST,
        MediaType.ALBUM,
        MediaType.TRACK,
        MediaType.PLAYLIST,
        MediaType.RADIO,
        MediaType.PODCAST,
        MediaType.PODCAST_EPISODE,
        MediaType.AUDIOBOOK,
    }
)
_CATALOG_TYPES = frozenset({MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK})
_SHARE_SOURCE_ACCESS: ContextVar[Callable[[str, str], bool] | None] = ContextVar(
    "share_source_access", default=None
)


@contextmanager
def share_source_access(allowed_source: Callable[[str, str], bool]) -> Iterator[None]:
    """
    Apply the sharing request's source policy to nested provider lookups.

    :param allowed_source: Check the current access to an instance and its domain.
    """
    token = _SHARE_SOURCE_ACCESS.set(allowed_source)
    try:
        # Provider tasks carry this request's policy through cache/throttle waits and
        # worker threads. Another caller must neither inherit nor bypass that policy.
        with isolate_task_scope():
            yield
    finally:
        _SHARE_SOURCE_ACCESS.reset(token)


def check_share_source_access(instance_id: str, domain: str) -> None:
    """
    Raise if the active sharing request may no longer use this provider instance.

    Calls outside a sharing request keep their normal provider access.

    :param instance_id: Exact provider account being used.
    :param domain: Domain of that provider account.
    """
    if (allowed_source := _SHARE_SOURCE_ACCESS.get()) is not None and not allowed_source(
        instance_id, domain
    ):
        raise MediaNotFoundError("Item not found")


def share_title(item: MediaItemType) -> str:
    """Return the item's human-readable name without addresses or control characters."""
    return _label(item.name) or "Music item"


def share_text(item: MediaItemType) -> str:
    """Format human metadata, preserving station identity for radio items."""
    title = share_title(item)
    if item.media_type == MediaType.RADIO:
        return title
    artists = ", ".join(
        name for artist in getattr(item, "artists", ()) if (name := _label(artist.name))
    )
    text = f"{artists} - {title}" if artists else title
    if item.media_type == MediaType.TRACK and (album := getattr(item, "album", None)):
        if album_name := _label(album.name):
            text += f" ({album_name})"
    return text


async def cached_public_provider_url(
    cache: CacheController, media_type: MediaType, mapping: ProviderMapping
) -> str | None:
    """Resolve a public page using cached visibility evidence where IDs are insufficient."""
    publicly_visible = False
    if mapping.provider_domain == "ytmusic" and media_type in {
        MediaType.TRACK,
        MediaType.PODCAST_EPISODE,
    }:
        video_id = mapping.item_id.rsplit("|", 1)[-1]
        publicly_visible = (
            await cache.get(
                f"track.{video_id}",
                provider=mapping.provider_instance,
                category=CACHE_CATEGORY_SHARE_VISIBILITY,
                allow_bypass=False,
            )
            is True
        )
    return public_provider_url(media_type, mapping, publicly_visible=publicly_visible)


def public_provider_url(
    media_type: MediaType, mapping: ProviderMapping, *, publicly_visible: bool = False
) -> str | None:
    """Resolve an audited public website page without network access."""
    domain, item_id = mapping.provider_domain, mapping.item_id
    kind = media_type.value
    if media_type not in SHARE_MEDIA_TYPES or media_type == MediaType.PLAYLIST:
        return None
    if media_type == MediaType.RADIO or (
        media_type == MediaType.PODCAST and domain in {"itunes_podcasts", "podcast_index"}
    ):
        return _radio_url(mapping) if media_type == MediaType.RADIO else _podcast_url(mapping)
    if domain == "spotify":
        route = {
            MediaType.ARTIST: "artist",
            MediaType.ALBUM: "album",
            MediaType.TRACK: "track",
            MediaType.PODCAST: "show",
            MediaType.PODCAST_EPISODE: "episode",
            MediaType.AUDIOBOOK: "audiobook",
        }.get(media_type)
        if route and re.fullmatch(r"[A-Za-z0-9]{22}", item_id):
            return f"https://open.spotify.com/{route}/{item_id}"
        return None
    if domain == "ytmusic":
        return _youtube_url(media_type, item_id, mapping.url, publicly_visible)
    if domain == "internet_archive":
        return _archive_url(media_type, item_id)
    if domain == "yandex_music":
        route = {
            MediaType.ARTIST: "artist",
            MediaType.ALBUM: "album",
            MediaType.TRACK: "track",
            MediaType.PODCAST: "album",
            MediaType.PODCAST_EPISODE: "track",
            MediaType.AUDIOBOOK: "album",
        }.get(media_type)
        if route and re.fullmatch(r"[0-9]+", item_id):
            return f"https://music.yandex.ru/{route}/{item_id}"
        return None
    if media_type not in _CATALOG_TYPES:
        return None
    if domain == "nicovideo":
        return _nicovideo_url(media_type, item_id, mapping.url)
    if domain in {"neteasecloudmusic", "zvuk_music", "qqmusic"}:
        return _regional_url(domain, media_type, item_id)
    if domain in {"tidal", "deezer", "qobuz"}:
        pattern = (
            r"[A-Za-z0-9]+" if domain == "qobuz" and media_type == MediaType.ALBUM else r"[0-9]+"
        )
        if not re.fullmatch(pattern, item_id):
            return None
        host = {"tidal": "tidal.com", "deezer": "www.deezer.com", "qobuz": "open.qobuz.com"}[domain]
        return f"https://{host}/{kind}/{item_id}"
    if domain in {"apple_music", "soundcloud", "bandcamp"}:
        return _website_url(domain, media_type, item_id, mapping.url)
    return catalogue_url(media_type, mapping)


def _radio_url(mapping: ProviderMapping) -> str | None:
    """Build audited station website destinations from provider identities."""
    domain, item_id = mapping.provider_domain, mapping.item_id
    if domain == "nts" and item_id in {"nts_channel_1", "nts_channel_2"}:
        return f"https://www.nts.live/{item_id[-1]}"
    if domain == "somafm" and re.fullmatch(r"[a-z0-9]+", item_id):
        return f"https://somafm.com/{item_id}/"
    if domain == "iheartradio" and re.fullmatch(r"[1-9][0-9]*", item_id):
        if not (url := mapping.url) or any(not char.isprintable() for char in url):
            return None
        match = re.fullmatch(
            rf"https://www\.iheart\.com/live/([a-z0-9]+(?:-[a-z0-9]+)*)-{item_id}/?"
            r"(?:\?[^\s\\#]*)?(?:\#[^\s\\]*)?",
            url,
        )
        if match:
            return f"https://www.iheart.com/live/{match[1]}-{item_id}/"
    return None


def _podcast_url(mapping: ProviderMapping) -> str | None:
    """Resolve podcast directory pages without sharing feed or enclosure URLs."""
    if mapping.provider_domain == "podcast_index":
        if re.fullmatch(r"[1-9][0-9]*", mapping.item_id):
            return f"https://podcastindex.org/podcast/{mapping.item_id}"
        return None
    if not mapping.details:
        return None
    try:
        details = json.loads(mapping.details)
    except ValueError, RecursionError:
        return None
    if not isinstance(details, dict):
        return None
    itunes_id = details.get("itunes_id")
    if type(itunes_id) is int and itunes_id > 0:
        return f"https://podcasts.apple.com/podcast/id{itunes_id}"
    return None


def _label(value: str) -> str:
    """Keep human labels, excluding embedded addresses and control characters."""
    value = "".join(char for char in value if char.isprintable())
    value = re.sub(r"\S*(?:://|www\.)\S*", "", value, flags=re.IGNORECASE)
    return " ".join(value.split())


def _website_url(domain: str, media_type: MediaType, item_id: str, url: str | None) -> str | None:
    """Validate stored website shapes and reconstruct a clean public URL."""
    if not url or any(ord(char) < 33 for char in url) or "\\" in url:
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password:
            return None
        if parsed.port is not None or not parsed.hostname:
            return None
    except ValueError:
        return None
    host, path = parsed.hostname.lower(), parsed.path
    if domain == "apple_music":
        return _apple_url(media_type, item_id, parsed)
    if domain == "soundcloud":
        return _soundcloud_url(media_type, parsed)
    if domain == "bandcamp":
        return _bandcamp_url(media_type, host, path)
    return None


def _apple_url(media_type: MediaType, item_id: str, parsed: SplitResult) -> str | None:
    """Reconstruct Apple catalog pages with verified item identity."""
    host, path = parsed.hostname, parsed.path
    if host != "music.apple.com":
        return None
    match = re.fullmatch(
        r"/([a-z]{2})/(artist|album|song)/(?:[A-Za-z0-9_~.%+-]+/)?([A-Za-z0-9.-]+)",
        path,
    )
    if not match:
        return None
    country, kind, page_id = match.groups()
    query = parse_qs(parsed.query)
    if media_type == MediaType.TRACK and kind == "album":
        if not re.fullmatch(r"[0-9]+", page_id) or query.get("i") != [item_id]:
            return None
        if not re.fullmatch(r"[0-9]+", item_id):
            return None
        return f"https://music.apple.com/{country}/album/{page_id}?i={item_id}"
    expected = "song" if media_type == MediaType.TRACK else media_type.value
    pattern = r"[0-9]+"
    if kind != expected or page_id != item_id or not re.fullmatch(pattern, item_id):
        return None
    return f"https://music.apple.com/{country}/{kind}/{page_id}"


def _soundcloud_url(media_type: MediaType, parsed: SplitResult) -> str | None:
    """Accept artist pages; track mappings do not establish public visibility."""
    host, path = parsed.hostname, parsed.path
    if host not in {"soundcloud.com", "www.soundcloud.com"}:
        return None
    # Track privacy and secret tokens can be separate fields discarded by the provider parser.
    # A clean permalink alone therefore cannot establish that a track is public.
    if "secret_token" in parse_qs(parsed.query):
        return None
    if media_type != MediaType.ARTIST or not re.fullmatch(r"/[A-Za-z0-9_-]+", path):
        return None
    parts = path.strip("/").split("/")
    if parts[0].lower() in {
        "api",
        "stream",
        "download",
        "tracks",
        "users",
        "you",
        "discover",
        "search",
    }:
        return None
    return f"https://soundcloud.com{path}"


def _bandcamp_url(media_type: MediaType, host: str, path: str) -> str | None:
    """Accept artist subdomains and public album/track page paths."""
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.bandcamp\.com", host):
        return None
    if host in {"www.bandcamp.com", "daily.bandcamp.com", "api.bandcamp.com"}:
        return None
    if media_type == MediaType.ARTIST and path in {"", "/", "/music"}:
        return f"https://{host}"
    if media_type in {MediaType.ALBUM, MediaType.TRACK} and re.fullmatch(
        rf"/{media_type.value}/[A-Za-z0-9_-]+", path
    ):
        return f"https://{host}{path}"
    return None


def _youtube_url(
    media_type: MediaType, item_id: str, url: str | None, publicly_visible: bool
) -> str | None:
    """Build YouTube Music pages, excluding account-specific collections."""
    if media_type == MediaType.PODCAST_EPISODE and publicly_visible:
        match = re.fullmatch(r"[A-Za-z0-9_-]+\|([A-Za-z0-9_-]{11})", item_id)
        if match:
            return f"https://music.youtube.com/watch?v={match[1]}"
        return None
    if (
        media_type == MediaType.TRACK
        and publicly_visible
        and re.fullmatch(r"[A-Za-z0-9_-]{11}", item_id)
    ):
        return f"https://music.youtube.com/watch?v={item_id}"
    if media_type == MediaType.ARTIST and re.fullmatch(r"UC[A-Za-z0-9_-]{22}", item_id):
        return f"https://music.youtube.com/channel/{item_id}"
    if media_type == MediaType.ALBUM and re.fullmatch(r"MPREb_[A-Za-z0-9_-]+", item_id):
        # An album browse id differs from its audio playlist id. Prefer the verified
        # playlist mapping when present; otherwise the provider's browse route works.
        if url and re.fullmatch(
            r"https://music\.youtube\.com/playlist\?list=OLAK5uy_[A-Za-z0-9_-]+", url
        ):
            return url
        return f"https://music.youtube.com/browse/{item_id}"
    return None


def _regional_url(domain: str, media_type: MediaType, item_id: str) -> str | None:
    """Build regional catalog pages from the provider's catalog identifiers."""
    if domain == "qqmusic":
        if not re.fullmatch(r"[A-Za-z0-9]+", item_id) or not re.search(r"[A-Za-z]", item_id):
            return None
        route = {
            MediaType.ARTIST: "singer",
            MediaType.ALBUM: "albumDetail",
            MediaType.TRACK: "songDetail",
        }[media_type]
        return f"https://y.qq.com/n/ryqq/{route}/{item_id}"
    if not re.fullmatch(r"[0-9]+", item_id):
        return None
    if domain == "neteasecloudmusic":
        route = "song" if media_type == MediaType.TRACK else media_type.value
        return f"https://music.163.com/#/{route}?id={item_id}"
    route = "release" if media_type == MediaType.ALBUM else media_type.value
    return f"https://zvuk.com/{route}/{item_id}"


def _nicovideo_url(media_type: MediaType, item_id: str, url: str | None) -> str | None:
    """Resolve video and series IDs, using website identity to distinguish artists."""
    if media_type == MediaType.TRACK and re.fullmatch(r"(?:sm|so|nm)[0-9]+", item_id):
        return f"https://www.nicovideo.jp/watch/{item_id}"
    if media_type == MediaType.ALBUM and re.fullmatch(r"[0-9]+", item_id):
        return f"https://www.nicovideo.jp/series/{item_id}"
    if media_type != MediaType.ARTIST or not url:
        return None
    if not re.fullmatch(r"[0-9]+", item_id):
        return None
    for route in ("user", "channel"):
        if url == f"https://www.nicovideo.jp/{route}/{item_id}":
            return f"https://www.nicovideo.jp/{route}/{item_id}"
    return None


def _archive_url(media_type: MediaType, item_id: str) -> str | None:
    """Share an Archive item page, never a file or a creator-name search."""
    if media_type == MediaType.ARTIST:
        return None
    identifier, separator, filename = item_id.partition("#")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", identifier):
        return None
    if separator:
        if media_type not in {MediaType.TRACK, MediaType.PODCAST_EPISODE}:
            return None
        if not filename or any(char in filename for char in "\\/#?%"):
            return None
        if filename in {".", ".."} or any(not char.isprintable() for char in filename):
            return None
    return f"https://archive.org/details/{identifier}"
