"""Parsers for official Tidal API (openapi.tidal.com/v2) JSON:API responses."""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import (
    AlbumType,
    ContentType,
    ExternalID,
    ImageType,
    LinkType,
    MediaType,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    AudioMetadata,
    MediaItemImage,
    MediaItemLink,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.helpers.util import infer_album_type, parse_title_and_version

if TYPE_CHECKING:
    from ._openapi_models import AlbumsAttributes, ArtistsAttributes, TracksAttributes
    from .jsonapi import JsonApiDocument
    from .provider import TidalProvider

# Preferred artwork width; Tidal serves square art in a range of sizes.
_PREFERRED_IMAGE_WIDTH = 750

_ISO_DURATION_RE = re.compile(
    r"^P(?:\d+Y)?(?:\d+M)?(?:\d+W)?(?:\d+D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$"
)

# Tidal biographies embed internal cross-reference markup, e.g.
# [wimpLink artistId="123"]Some Artist[/wimpLink]. Strip the tags, keep the text.
_WIMP_LINK_RE = re.compile(r"\[/?wimpLink[^\]]*\]")

# Tidal externalLinks meta.type -> generic LinkType. Types not listed here
# (Tidal share links, autoplay and payment/claim redirects) are not exposed.
_LINK_TYPE = {
    "OFFICIAL_HOMEPAGE": LinkType.WEBSITE,
    "FACEBOOK": LinkType.FACEBOOK,
    "TWITTER": LinkType.TWITTER,
    "INSTAGRAM": LinkType.INSTAGRAM,
    "TIKTOK": LinkType.TIKTOK,
    "SNAPCHAT": LinkType.SNAPCHAT,
}

# Tidal key enum -> pitch-class notation used in Metadata.musical_key.
_KEY_PITCH = {
    "C": "C",
    "CSharp": "C#",
    "D": "D",
    "Eb": "Eb",
    "E": "E",
    "F": "F",
    "FSharp": "F#",
    "G": "G",
    "Ab": "Ab",
    "A": "A",
    "Bb": "Bb",
    "B": "B",
}


def parse_artist(provider: TidalProvider, doc: JsonApiDocument, resource: dict[str, Any]) -> Artist:
    """Parse an official Tidal artist resource to a generic Artist."""
    artist_id = str(resource["id"])
    attributes: ArtistsAttributes = resource.get("attributes", {})
    artist = Artist(
        item_id=artist_id,
        provider=provider.instance_id,
        name=attributes.get("name", ""),
        provider_mappings={
            ProviderMapping(
                item_id=artist_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=f"https://tidal.com/artist/{artist_id}",
            )
        },
    )
    if attributes.get("popularity") is not None:
        artist.metadata.popularity = _scale_popularity(attributes["popularity"])
    if links := _parse_links(attributes):
        artist.metadata.links = links
    if image := _resolve_image(provider, doc, resource, "profileArt"):
        artist.metadata.images = UniqueList([image])
    if biography := doc.related_one(resource, "biography"):
        if text := biography.get("attributes", {}).get("text"):
            artist.metadata.description = _clean_biography(text)
    return artist


def parse_album(provider: TidalProvider, doc: JsonApiDocument, resource: dict[str, Any]) -> Album:
    """Parse an official Tidal album resource to a generic Album."""
    album_id = str(resource["id"])
    attributes: AlbumsAttributes = resource.get("attributes", {})
    name, version = _split_title_version(
        attributes.get("title", "Unknown Album"), attributes.get("version") or None
    )
    available = "STREAM" in (attributes.get("availability") or ["STREAM"])
    album = Album(
        item_id=album_id,
        provider=provider.instance_id,
        name=name,
        version=version,
        provider_mappings={
            ProviderMapping(
                item_id=album_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(content_type=ContentType.FLAC),
                url=f"https://tidal.com/album/{album_id}",
                available=available,
            )
        },
    )

    various_artists = False
    for artist_resource in doc.related(resource, "artists"):
        artist = parse_artist(provider, doc, artist_resource)
        if artist.name == "Various Artists":
            various_artists = True
        album.artists.append(artist)

    album.album_type = _map_album_type(attributes.get("albumType"), name, version, various_artists)

    if release_date := attributes.get("releaseDate"):
        with suppress(ValueError, IndexError):
            album.year = int(release_date.split("-")[0])
        with suppress(ValueError):
            album.metadata.release_date = datetime.fromisoformat(release_date)

    if barcode := attributes.get("barcodeId"):
        album.external_ids.add((ExternalID.BARCODE, barcode))
    if copyright_data := attributes.get("copyright"):
        album.metadata.copyright = copyright_data.get("text", "")
    album.metadata.explicit = attributes.get("explicit", False)
    if attributes.get("popularity") is not None:
        album.metadata.popularity = _scale_popularity(attributes["popularity"])
    if genres := _parse_genres(doc, resource):
        album.metadata.genres = genres
    if links := _parse_links(attributes):
        album.metadata.links = links
    if image := _resolve_image(provider, doc, resource, "coverArt"):
        album.metadata.images = UniqueList([image])

    return album


def parse_track(provider: TidalProvider, doc: JsonApiDocument, resource: dict[str, Any]) -> Track:
    """Parse an official Tidal track resource to a generic Track."""
    track_id = str(resource["id"])
    attributes: TracksAttributes = resource.get("attributes", {})
    name, version = _split_title_version(
        attributes.get("title", "Unknown"), attributes.get("version") or None
    )
    hi_res_lossless = "HIRES_LOSSLESS" in (attributes.get("mediaTags") or [])
    available = "STREAM" in (attributes.get("availability") or ["STREAM"])
    track = Track(
        item_id=track_id,
        provider=provider.instance_id,
        name=name,
        version=version,
        duration=_parse_iso_duration(attributes.get("duration", "")),
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.FLAC,
                    bit_depth=24 if hi_res_lossless else 16,
                ),
                url=f"https://tidal.com/track/{track_id}",
                available=available,
            )
        },
    )

    if isrc := attributes.get("isrc"):
        track.external_ids.add((ExternalID.ISRC, isrc))

    track.artists = UniqueList(
        [
            parse_artist(provider, doc, artist_resource)
            for artist_resource in doc.related(resource, "artists")
        ]
    )

    track.metadata.explicit = attributes.get("explicit", False)
    if attributes.get("popularity") is not None:
        track.metadata.popularity = _scale_popularity(attributes["popularity"])
    if copyright_data := attributes.get("copyright"):
        track.metadata.copyright = copyright_data.get("text", "")

    if genres := _parse_genres(doc, resource):
        track.metadata.genres = genres
    if links := _parse_links(attributes):
        track.metadata.links = links

    bpm = attributes.get("bpm")
    musical_key = _parse_musical_key(attributes.get("key"), attributes.get("keyScale"))
    if bpm is not None or musical_key:
        track.audio_metadata = AudioMetadata(bpm=bpm, musical_key=musical_key)

    # The album relationship carries a minimal album resource; use an ItemMapping
    # (as with the unofficial API) and take the track image from the album cover.
    if album_resource := doc.related_one(resource, "albums"):
        album_attributes: dict[str, Any] = album_resource.get("attributes", {})
        track.album = provider.get_item_mapping(
            media_type=MediaType.ALBUM,
            key=str(album_resource["id"]),
            name=album_attributes.get("title") or "",
        )
        if image := _resolve_image(provider, doc, album_resource, "coverArt"):
            track.metadata.images = UniqueList([image])

    return track


