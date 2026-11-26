"""Parsers for Emby API responses."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, ImageType
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    AudioFormat,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.providers.emby.const import (
    AUDIO_STREAM_BIT_DEPTH,
    AUDIO_STREAM_BIT_RATE,
    AUDIO_STREAM_CHANNELS,
    AUDIO_STREAM_CODEC,
    AUDIO_STREAM_SAMPLE_RATE,
    ITEM_KEY_ALBUM_ARTISTS,
    ITEM_KEY_ALBUM_ID,
    ITEM_KEY_ALBUM_NAME,
    ITEM_KEY_ARTIST_ITEMS,
    ITEM_KEY_ARTISTS,
    ITEM_KEY_CONTAINER,
    ITEM_KEY_GENRES,
    ITEM_KEY_ID,
    ITEM_KEY_IMAGE_TAGS,
    ITEM_KEY_INDEX_NUMBER,
    ITEM_KEY_MEDIA_STREAMS,
    ITEM_KEY_NAME,
    ITEM_KEY_OVERVIEW,
    ITEM_KEY_PARENT_INDEX_NUMBER,
    ITEM_KEY_PRIMARY_IMAGE_ITEM_ID,
    ITEM_KEY_PRIMARY_IMAGE_TAG,
    ITEM_KEY_PRODUCTION_YEAR,
    ITEM_KEY_RUNTIME_TICKS,
    ITEM_KEY_TYPE,
    ITEM_KEY_USER_DATA,
    USER_DATA_KEY_IS_FAVORITE,
    USER_DATA_KEY_LAST_PLAYED_DATE,
    USER_DATA_KEY_PLAYBACK_POSITION_TICKS,
    USER_DATA_KEY_PLAYED,
)

if TYPE_CHECKING:
    from music_assistant.providers.emby import EmbyProvider


def parse_track(
    instance_id: str,
    provider: EmbyProvider,
    item: dict[str, Any],
) -> Track:
    """Parse an Emby Audio item into a Track."""
    track_id = str(item.get(ITEM_KEY_ID))
    name = str(item.get(ITEM_KEY_NAME))

    # Extract artist info
    artists = UniqueList[Artist | ItemMapping]()
    if artist_items := item.get(ITEM_KEY_ARTIST_ITEMS):
        for artist_item in artist_items:
            artist_name = str(artist_item.get(ITEM_KEY_NAME))
            artist_id = str(artist_item.get(ITEM_KEY_ID))

            artists.append(
                Artist(
                    item_id=artist_id,
                    name=artist_name,
                    provider=instance_id,
                    provider_mappings={
                        ProviderMapping(
                            item_id=artist_id,
                            provider_domain=provider.domain,
                            provider_instance=instance_id,
                        )
                    },
                )
            )

    album_id = str(item.get(ITEM_KEY_ALBUM_ID))
    album_name = str(item.get(ITEM_KEY_ALBUM_NAME))

    album = Album(
        item_id=album_id,
        name=album_name,
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=album_id,
                provider_domain=provider.domain,
                provider_instance=instance_id,
            )
        },
    )

    duration = int(item.get(ITEM_KEY_RUNTIME_TICKS, 0) / 10000000)  # Convert ticks to seconds
    track_number = int(item.get(ITEM_KEY_INDEX_NUMBER, 0))
    disc_number = int(item.get(ITEM_KEY_PARENT_INDEX_NUMBER, 0))
    audio_format = parse_stream_details(item)

    track = Track(
        item_id=track_id,
        name=name,
        album=album,
        artists=artists,
        track_number=track_number,
        disc_number=disc_number,
        duration=duration,
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain=provider.domain,
                provider_instance=instance_id,
                audio_format=audio_format,
            )
        },
    )

    # Extract images
    if primary_tag := item.get(ITEM_KEY_IMAGE_TAGS, {}).get("Primary"):
        image_url = f"{provider._base_url}Items/{track_id}/Images/Primary?tag={primary_tag}"
        if track.metadata.images is None:
            track.metadata.images = UniqueList[MediaItemImage]()
        track.metadata.images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=image_url,
                provider=instance_id,
                remotely_accessible=False,
            )
        )

    user_data = item.get(ITEM_KEY_USER_DATA, {})
    track.favorite = True if user_data.get(USER_DATA_KEY_IS_FAVORITE) else None

    if last_played_date := user_data.get(USER_DATA_KEY_LAST_PLAYED_DATE):
        track.last_played = int(datetime.fromisoformat(last_played_date).timestamp())

    if genres := item.get(ITEM_KEY_GENRES):
        track.metadata.genres = set(genres)

    return track


def parse_artist(
    instance_id: str,
    provider: EmbyProvider,
    item: dict[str, Any],
) -> Artist:
    """Parse an Emby MusicArtist item into an Artist."""
    artist_id = str(item.get(ITEM_KEY_ID))
    name = str(item.get(ITEM_KEY_NAME))

    artist = Artist(
        item_id=artist_id,
        name=name,
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=artist_id,
                provider_domain=provider.domain,
                provider_instance=instance_id,
            )
        },
    )

    # Extract images
    if primary_tag := item.get(ITEM_KEY_IMAGE_TAGS, {}).get("Primary"):
        image_url = f"{provider._base_url}Items/{artist_id}/Images/Primary?tag={primary_tag}"
        if artist.metadata.images is None:
            artist.metadata.images = UniqueList[MediaItemImage]()
        artist.metadata.images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=image_url,
                provider=instance_id,
                remotely_accessible=False,
            )
        )

    user_data = item.get(ITEM_KEY_USER_DATA, {})
    artist.favorite = True if user_data.get(USER_DATA_KEY_IS_FAVORITE) else None

    if genres := item.get(ITEM_KEY_GENRES):
        artist.metadata.genres = set(genres)

    return artist


def parse_album(
    instance_id: str,
    provider: EmbyProvider,
    item: dict[str, Any],
) -> Album:
    """Parse an Emby MusicAlbum item into an Album."""
    album_id = str(item.get(ITEM_KEY_ID))
    name = str(item.get(ITEM_KEY_NAME))
    year = item.get(ITEM_KEY_PRODUCTION_YEAR)

    # Extract artist info
    artists = UniqueList[Artist | ItemMapping]()
    # ArtistItems on an album also holds featured track artists
    if artist_items := item.get(ITEM_KEY_ALBUM_ARTISTS) or item.get(ITEM_KEY_ARTIST_ITEMS):
        for artist_item in artist_items:
            artist_id = str(artist_item.get(ITEM_KEY_ID))
            artist_name = str(artist_item.get(ITEM_KEY_NAME))

            artists.append(
                Artist(
                    item_id=artist_id,
                    name=artist_name,
                    provider=instance_id,
                    provider_mappings={
                        ProviderMapping(
                            item_id=artist_id,
                            provider_domain=provider.domain,
                            provider_instance=instance_id,
                        )
                    },
                )
            )

    album = Album(
        item_id=album_id,
        name=name,
        artists=artists,
        year=year,
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=album_id,
                provider_domain=provider.domain,
                provider_instance=instance_id,
            )
        },
    )

    # Extract images
    image_item_id = item.get(ITEM_KEY_PRIMARY_IMAGE_ITEM_ID)
    primary_tag = item.get(ITEM_KEY_PRIMARY_IMAGE_TAG)
    if not primary_tag:
        # the fallback tag is the album's own, so it only validates against the album's id
        primary_tag = item.get(ITEM_KEY_IMAGE_TAGS, {}).get("Primary")
        image_item_id = None
    if primary_tag:
        image_url = (
            f"{provider._base_url}Items/{image_item_id or album_id}/Images/Primary"
            f"?tag={primary_tag}"
        )
        if album.metadata.images is None:
            album.metadata.images = UniqueList[MediaItemImage]()
        album.metadata.images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=image_url,
                provider=instance_id,
                remotely_accessible=False,
            )
        )

    user_data = item.get(ITEM_KEY_USER_DATA, {})
    album.favorite = True if user_data.get(USER_DATA_KEY_IS_FAVORITE) else None

    if genres := item.get(ITEM_KEY_GENRES):
        album.metadata.genres = set(genres)

    return album


def parse_playlist(
    instance_id: str,
    provider: EmbyProvider,
    item: dict[str, Any],
) -> Playlist:
    """Parse an Emby Playlist item into a Playlist."""
    playlist_id = str(item.get(ITEM_KEY_ID))
    name = str(item.get(ITEM_KEY_NAME))

    playlist = Playlist(
        item_id=playlist_id,
        name=name,
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=playlist_id,
                provider_domain=provider.domain,
                provider_instance=instance_id,
            )
        },
    )
    # Extract images
    if primary_tag := item.get(ITEM_KEY_IMAGE_TAGS, {}).get("Primary"):
        image_url = f"{provider._base_url}Items/{playlist_id}/Images/Primary?tag={primary_tag}"
        if playlist.metadata.images is None:
            playlist.metadata.images = UniqueList[MediaItemImage]()
        playlist.metadata.images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=image_url,
                provider=instance_id,
                remotely_accessible=False,
            )
        )

    user_data = item.get(ITEM_KEY_USER_DATA, {})
    playlist.favorite = True if user_data.get(USER_DATA_KEY_IS_FAVORITE) else None

    return playlist


def parse_audiobook(
    instance_id: str,
    provider: EmbyProvider,
    item: dict[str, Any],
    parts: list[dict[str, Any]] | None = None,
) -> Audiobook:
    """
    Parse an Emby Audiobook item into an Audiobook.

    Emby models an audiobook as an album-like item whose part files are its child
    audio items; the provider passes those children (ordered) in ``parts``.

    :param item: The raw Emby audiobook (album) item.
    :param parts: The book's part files as child items, when fetched.
    """
    audiobook_id = str(item.get(ITEM_KEY_ID))
    name = str(item.get(ITEM_KEY_NAME))

    audiobook = Audiobook(
        item_id=audiobook_id,
        name=name,
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=audiobook_id,
                provider_domain=provider.domain,
                provider_instance=instance_id,
            )
        },
    )

    # Emby follows the audiobook metadata convention of storing the author as the
    # album artist and the narrator as the artist; expose them as plain names
    # (this provider does not support author/narrator artist types).
    if authors := _person_names(item.get(ITEM_KEY_ALBUM_ARTISTS)):
        audiobook.authors = UniqueList(authors)
    if narrators := _person_names(item.get(ITEM_KEY_ARTISTS)) or _person_names(
        item.get(ITEM_KEY_ARTIST_ITEMS)
    ):
        audiobook.narrators = UniqueList(narrators)

    if overview := item.get(ITEM_KEY_OVERVIEW):
        audiobook.metadata.description = overview

    if year := item.get(ITEM_KEY_PRODUCTION_YEAR):
        audiobook.metadata.release_date = datetime(int(year), 1, 1, tzinfo=UTC)

    # Extract images
    image_item_id = item.get(ITEM_KEY_PRIMARY_IMAGE_ITEM_ID)
    primary_tag = item.get(ITEM_KEY_PRIMARY_IMAGE_TAG)
    if not primary_tag:
        # the fallback tag is the book's own, so it only validates against the book's id
        primary_tag = item.get(ITEM_KEY_IMAGE_TAGS, {}).get("Primary")
        image_item_id = None
    if primary_tag:
        image_url = (
            f"{provider._base_url}Items/{image_item_id or audiobook_id}/Images/Primary"
            f"?tag={primary_tag}"
        )
        if audiobook.metadata.images is None:
            audiobook.metadata.images = UniqueList[MediaItemImage]()
        audiobook.metadata.images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=image_url,
                provider=instance_id,
                remotely_accessible=False,
            )
        )

    user_data = item.get(ITEM_KEY_USER_DATA, {})
    audiobook.favorite = True if user_data.get(USER_DATA_KEY_IS_FAVORITE) else None
    audiobook.fully_played = bool(user_data.get(USER_DATA_KEY_PLAYED))
    audiobook.resume_position_ms = int(
        user_data.get(USER_DATA_KEY_PLAYBACK_POSITION_TICKS, 0) / 10000  # ticks to ms
    )
    # the timestamp lets library sync pick the resume point by recency, not just position
    if last_played_date := user_data.get(USER_DATA_KEY_LAST_PLAYED_DATE):
        audiobook.resume_position_timestamp = datetime.fromisoformat(last_played_date)

    if genres := item.get(ITEM_KEY_GENRES):
        audiobook.metadata.genres = set(genres)

    duration = int(item.get(ITEM_KEY_RUNTIME_TICKS, 0) / 10000000)  # ticks to seconds
    book_parts = [part for part in parts or [] if isinstance(part, dict)]
    if len(book_parts) > 1:
        # each file becomes a chapter so players (and MA's resync on the queue) can
        # navigate and seek across the whole book
        chapters: list[MediaItemChapter] = []
        cumulative = 0.0
        for position, part in enumerate(book_parts, 1):
            part_duration = part.get(ITEM_KEY_RUNTIME_TICKS, 0) / 10000000
            chapters.append(
                MediaItemChapter(
                    position=position,
                    name=str(part.get(ITEM_KEY_NAME) or f"Part {position}"),
                    start=cumulative,
                    end=cumulative + part_duration,
                )
            )
            cumulative += part_duration
        audiobook.metadata.chapters = chapters
        duration = max(duration, int(cumulative))
    audiobook.duration = duration

    return audiobook


def parse_stream_details(
    item: dict[str, Any],
) -> AudioFormat:
    """Parse Emby media stream details into an AudioFormat."""
    media_streams = item.get(ITEM_KEY_MEDIA_STREAMS, [{}])
    audio_stream = next((dict(s) for s in media_streams if s.get(ITEM_KEY_TYPE) == "Audio"), {})

    return AudioFormat(
        content_type=ContentType.try_parse(str(item.get(ITEM_KEY_CONTAINER))),
        codec_type=ContentType.try_parse(str(audio_stream.get(AUDIO_STREAM_CODEC))),
        sample_rate=int(audio_stream.get(AUDIO_STREAM_SAMPLE_RATE, 44100)),
        bit_depth=int(audio_stream.get(AUDIO_STREAM_BIT_DEPTH, 16)),
        channels=int(audio_stream.get(AUDIO_STREAM_CHANNELS, 2)),
        bit_rate=audio_stream.get(AUDIO_STREAM_BIT_RATE),
    )


def _person_name(person: dict[str, Any]) -> str | None:
    """Return the name of an Emby person entry, if it has one."""
    if name := person.get(ITEM_KEY_NAME):
        return str(name)
    return None


def _person_names(persons: Any) -> list[str]:
    """Return the names of a list of Emby person entries (items or plain strings)."""
    if not isinstance(persons, list):
        return []
    names: list[str] = []
    for person in persons:
        if isinstance(person, str) and person.strip():
            names.append(person.strip())
        elif isinstance(person, dict) and (name := _person_name(person)):
            names.append(name)
    return names
