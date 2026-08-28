"""Helpers for parsing (online and offline) playlists."""

from __future__ import annotations

import configparser
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final
from urllib.parse import urlparse

from aiohttp import ClientTimeout, client_exceptions
from music_assistant_models.enums import ContentType, ExternalID, ImageType, MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.helpers import create_uri
from music_assistant_models.media_items import (
    Artist,
    Audiobook,
    AudioFormat,
    ItemMapping,
    MediaItem,
    MediaItemImage,
    MediaItemType,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    SoundEffect,
    Track,
    UniqueList,
)

from music_assistant.helpers.aiohttp_client import encoded_request_url
from music_assistant.helpers.uri import BUILTIN_URL_SCHEMES
from music_assistant.helpers.util import detect_charset, try_parse_int

if TYPE_CHECKING:
    from aiohttp import StreamReader

    from music_assistant.mass import MusicAssistant


LOGGER = logging.getLogger(__name__)
HLS_CONTENT_TYPES = ("application/vnd.apple.mpegurl",)
PLAYLIST_CONTENT_TYPES = ("audio/x-mpegurl", "audio/x-scpls")
FIELD_SEPARATOR = "||"
# every character str.splitlines() treats as a line boundary: one of these inside a
# title, name or path splits the entry over two lines, and the tail is read back as a
# separate (bogus) playlist entry that can never be resolved
_LINE_BREAK_CHARS: Final[str] = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
_LINE_BREAK_TABLE: Final = str.maketrans(dict.fromkeys(_LINE_BREAK_CHARS, " "))
# playlists are small text files: cap the read so a stream served under a
# playlist content-type does not pull an endless body into memory
MAX_PLAYLIST_SIZE = 64 * 1024
PLAYLIST_READ_TIMEOUT = 5


class IsHLSPlaylist(InvalidDataError):
    """The playlist from an HLS stream and should not be parsed."""


# --------------------------------------------------------------------------- #
#  Dataclasses for M3U extended tags (shared between parsing and generation)   #
# --------------------------------------------------------------------------- #


@dataclass
class ProviderMappingInfo:
    """Provider mapping stored as #EXTPROV in M3U files."""

    domain: str
    item_id: str
    instance_id: str = ""
    content_type: str = ""
    sample_rate: int = 0
    bit_depth: int = 0
    bit_rate: int = 0


@dataclass
class ImageInfo:
    """Image metadata stored as #EXTIMG in M3U files."""

    type: str
    path: str
    provider: str
    remotely_accessible: bool = False


@dataclass
class ArtistInfo:
    """Artist metadata stored as #EXTARTIST in M3U files."""

    name: str
    provider_domain: str
    item_id: str
    provider_instance: str


@dataclass
class AlbumInfo:
    """Album metadata stored as #EXTALBUM in M3U files."""

    name: str
    provider_domain: str
    item_id: str
    provider_instance: str
    version: str = ""


@dataclass
class PodcastInfo:
    """Podcast metadata stored as #EXTPODCAST in M3U files."""

    name: str
    provider_domain: str
    item_id: str
    provider_instance: str


@dataclass
class PlaylistItem:
    """Single entry in an M3U playlist. Used for both parsing and generation."""

    path: str
    length: str | None = None
    title: str | None = None
    stream_info: dict[str, str] | None = None
    key: str | None = None
    metadata: dict[str, str] | None = None
    providers: list[ProviderMappingInfo] = field(default_factory=list)
    images: list[ImageInfo] = field(default_factory=list)
    artists: list[ArtistInfo] = field(default_factory=list)
    album: AlbumInfo | None = None
    podcast: PodcastInfo | None = None

    @property
    def is_url(self) -> bool:
        """Return True if the path looks like a valid URL."""
        result = urlparse(self.path)
        return all([result.scheme, result.netloc])


# --------------------------------------------------------------------------- #
#  Parsing helpers                                                             #
# --------------------------------------------------------------------------- #


def parse_extinf_title(title: str | None) -> tuple[str | None, str | None]:
    """Split "Artist - Title" into (artist, title). Returns (None, title) if no separator."""
    if not title:
        return None, None
    if " - " in title:
        artist, track_title = title.split(" - ", 1)
        return artist.strip(), track_title.strip()
    return None, title.strip()


