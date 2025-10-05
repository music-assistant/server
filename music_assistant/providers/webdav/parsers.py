"""Parsing utilities for WebDAV filesystem provider."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, ExternalID, ImageType
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    AudioFormat,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import VARIOUS_ARTISTS_MBID, VARIOUS_ARTISTS_NAME
from music_assistant.helpers.compare import compare_strings, create_safe_string
from music_assistant.helpers.tags import AudioTags
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.providers.filesystem_local.helpers import (
    FileSystemItem,
    get_album_dir,
    get_artist_dir,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig


def _create_provider_mapping(
    file_item: FileSystemItem, instance_id: str, domain: str, tags: AudioTags | None = None
) -> ProviderMapping:
    """Create a ProviderMapping with AudioFormat from file item and optional tags."""
    # Determine content type - prefer file extension, fallback to tags format, then "unknown"
    content_type_str = file_item.ext or (tags.format if tags else None) or "unknown"

    return ProviderMapping(
        item_id=file_item.relative_path,
        provider_domain=domain,
        provider_instance=instance_id,
        audio_format=AudioFormat(
            content_type=ContentType.try_parse(content_type_str),
            sample_rate=tags.sample_rate if tags and tags.sample_rate else 44100,
            bit_depth=tags.bits_per_sample if tags and tags.bits_per_sample else 16,
            channels=tags.channels if tags and tags.channels else 2,
            bit_rate=tags.bit_rate if tags else None,
        ),
        details=file_item.checksum,
    )


def parse_track_from_tags(
    file_item: FileSystemItem,
    tags: AudioTags,
    instance_id: str,
    domain: str,
    config: ProviderConfig,
) -> Track:
    """Parse track from AudioTags using MA's logic."""
    name, version = parse_title_and_version(tags.title, tags.version)
    provider_mapping = _create_provider_mapping(file_item, instance_id, domain, tags)

    track = Track(
        item_id=file_item.relative_path,
        provider=instance_id,
        name=name,
        sort_name=tags.title_sort,
        version=version,
        provider_mappings={provider_mapping},
        disc_number=tags.disc or 0,
        track_number=tags.track or 0,
    )

    # Add external IDs
    if isrc_tags := tags.isrc:
        for isrsc in isrc_tags:
            track.external_ids.add((ExternalID.ISRC, isrsc))

    if acoustid := tags.get("acoustid"):
        track.external_ids.add((ExternalID.ACOUSTID, acoustid))

    # Parse album (if present)
    if tags.album:
        track.album = parse_album_from_tags(
            track_path=file_item.relative_path,
            track_tags=tags,
            instance_id=instance_id,
            domain=domain,
            config=config,
        )

    # Parse track artists
    for index, track_artist_str in enumerate(tags.artists):
        # Prefer album artist if match
        if (
            track.album
            and isinstance(track.album, Album)
            and (
                album_artist_match := next(
                    (x for x in track.album.artists if x.name == track_artist_str), None
                )
            )
        ):
            track.artists.append(album_artist_match)
            continue
        artist = parse_artist_from_tags(
            track_artist_str,
            instance_id,
            domain,
            album_dir=str(PurePosixPath(file_item.relative_path).parent) if track.album else None,
            sort_name=(
                tags.artist_sort_names[index] if index < len(tags.artist_sort_names) else None
            ),
            mbid=(
                tags.musicbrainz_artistids[index]
                if index < len(tags.musicbrainz_artistids)
                else None
            ),
        )
        track.artists.append(artist)

    # Set other metadata
    track.duration = int(tags.duration or 0)
    track.metadata.genres = set(tags.genres)
    track.metadata.copyright = tags.get("copyright")
    track.metadata.lyrics = tags.lyrics
    track.metadata.description = tags.get("comment")

    explicit_tag = tags.get("itunesadvisory")
    if explicit_tag is not None:
        track.metadata.explicit = explicit_tag == "1"

    if tags.musicbrainz_recordingid:
        track.mbid = tags.musicbrainz_recordingid

    # Handle embedded cover image
    if tags.has_cover_image:
        track.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=file_item.relative_path,
                    provider=instance_id,
                    remotely_accessible=False,
                )
            ]
        )

    return track


