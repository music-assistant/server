"""Parse objects from py-opensonic into Music Assistant types."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, ImageType, MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Track,
)

from music_assistant.constants import UNKNOWN_ARTIST

if TYPE_CHECKING:
    from libopensonic.media import AlbumID3 as SonicAlbum
    from libopensonic.media import AlbumInfo as SonicAlbumInfo
    from libopensonic.media import ArtistID3 as SonicArtist
    from libopensonic.media import ArtistInfo2 as SonicArtistInfo
    from libopensonic.media import Child as SonicSong
    from libopensonic.media import Playlist as SonicPlaylist
    from libopensonic.media import PodcastChannel as SonicPodcast
    from libopensonic.media import PodcastEpisode as SonicEpisode


UNKNOWN_ARTIST_ID = "fake_artist_unknown"


# Because of some subsonic API weirdness, we have to lookup any podcast episode by finding it in
# the list of episodes in a channel, to facilitate, we will use both the episode id and the
# channel id concatenated as an episode id to MA
EP_CHAN_SEP = "$!$"


# We need the following prefix because of the way that Navidrome reports artists for individual
# tracks on Various Artists albums, see the note in the _parse_track() method and the handling
# in get_artist()
NAVI_VARIOUS_PREFIX = "MA-NAVIDROME-"


SUBSONIC_DOMAIN = "opensubsonic"


def get_item_mapping(instance_id: str, media_type: MediaType, key: str, name: str) -> ItemMapping:
    """Construct an ItemMapping for the specified media."""
    return ItemMapping(
        media_type=media_type,
        item_id=key,
        provider=instance_id,
        name=name,
    )


def parse_track(
    logger: logging.Logger,
    instance_id: str,
    sonic_song: SonicSong,
    album: Album | ItemMapping | None = None,
) -> Track:
    """Parse an OpenSubsonic.Child into an MA Track."""
    # Unfortunately, the Song response type is not defined in the open subsonic spec so we have
    # implementations which disagree about where the album id for this song should be stored.
    # We accept either song.ablum_id or song.parent but prefer album_id.
    if not album:
        if sonic_song.album_id and sonic_song.album:
            album = get_item_mapping(
                instance_id, MediaType.ALBUM, sonic_song.album_id, sonic_song.album
            )
        elif sonic_song.parent and sonic_song.album:
            album = get_item_mapping(
                instance_id, MediaType.ALBUM, sonic_song.parent, sonic_song.album
            )

    metadata: MediaItemMetadata = MediaItemMetadata()

    if sonic_song.explicit_status and sonic_song.explicit_status != "clean":
        metadata.explicit = True

    if sonic_song.genre:
        if not metadata.genres:
            metadata.genres = set()
        metadata.genres.add(sonic_song.genre)

    if sonic_song.genres:
        if not metadata.genres:
            metadata.genres = set()
        for g in sonic_song.genres:
            metadata.genres.add(g.name)

    if sonic_song.moods:
        metadata.mood = sonic_song.moods[0]

    if sonic_song.contributors:
        if not metadata.performers:
            metadata.performers = set()
        for c in sonic_song.contributors:
            metadata.performers.add(c.artist.name)

    track = Track(
        item_id=sonic_song.id,
        provider=instance_id,
        name=sonic_song.title,
        album=album,
        duration=sonic_song.duration or 0,
        disc_number=sonic_song.disc_number or 0,
        favorite=bool(sonic_song.starred),
        metadata=metadata,
        provider_mappings={
            ProviderMapping(
                item_id=sonic_song.id,
                provider_domain=SUBSONIC_DOMAIN,
                provider_instance=instance_id,
                available=True,
                audio_format=AudioFormat(
                    content_type=ContentType.try_parse(sonic_song.content_type or "?"),
                    sample_rate=sonic_song.sampling_rate if sonic_song.sampling_rate else 44100,
                    bit_depth=sonic_song.bit_depth if sonic_song.bit_depth else 16,
                    channels=sonic_song.channel_count if sonic_song.channel_count else 2,
                    bit_rate=sonic_song.bit_rate if sonic_song.bit_rate else None,
                ),
            )
        },
        track_number=sonic_song.track if sonic_song.track else 0,
    )

    if sonic_song.music_brainz_id:
        track.mbid = sonic_song.music_brainz_id

    if sonic_song.sort_name:
        track.sort_name = sonic_song.sort_name

    # We need to find an artist for this track but various implementations seem to disagree
    # about where the artist with the valid ID needs to be found. We will add any artist with
    # an ID and only use UNKNOWN if none are found.

    if sonic_song.artist_id:
        track.artists.append(
            get_item_mapping(
                instance_id,
                MediaType.ARTIST,
                sonic_song.artist_id,
                sonic_song.artist if sonic_song.artist else UNKNOWN_ARTIST,
            )
        )

    for entry in sonic_song.artists:
        if entry.id == sonic_song.artist_id:
            continue
        if entry.id is not None and entry.name is not None:
            track.artists.append(
                get_item_mapping(instance_id, MediaType.ARTIST, entry.id, entry.name)
            )

    if not track.artists:
        if sonic_song.artist and not sonic_song.artist_id:
            # This is how Navidrome handles tracks from albums which are marked
            # 'Various Artists'. Unfortunately, we cannot lookup this artist independently
            # because it will not have an entry in the artists table so the best we can do it
            # add a 'fake' id with the proper artist name and have get_artist() check for this
            # id and handle it locally.
            fake_id = f"{NAVI_VARIOUS_PREFIX}{sonic_song.artist}"
            artist = Artist(
                item_id=fake_id,
                provider=SUBSONIC_DOMAIN,
                name=sonic_song.artist,
                provider_mappings={
                    ProviderMapping(
                        item_id=fake_id,
                        provider_domain=SUBSONIC_DOMAIN,
                        provider_instance=instance_id,
                    )
                },
            )
        else:
            logger.info(
                "Unable to find artist ID for track '%s' with ID '%s'.",
                sonic_song.title,
                sonic_song.id,
            )
            artist = Artist(
                item_id=UNKNOWN_ARTIST_ID,
                name=UNKNOWN_ARTIST,
                provider=instance_id,
                provider_mappings={
                    ProviderMapping(
                        item_id=UNKNOWN_ARTIST_ID,
                        provider_domain=SUBSONIC_DOMAIN,
                        provider_instance=instance_id,
                    )
                },
            )

        track.artists.append(artist)
    return track


def parse_artist(
    instance_id: str, sonic_artist: SonicArtist, sonic_info: SonicArtistInfo | None = None
) -> Artist:
    """Parse artist and artistInfo into a Music Assistant Artist."""
    metadata: MediaItemMetadata = MediaItemMetadata()

    if sonic_artist.artist_image_url:
        metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=sonic_artist.artist_image_url,
                provider=instance_id,
                remotely_accessible=True,
            )
        )

    if sonic_artist.cover_art:
        metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=sonic_artist.cover_art,
                provider=instance_id,
                remotely_accessible=False,
            )
        )
    if sonic_info:
        if sonic_info.biography:
            metadata.description = sonic_info.biography
        if sonic_info.small_image_url:
            metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=sonic_info.small_image_url,
                    provider=instance_id,
                    remotely_accessible=True,
                )
            )

    artist = Artist(
        item_id=sonic_artist.id,
        name=sonic_artist.name,
        metadata=metadata,
        provider=SUBSONIC_DOMAIN,
        favorite=bool(sonic_artist.starred),
        provider_mappings={
            ProviderMapping(
                item_id=sonic_artist.id,
                provider_domain=SUBSONIC_DOMAIN,
                provider_instance=instance_id,
            )
        },
        sort_name=sonic_artist.sort_name if sonic_artist.sort_name else None,
    )

    if sonic_artist.music_brainz_id:
        artist.mbid = sonic_artist.music_brainz_id

    return artist


def parse_album(
    logger: logging.Logger,
    instance_id: str,
    sonic_album: SonicAlbum,
    sonic_info: SonicAlbumInfo | None = None,
) -> Album:
    """Parse album and albumInfo into a Music Assistant Album."""
    metadata: MediaItemMetadata = MediaItemMetadata()

    if sonic_album.cover_art:
        metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=sonic_album.cover_art,
                provider=instance_id,
                remotely_accessible=False,
            ),
        )

    if sonic_info:
        if sonic_info.small_image_url:
            metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=sonic_info.small_image_url,
                    remotely_accessible=True,
                    provider=instance_id,
                )
            )
        if sonic_info.notes:
            metadata.description = sonic_info.notes

    if sonic_album.genre:
        if not metadata.genres:
            metadata.genres = set()
        metadata.genres.add(sonic_album.genre)

    if sonic_album.genres:
        if not metadata.genres:
            metadata.genres = set()
        for g in sonic_album.genres:
            metadata.genres.add(g.name)

    if sonic_album.moods:
        metadata.mood = sonic_album.moods[0]

    album = Album(
        item_id=sonic_album.id,
        provider=SUBSONIC_DOMAIN,
        metadata=metadata,
        name=sonic_album.name,
        favorite=bool(sonic_album.starred),
        provider_mappings={
            ProviderMapping(
                item_id=sonic_album.id,
                provider_domain=SUBSONIC_DOMAIN,
                provider_instance=instance_id,
            )
        },
        year=sonic_album.year,
    )

    if sonic_album.sort_name:
        album.sort_name = sonic_album.sort_name

    if sonic_album.music_brainz_id:
        album.mbid = sonic_album.music_brainz_id

    if sonic_album.artist_id:
        album.artists.append(
            ItemMapping(
                media_type=MediaType.ARTIST,
                item_id=sonic_album.artist_id,
                provider=instance_id,
                name=sonic_album.artist if sonic_album.artist else UNKNOWN_ARTIST,
            )
        )
    elif not sonic_album.artists:
        logger.info(
            "Unable to find an artist ID for album '%s' with ID '%s'.",
            sonic_album.name,
            sonic_album.id,
        )
        album.artists.append(
            Artist(
                item_id=UNKNOWN_ARTIST_ID,
                name=UNKNOWN_ARTIST,
                provider=instance_id,
                provider_mappings={
                    ProviderMapping(
                        item_id=UNKNOWN_ARTIST_ID,
                        provider_domain=SUBSONIC_DOMAIN,
                        provider_instance=instance_id,
                    )
                },
            )
        )

    if sonic_album.artists:
        for a in sonic_album.artists:
            if a.id == sonic_album.artist_id:
                continue
            album.artists.append(
                ItemMapping(
                    media_type=MediaType.ARTIST, item_id=a.id, provider=instance_id, name=a.name
                )
            )

    return album


def parse_playlist(instance_id: str, sonic_playlist: SonicPlaylist) -> Playlist:
    """Parse subsonic Playlist into MA Playlist."""
    playlist = Playlist(
        item_id=sonic_playlist.id,
        provider=SUBSONIC_DOMAIN,
        name=sonic_playlist.name,
        is_editable=True,
        provider_mappings={
            ProviderMapping(
                item_id=sonic_playlist.id,
                provider_domain=SUBSONIC_DOMAIN,
                provider_instance=instance_id,
            )
        },
    )

    if sonic_playlist.cover_art:
        playlist.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=sonic_playlist.cover_art,
                provider=instance_id,
                remotely_accessible=False,
            )
        )

    return playlist


def parse_podcast(instance_id: str, sonic_podcast: SonicPodcast) -> Podcast:
    """Parse Subsonic PodcastChannel into MA Podcast."""
    podcast = Podcast(
        item_id=sonic_podcast.id,
        provider=SUBSONIC_DOMAIN,
        name=sonic_podcast.title,
        uri=sonic_podcast.url,
        total_episodes=len(sonic_podcast.episode),
        provider_mappings={
            ProviderMapping(
                item_id=sonic_podcast.id,
                provider_domain=SUBSONIC_DOMAIN,
                provider_instance=instance_id,
            )
        },
    )

    podcast.metadata.description = sonic_podcast.description

    if sonic_podcast.cover_art:
        podcast.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=sonic_podcast.cover_art,
                provider=instance_id,
                remotely_accessible=False,
            )
        )

    return podcast


def parse_epsiode(
    instance_id: str, sonic_episode: SonicEpisode, sonic_channel: SonicPodcast
) -> PodcastEpisode:
    """Parse an Open Subsonic Podcast Episode into an MA PodcastEpisode."""
    eid = f"{sonic_episode.channel_id}{EP_CHAN_SEP}{sonic_episode.id}"
    pos = 1
    if not sonic_channel.episode:
        raise MediaNotFoundError(f"Podcast Channel '{sonic_channel.id}' missing episode list")

    for ep in sonic_channel.episode:
        if ep.id == sonic_episode.id:
            break
        pos += 1

    episode = PodcastEpisode(
        item_id=eid,
        provider=SUBSONIC_DOMAIN,
        name=sonic_episode.title,
        position=pos,
        podcast=parse_podcast(instance_id, sonic_channel),
        provider_mappings={
            ProviderMapping(
                item_id=eid,
                provider_domain=SUBSONIC_DOMAIN,
                provider_instance=instance_id,
            )
        },
        duration=sonic_episode.duration,
    )

    if sonic_episode.publish_date:
        episode.metadata.release_date = datetime.fromisoformat(sonic_episode.publish_date)

    if sonic_episode.description:
        episode.metadata.description = sonic_episode.description

    return episode