def parse_m3u(m3u_data: str) -> list[PlaylistItem]:  # noqa: PLR0915
    """
    Parse M3U/M3U8 data into a list of PlaylistItem entries.

    Supports standard tags (#EXTINF, #EXT-X-STREAM-INF, #EXT-X-KEY) and
    Music Assistant extensions (#EXTMA, #EXTPROV, #EXTIMG etc.).
    """
    m3u_lines = m3u_data.splitlines()
    playlist: list[PlaylistItem] = []

    # per-entry accumulators (reset after each path line)
    length = None
    title = None
    stream_info: dict[str, str] | None = None
    key = None
    metadata: dict[str, str] | None = None
    providers: list[ProviderMappingInfo] = []
    images: list[ImageInfo] = []
    artists: list[ArtistInfo] = []
    album: AlbumInfo | None = None
    podcast: PodcastInfo | None = None

    for line in m3u_lines:
        line = line.strip()  # noqa: PLW2901
        if line.startswith("#EXTINF:"):
            info = line.split("#EXTINF:")[1].split(",", 1)
            if len(info) != 2:
                continue
            length = info[0].strip()
            if length == "-1":
                length = None
            title = info[1].strip()
        elif line.startswith("#EXT-X-STREAM-INF:"):
            stream_info = _parse_hls_stream_info(line)
        elif line.startswith("#EXT-X-KEY:"):
            key = _parse_hls_key(line, key)
        elif line.startswith("#EXTMA:"):
            metadata = _parse_extma_line(line)
        elif line.startswith("#EXTPROV:"):
            if prov_info := _parse_extprov_line(line):
                providers.append(prov_info)
        elif line.startswith("#EXTIMG:"):
            if img_info := _parse_extimg_line(line):
                images.append(img_info)
        elif line.startswith("#EXTARTIST:"):
            if artist_info := _parse_extartist_line(line):
                artists.append(artist_info)
        elif line.startswith("#EXTALBUM:"):
            album = _parse_extalbum_line(line)
        elif line.startswith("#EXTPODCAST:"):
            podcast = _parse_extpodcast_line(line)
        elif line.startswith("#"):
            continue
        elif line:
            filepath = line.replace("%20", " ").replace("\\", "/")
            playlist.append(
                PlaylistItem(
                    path=filepath,
                    length=length,
                    title=title,
                    stream_info=stream_info,
                    key=key,
                    metadata=metadata,
                    providers=providers,
                    images=images,
                    artists=artists,
                    album=album,
                    podcast=podcast,
                )
            )
            # reset accumulators
            length = None
            title = None
            stream_info = None
            metadata = None
            providers = []
            images = []
            artists = []
            album = None
            podcast = None

    return playlist


def parse_m3u_playlist_name(m3u_data: str) -> str | None:
    """Extract the playlist name from an M3U #PLAYLIST directive."""
    for line in m3u_data.splitlines():
        line = line.strip()  # noqa: PLW2901
        if line.startswith("#PLAYLIST:"):
            return line.split("#PLAYLIST:", 1)[1].strip()
    return None


def parse_m3u_playlist_image(m3u_data: str) -> str | None:
    """
    Extract the playlist cover image from an M3U #EXTIMG directive.

    Looks for a simple #EXTIMG:url line before the first track entry.
    This is a de facto standard for playlist-level cover images.
    """
    for line in m3u_data.splitlines():
        line = line.strip()  # noqa: PLW2901
        if not line or line.startswith(("#EXTM3U", "#PLAYLIST:")):
            continue
        # stop at first track entry or track-level metadata
        if not line.startswith("#"):
            break
        if line.startswith(("#EXTINF:", "#EXTMA:", "#EXTPROV:")):
            break
        # simple playlist-level image (just URL, no field separator)
        if line.startswith("#EXTIMG:") and FIELD_SEPARATOR not in line:
            return line.split("#EXTIMG:", 1)[1].strip()
    return None