def _split_title_version(title: str, version: str | None) -> tuple[str, str]:
    """
    Split a title into (name, version).

    The official API provides an explicit version qualifier (e.g. "International
    Version"). When present, trust it: strip that exact qualifier from the end of
    the title to form the name. This is more reliable than the shared heuristic,
    which guesses the version from bracketed parts of the title and mishandles
    titles carrying more than one parenthetical. Fall back to the heuristic only
    when no explicit version is given.
    """
    if not version:
        return parse_title_and_version(title)
    # Strip a trailing "(<version>)", "[<version>]" or " - <version>" qualifier.
    pattern = rf"\s*(?:-\s*)?[(\[]?\s*{re.escape(version)}\s*[)\]]?\s*$"
    name = re.sub(pattern, "", title, flags=re.IGNORECASE).strip()
    return name or title, version


def _clean_biography(text: str) -> str:
    """Strip Tidal's internal [wimpLink] cross-reference markup from bio text."""
    return _WIMP_LINK_RE.sub("", text).strip()


def _parse_genres(doc: JsonApiDocument, resource: dict[str, Any]) -> set[str] | None:
    """Resolve the genres relationship to a set of genre names."""
    genres = {
        name
        for genre in doc.related(resource, "genres")
        if (name := genre.get("attributes", {}).get("genreName"))
    }
    return genres or None


