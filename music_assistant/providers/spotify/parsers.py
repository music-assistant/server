"""Parsing utilities to convert Spotify API responses into Music Assistant model objects."""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import AlbumType, ContentType, ExternalID, ImageType
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    AudioFormat,
    MediaItemImage,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.helpers.util import infer_album_type, parse_title_and_version

if TYPE_CHECKING:
    from .provider import SpotifyProvider


def parse_images(
    images_list: list[dict[str, Any]], instance_id: str, exclude_generic: bool = False
) -> UniqueList[MediaItemImage]:
    """Parse images list into MediaItemImage objects."""
    if not images_list:
        return UniqueList([])

    # Filter out generic images if requested (for artists)
    filtered_images = []
    for img in images_list:
        img_url = img["url"]
        if exclude_generic and "2a96cbd8b46e442fc41c2b86b821562f" in img_url:
            continue
        filtered_images.append(img)

    if not filtered_images:
        return UniqueList([])

    # Spotify images come in various sizes (typically 640x640, 300x300, 64x64)
    # Find the largest image available
    best_image = max(
        filtered_images, key=lambda img: img.get("height", 0), default=filtered_images[0]
    )

    return UniqueList(
        [
            MediaItemImage(
                type=ImageType.THUMB,
                path=best_image["url"],
                provider=instance_id,
                remotely_accessible=True,
            )
        ]
    )


def parse_artist(artist_obj: dict[str, Any], provider: SpotifyProvider) -> Artist:
    """Parse spotify artist object to generic layout."""
    artist = Artist(
        item_id=artist_obj["id"],
        provider=provider.instance_id,
        name=artist_obj["name"] or artist_obj["id"],
        provider_mappings={
            ProviderMapping(
                item_id=artist_obj["id"],
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=artist_obj["external_urls"]["spotify"],
            )
        },
    )
    if "genres" in artist_obj:
        artist.metadata.genres = set(artist_obj["genres"])

    # Use unified image parsing with generic exclusion
    artist.metadata.images = parse_images(
        artist_obj.get("images", []), provider.instance_id, exclude_generic=True
    )
    return artist


def parse_album(album_obj: dict[str, Any], provider: SpotifyProvider) -> Album:
    """Parse spotify album object to generic layout."""
    name, version = parse_title_and_version(album_obj["name"])
    album = Album(
        item_id=album_obj["id"],
        provider=provider.instance_id,
        name=name,
        version=version,
        provider_mappings={
            ProviderMapping(
                item_id=album_obj["id"],
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(content_type=ContentType.OGG, bit_rate=320),
                url=album_obj["external_urls"]["spotify"],
            )
        },
    )
    if "external_ids" in album_obj and album_obj["external_ids"].get("upc"):
        album.external_ids.add((ExternalID.BARCODE, "0" + album_obj["external_ids"]["upc"]))
    if "external_ids" in album_obj and album_obj["external_ids"].get("ean"):
        album.external_ids.add((ExternalID.BARCODE, album_obj["external_ids"]["ean"]))

    for artist_obj in album_obj["artists"]:
        if not artist_obj.get("name") or not artist_obj.get("id"):
            continue
        album.artists.append(parse_artist(artist_obj, provider))

    with contextlib.suppress(ValueError):
        album.album_type = AlbumType(album_obj["album_type"])

    # Override with inferred type if version indicates it
    inferred_type = infer_album_type(album.name, album.version)
    if inferred_type in (AlbumType.LIVE, AlbumType.SOUNDTRACK):
        album.album_type = inferred_type

    if "genres" in album_obj:
        album.metadata.genres = set(album_obj["genres"])

    album.metadata.images = parse_images(album_obj.get("images", []), provider.instance_id)

    if "label" in album_obj:
        album.metadata.label = album_obj["label"]
    if album_obj.get("release_date"):
        album.year = int(album_obj["release_date"].split("-")[0])
    if album_obj.get("copyrights"):
        album.metadata.copyright = album_obj["copyrights"][0]["text"]
    if album_obj.get("explicit"):
        album.metadata.explicit = album_obj["explicit"]
    return album