def parse_pls(pls_data: str) -> list[PlaylistItem]:
    """Parse a PLS playlist file into PlaylistItem entries."""
    pls_parser = configparser.ConfigParser(strict=False)
    try:
        pls_parser.read_string(pls_data, "playlist")
    except configparser.Error as err:
        raise InvalidDataError("Can't parse playlist") from err

    if "playlist" not in pls_parser:
        raise InvalidDataError("Invalid playlist")

    try:
        num_entries = pls_parser.getint("playlist", "NumberOfEntries")
    except (configparser.NoOptionError, ValueError) as err:
        raise InvalidDataError("Invalid NumberOfEntries in playlist") from err

    playlist_section = pls_parser["playlist"]
    playlist: list[PlaylistItem] = []
    for entry in range(1, num_entries + 1):
        file_option = f"File{entry}"
        if file_option not in playlist_section:
            continue
        itempath = playlist_section[file_option]
        length = playlist_section.get(f"Length{entry}")
        playlist.append(
            PlaylistItem(
                length=length if length and length != "-1" else None,
                title=playlist_section.get(f"Title{entry}"),
                path=itempath,
            )
        )
    return playlist


async def read_playlist_body(content: StreamReader) -> bytes:
    """
    Read a playlist body, up to MAX_PLAYLIST_SIZE bytes.

    :param content: Response body to read from.
    """
    # a single read returns whatever happens to be buffered, so a playlist spread over
    # several chunks would otherwise be parsed truncated
    raw_data = bytearray()
    while len(raw_data) < MAX_PLAYLIST_SIZE:
        chunk = await content.read(MAX_PLAYLIST_SIZE - len(raw_data))
        if not chunk:
            break
        raw_data += chunk
    return bytes(raw_data)


async def parse_playlist_data(
    url: str, raw_data: bytes, charset: str | None = None, raise_on_hls: bool = True
) -> list[PlaylistItem]:
    """
    Parse the raw body of an M3U or PLS playlist into entries.

    :param url: URL the body was fetched from, used to pick the playlist flavour.
    :param raw_data: Raw (undecoded) playlist body.
    :param charset: Charset declared by the server, if any.
    :param raise_on_hls: Raise IsHLSPlaylist for an HLS media playlist.
    """
    encoding = await detect_charset(raw_data, preferred=charset)
    playlist_data = raw_data.decode(encoding, errors="replace")

    if (
        raise_on_hls
        and ("#EXT-X-VERSION:" in playlist_data or "#EXT-X-TARGETDURATION:" in playlist_data)
    ) or "#EXT-X-STREAM-INF:" in playlist_data:
        raise IsHLSPlaylist

    if urlparse(url).path.endswith("pls") or "[playlist]" in playlist_data:
        playlist = parse_pls(playlist_data)
    else:
        playlist = parse_m3u(playlist_data)

    if not playlist:
        msg = f"Empty playlist {url}"
        raise InvalidDataError(msg)

    return playlist


async def fetch_playlist(
    mass: MusicAssistant, url: str, raise_on_hls: bool = True
) -> list[PlaylistItem]:
    """Fetch and parse a remote M3U or PLS playlist."""
    try:
        async with mass.http_session.get(
            encoded_request_url(url),
            allow_redirects=True,
            timeout=ClientTimeout(total=PLAYLIST_READ_TIMEOUT),
        ) as resp:
            # an error page is still a body: its markup would otherwise parse into entries
            resp.raise_for_status()
            raw_data = await read_playlist_body(resp.content)
            charset = resp.charset
    except TimeoutError as err:
        msg = f"Timeout while fetching playlist {url}"
        raise InvalidDataError(msg) from err
    except client_exceptions.ClientError as err:
        msg = f"Error while fetching playlist {url}"
        raise InvalidDataError(msg) from err

    return await parse_playlist_data(url, raw_data, charset, raise_on_hls)


# --------------------------------------------------------------------------- #
#  Generation                                                                  #
# --------------------------------------------------------------------------- #


def sanitize_m3u_value(value: str) -> str:
    """
    Replace line breaks in a value so it stays on a single line in an M3U file.

    :param value: Raw value (name, title, path or URL) about to be written to an M3U line.
    """
    return value.translate(_LINE_BREAK_TABLE)