def parse_album_from_tags(
    track_path: str, track_tags: AudioTags, instance_id: str, domain: str, config: ProviderConfig
) -> Album:
    """Parse Album metadata from Track tags."""
    assert track_tags.album

    # Work out if we have an album and/or disc folder
    track_dir = str(PurePosixPath(track_path).parent)
    album_dir = get_album_dir(track_dir, track_tags.album)

    # Album artist(s)
    album_artists: UniqueList[Artist | ItemMapping] = UniqueList()
    if track_tags.album_artists:
        for index, album_artist_str in enumerate(track_tags.album_artists):
            artist = parse_artist_from_tags(
                album_artist_str,
                instance_id,
                domain,
                album_dir=album_dir,
                sort_name=(
                    track_tags.album_artist_sort_names[index]
                    if index < len(track_tags.album_artist_sort_names)
                    else None
                ),
                mbid=(
                    track_tags.musicbrainz_albumartistids[index]
                    if index < len(track_tags.musicbrainz_albumartistids)
                    else None
                ),
            )
            album_artists.append(artist)
    else:
        # Album artist tag is missing, determine fallback
        fallback_action = config.get_value("missing_album_artist_action") or "various_artists"
        if fallback_action == "folder_name" and album_dir:
            possible_artist_folder = str(PurePosixPath(album_dir).parent)
            album_artist_str = PurePosixPath(possible_artist_folder).name
            album_artists = UniqueList(
                [
                    parse_artist_from_tags(
                        name=album_artist_str,
                        instance_id=instance_id,
                        domain=domain,
                        album_dir=album_dir,
                    )
                ]
            )
        elif fallback_action == "track_artist":
            album_artists = UniqueList(
                [
                    parse_artist_from_tags(
                        name=track_artist_str,
                        instance_id=instance_id,
                        domain=domain,
                        album_dir=album_dir,
                    )
                    for track_artist_str in track_tags.artists
                ]
            )
        else:
            # Fallback to various artists
            album_artists = UniqueList(
                [
                    parse_artist_from_tags(
                        name=VARIOUS_ARTISTS_NAME,
                        instance_id=instance_id,
                        domain=domain,
                        mbid=VARIOUS_ARTISTS_MBID,
                    )
                ]
            )

    # Follow filesystem_local pattern for item_id
    item_id = album_dir or album_artists[0].name + "/" + track_tags.album

    name, version = parse_title_and_version(track_tags.album)
    album = Album(
        item_id=item_id,
        provider=instance_id,
        name=name,
        version=version,
        sort_name=track_tags.album_sort,
        artists=album_artists,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=domain,
                provider_instance=instance_id,
                url=album_dir,
            )
        },
    )

    if track_tags.year:
        album.year = track_tags.year
    album.album_type = track_tags.album_type

    if track_tags.barcode:
        album.external_ids.add((ExternalID.BARCODE, track_tags.barcode))

    if track_tags.musicbrainz_albumid:
        album.mbid = track_tags.musicbrainz_albumid

    if track_tags.musicbrainz_releasegroupid:
        album.add_external_id(ExternalID.MB_RELEASEGROUP, track_tags.musicbrainz_releasegroupid)

    return album


def parse_artist_from_tags(
    name: str,
    instance_id: str,
    domain: str,
    album_dir: str | None = None,
    artist_path: str | None = None,
    sort_name: str | None = None,
    mbid: str | None = None,
) -> Artist:
    """Parse artist from name and optional metadata."""
    if not artist_path:
        # Hunt for the artist (metadata) path on disk
        safe_artist_name = create_safe_string(name, lowercase=False, replace_space=False)

        if album_dir and (foldermatch := get_artist_dir(name, album_dir=album_dir)):
            # Try to find (album)artist folder based on album path
            artist_path = foldermatch
        elif "/" in name or "\\" in name:
            # Name looks like a path
            artist_path = name
        elif compare_strings(name, safe_artist_name):
            artist_path = safe_artist_name

    prov_artist_id = artist_path or name
    artist = Artist(
        item_id=prov_artist_id,
        provider=instance_id,
        name=name,
        sort_name=sort_name,
        provider_mappings={
            ProviderMapping(
                item_id=prov_artist_id,
                provider_domain=domain,
                provider_instance=instance_id,
                url=artist_path,
            )
        },
    )

    if mbid:
        artist.mbid = mbid

    return artist


