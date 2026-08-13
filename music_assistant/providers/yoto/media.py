"""Map stable Yoto catalogue records to Music Assistant media models."""

from music_assistant_models.enums import ContentType, ImageType, MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    AudioFormat,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    ProviderMapping,
    Track,
    UniqueList,
)

from .catalogue import CatalogueCard, CatalogueTrack


def map_album(card: CatalogueCard, instance_id: str) -> Album:
    """
    Map a catalogue card to a Music Assistant album.

    :param card: Stable Yoto card record.
    :param instance_id: Music Assistant provider instance ID.
    :return: Mapped album.
    """
    album = Album(
        item_id=card.item_id,
        provider=instance_id,
        name=card.title,
        artists=UniqueList([map_artist(card.author, instance_id)]),
        provider_mappings={_mapping(card.item_id, instance_id)},
    )
    album.metadata.description = card.description
    album.metadata.grouping = card.series_title
    if card.artwork:
        album.metadata.images = UniqueList([map_image(card.artwork, instance_id)])
    return album


def map_audiobook(card: CatalogueCard, instance_id: str) -> Audiobook:
    """
    Map a story card to one resumable Music Assistant audiobook.

    :param card: Stable Yoto story card record.
    :param instance_id: Music Assistant provider instance ID.
    :return: Mapped audiobook.
    """
    duration = sum(max(track.duration, 0) for track in card.tracks)
    is_playable = bool(card.tracks) and duration > 0 and has_compatible_formats(card)
    audiobook = Audiobook(
        item_id=card.item_id,
        provider=instance_id,
        name=card.title,
        authors=UniqueList([card.author] if card.author else []),
        duration=duration,
        position=card.series_order,
        provider_mappings={
            _mapping(card.item_id, instance_id, common_format(card), available=is_playable)
        },
        is_playable=is_playable,
    )
    audiobook.metadata.description = card.description
    audiobook.metadata.grouping = card.series_title
    if card.category:
        audiobook.metadata.genres = {card.category}
    if card.artwork:
        audiobook.metadata.images = UniqueList([map_image(card.artwork, instance_id)])
    elapsed = 0
    chapter_starts: list[tuple[str, str, int]] = []
    for track in card.tracks:
        if not chapter_starts or chapter_starts[-1][0] != track.chapter_key:
            chapter_starts.append((track.chapter_key, track.chapter_title or track.title, elapsed))
        elapsed += max(track.duration, 0)
    audiobook.metadata.chapters = [
        MediaItemChapter(
            position=index + 1,
            name=name,
            start=start,
            end=chapter_starts[index + 1][2] if index + 1 < len(chapter_starts) else elapsed,
        )
        for index, (_, name, start) in enumerate(chapter_starts)
    ]
    return audiobook


def map_track(card: CatalogueCard, source: CatalogueTrack, instance_id: str) -> Track:
    """
    Map a catalogue track to a Music Assistant track.

    :param card: Parent Yoto card record.
    :param source: Stable Yoto track record.
    :param instance_id: Music Assistant provider instance ID.
    :return: Mapped track.
    """
    artwork = source.artwork or card.artwork
    track = Track(
        item_id=source.item_id,
        provider=instance_id,
        name=source.title,
        duration=source.duration,
        artists=UniqueList([map_artist(card.author, instance_id)]),
        album=ItemMapping(
            item_id=card.item_id,
            provider=instance_id,
            name=card.title,
            media_type=MediaType.ALBUM,
            image=map_image(card.artwork, instance_id) if card.artwork else None,
        ),
        disc_number=1,
        track_number=source.track_number,
        provider_mappings={
            _mapping(source.item_id, instance_id, source.format, available=source.duration > 0)
        },
        is_playable=source.duration > 0,
    )
    if artwork:
        track.metadata.images = UniqueList([map_image(artwork, instance_id)])
    return track


def map_artist(name: str | None, instance_id: str) -> Artist:
    """Map a Yoto author name to a stable Music Assistant artist."""
    artist_name = name or "Yoto"
    item_id = f"author:{artist_name.casefold()}"
    return Artist(
        item_id=item_id,
        provider=instance_id,
        name=artist_name,
        provider_mappings={_mapping(item_id, instance_id)},
    )


def _mapping(
    item_id: str,
    instance_id: str,
    content_format: str | None = None,
    *,
    available: bool = True,
) -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id,
        provider_domain="yoto",
        provider_instance=instance_id,
        available=available,
        audio_format=AudioFormat(content_type=content_type(content_format)),
    )


def content_type(content_format: str | None) -> ContentType:
    """Map a Yoto stream format to a Music Assistant content type."""
    return {
        "aac": ContentType.AAC,
        "mp3": ContentType.MP3,
        "m4a": ContentType.M4A,
        "mp4a": ContentType.MP4A,
    }.get((content_format or "").strip().casefold(), ContentType.UNKNOWN)


def map_image(path: str, instance_id: str) -> MediaItemImage:
    """Map Yoto artwork to a Music Assistant image."""
    return MediaItemImage(
        type=ImageType.THUMB,
        path=path,
        provider=instance_id,
        remotely_accessible=path.startswith(("http://", "https://")),
    )


def common_format(card: CatalogueCard) -> str | None:
    """Return the common normalized format for all tracks, if one exists."""
    formats = {_normalize_stream_property(track.format) for track in card.tracks}
    formats.discard(None)
    return formats.pop() if len(formats) == 1 else None


def has_compatible_formats(card: CatalogueCard) -> bool:
    """Return whether all audiobook parts can share one multipart stream."""
    formats = [_normalize_stream_property(track.format) for track in card.tracks]
    channels = [_normalize_stream_property(track.channels) for track in card.tracks]
    return (
        bool(formats)
        and all(value in {"aac", "mp3", "m4a", "mp4a"} for value in formats)
        and len(set(formats)) == 1
        and all(value in {"mono", "stereo"} for value in channels)
        and len(set(channels)) == 1
    )


def _normalize_stream_property(value: str | None) -> str | None:
    return value.strip().casefold() if value and value.strip() else None