def generate_m3u(
    playlist_name: str,
    items: Sequence[PlaylistItem],
    playlist_image_url: str | None = None,
) -> str:
    """
    Generate an M3U8 playlist string from PlaylistItem entries.

    :param playlist_name: Human-readable name (written as #PLAYLIST directive).
    :param items: Entries to write. Only fields that are set are emitted.
    :param playlist_image_url: Optional playlist cover image URL.
    """
    # every value goes through sanitize_m3u_value: a line break inside a title or name
    # (they do occur in provider metadata and file tags) would otherwise split the entry
    # over two lines, and the tail would be read back as an unresolvable entry
    clean = sanitize_m3u_value
    # Playlist-level image using #EXTIMG directive (de facto standard for playlist covers)
    lines: list[str] = ["#EXTM3U"]
    if playlist_image_url:
        lines.append(f"#EXTIMG:{clean(playlist_image_url)}")
    lines.append(f"#PLAYLIST:{clean(playlist_name)}")
    sep = FIELD_SEPARATOR
    for item in items:
        if item.metadata:
            pairs = sep.join(f"{clean(k)}={clean(v)}" for k, v in item.metadata.items())
            lines.append(f"#EXTMA:{pairs}")
        for prov in item.providers:
            fields = [
                clean(prov.domain),
                clean(prov.item_id),
                clean(prov.instance_id),
                clean(prov.content_type),
                str(prov.sample_rate),
                str(prov.bit_depth),
                str(prov.bit_rate),
            ]
            lines.append(f"#EXTPROV:{sep.join(fields)}")
        for artist in item.artists:
            fields = [
                clean(artist.name),
                clean(artist.provider_domain),
                clean(artist.item_id),
                clean(artist.provider_instance),
            ]
            lines.append(f"#EXTARTIST:{sep.join(fields)}")
        if item.album:
            fields = [
                clean(item.album.name),
                clean(item.album.provider_domain),
                clean(item.album.item_id),
                clean(item.album.provider_instance),
                clean(item.album.version),
            ]
            lines.append(f"#EXTALBUM:{sep.join(fields)}")
        if item.podcast:
            fields = [
                clean(item.podcast.name),
                clean(item.podcast.provider_domain),
                clean(item.podcast.item_id),
                clean(item.podcast.provider_instance),
            ]
            lines.append(f"#EXTPODCAST:{sep.join(fields)}")
        for img in item.images:
            remotely = "true" if img.remotely_accessible else "false"
            fields = [clean(img.type), clean(img.path), clean(img.provider), remotely]
            lines.append(f"#EXTIMG:{sep.join(fields)}")
        if item.title is not None:
            # -1 is the M3U convention for unknown length; without it an entry without
            # duration (such as a radio station) loses its title on every rewrite
            length = item.length if item.length is not None else -1
            lines.append(f"#EXTINF:{length},{clean(item.title)}")
        lines.append(clean(item.path))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
#  Media item construction from playlist metadata                              #
# --------------------------------------------------------------------------- #