def parse_track(
    track_obj: dict[str, Any],
    provider: SpotifyProvider,
    artist: Artist | None = None,
) -> Track:
    """Parse spotify track object to generic layout."""
    name, version = parse_title_and_version(track_obj["name"])
    track = Track(
        item_id=track_obj["id"],
        provider=provider.instance_id,
        name=name,
        version=version,
        duration=track_obj["duration_ms"] / 1000,
        provider_mappings={
            ProviderMapping(
                item_id=track_obj["id"],
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(content_type=ContentType.OGG, bit_rate=320),
                url=track_obj["external_urls"]["spotify"],
                available=not track_obj["is_local"] and track_obj["is_playable"],
            )
        },
        disc_number=track_obj.get("disc_number", 0),
        track_number=track_obj.get("track_number", 0),
    )
    if isrc := track_obj.get("external_ids", {}).get("isrc"):
        track.external_ids.add((ExternalID.ISRC, isrc))

    if artist:
        track.artists.append(artist)
    for track_artist in track_obj.get("artists", []):
        if not track_artist.get("name") or not track_artist.get("id"):
            continue
        artist_parsed = parse_artist(track_artist, provider)
        if artist_parsed and artist_parsed.item_id not in {x.item_id for x in track.artists}:
            track.artists.append(artist_parsed)

    track.metadata.explicit = track_obj["explicit"]
    if "preview_url" in track_obj:
        track.metadata.preview = track_obj["preview_url"]
    if "album" in track_obj:
        track.album = parse_album(track_obj["album"], provider)
        track.metadata.images = parse_images(
            track_obj["album"].get("images", []), provider.instance_id
        )
    if track_obj.get("copyright"):
        track.metadata.copyright = track_obj["copyright"]
    if track_obj.get("explicit"):
        track.metadata.explicit = True
    if track_obj.get("popularity"):
        track.metadata.popularity = track_obj["popularity"]
    return track


def parse_playlist(playlist_obj: dict[str, Any], provider: SpotifyProvider) -> Playlist:
    """Parse spotify playlist object to generic layout."""
    owner_id = playlist_obj["owner"].get("id", "")
    is_editable = (
        provider._sp_user is not None and owner_id == provider._sp_user["id"]
    ) or playlist_obj["collaborative"]

    # Spotify-owned playlists (Daily Mix, Discover Weekly, etc.) are personalized per user
    is_spotify_owned = owner_id.lower() == "spotify"

    # Get owner name with fallback
    owner_name = playlist_obj["owner"].get("display_name")
    if owner_name is None and provider._sp_user is not None:
        owner_name = provider._sp_user["display_name"]

    # Mark as unique if user-owned/editable OR if it's a Spotify personalized playlist
    is_unique = is_editable or is_spotify_owned

    playlist = Playlist(
        item_id=playlist_obj["id"],
        provider=provider.instance_id,
        name=playlist_obj["name"],
        owner=owner_name,
        provider_mappings={
            ProviderMapping(
                item_id=playlist_obj["id"],
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=playlist_obj["external_urls"]["spotify"],
                is_unique=is_unique,
            )
        },
        is_editable=is_editable,
    )

    playlist.metadata.images = parse_images(playlist_obj.get("images", []), provider.instance_id)
    return playlist


def parse_podcast(podcast_obj: dict[str, Any], provider: SpotifyProvider) -> Podcast:
    """Parse spotify podcast (show) object to generic layout."""
    podcast = Podcast(
        item_id=podcast_obj["id"],
        provider=provider.instance_id,
        name=podcast_obj["name"],
        provider_mappings={
            ProviderMapping(
                item_id=podcast_obj["id"],
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=podcast_obj["external_urls"]["spotify"],
            )
        },
        publisher=podcast_obj.get("publisher"),
        total_episodes=podcast_obj.get("total_episodes"),
    )

    # Set metadata
    if podcast_obj.get("description"):
        podcast.metadata.description = podcast_obj["description"]

    podcast.metadata.images = parse_images(podcast_obj.get("images", []), provider.instance_id)

    if "explicit" in podcast_obj:
        podcast.metadata.explicit = podcast_obj["explicit"]

    # Convert languages list to genres for categorization
    if "languages" in podcast_obj:
        podcast.metadata.genres = set(podcast_obj["languages"])

    return podcast