def _parse_links(attributes: Mapping[str, Any]) -> set[MediaItemLink] | None:
    """Map the externalLinks attribute to a set of generic MediaItemLinks."""
    links = {
        MediaItemLink(type=link_type, url=href)
        for link in attributes.get("externalLinks") or []
        if (link_type := _LINK_TYPE.get(link.get("meta", {}).get("type", "")))
        and (href := link.get("href"))
    }
    return links or None


def _scale_popularity(value: float) -> int:
    """Convert the official API's 0..1 popularity to the 0..100 scale MA uses."""
    return round(value * 100)


def _parse_iso_duration(value: str) -> int:
    """Parse an ISO-8601 duration (e.g. "PT3M57S") into whole seconds."""
    if not value or not (match := _ISO_DURATION_RE.match(value)):
        return 0
    hours, minutes, seconds = match.groups()
    total = int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)
    return int(total)


def _parse_musical_key(key: str | None, scale: str | None) -> str | None:
    """Build a Metadata.musical_key value (e.g. "F# minor") from Tidal's enums."""
    pitch = _KEY_PITCH.get(key or "")
    if not pitch:
        return None
    if scale and scale != "UNKNOWN":
        return f"{pitch} {scale.lower().replace('_', ' ')}"
    return pitch


def _map_album_type(
    album_type: str | None, name: str, version: str | None, various_artists: bool
) -> AlbumType:
    """Map the official albumType (plus inference) to a generic AlbumType."""
    if various_artists:
        return AlbumType.COMPILATION
    inferred = infer_album_type(name, version or "")
    if inferred in (AlbumType.SOUNDTRACK, AlbumType.LIVE):
        return inferred
    return {
        "ALBUM": AlbumType.ALBUM,
        "EP": AlbumType.EP,
        "SINGLE": AlbumType.SINGLE,
    }.get(album_type or "ALBUM", AlbumType.ALBUM)


def _resolve_image(
    provider: TidalProvider,
    doc: JsonApiDocument,
    resource: dict[str, Any],
    relationship: str,
) -> MediaItemImage | None:
    """Resolve an artwork relationship to a MediaItemImage, if available."""
    artwork = doc.related_one(resource, relationship)
    if not artwork:
        return None
    files = artwork.get("attributes", {}).get("files") or []
    if not (url := _select_image_url(files)):
        return None
    return MediaItemImage(
        type=ImageType.THUMB,
        path=url,
        provider=provider.instance_id,
        remotely_accessible=True,
    )


def _select_image_url(files: list[dict[str, Any]]) -> str | None:
    """Pick the artwork file nearest the preferred width (preferring larger)."""
    usable = [f for f in files if f.get("href")]
    if not usable:
        return None

    def sort_key(file: dict[str, Any]) -> tuple[bool, int]:
        width = file.get("meta", {}).get("width", 0)
        # Prefer the smallest file at or above the preferred width; if none reach
        # it, fall back to the largest available.
        below = width < _PREFERRED_IMAGE_WIDTH
        distance = abs(width - _PREFERRED_IMAGE_WIDTH)
        return (below, distance)

    return str(min(usable, key=sort_key)["href"])