def construct_media_item_from_playlist_item(
    item: PlaylistItem,
    mass: MusicAssistant,
    default_media_type: MediaType = MediaType.TRACK,
) -> MediaItemType | None:
    """
    Construct a MediaItem from a PlaylistItem's stored metadata.

    Resolves provider mappings by instance_id first (free dict lookup),
    falling back to domain if the instance no longer exists.

    :param item: Parsed PlaylistItem with metadata, providers, and images.
    :param mass: MusicAssistant instance for provider resolution.
    :param default_media_type: Media type to build when the entry's #EXTMA metadata
        holds no usable media_type.
    """
    metadata = item.metadata or {}
    try:
        media_type = MediaType(metadata.get("media_type", default_media_type.value))
    except ValueError:
        media_type = default_media_type
    name = metadata.get("name") or item.title or item.path
    duration = try_parse_int(item.length, default=None) if item.length else None

    provider_mappings = _resolve_provider_mappings(item, mass)
    if not provider_mappings and item.path.startswith(BUILTIN_URL_SCHEMES):
        # an item without any mapping never reaches library_add, leaving an unplayable
        # library entry, so a plain stream URL is mapped to the provider that serves it
        provider_mappings = _builtin_fallback_mappings(item.path, mass)
    external_ids = _collect_external_ids(metadata)

    # a set has no order, so sort to keep the chosen mapping stable across runs
    sorted_mappings = sorted(provider_mappings, key=lambda pm: (pm.provider_instance, pm.item_id))
    first_provider = next(
        (pm for pm in sorted_mappings if pm.available),
        next(iter(sorted_mappings), None),
    )
    # prefer the instance over the domain: a domain resolves to whichever instance of it
    # happens to be loaded first, which is the wrong one when several are configured
    item_provider = (
        (first_provider.provider_instance or first_provider.provider_domain)
        if first_provider
        else "builtin"
    )
    item_id = first_provider.item_id if first_provider else item.path

    media_item: MediaItemType
    if media_type == MediaType.SOUND_EFFECT:
        media_item = SoundEffect(
            item_id=item_id,
            provider=item_provider,
            name=name,
            provider_mappings=provider_mappings,
            external_ids=external_ids,
        )
        if duration is not None:
            media_item.duration = duration
    elif media_type == MediaType.RADIO:
        media_item = Radio(
            item_id=item_id,
            provider=item_provider,
            name=name,
            provider_mappings=provider_mappings,
            external_ids=external_ids,
        )
    elif media_type == MediaType.PODCAST_EPISODE:
        if not item.podcast:
            return None
        p_provider = item.podcast.provider_domain or item_provider
        p_item_id = item.podcast.item_id or item.podcast.name
        podcast_mapping = ItemMapping(
            item_id=p_item_id,
            provider=p_provider,
            name=item.podcast.name,
            media_type=MediaType.PODCAST,
        )
        media_item = PodcastEpisode(
            item_id=item_id,
            provider=item_provider,
            name=name,
            position=0,
            provider_mappings=provider_mappings,
            podcast=podcast_mapping,
            external_ids=external_ids,
        )
        if duration is not None:
            media_item.duration = duration
    elif media_type == MediaType.AUDIOBOOK:
        media_item = Audiobook(
            item_id=item_id,
            provider=item_provider,
            name=name,
            provider_mappings=provider_mappings,
            authors=UniqueList(
                metadata.get("authors", "").split("; ") if metadata.get("authors") else []
            ),
            narrators=UniqueList(
                metadata.get("narrators", "").split("; ") if metadata.get("narrators") else []
            ),
            external_ids=external_ids,
        )
        if duration is not None:
            media_item.duration = duration
    else:
        media_item = _construct_track(
            item,
            metadata,
            item_id,
            item_provider,
            name,
            duration,
            provider_mappings,
            external_ids,
        )

    for img in item.images:
        try:
            image_type = ImageType(img.type)
        except ValueError:
            continue
        media_item.metadata.add_image(
            MediaItemImage(
                type=image_type,
                path=img.path,
                provider=img.provider,
                remotely_accessible=img.remotely_accessible,
            )
        )
    return media_item


def _resolve_provider_mappings(item: PlaylistItem, mass: MusicAssistant) -> set[ProviderMapping]:
    """Resolve provider mappings from playlist item, checking loaded providers."""
    provider_mappings: set[ProviderMapping] = set()
    for prov_info in item.providers:
        prov = None
        if prov_info.instance_id:
            prov = mass.get_provider(prov_info.instance_id)
        if not prov:
            prov = mass.get_provider(prov_info.domain)
        audio_format = AudioFormat(
            content_type=ContentType.try_parse(prov_info.content_type),
            sample_rate=prov_info.sample_rate,
            bit_depth=prov_info.bit_depth,
            bit_rate=prov_info.bit_rate,
        )
        provider_mappings.add(
            ProviderMapping(
                item_id=prov_info.item_id,
                provider_domain=prov.domain if prov else prov_info.domain,
                provider_instance=prov.instance_id if prov else prov_info.instance_id,
                available=prov is not None,
                audio_format=audio_format,
            )
        )
    return provider_mappings


def _builtin_fallback_mappings(path: str, mass: MusicAssistant) -> set[ProviderMapping]:
    """Return the builtin mapping for a stream URL, which builtin takes as its item_id."""
    prov = mass.get_provider("builtin")
    return {
        ProviderMapping(
            item_id=path,
            provider_domain="builtin",
            provider_instance=prov.instance_id if prov else "builtin",
            available=prov is not None,
        )
    }


def _collect_external_ids(metadata: dict[str, str]) -> set[tuple[ExternalID, str]]:
    """Collect external IDs (ISRC, MusicBrainz) from metadata dict."""
    external_ids: set[tuple[ExternalID, str]] = set()
    if isrc := metadata.get("isrc"):
        external_ids.add((ExternalID.ISRC, isrc))
    if mbid := metadata.get("mbid"):
        external_ids.add((ExternalID.MB_RECORDING, mbid))
    # the release-track id (as opposed to the recording id) pins a specific release,
    # which is what lets import matching reach an EXACT confidence instead of merely
    # the same underlying recording on a possibly different release
    if mb_track := metadata.get("mb_track"):
        external_ids.add((ExternalID.MB_TRACK, mb_track))
    return external_ids