def parse_podcast_episode(
    episode_obj: dict[str, Any], provider: SpotifyProvider, podcast: Podcast | None = None
) -> PodcastEpisode:
    """Parse spotify podcast episode object to generic layout."""
    # Get or create a basic podcast reference if not provided
    if podcast is None and "show" in episode_obj:
        podcast = Podcast(
            item_id=episode_obj["show"]["id"],
            provider=provider.instance_id,
            name=episode_obj["show"]["name"],
            provider_mappings={
                ProviderMapping(
                    item_id=episode_obj["show"]["id"],
                    provider_domain=provider.domain,
                    provider_instance=provider.instance_id,
                    url=episode_obj["show"]["external_urls"]["spotify"],
                )
            },
        )
    elif podcast is None:
        # Create a minimal podcast reference if none available
        podcast = Podcast(
            item_id="unknown",
            provider=provider.instance_id,
            name="Unknown Podcast",
            provider_mappings=set(),
        )

    episode = PodcastEpisode(
        item_id=episode_obj["id"],
        provider=provider.instance_id,
        name=episode_obj["name"],
        duration=episode_obj["duration_ms"] // 1000 if episode_obj.get("duration_ms") else 0,
        podcast=podcast,
        position=0,
        provider_mappings={
            ProviderMapping(
                item_id=episode_obj["id"],
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(content_type=ContentType.OGG, bit_rate=160),
                url=episode_obj["external_urls"]["spotify"],
            )
        },
    )

    # Set description in metadata
    if episode_obj.get("description"):
        episode.metadata.description = episode_obj["description"]

    # Add release date to metadata
    if episode_obj.get("release_date"):
        with contextlib.suppress(ValueError, TypeError):
            date_str = episode_obj["release_date"].strip()

            if len(date_str) == 4:
                # Year only: "2023" -> "2023-01-01T00:00:00+00:00"
                date_str = f"{date_str}-01-01T00:00:00+00:00"
            elif len(date_str) == 10:
                # Date only: "2023-12-25" -> "2023-12-25T00:00:00+00:00"
                date_str = f"{date_str}T00:00:00+00:00"

            episode.metadata.release_date = datetime.fromisoformat(date_str)

    episode.metadata.images = parse_images(episode_obj.get("images", []), provider.instance_id)

    # Use podcast artwork if episode has none
    if not episode.metadata.images and isinstance(podcast, Podcast) and podcast.metadata.images:
        episode.metadata.images = podcast.metadata.images

    if "explicit" in episode_obj:
        episode.metadata.explicit = episode_obj["explicit"]

    if "audio_preview_url" in episode_obj:
        episode.metadata.preview = episode_obj["audio_preview_url"]

    return episode


def parse_audiobook(audiobook_obj: dict[str, Any], provider: SpotifyProvider) -> Audiobook:
    """Parse spotify audiobook object to generic layout."""
    audiobook = Audiobook(
        item_id=audiobook_obj["id"],
        provider=provider.instance_id,
        name=audiobook_obj["name"],
        provider_mappings={
            ProviderMapping(
                item_id=audiobook_obj["id"],
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(content_type=ContentType.OGG, bit_rate=320),
                url=audiobook_obj["external_urls"]["spotify"],
            )
        },
    )

    if "duration_ms" in audiobook_obj:
        provider.logger.debug(
            f"Found duration_ms in audiobook object: {audiobook_obj['duration_ms']}"
        )
        audiobook.duration = audiobook_obj["duration_ms"] // 1000
    else:
        provider.logger.debug(
            "No duration_ms found in main audiobook object - will calculate from chapters"
        )
        # Don't set duration here - let get_audiobook calculate it from chapters
        audiobook.duration = 0

    # Set authors
    if "authors" in audiobook_obj:
        for author_obj in audiobook_obj["authors"]:
            if author_obj.get("name"):
                audiobook.authors.append(author_obj["name"])

    # Set narrators
    if "narrators" in audiobook_obj:
        for narrator_obj in audiobook_obj["narrators"]:
            if narrator_obj.get("name"):
                audiobook.narrators.append(narrator_obj["name"])

    # Set metadata
    if audiobook_obj.get("description"):
        audiobook.metadata.description = audiobook_obj["description"]

    if audiobook_obj.get("publisher"):
        audiobook.publisher = audiobook_obj["publisher"]

    audiobook.metadata.images = parse_images(audiobook_obj.get("images", []), provider.instance_id)

    if audiobook_obj.get("explicit"):
        audiobook.metadata.explicit = audiobook_obj["explicit"]

    if audiobook_obj.get("languages"):
        audiobook.metadata.languages = audiobook_obj["languages"][0]

    # Set publication date if available
    if audiobook_obj.get("publication_date"):
        with contextlib.suppress(ValueError, TypeError):
            date_str = audiobook_obj["publication_date"].strip()
            if len(date_str) == 4:
                # Year only: "2023" -> "2023-01-01T00:00:00+00:00"
                date_str = f"{date_str}-01-01T00:00:00+00:00"
            elif len(date_str) == 10:
                # Date only: "2023-12-25" -> "2023-12-25T00:00:00+00:00"
                date_str = f"{date_str}T00:00:00+00:00"
            audiobook.metadata.release_date = datetime.fromisoformat(date_str)

    return audiobook
