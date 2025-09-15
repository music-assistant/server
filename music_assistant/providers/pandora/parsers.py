"""Parsing utilities to convert Pandora API responses into Music Assistant model objects."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, ImageType, MediaType, StreamType
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.util import parse_title_and_version

from .constants import (
    AUDIO_QUALITIES,
    DEFAULT_AUDIO_QUALITY,
)
from .helpers import safe_get

if TYPE_CHECKING:
    from .provider import PandoraProvider


def parse_artist(artist_data: dict[str, Any], provider: PandoraProvider) -> Artist:
    """Parse Pandora artist data into Music Assistant Artist object."""
    artist_id = str(artist_data.get("pandoraId", artist_data.get("artistId", "")))
    if not artist_id:
        artist_id = str(artist_data.get("musicId", ""))

    artist = Artist(
        item_id=artist_id,
        provider=provider.lookup_key,
        name=artist_data.get("artistName", "Unknown Artist"),
        provider_mappings={
            ProviderMapping(
                item_id=artist_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
            )
        },
    )

    # Add artist image if available
    if artist_art := safe_get(artist_data, "artistArt"):
        artist.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=artist_art,
                provider=provider.lookup_key,
                remotely_accessible=True,
            )
        )

    return artist


def parse_album(album_data: dict[str, Any], provider: PandoraProvider) -> Album:
    """Parse Pandora album data into Music Assistant Album object."""
    album_id = str(album_data.get("pandoraId", album_data.get("albumId", "")))
    if not album_id:
        album_id = str(album_data.get("musicId", ""))

    album_name = album_data.get("albumName", "Unknown Album")
    name, version = parse_title_and_version(album_name)

    album = Album(
        item_id=album_id,
        provider=provider.lookup_key,
        name=name,
        version=version,
        provider_mappings={
            ProviderMapping(
                item_id=album_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
            )
        },
    )

    # Add album artist if available
    if artist_name := album_data.get("artistName"):
        artist = ItemMapping(
            item_id=str(album_data.get("artistId", "")),
            provider=provider.lookup_key,
            name=artist_name,
            media_type=MediaType.ARTIST,
        )
        album.artists.append(artist)

    # Add album art if available
    if album_art := safe_get(album_data, "albumArt"):
        album.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=album_art,
                provider=provider.lookup_key,
                remotely_accessible=True,
            )
        )

    return album


def parse_track(track_data: dict[str, Any], provider: PandoraProvider) -> Track:
    """Parse Pandora track data into Music Assistant Track object."""
    track_id = str(track_data.get("pandoraId", track_data.get("trackId", "")))
    if not track_id:
        track_id = str(track_data.get("musicId", ""))

    track_name = track_data.get("songName", track_data.get("trackName", "Unknown Track"))
    name, version = parse_title_and_version(track_name)

    # Get duration in milliseconds (Track expects int milliseconds)
    duration_ms = 0
    if track_length := track_data.get("trackLength"):
        # trackLength is usually in seconds already
        duration_ms = int(track_length * 1000)  # Convert to milliseconds

    track = Track(
        item_id=track_id,
        provider=provider.lookup_key,
        name=name,
        version=version,
        duration=duration_ms,
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.AAC,
                    bit_rate=128,  # Pandora typically uses 128kbps
                ),
                available=True,
            )
        },
    )

    # Add artist information
    if artist_name := track_data.get("artistName"):
        artist = ItemMapping(
            item_id=str(track_data.get("artistMusicId", track_data.get("artistId", ""))),
            provider=provider.lookup_key,
            name=artist_name,
            media_type=MediaType.ARTIST,
        )
        track.artists.append(artist)

    # Add album information if available
    if album_name := track_data.get("albumTitle", track_data.get("albumName")):
        album = ItemMapping(
            item_id=str(track_data.get("albumId", "")),
            provider=provider.lookup_key,
            name=album_name,
            media_type=MediaType.ALBUM,
        )
        track.album = album

    # Add track art/album art if available
    if track_art := safe_get(track_data, "albumArt"):
        track.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=track_art,
                provider=provider.lookup_key,
                remotely_accessible=True,
            )
        )

    # Set explicit flag if available
    if explicit := track_data.get("explicit"):
        track.metadata.explicit = explicit

    return track


def parse_station(station_data: dict[str, Any], provider: PandoraProvider) -> Playlist:
    """Parse Pandora station data into Music Assistant Playlist object."""
    station_id = str(station_data.get("stationId", ""))

    # Stations in Pandora are represented as playlists in Music Assistant
    station = Playlist(
        item_id=station_id,
        provider=provider.lookup_key,
        name=station_data.get("stationName", "Unknown Station"),
        owner="Pandora Radio",
        provider_mappings={
            ProviderMapping(
                item_id=station_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
            )
        },
        is_editable=False,  # Pandora stations are not directly editable
    )

    # Add station art if available
    if station_art := safe_get(station_data, "artUrl"):
        station.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=station_art,
                provider=provider.lookup_key,
                remotely_accessible=True,
            )
        )

    # Use station creation date as cache checksum if available
    if date_created := station_data.get("dateCreated"):
        station.cache_checksum = str(date_created)

    return station


def parse_search_results(search_data: dict[str, Any], provider: PandoraProvider) -> SearchResults:
    """Parse Pandora search results into Music Assistant SearchResults object."""
    results = SearchResults()

    # Parse artists
    artist_list = []
    if artists := safe_get(search_data, "artists"):
        for artist_data in artists:
            try:
                artist_list.append(parse_artist(artist_data, provider))
            except Exception as e:
                provider.logger.debug("Failed to parse artist: %s", e)
    results.artists = artist_list

    # Parse albums
    album_list = []
    if albums := safe_get(search_data, "albums"):
        for album_data in albums:
            try:
                album_list.append(parse_album(album_data, provider))
            except Exception as e:
                provider.logger.debug("Failed to parse album: %s", e)
    results.albums = album_list

    # Parse tracks/songs
    track_list = []
    if tracks := safe_get(search_data, "songs"):
        for track_data in tracks:
            try:
                track_list.append(parse_track(track_data, provider))
            except Exception as e:
                provider.logger.debug("Failed to parse track: %s", e)
    results.tracks = track_list

    # Parse stations (as playlists)
    playlist_list = []
    if stations := safe_get(search_data, "stations"):
        for station_data in stations:
            try:
                playlist_list.append(parse_station(station_data, provider))
            except Exception as e:
                provider.logger.debug("Failed to parse station: %s", e)
    results.playlists = playlist_list

    return results


def create_stream_details(track_id: str, provider: PandoraProvider) -> StreamDetails:
    """Create StreamDetails for a Pandora track."""
    # Get audio quality from provider config with proper type handling
    quality_setting = provider.config.get_value("audio_quality", DEFAULT_AUDIO_QUALITY)
    if not isinstance(quality_setting, str):
        quality_setting = DEFAULT_AUDIO_QUALITY

    audio_quality = AUDIO_QUALITIES.get(quality_setting, AUDIO_QUALITIES[DEFAULT_AUDIO_QUALITY])
    content_type = ContentType.AAC if audio_quality["format"] == "AAC+" else ContentType.MP3

    # Safely extract bitrate with type checking
    bitrate_value = audio_quality["bitrate"]
    if isinstance(bitrate_value, int):
        bit_rate = bitrate_value
    elif isinstance(bitrate_value, (float, str)):
        bit_rate = int(bitrate_value)
    else:
        bit_rate = 128  # fallback default

    return StreamDetails(
        item_id=track_id,
        provider=provider.lookup_key,
        audio_format=AudioFormat(
            content_type=content_type,
            bit_rate=bit_rate,
        ),
        stream_type=StreamType.HTTP,
        allow_seek=False,  # Pandora radio doesn't typically allow seeking
        can_seek=False,
    )