def parse_audiobook_from_tags(
    file_item: FileSystemItem,
    tags: AudioTags,
    instance_id: str,
    domain: str,
    config: ProviderConfig,  # noqa: ARG001
) -> Audiobook:
    """Parse audiobook from AudioTags."""
    # Prefer album name for audiobook, fallback to title
    book_name = tags.album or tags.title
    provider_mapping = _create_provider_mapping(file_item, instance_id, domain, tags)

    audiobook = Audiobook(
        item_id=file_item.relative_path,
        provider=instance_id,
        name=book_name,
        sort_name=tags.album_sort or tags.title_sort,
        version=tags.version,
        duration=int(tags.duration or 0),
        provider_mappings={provider_mapping},
    )

    # Set authors from writers or album artists or artists
    audiobook.authors.set(tags.writers or tags.album_artists or tags.artists)
    audiobook.metadata.genres = set(tags.genres)
    audiobook.metadata.copyright = tags.get("copyright")
    audiobook.metadata.description = tags.get("comment")

    if tags.musicbrainz_recordingid:
        audiobook.mbid = tags.musicbrainz_recordingid

    # Handle chapters if present
    if tags.chapters:
        audiobook.metadata.chapters = [
            MediaItemChapter(
                position=chapter.chapter_id,
                name=chapter.title or f"Chapter {chapter.chapter_id}",
                start=chapter.position_start,
                end=chapter.position_end,
            )
            for chapter in tags.chapters
        ]

    return audiobook


def parse_podcast_episode_from_tags(
    file_item: FileSystemItem, tags: AudioTags, instance_id: str, domain: str
) -> PodcastEpisode:
    """Parse podcast episode from AudioTags."""
    podcast_name = tags.album or PurePosixPath(file_item.relative_path).parent.name
    podcast_path = str(PurePosixPath(file_item.relative_path).parent)
    provider_mapping = _create_provider_mapping(file_item, instance_id, domain, tags)

    episode = PodcastEpisode(
        item_id=file_item.relative_path,
        provider=instance_id,
        name=tags.title,
        sort_name=tags.title_sort,
        provider_mappings={provider_mapping},
        position=tags.track or 0,
        duration=int(tags.duration or 0),
        podcast=Podcast(
            item_id=podcast_path,
            provider=instance_id,
            name=podcast_name,
            sort_name=tags.album_sort,
            publisher=tags.get("publisher"),
            total_episodes=0,  # Set to 0 - will be updated by MA during sync
            provider_mappings={
                ProviderMapping(
                    item_id=podcast_path,
                    provider_domain=domain,
                    provider_instance=instance_id,
                )
            },
        ),
    )

    # Set episode metadata
    episode.metadata.genres = set(tags.genres)
    episode.metadata.copyright = tags.get("copyright")
    episode.metadata.description = tags.get("comment")

    # Handle chapters if present
    if tags.chapters:
        episode.metadata.chapters = [
            MediaItemChapter(
                position=chapter.chapter_id,
                name=chapter.title or f"Chapter {chapter.chapter_id}",
                start=chapter.position_start,
                end=chapter.position_end,
            )
            for chapter in tags.chapters
        ]

    # Handle embedded cover image
    if tags.has_cover_image:
        episode.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=file_item.relative_path,
                provider=instance_id,
                remotely_accessible=False,
            )
        )

    # Copy (embedded) image from episode to podcast
    assert isinstance(episode.podcast, Podcast)
    if not episode.podcast.image and episode.image:
        episode.podcast.metadata.add_image(episode.image)

    return episode
