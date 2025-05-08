"""Parse objects from py-opensonic into Music Assistant types."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemImage,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import UNKNOWN_ARTIST

if TYPE_CHECKING:
    from libopensonic.media import Album as SonicAlbum
    from libopensonic.media import AlbumInfo as SonicAlbumInfo
    from libopensonic.media import Artist as SonicArtist
    from libopensonic.media import ArtistInfo as SonicArtistInfo
    from libopensonic.media import Playlist as SonicPlaylist
    from libopensonic.media import PodcastChannel as SonicPodcast
    from libopensonic.media import PodcastEpisode as SonicEpisode


UNKNOWN_ARTIST_ID = "fake_artist_unknown"


# Because of some subsonic API weirdness, we have to lookup any podcast episode by finding it in
# the list of episodes in a channel, to facilitate, we will use both the episode id and the
# channel id concatenated as an episode id to MA
EP_CHAN_SEP = "$!$"


def parse_artist(
    instance_id: str, sonic_artist: SonicArtist, sonic_info: SonicArtistInfo = None
) -> Artist:
    """Parse artist and artistInfo into a Music Assistant Artist."""
    artist = Artist(
        item_id=sonic_artist.id,
        name=sonic_artist.name,
        provider="opensubsonic",
        favorite=bool(sonic_artist.starred),
        provider_mappings={
            ProviderMapping(
                item_id=sonic_artist.id,
                provider_domain="opensubsonic",
                provider_instance=instance_id,
            )
        },
    )

    artist.metadata.images = UniqueList()
    if sonic_artist.cover_art:
        artist.metadata.images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=sonic_artist.cover_art,
                provider=instance_id,
                remotely_accessible=False,
            )
        )

    if sonic_info:
        if sonic_info.biography:
            artist.metadata.description = sonic_info.biography
        if sonic_info.small_image_url:
            artist.metadata.images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=sonic_info.small_image_url,
                    provider=instance_id,
                    remotely_accessible=True,
                )
            )

    return artist


def parse_album(
    logger: logging.Logger,
    instance_id: str,
    sonic_album: SonicAlbum,
    sonic_info: SonicAlbumInfo | None = None,
) -> Album:
    """Parse album and albumInfo into a Music Assistant Album."""
    album_id = sonic_album.id
    album = Album(
        item_id=album_id,
        provider="opensubsonic",
        name=sonic_album.name,
        favorite=bool(sonic_album.starred),
        provider_mappings={
            ProviderMapping(
                item_id=album_id,
                provider_domain="opensubsonic",
                provider_instance=instance_id,
            )
        },
        year=sonic_album.year,
    )

    album.metadata.images = UniqueList()
    if sonic_album.cover_art:
        album.metadata.images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=sonic_album.cover_art,
                provider=instance_id,
                remotely_accessible=False,
            ),
        )

    if sonic_album.artist_id:
        album.artists.append(
            ItemMapping(
                media_type=MediaType.ARTIST,
                item_id=sonic_album.artist_id,
                provider=instance_id,
                name=sonic_album.artist if sonic_album.artist else UNKNOWN_ARTIST,
            )
        )
    else:
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
                        provider_domain="opensubsonic",
                        provider_instance=instance_id,
                    )
                },
            )
        )

    if sonic_info:
        if sonic_info.small_image_url:
            album.metadata.images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=sonic_info.small_image_url,
                    remotely_accessible=False,
                    provider=instance_id,
                )
            )
        if sonic_info.notes:
            album.metadata.description = sonic_info.notes

    return album


def parse_playlist(instance_id: str, sonic_playlist: SonicPlaylist) -> Playlist:
    """Parse subsonic Playlist into MA Playlist."""
    playlist = Playlist(
        item_id=sonic_playlist.id,
        provider="opensubsonic",
        name=sonic_playlist.name,
        is_editable=True,
        provider_mappings={
            ProviderMapping(
                item_id=sonic_playlist.id,
                provider_domain="opensubsonic",
                provider_instance=instance_id,
            )
        },
    )

    if sonic_playlist.cover_art:
        playlist.metadata.images = UniqueList()
        playlist.metadata.images.append(
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
        provider="opensubsonic",
        name=sonic_podcast.title,
        uri=sonic_podcast.url,
        total_episodes=len(sonic_podcast.episode),
        provider_mappings={
            ProviderMapping(
                item_id=sonic_podcast.id,
                provider_domain="opensubsonic",
                provider_instance=instance_id,
            )
        },
    )

    podcast.metadata.description = sonic_podcast.description
    podcast.metadata.images = UniqueList()

    if sonic_podcast.cover_art:
        podcast.metadata.images.append(
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
        provider="opensubsonic",
        name=sonic_episode.title,
        position=pos,
        podcast=parse_podcast(instance_id, sonic_channel),
        provider_mappings={
            ProviderMapping(
                item_id=eid,
                provider_domain="opensubsonic",
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
