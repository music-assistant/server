"""Conversion of Music Assistant media items to Subsonic response objects."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from libopensonic.media import AlbumID3, ArtistID3, Child, ItemGenre
from libopensonic.media import Playlist as SubsonicPlaylist
from music_assistant_models.enums import AlbumType, MediaType, ProviderSharing

from music_assistant.helpers.audio import get_mime_type

from .ids import to_subsonic_id

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Artist, ItemMapping, Playlist, Track
    from music_assistant_models.media_items.audio_format import AudioFormat

EPOCH = "1970-01-01T00:00:00Z"
SUFFIX_OVERRIDES = {"mpeg": "mp3", "mp4": "m4a", "mp4a": "m4a", "alac": "m4a", "vorbis": "ogg"}


def song(track: Track) -> dict[str, Any]:
    """
    Return the Subsonic ``Child`` of a track.

    :param track: A library or provider track (full or summary).
    """
    album = track.album
    album_id = _item_id(MediaType.ALBUM, album)
    track_id = to_subsonic_id(MediaType.TRACK, track.item_id, track.provider)
    artist_ids = [_item_id(MediaType.ARTIST, item) for item in track.artists]
    audio = _audio_format(track)
    content_type = audio.content_type.value if audio else "mpeg"
    genres = sorted(track.metadata.genres or ()) if track.metadata else []
    title = f"{track.name} ({track.version})" if track.version else track.name
    child = Child(
        id=track_id,
        is_dir=False,
        title=title,
        parent=album_id,
        album=album.name if album else None,
        album_id=album_id,
        artist=track.artist_str or None,
        artist_id=artist_ids[0] if artist_ids else None,
        track=track.track_number or None,
        disc_number=track.disc_number or None,
        year=getattr(album, "year", None) or None,
        genre=genres[0] if genres else None,
        genres=[ItemGenre(name=genre) for genre in genres] or None,
        cover_art=album_id or track_id,
        duration=track.duration or 0,
        content_type=get_mime_type(content_type),
        suffix=SUFFIX_OVERRIDES.get(content_type, content_type),
        bit_rate=(audio.bit_rate or None) if audio else None,
        bit_depth=audio.bit_depth if audio else None,
        sampling_rate=audio.sample_rate if audio else None,
        channel_count=audio.channels if audio else None,
        is_video=False,
        type="music",
        media_type="song",
        created=iso_timestamp(track.date_added),
        starred=iso_timestamp(track.date_added) if track.favorite else None,
        sort_name=track.sort_name,
        artists=[
            ArtistID3(id=artist_id, name=item.name)
            for artist_id, item in zip(artist_ids, track.artists, strict=True)
            if artist_id
        ]
        or None,
        display_artist=track.artist_str or None,
    )
    data: dict[str, Any] = child.to_dict()
    return data


def album(
    item: Album,
    song_count: int,
    duration: int,
    songs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Return the Subsonic ``AlbumID3`` of an album.

    :param item: A library or provider album (full or summary).
    :param song_count: Number of tracks on the album.
    :param duration: Total duration of the album in seconds.
    :param songs: The album tracks as Subsonic ``Child`` objects, for ``getAlbum``.
    """
    artist_ids = [_item_id(MediaType.ARTIST, album_artist) for album_artist in item.artists]
    genres = sorted(item.metadata.genres or ()) if item.metadata else []
    album_id = to_subsonic_id(MediaType.ALBUM, item.item_id, item.provider)
    data: dict[str, Any] = AlbumID3(
        id=album_id,
        name=item.name,
        version=item.version or None,
        song_count=song_count,
        duration=duration,
        created=iso_timestamp(item.date_added),
        artist=item.artist_str or None,
        artist_id=artist_ids[0] if artist_ids else None,
        cover_art=album_id,
        starred=iso_timestamp(item.date_added) if item.favorite else None,
        year=item.year or None,
        genre=genres[0] if genres else None,
        genres=[ItemGenre(name=genre) for genre in genres] or None,
        artists=[
            ArtistID3(id=artist_id, name=album_artist.name)
            for artist_id, album_artist in zip(artist_ids, item.artists, strict=True)
            if artist_id
        ]
        or None,
        display_artist=item.artist_str or None,
        release_types=(
            [item.album_type.value.title()] if item.album_type != AlbumType.UNKNOWN else None
        ),
        sort_name=item.sort_name,
        is_compilation=item.album_type == AlbumType.COMPILATION,
    ).to_dict()
    if songs is not None:
        data["song"] = songs
    return data


def artist(item: Artist, album_count: int | None = None) -> dict[str, Any]:
    """
    Return the Subsonic ``ArtistID3`` of an artist.

    :param item: A library or provider artist (full or summary).
    :param album_count: Number of albums of the artist, when known.
    """
    artist_id = to_subsonic_id(MediaType.ARTIST, item.item_id, item.provider)
    data: dict[str, Any] = ArtistID3(
        id=artist_id,
        name=item.name,
        album_count=album_count,
        cover_art=artist_id if item.image else None,
        starred=iso_timestamp(item.date_added) if item.favorite else None,
        sort_name=item.sort_name,
    ).to_dict()
    return data


def playlist(
    item: Playlist,
    owner: str,
    songs: list[dict[str, Any]],
    duration: int,
    include_songs: bool = False,
) -> dict[str, Any]:
    """
    Return the Subsonic ``Playlist`` of a playlist.

    :param item: A library or provider playlist.
    :param owner: Username of the playlist owner.
    :param songs: The playlist tracks as Subsonic ``Child`` objects.
    :param duration: Total duration of the tracks in seconds.
    :param include_songs: Add the tracks as ``entry``, for ``getPlaylist``.
    """
    playlist_id = to_subsonic_id(MediaType.PLAYLIST, item.item_id, item.provider)
    created = iso_timestamp(item.date_added)
    data: dict[str, Any] = SubsonicPlaylist(
        id=playlist_id,
        name=item.name,
        song_count=len(songs),
        duration=duration,
        created=created,
        changed=created,
        owner=owner,
        public=item.access is None or item.access.sharing != ProviderSharing.PRIVATE,
        cover_art=playlist_id,
        readonly=not item.is_editable,
    ).to_dict()
    if include_songs:
        data["entry"] = songs
    return data


def iso_timestamp(value: datetime | str | None) -> str:
    """
    Return a timestamp in the ISO 8601 UTC form Subsonic uses.

    :param value: A datetime, an ISO 8601 string or None (gives the Unix epoch).
    """
    if value is None:
        return EPOCH
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _item_id(
    media_type: MediaType, item: Album | Artist | Track | ItemMapping | None
) -> str | None:
    if item is None:
        return None
    return to_subsonic_id(media_type, item.item_id, item.provider)


def _audio_format(track: Track) -> AudioFormat | None:
    for mapping in track.provider_mappings:
        if mapping.available and mapping.audio_format:
            return mapping.audio_format
    return None