def _construct_track(
    item: PlaylistItem,
    metadata: dict[str, str],
    item_id: str,
    item_provider: str,
    name: str,
    duration: int | None,
    provider_mappings: set[ProviderMapping],
    external_ids: set[tuple[ExternalID, str]],
) -> Track:
    """Construct a Track from playlist item data, including artists and album."""
    artists: UniqueList[Artist | ItemMapping] = UniqueList()
    for artist_info in item.artists:
        artist_provider = artist_info.provider_domain or item_provider
        artist_item_id = artist_info.item_id or artist_info.name
        artists.append(
            ItemMapping(
                item_id=artist_item_id,
                provider=artist_provider,
                name=artist_info.name,
                media_type=MediaType.ARTIST,
            )
        )
    album_mapping: ItemMapping | None = None
    if item.album:
        album_provider = item.album.provider_domain or item_provider
        album_item_id = item.album.item_id or item.album.name
        album_mapping = ItemMapping(
            item_id=album_item_id,
            provider=album_provider,
            name=item.album.name,
            version=item.album.version,
            media_type=MediaType.ALBUM,
        )
    track = Track(
        item_id=item_id,
        provider=item_provider,
        name=name,
        version=metadata.get("version", ""),
        artists=artists,
        album=album_mapping,
        provider_mappings=provider_mappings,
        external_ids=external_ids,
    )
    if duration is not None:
        track.duration = duration
    return track


# --------------------------------------------------------------------------- #
#  Media item to playlist dataclass helpers                                    #
# --------------------------------------------------------------------------- #


def collect_artist_infos(full_item: MediaItem) -> list[ArtistInfo]:
    """Extract artist info from a media item for M3U serialization."""
    artist_infos: list[ArtistInfo] = []
    if not hasattr(full_item, "artists") or not full_item.artists:
        return artist_infos
    for artist in full_item.artists:
        prov_mappings = getattr(artist, "provider_mappings", None)
        if prov_mappings:
            first_mapping = next(iter(prov_mappings))
            a_domain = first_mapping.provider_domain
            a_item_id = first_mapping.item_id
            a_instance = first_mapping.provider_instance
        else:
            a_domain = artist.provider
            a_item_id = artist.item_id
            a_instance = artist.provider
        artist_infos.append(
            ArtistInfo(
                name=artist.name,
                provider_domain=a_domain,
                item_id=a_item_id,
                provider_instance=a_instance,
            )
        )
    return artist_infos


def collect_album_info(full_item: MediaItem) -> AlbumInfo | None:
    """Extract album info from a media item for M3U serialization."""
    if not hasattr(full_item, "album") or not full_item.album:
        return None
    album = full_item.album
    prov_mappings = getattr(album, "provider_mappings", None)
    if prov_mappings:
        first_mapping = next(iter(prov_mappings))
        al_domain = first_mapping.provider_domain
        al_item_id = first_mapping.item_id
        al_instance = first_mapping.provider_instance
    else:
        al_domain = album.provider
        al_item_id = album.item_id
        al_instance = album.provider
    return AlbumInfo(
        name=album.name,
        provider_domain=al_domain,
        item_id=al_item_id,
        provider_instance=al_instance,
        version=getattr(album, "version", "") or "",
    )


def collect_podcast_info(full_item: MediaItem) -> PodcastInfo | None:
    """Extract podcast info from a media item for M3U serialization."""
    if not hasattr(full_item, "podcast") or not full_item.podcast:
        return None
    podcast = full_item.podcast
    prov_mappings = getattr(podcast, "provider_mappings", None)
    if prov_mappings:
        first_mapping = next(iter(prov_mappings))
        p_domain = first_mapping.provider_domain
        p_item_id = first_mapping.item_id
        p_instance = first_mapping.provider_instance
    else:
        p_domain = podcast.provider
        p_item_id = podcast.item_id
        p_instance = podcast.provider
    return PodcastInfo(
        name=podcast.name,
        provider_domain=p_domain,
        item_id=p_item_id,
        provider_instance=p_instance,
    )


