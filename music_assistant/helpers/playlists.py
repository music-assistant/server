"""Helpers for parsing (online and offline) playlists."""

from __future__ import annotations

import configparser
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from aiohttp import ClientTimeout, client_exceptions
from music_assistant_models.enums import ContentType, ExternalID, ImageType, MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Artist,
    Audiobook,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    Track,
    UniqueList,
)

from music_assistant.helpers.util import detect_charset

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


LOGGER = logging.getLogger(__name__)
HLS_CONTENT_TYPES = ("application/vnd.apple.mpegurl",)


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


def parse_m3u(m3u_data: str) -> list[PlaylistItem]:
    """Parse M3U/M3U8 data into a list of PlaylistItem entries.

    Supports standard tags (#EXTINF, #EXT-X-STREAM-INF, #EXT-X-KEY) and
    Music Assistant extensions (#EXTMA, #EXTPROV, #EXTIMG).
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
                )
            )
            # reset accumulators
            length = None
            title = None
            stream_info = None
            metadata = None
            providers = []
            images = []

    return playlist


def parse_m3u_playlist_name(m3u_data: str) -> str | None:
    """Extract the playlist name from an M3U #PLAYLIST directive."""
    for line in m3u_data.splitlines():
        line = line.strip()  # noqa: PLW2901
        if line.startswith("#PLAYLIST:"):
            return line.split("#PLAYLIST:", 1)[1].strip()
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


