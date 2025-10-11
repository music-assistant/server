"""Metadata parsing utilities for the Internet Archive provider."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from music_assistant_models.enums import AlbumType, ImageType
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    MediaItemImage,
    Podcast,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from .constants import AUDIOBOOK_COLLECTIONS
from .helpers import clean_text, extract_year, get_image_url


def is_likely_album(doc: dict[str, Any]) -> bool:
    """
    Determine if an Internet Archive item is likely an album using metadata heuristics.

    Uses collection types, media types, title analysis, and file count hints to classify items
    without making expensive API calls to check individual file counts.

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

    # Skip obvious audiobook/speech collections - these are handled separately
    if any(coll in AUDIOBOOK_COLLECTIONS for coll in collection):
        return False

    # Check for hints in the metadata that suggest multiple files
    # Some IA items include file count information
    if "files" in doc:
        # If we have file info and it's more than 2-3 files, likely an album
        # (accounting for derivative files like thumbnails)
        try:
            file_count = len(doc["files"]) if isinstance(doc["files"], list) else 0
            if file_count > 3:  # More than just 1-2 audio files + derivatives
                return True
        except (TypeError, KeyError):
            pass

    # Use title keywords to identify likely albums vs singles
    album_indicators = [
        "album",
        "live",
        "concert",
        "session",
        "collection",
        "compilation",
        "complete",
        "anthology",
        "best of",
        "greatest hits",
        "discography",
        "vol ",
        "volume",
        "part ",
        "disc ",
        "cd ",
        "lp ",
    ]

    single_indicators = [
        "single",
        "track",
        "song",
        "remix",
        "edit",
        "version",
        "demo",
        "instrumental",
        "acoustic version",
    ]

    # Strong album indicators in title
    if any(indicator in title for indicator in album_indicators):
        return True

    # Strong single indicators in title
    if any(indicator in title for indicator in single_indicators):
        return False

    # Collection-specific logic
    if "netlabels" in collection:
        # Netlabel releases are usually albums/EPs
        return True

    if "78rpm" in collection:
        # 78 RPM records are usually single tracks (A-side/B-side)
        return False

    if "oldtimeradio" in collection:
        # Radio shows are usually single episodes, treat as tracks
        return False

    if "audio_music" in collection:
        # General music uploads - check for multi-track indicators in title
        multi_track_indicators = ["ep", "album", "mixtape", "playlist"]
        return any(indicator in title for indicator in multi_track_indicators)

    # For unknown collections with audio mediatype, be conservative
    # Default to single track unless we have strong evidence of multiple tracks
    if mediatype == "audio":
        # Look for numbering that suggests multiple parts/tracks
        if re.search(r"\b(track|part|chapter)\s*\d+", title):
            return True  # Likely part of a larger work
        return bool(re.search(r"\b\d+\s*of\s*\d+\b", title))

    return False


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


def add_item_image(
    item: Track | Album | Audiobook | Podcast, identifier: str, instance_id: str
) -> None:
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


def is_audiobook_content(doc: dict[str, Any]) -> bool:
    """
    Determine if an Internet Archive item is audiobook content.

    Checks if the item is from a known audiobook collection.

    Args:
        doc: Internet Archive document metadata

    Returns:
        True if the item is from a known audiobook collection
    """
    collection = doc.get("collection", [])
    if isinstance(collection, str):
        collection = [collection]

    return any(coll in AUDIOBOOK_COLLECTIONS for coll in collection)


def doc_to_podcast(
    doc: dict[str, Any], domain: str, instance_id: str, item_url_func: Callable[[str], str]
) -> Podcast | None:
    """
    Convert Internet Archive document to Podcast object.

    Args:
        doc: Internet Archive document metadata
        domain: Provider domain
        instance_id: Provider instance identifier
        item_url_func: Function to generate item URLs

    Returns:
        Podcast object or None if conversion fails
    """
    identifier = doc.get("identifier")
    title = clean_text(doc.get("title"))
    creator = clean_text(doc.get("creator"))

    if not identifier or not title:
        return None

    podcast = Podcast(
        item_id=identifier,
        provider=instance_id,
        name=title,
        provider_mappings={create_provider_mapping(identifier, domain, instance_id, item_url_func)},
    )

    # Add publisher/creator
    if creator:
        podcast.publisher = creator

    # Add metadata
    if description := clean_text(doc.get("description")):
        podcast.metadata.description = description

    # Add thumbnail
    add_item_image(podcast, identifier, instance_id)

    return podcast


def is_podcast_content(doc: dict[str, Any]) -> bool:
    """
    Determine if an Internet Archive item is podcast content.

    Args:
        doc: Internet Archive document metadata

    Returns:
        True if the item is from a podcast collection
    """
    collection = doc.get("collection", [])
    if isinstance(collection, str):
        collection = [collection]

    return "podcasts" in collection