def media_item_to_playlist_item(full_item: MediaItem) -> PlaylistItem:
    """
    Convert a MediaItem to a PlaylistItem with full M3U metadata.

    Pure conversion — takes an already-fetched MediaItem and produces a
    PlaylistItem suitable for ``generate_m3u``.

    :param full_item: Any MediaItem (Track, Radio, PodcastEpisode, Audiobook, etc.).
    """
    # build M3U-compliant EXTINF title
    if hasattr(full_item, "artists") and full_item.artists:
        artist_names = ", ".join(a.name for a in full_item.artists)
        title = f"{artist_names} - {full_item.name}"
    elif hasattr(full_item, "podcast") and full_item.podcast:
        title = f"{full_item.podcast.name} - {full_item.name}"
    else:
        title = full_item.name

    duration = getattr(full_item, "duration", None)

    # build EXTMA metadata
    metadata: dict[str, str] = {
        "media_type": full_item.media_type.value,
        "name": full_item.name,
    }
    if hasattr(full_item, "authors") and full_item.authors:
        metadata["authors"] = "; ".join(full_item.authors)
    if hasattr(full_item, "narrators") and full_item.narrators:
        metadata["narrators"] = "; ".join(full_item.narrators)
    if full_item.version:
        metadata["version"] = full_item.version
    if isrc := _unambiguous_external_id(full_item, ExternalID.ISRC):
        metadata["isrc"] = isrc
    if mbid := _unambiguous_external_id(full_item, ExternalID.MB_RECORDING):
        metadata["mbid"] = mbid
    if mb_track := _unambiguous_external_id(full_item, ExternalID.MB_TRACK):
        metadata["mb_track"] = mb_track

    # collect one provider mapping per domain (highest quality)
    prov_infos: list[ProviderMappingInfo] = []
    seen_domains: set[str] = set()
    # provider_mappings is a set, so its iteration order is not guaranteed stable across
    # runs; tie-break equal-quality mappings by identity so the chosen primary URI is
    # deterministic instead of process-dependent. Available mappings are ranked before
    # unavailable ones so an unplayable primary URI is never chosen while a playable
    # sibling mapping exists (in the same or another domain).
    sorted_mappings = sorted(
        full_item.provider_mappings,
        key=lambda x: (
            not x.available,
            -x.quality,
            x.provider_domain,
            x.provider_instance,
            x.item_id,
        ),
    )
    for prov_mapping in sorted_mappings:
        domain = prov_mapping.provider_domain
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        prov_infos.append(
            ProviderMappingInfo(
                domain=domain,
                item_id=prov_mapping.item_id,
                instance_id=prov_mapping.provider_instance,
                content_type=prov_mapping.audio_format.content_type.value,
                sample_rate=prov_mapping.audio_format.sample_rate,
                bit_depth=prov_mapping.audio_format.bit_depth,
                bit_rate=prov_mapping.audio_format.bit_rate or 0,
            )
        )

    # primary URI = highest quality provider (or first if no mappings)
    if prov_infos:
        primary = prov_infos[0]
        primary_uri = create_uri(full_item.media_type, primary.domain, primary.item_id)
    else:
        primary_uri = full_item.uri or ""

    artist_infos = collect_artist_infos(full_item)
    album_info = collect_album_info(full_item)
    podcast_info = collect_podcast_info(full_item)

    # collect images
    images: list[ImageInfo] = []
    if hasattr(full_item, "metadata") and full_item.metadata and full_item.metadata.images:
        for img in full_item.metadata.images:
            images.append(
                ImageInfo(
                    type=img.type.value,
                    path=img.path,
                    provider=img.provider,
                    remotely_accessible=img.remotely_accessible,
                )
            )

    return PlaylistItem(
        path=primary_uri,
        title=title,
        length=str(duration) if duration else None,
        metadata=metadata,
        providers=prov_infos,
        images=images,
        artists=artist_infos,
        album=album_info,
        podcast=podcast_info,
    )


def _unambiguous_external_id(full_item: MediaItem, external_id_type: ExternalID) -> str | None:
    """
    Return an external ID value only when the item carries exactly one of that type.

    A library item merges external IDs from every matched provider, so more than
    one distinct value for the same type means they disagree about which release
    the item actually is (e.g. different MB_TRACK release-track pairings) - persisting
    one arbitrarily would misrepresent it as reliable release evidence on export.
    """
    values = {
        value for current_type, value in full_item.external_ids if current_type == external_id_type
    }
    if len(values) == 1:
        return next(iter(values))
    return None