async def fetch_playlist(
    mass: MusicAssistant, url: str, raise_on_hls: bool = True
) -> list[PlaylistItem]:
    """Fetch and parse a remote M3U or PLS playlist."""
    try:
        async with mass.http_session.get(
            url, allow_redirects=True, timeout=ClientTimeout(total=5)
        ) as resp:
            try:
                raw_data = await resp.content.read(64 * 1024)
                encoding = resp.charset or await detect_charset(raw_data)
                playlist_data = raw_data.decode(encoding, errors="replace")
            except (ValueError, UnicodeDecodeError) as err:
                msg = f"Could not decode playlist {url}"
                raise InvalidDataError(msg) from err
    except TimeoutError as err:
        msg = f"Timeout while fetching playlist {url}"
        raise InvalidDataError(msg) from err
    except client_exceptions.ClientError as err:
        msg = f"Error while fetching playlist {url}"
        raise InvalidDataError(msg) from err

    if (
        raise_on_hls and "#EXT-X-VERSION:" in playlist_data
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


# --------------------------------------------------------------------------- #
#  Generation                                                                  #
# --------------------------------------------------------------------------- #


def generate_m3u(
    playlist_name: str,
    items: Sequence[PlaylistItem],
) -> str:
    """Generate an M3U8 playlist string from PlaylistItem entries.

    :param playlist_name: Human-readable name (written as #PLAYLIST directive).
    :param items: Entries to write. Only fields that are set are emitted.
    """
    lines: list[str] = ["#EXTM3U", f"#PLAYLIST:{playlist_name}"]
    for item in items:
        if item.metadata:
            pairs = ",".join(f"{k}={v}" for k, v in item.metadata.items())
            lines.append(f"#EXTMA:{pairs}")
        for prov in item.providers:
            lines.append(
                f"#EXTPROV:{prov.domain}|{prov.item_id}"
                f"|{prov.instance_id}|{prov.content_type}"
                f"|{prov.sample_rate}|{prov.bit_depth}|{prov.bit_rate}"
            )
        for img in item.images:
            remotely = "true" if img.remotely_accessible else "false"
            lines.append(f"#EXTIMG:{img.type}|{img.path}|{img.provider}|{remotely}")
        if item.title is not None and item.length is not None:
            lines.append(f"#EXTINF:{item.length},{item.title}")
        lines.append(item.path)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
#  Media item construction from playlist metadata                              #
# --------------------------------------------------------------------------- #


def construct_media_item_from_playlist_item(
    item: PlaylistItem,
    mass: MusicAssistant,
) -> MediaItemType:
    """Construct a MediaItem from a PlaylistItem's stored metadata.

    Resolves provider mappings by instance_id first (free dict lookup),
    falling back to domain if the instance no longer exists.

    :param item: Parsed PlaylistItem with metadata, providers, and images.
    :param mass: MusicAssistant instance for provider resolution.
    """
    metadata = item.metadata or {}
    try:
        media_type = MediaType(metadata.get("media_type", "track"))
    except ValueError:
        media_type = MediaType.TRACK
    artist_name, track_name = parse_extinf_title(item.title)
    name = track_name or item.path
    try:
        duration = int(item.length) if item.length else 0
    except ValueError:
        duration = 0

    # resolve provider mappings: try instance_id, fall back to domain
    # always include the mapping; mark available=False if provider is not loaded
    provider_mappings: set[ProviderMapping] = set()
    audio_format = AudioFormat()
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

    external_ids: set[tuple[ExternalID, str]] = set()
    if isrc := metadata.get("isrc"):
        external_ids.add((ExternalID.ISRC, isrc))
    if mbid := metadata.get("mbid"):
        external_ids.add((ExternalID.MB_RECORDING, mbid))

    # use first available provider domain as the item's provider, or "builtin" as fallback
    first_provider = next(
        (pm for pm in provider_mappings if pm.available),
        next(iter(provider_mappings), None),
    )
    item_provider = first_provider.provider_domain if first_provider else "builtin"
    item_instance = first_provider.provider_instance if first_provider else "builtin"

    media_item: MediaItemType
    if media_type == MediaType.RADIO:
        media_item = Radio(
            item_id=item.path,
            provider=item_provider,
            name=name,
            provider_mappings=provider_mappings,
            external_ids=external_ids,
        )
    elif media_type == MediaType.PODCAST_EPISODE:
        podcast_name = metadata.get("podcast", "")
        media_item = PodcastEpisode(
            item_id=item.path,
            provider=item_provider,
            name=name,
            duration=duration,
            position=0,
            provider_mappings=provider_mappings,
            podcast=ItemMapping(
                item_id=podcast_name,
                provider=item_provider,
                name=podcast_name,
                media_type=MediaType.PODCAST,
            ),
            external_ids=external_ids,
        )
    elif media_type == MediaType.AUDIOBOOK:
        media_item = Audiobook(
            item_id=item.path,
            provider=item_provider,
            name=name,
            duration=duration,
            provider_mappings=provider_mappings,
            authors=UniqueList(
                metadata.get("authors", "").split("; ") if metadata.get("authors") else []
            ),
            narrators=UniqueList(
                metadata.get("narrators", "").split("; ") if metadata.get("narrators") else []
            ),
            external_ids=external_ids,
        )
    else:
        artists: UniqueList[Artist | ItemMapping] = UniqueList()
        if artist_name:
            artists.append(
                Artist(
                    item_id=artist_name,
                    provider=item_provider,
                    name=artist_name,
                    provider_mappings={
                        ProviderMapping(
                            item_id=artist_name,
                            provider_domain=item_provider,
                            provider_instance=item_instance,
                            available=False,
                        )
                    },
                )
            )
        media_item = Track(
            item_id=item.path,
            provider=item_provider,
            name=name,
            duration=duration,
            artists=artists,
            provider_mappings=provider_mappings,
            external_ids=external_ids,
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
    """Parse #EXTMA into a metadata dict (comma-separated key=value pairs)."""
    raw = line.split("#EXTMA:", 1)[1]
    metadata: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        metadata[k.strip()] = v.strip()
    return metadata


def _parse_extprov_line(line: str) -> ProviderMappingInfo | None:
    """Parse #EXTPROV (pipe-separated: domain|item_id|instance_id|content_type|sr|bd|br)."""
    parts = line.split("#EXTPROV:", 1)[1].strip().split("|")
    if len(parts) < 2:
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
    except (ValueError, IndexError):
        return None


def _parse_extimg_line(line: str) -> ImageInfo | None:
    """Parse #EXTIMG (pipe-separated: type|path|provider|remotely_accessible)."""
    parts = line.split("#EXTIMG:", 1)[1].strip().split("|")
    if len(parts) < 3:
        return None
    return ImageInfo(
        type=parts[0],
        path=parts[1],
        provider=parts[2],
        remotely_accessible=parts[3].lower() == "true" if len(parts) > 3 else False,
    )
