"""Metadata parsing utilities for the Internet Archive provider."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from music_assistant_models.enums import AlbumType, ImageType
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    MediaItemImage,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from .constants import AUDIOBOOK_COLLECTIONS
from .helpers import clean_text, extract_year, get_image_url


def is_likely_album(doc: dict[str, Any]) -> bool:
    """
    Determine if an Internet Archive item is likely an album using metadata heuristics.

    This method uses collection types, media types, and title analysis to classify items
    without making expensive API calls to check individual file counts. This optimization
    significantly improves performance for artist browsing and search operations.

    Args:
        doc: Internet Archive document metadata

    Returns:
        True if the item is likely an album, False if likely a single track
    """
    mediatype = doc.get("mediatype", "")
    collection = doc.get("collection", [])
    title = clean_text(doc.get("title", "")).lower()

    if isinstance(collection, str):
        collection = [collection]

    # etree collection items are almost always live concert albums
    if "etree" in collection:
        return True

    # Skip obvious audiobook/speech collections
    if any(coll in AUDIOBOOK_COLLECTIONS for coll in collection):
        return False

    # Use title keywords to identify likely albums vs singles
    album_indicators = ["album", "live", "concert", "session", "collection", "compilation"]
    single_indicators = ["single", "track", "song"]

    if any(indicator in title for indicator in album_indicators):
        return True
    if any(indicator in title for indicator in single_indicators):
        return False

    # Default to treating audio items as albums - better user experience
    # Individual tracks will still be accessible through album track listings
    return bool(mediatype == "audio")


def doc_to_audiobook(
    doc: dict[str, Any], domain: str, instance_id: str, item_url_func: Callable[[str], str]
) -> Audiobook | None:
    """
    Convert Internet Archive document to Audiobook object.

    Args:
        doc: Internet Archive document metadata
        domain: Provider domain
        instance_id: Provider instance identifier
        item_url_func: Function to generate item URLs

    Returns:
        Audiobook object or None if conversion fails
    """
    identifier = doc.get("identifier")
    title = clean_text(doc.get("title"))
    creator = clean_text(doc.get("creator"))

    if not identifier or not title:
        return None

    audiobook = Audiobook(
        item_id=identifier,
        provider=instance_id,
        name=title,
        provider_mappings={create_provider_mapping(identifier, domain, instance_id, item_url_func)},
    )

    # Add author/narrator
    if creator:
        audiobook.authors.append(creator)

    # Add metadata
    if description := clean_text(doc.get("description")):
        audiobook.metadata.description = description

    # Add thumbnail
    add_item_image(audiobook, identifier, instance_id)

    return audiobook


def doc_to_track(
    doc: dict[str, Any], domain: str, instance_id: str, item_url_func: Callable[[str], str]
) -> Track | None:
    """
    Convert Internet Archive document to Track object.

    Args:
        doc: Internet Archive document metadata
        domain: Provider domain
        instance_id: Provider instance identifier
        item_url_func: Function to generate item URLs

    Returns:
        Track object or None if conversion fails
    """
    identifier = doc.get("identifier")
    title = clean_text(doc.get("title"))
    creator = clean_text(doc.get("creator"))

    if not identifier or not title:
        return None

    track = Track(
        item_id=identifier,
        provider=instance_id,
        name=title,
        provider_mappings={create_provider_mapping(identifier, domain, instance_id, item_url_func)},
    )

    # Add artist if available
    if creator:
        track.artists = UniqueList([create_artist(creator, domain, instance_id)])

    # Add thumbnail
    add_item_image(track, identifier, instance_id)

    return track


def doc_to_album(
    doc: dict[str, Any], domain: str, instance_id: str, item_url_func: Callable[[str], str]
) -> Album | None:
    """
    Convert Internet Archive document to Album object.

    Args:
        doc: Internet Archive document metadata
        domain: Provider domain
        instance_id: Provider instance identifier
        item_url_func: Function to generate item URLs

    Returns:
        Album object or None if conversion fails
    """
    identifier = doc.get("identifier")
    title = clean_text(doc.get("title"))
    creator = clean_text(doc.get("creator"))

    if not identifier or not title:
        return None

    album = Album(
        item_id=identifier,
        provider=instance_id,
        name=title,
        provider_mappings={create_provider_mapping(identifier, domain, instance_id, item_url_func)},
    )

    # Add artist if available
    if creator:
        album.artists = UniqueList([create_artist(creator, domain, instance_id)])

    # Add metadata
    if date := extract_year(doc.get("date")):
        album.year = date

    if description := clean_text(doc.get("description")):
        album.metadata.description = description

    # Add thumbnail
    add_item_image(album, identifier, instance_id)

    # Add album type
    album.album_type = AlbumType.ALBUM

    return album


def doc_to_artist(creator_name: str, domain: str, instance_id: str) -> Artist:
    """Convert creator name to Artist object."""
    return create_artist(creator_name, domain, instance_id)


def create_title_from_identifier(identifier: str) -> str:
    """Create a human-readable title from an Internet Archive identifier."""
    return identifier.replace("_", " ").replace("-", " ").title()


def artist_exists(artist: Artist, artists: list[Artist]) -> bool:
    """Check if an artist already exists in the list to avoid duplicates."""
    return any(existing.name == artist.name for existing in artists)


def create_provider_mapping(
    identifier: str, domain: str, instance_id: str, item_url_func: Callable[[str], str]
) -> ProviderMapping:
    """Create a standardized provider mapping for an item."""
    return ProviderMapping(
        item_id=identifier,
        provider_domain=domain,
        provider_instance=instance_id,
        url=item_url_func(identifier),
        available=True,
    )


def create_artist(creator_name: str, domain: str, instance_id: str) -> Artist:
    """Create an Artist object from creator name."""
    return Artist(
        item_id=creator_name,
        provider=instance_id,
        name=creator_name,
        provider_mappings={
            ProviderMapping(
                item_id=creator_name,
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
    )


def add_item_image(item: Track | Album | Audiobook, identifier: str, instance_id: str) -> None:
    """Add thumbnail image to a media item if available."""
    if thumb_url := get_image_url(identifier):
        item.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=thumb_url,
                provider=instance_id,
                remotely_accessible=True,
            )
        )