# --------------------------------------------------------------------------- #
#  Internal line parsers                                                       #
# --------------------------------------------------------------------------- #


def _parse_hls_stream_info(line: str) -> dict[str, str]:
    """Parse #EXT-X-STREAM-INF into a dict of stream properties."""
    stream_info: dict[str, str] = {}
    for part in line.replace("#EXT-X-STREAM-INF:", "").split(","):
        if "=" not in part:
            continue
        kev_value_parts = part.strip().split("=")
        stream_info[kev_value_parts[0]] = kev_value_parts[1]
    return stream_info


def _parse_hls_key(line: str, current_key: str | None) -> str | None:
    """Parse #EXT-X-KEY and return the key URI, or None for METHOD=NONE."""
    if "METHOD=NONE" in line:
        return None
    if ",URI=" in line:
        return line.split(",URI=")[1].strip('"')
    return current_key


def _parse_extma_line(line: str) -> dict[str, str]:
    """Parse #EXTMA into a metadata dict (double-pipe-separated key=value pairs)."""
    raw = line.split("#EXTMA:", 1)[1]
    metadata: dict[str, str] = {}
    for pair in raw.split(FIELD_SEPARATOR):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        metadata[k.strip()] = v.strip()
    return metadata


def _parse_extprov_line(line: str) -> ProviderMappingInfo | None:
    """Parse #EXTPROV (double-pipe-separated, 2 required + 5 optional fields)."""
    parts = line.split("#EXTPROV:", 1)[1].strip().split(FIELD_SEPARATOR)
    if len(parts) < 2 or len(parts) > 7:
        return None
    try:
        return ProviderMappingInfo(
            domain=parts[0],
            item_id=parts[1],
            instance_id=parts[2] if len(parts) > 2 else "",
            content_type=parts[3] if len(parts) > 3 else "",
            sample_rate=int(parts[4]) if len(parts) > 4 and parts[4] else 0,
            bit_depth=int(parts[5]) if len(parts) > 5 and parts[5] else 0,
            bit_rate=int(parts[6]) if len(parts) > 6 and parts[6] else 0,
        )
    except ValueError:
        return None


def _parse_extimg_line(line: str) -> ImageInfo | None:
    """Parse #EXTIMG (double-pipe-separated, expects 4 fields)."""
    parts = line.split("#EXTIMG:", 1)[1].strip().split(FIELD_SEPARATOR)
    if len(parts) != 4:
        return None
    return ImageInfo(
        type=parts[0],
        path=parts[1],
        provider=parts[2],
        remotely_accessible=parts[3].lower() == "true",
    )


def _parse_extartist_line(line: str) -> ArtistInfo | None:
    """Parse #EXTARTIST (double-pipe-separated, expects 4 fields)."""
    parts = line.split("#EXTARTIST:", 1)[1].strip().split(FIELD_SEPARATOR)
    if len(parts) != 4 or not parts[0]:
        return None
    return ArtistInfo(
        name=parts[0],
        provider_domain=parts[1],
        item_id=parts[2],
        provider_instance=parts[3],
    )


def _parse_extalbum_line(line: str) -> AlbumInfo | None:
    """Parse #EXTALBUM (double-pipe-separated, expects 5 fields)."""
    parts = line.split("#EXTALBUM:", 1)[1].strip().split(FIELD_SEPARATOR)
    if len(parts) != 5 or not parts[0]:
        return None
    return AlbumInfo(
        name=parts[0],
        provider_domain=parts[1],
        item_id=parts[2],
        provider_instance=parts[3],
        version=parts[4],
    )


def _parse_extpodcast_line(line: str) -> PodcastInfo | None:
    """Parse #EXTPODCAST (double-pipe-separated, expects 4 fields)."""
    parts = line.split("#EXTPODCAST:", 1)[1].strip().split(FIELD_SEPARATOR)
    if len(parts) != 4 or not parts[0]:
        return None
    return PodcastInfo(
        name=parts[0],
        provider_domain=parts[1],
        item_id=parts[2],
        provider_instance=parts[3],
    )
