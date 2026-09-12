"""Parsers for the iHeartRadio provider."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, ImageType, LinkType, MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemLink,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamMetadata

from .constants import ARTIST_IMAGE_URL, ARTIST_RADIO_PREFIX, ID_SEPARATOR, STREAM_PREFERENCE

if TYPE_CHECKING:
    from collections.abc import Mapping


def parse_live_station(station: Mapping[str, Any], instance_id: str, domain: str) -> Radio | None:
    """
    Create a Radio item from a live station payload.

    Returns None when the payload carries no station id.

    :param station: A station as returned by the liveStations or search endpoint.
    :param instance_id: The provider instance id.
    :param domain: The provider domain.
    """
    if not (station_id := _as_id(station.get("id"))):
        return None
    website = station.get("website") or None
    radio = Radio(
        item_id=station_id,
        provider=instance_id,
        name=str(station.get("name") or station.get("callLetters") or station_id),
        provider_mappings={
            ProviderMapping(
                item_id=station_id,
                provider_domain=domain,
                provider_instance=instance_id,
                # the streams are resolved at playback time, so the format is only
                # known once ffmpeg reads the stream
                audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
                available=bool(station.get("isActive", True)),
                url=website,
            )
        },
    )
    if description := station.get("description"):
        radio.metadata.description = str(description)
    # the station endpoints list genre objects, a search hit names a single genre
    genres = {str(genre["name"]) for genre in station.get("genres") or [] if genre.get("name")}
    if genre := station.get("genre"):
        genres.add(str(genre))
    if genres:
        radio.metadata.genres = genres
    # the station endpoints call the artwork logo, a search hit imageUrl
    if logo := station.get("logo") or station.get("imageUrl"):
        radio.metadata.images = UniqueList(
            [
                remote_image(str(logo), instance_id),
                remote_image(str(logo), instance_id, ImageType.LOGO),
            ]
        )
    if website:
        radio.metadata.links = {MediaItemLink(type=LinkType.WEBSITE, url=str(website))}
    return radio


def parse_artist_radio(artist: Mapping[str, Any], instance_id: str, domain: str) -> Radio | None:
    """
    Create a dynamic Radio item for the artist radio station seeded by an artist.

    Returns None when the payload carries no artist id.

    :param artist: An artist as returned by the follows, search or artist profile endpoint.
    :param instance_id: The provider instance id.
    :param domain: The provider domain.
    """
    # the follows endpoint names the seed artistSeed, the profile artistId, a search hit id
    artist_id = _as_id(artist.get("artistSeed") or artist.get("artistId") or artist.get("id"))
    if not artist_id:
        return None
    item_id = artist_radio_item_id(artist_id)
    name = str(artist.get("artistName") or artist.get("name") or artist_id)
    radio = Radio(
        item_id=item_id,
        provider=instance_id,
        name=f"{name} Radio",
        is_dynamic=True,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=domain,
                provider_instance=instance_id,
                audio_format=AudioFormat(content_type=ContentType.AAC),
            )
        },
    )
    radio.metadata.images = UniqueList([_artist_image(artist_id, instance_id)])
    return radio


def parse_track(
    track: Mapping[str, Any], instance_id: str, domain: str, available: bool = True
) -> Track | None:
    """
    Create a Track item from a track payload.

    Returns None when the payload carries no track id.

    :param track: A track as returned by an artist radio batch or the catalog endpoint.
    :param instance_id: The provider instance id.
    :param domain: The provider domain.
    :param available: Whether the track can be played through this provider.
    """
    if not (track_id := _as_id(track.get("id"))):
        return None
    mass_track = Track(
        item_id=track_id,
        provider=instance_id,
        name=str(track.get("title") or track_id),
        version=str(track.get("version") or ""),
        duration=int(track.get("duration") or 0),
        track_number=int(track.get("trackNumber") or 0),
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain=domain,
                provider_instance=instance_id,
                audio_format=AudioFormat(content_type=ContentType.AAC),
                available=available,
            )
        },
    )
    if artist := _artist_mapping(track, instance_id):
        mass_track.artists = UniqueList([artist])
    if album_id := _as_id(track.get("albumId")):
        mass_track.album = ItemMapping(
            item_id=album_id,
            provider=instance_id,
            name=str(track.get("albumName") or album_id),
            media_type=MediaType.ALBUM,
        )
    if (explicit := track.get("explicitLyrics")) is not None:
        mass_track.metadata.explicit = bool(explicit)
    # a batch item calls the artwork imagePath, the catalog imageUrl
    if image := track.get("imagePath") or track.get("imageUrl"):
        mass_track.metadata.images = UniqueList([remote_image(str(image), instance_id)])
    return mass_track


def parse_artist(artist: Mapping[str, Any], instance_id: str, domain: str) -> Artist | None:
    """
    Create an Artist item from an artist profile payload.

    Returns None when the payload carries no artist id.

    :param artist: The ``artist`` object of an artist profile.
    :param instance_id: The provider instance id.
    :param domain: The provider domain.
    """
    if not (artist_id := _as_id(artist.get("artistId"))):
        return None
    mass_artist = Artist(
        item_id=artist_id,
        provider=instance_id,
        name=str(artist.get("name") or artist_id),
        provider_mappings={
            ProviderMapping(
                item_id=artist_id,
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
    )
    mass_artist.metadata.images = UniqueList([_artist_image(artist_id, instance_id)])
    return mass_artist


def parse_album(album: Mapping[str, Any], instance_id: str, domain: str) -> Album | None:
    """
    Create an Album item from a catalog album payload.

    Returns None when the payload carries no album id.

    :param album: An album as returned by the catalog album endpoint.
    :param instance_id: The provider instance id.
    :param domain: The provider domain.
    """
    if not (album_id := _as_id(album.get("albumId"))):
        return None
    mass_album = Album(
        item_id=album_id,
        provider=instance_id,
        name=str(album.get("title") or album_id),
        provider_mappings={
            ProviderMapping(
                item_id=album_id,
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
    )
    if artist := _artist_mapping(album, instance_id):
        mass_album.artists = UniqueList([artist])
    if (release_date := _as_epoch_seconds(album.get("releaseDate"))) is not None:
        mass_album.year = datetime.fromtimestamp(release_date, tz=UTC).year
    if (explicit := album.get("explicitLyrics")) is not None:
        mass_album.metadata.explicit = bool(explicit)
    if image := album.get("image"):
        mass_album.metadata.images = UniqueList([remote_image(str(image), instance_id)])
    return mass_album


def parse_podcast(podcast: Mapping[str, Any], instance_id: str, domain: str) -> Podcast | None:
    """
    Create a Podcast item from a podcast payload.

    Returns None when the payload carries no podcast id.

    :param podcast: A podcast as returned by the podcast, category or search endpoint.
    :param instance_id: The provider instance id.
    :param domain: The provider domain.
    """
    if not (podcast_id := _as_id(podcast.get("id"))):
        return None
    mass_podcast = Podcast(
        item_id=podcast_id,
        provider=instance_id,
        name=str(podcast.get("title") or podcast_id),
        provider_mappings={
            ProviderMapping(
                item_id=podcast_id,
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
    )
    if description := podcast.get("description"):
        mass_podcast.metadata.description = str(description)
    # the podcast endpoints call the artwork imageUrl, a search hit image
    if image := podcast.get("imageUrl") or podcast.get("image"):
        mass_podcast.metadata.images = UniqueList([remote_image(str(image), instance_id)])
    return mass_podcast


def parse_podcast_episode(
    episode: Mapping[str, Any],
    podcast_id: str,
    position: int,
    instance_id: str,
    domain: str,
    podcast: Mapping[str, Any] | None = None,
) -> PodcastEpisode | None:
    """
    Create a PodcastEpisode item from an episode payload.

    Returns None when the payload carries no episode id.

    :param episode: An episode as returned by the episodes or episode endpoint.
    :param podcast_id: The provider id of the podcast the episode belongs to.
    :param position: The episode's listing position, oldest to newest.
    :param instance_id: The provider instance id.
    :param domain: The provider domain.
    :param podcast: The parent podcast payload, for its name and fallback artwork.
    """
    if not (episode_id := _as_id(episode.get("id"))):
        return None
    podcast = podcast or {}
    item_id = episode_item_id(podcast_id, episode_id)
    name = str(episode.get("title") or episode_id)
    mass_episode = PodcastEpisode(
        item_id=item_id,
        provider=instance_id,
        name=name,
        duration=int(episode.get("duration") or 0),
        position=position,
        podcast=ItemMapping(
            item_id=podcast_id,
            provider=instance_id,
            name=str(podcast.get("title") or name),
            media_type=MediaType.PODCAST,
        ),
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
    )
    if description := episode.get("description"):
        mass_episode.metadata.description = str(description)
    if (start_date := _as_epoch_seconds(episode.get("startDate"))) is not None:
        mass_episode.metadata.release_date = datetime.fromtimestamp(start_date, tz=UTC)
    if (explicit := episode.get("isExplicit")) is not None:
        mass_episode.metadata.explicit = bool(explicit)
    if image := episode.get("imageUrl") or podcast.get("imageUrl"):
        mass_episode.metadata.images = UniqueList([remote_image(str(image), instance_id)])
    return mass_episode


def parse_now_playing(
    now_playing: Mapping[str, Any], fallback_image: str | None = None
) -> StreamMetadata | None:
    """
    Create the live metadata of a station from a currentTrackMeta payload.

    Returns None when the payload names no track.

    :param now_playing: The currentTrackMeta payload of a station.
    :param fallback_image: Image to show when the track has no artwork of its own.
    """
    if not (title := now_playing.get("title")):
        return None
    duration = int(now_playing.get("trackDuration") or 0) or None
    elapsed: int | None = None
    if start_time := _as_epoch_seconds(now_playing.get("startTime")):
        # the payload only dates the track's start, so progress follows the wall clock
        elapsed = max(0, int(time.time() - start_time))
        if duration:
            elapsed = min(elapsed, duration)
    return StreamMetadata(
        title=str(title),
        artist=str(now_playing["artist"]) if now_playing.get("artist") else None,
        album=str(now_playing["album"]) if now_playing.get("album") else None,
        image_url=str(now_playing.get("imagePath") or fallback_image or "") or None,
        duration=duration,
        elapsed_time=elapsed,
        elapsed_time_last_updated=time.time() if elapsed is not None else None,
    )


def pick_stream_url(streams: Mapping[str, Any]) -> str | None:
    """
    Return the best stream url a station offers, or None if it offers none.

    :param streams: The station's ``streams`` mapping.
    """
    for key in STREAM_PREFERENCE:
        if url := streams.get(key):
            return str(url)
    return None


def remote_image(
    url: str, instance_id: str, image_type: ImageType = ImageType.THUMB
) -> MediaItemImage:
    """
    Build an image entry for artwork served by iHeartRadio's CDN.

    :param url: The remote image url.
    :param instance_id: The provider instance id.
    :param image_type: The role of the image.
    """
    return MediaItemImage(type=image_type, path=url, provider=instance_id, remotely_accessible=True)


def episode_item_id(podcast_id: str, episode_id: str) -> str:
    """Build the MA item id of a podcast episode."""
    return f"{podcast_id}{ID_SEPARATOR}{episode_id}"


def split_episode_item_id(item_id: str) -> tuple[str, str] | None:
    """Split an episode item id back into its podcast and episode id."""
    podcast_id, _, episode_id = item_id.partition(ID_SEPARATOR)
    if podcast_id and episode_id:
        return podcast_id, episode_id
    return None


def artist_radio_item_id(artist_id: str) -> str:
    """Build the MA item id of the artist radio seeded by an artist."""
    return f"{ARTIST_RADIO_PREFIX}{artist_id}"


def split_artist_radio_item_id(item_id: str) -> str | None:
    """Return the seed artist id of an artist radio item id, None for a live station."""
    if item_id.startswith(ARTIST_RADIO_PREFIX):
        return item_id.removeprefix(ARTIST_RADIO_PREFIX) or None
    return None


def _artist_mapping(item: Mapping[str, Any], instance_id: str) -> ItemMapping | None:
    """Return the artist a track or album payload names, if any."""
    if not (artist_id := _as_id(item.get("artistId"))):
        return None
    return ItemMapping(
        item_id=artist_id,
        provider=instance_id,
        name=str(item.get("artistName") or artist_id),
        media_type=MediaType.ARTIST,
    )


def _artist_image(artist_id: str, instance_id: str) -> MediaItemImage:
    """Return the artwork iHeartRadio serves for an artist."""
    return remote_image(ARTIST_IMAGE_URL.format(artist_id=artist_id), instance_id)


def _as_id(value: Any) -> str | None:
    """Return an id as string, or None when it is missing (ids come back as numbers)."""
    if value is None or value == "":
        return None
    return str(value)


def _as_epoch_seconds(value: Any) -> int | None:
    """Convert an epoch timestamp in milliseconds to seconds, or None if unusable."""
    try:
        millis = int(value)
    except TypeError, ValueError:
        return None
    return millis // 1000 if millis > 0 else None
