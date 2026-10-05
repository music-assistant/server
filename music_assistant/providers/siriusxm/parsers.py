"""Parsers for the SiriusXM provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ImageType, LinkType, MediaType
from music_assistant_models.media_items import (
    ItemMapping,
    MediaItemImage,
    MediaItemLink,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    Radio,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamMetadata

from .constants import ID_SEPARATOR, TRACK_QUEUE_TYPES

if TYPE_CHECKING:
    from aiosxm import ArtistStation, Channel, NowPlaying
    from aiosxm import Track as SxmTrack

# SiriusXM's image service renders whatever size is asked for; these are the
# sizes MA actually displays.
THUMB_SIZE = (600, 600)
BANNER_SIZE = (1280, 720)


def parse_radio(
    channel: Channel,
    instance_id: str,
    provider_domain: str,
    *,
    number_prefix: bool = False,
) -> Radio:
    """
    Create a Radio item from a SiriusXM channel.

    :param channel: The channel to parse.
    :param instance_id: The provider instance id.
    :param provider_domain: The provider domain string.
    :param number_prefix: Prefix the name with the channel number. Listeners
        know stations by number, and it gives browse listings a useful sort
        order; library items keep the plain name.
    """
    name = channel.title
    if number_prefix and channel.channel_number:
        name = f"{channel.channel_number} - {name}"
    radio = Radio(
        provider=instance_id,
        item_id=channel.id,
        name=name,
        provider_mappings={
            ProviderMapping(
                provider_domain=provider_domain,
                provider_instance=instance_id,
                item_id=channel.id,
                # An off-air channel still resolves, it just won't play.
                available=not channel.off_air and not channel.unentitled,
            )
        },
    )

    images: UniqueList[MediaItemImage] = UniqueList()
    if thumb := channel.image_url("tile", "1x1", THUMB_SIZE):
        for image_type in (ImageType.THUMB, ImageType.LOGO):
            images.append(
                MediaItemImage(
                    provider=instance_id,
                    type=image_type,
                    path=thumb,
                    remotely_accessible=True,
                )
            )
    if banner := channel.image_url("background", "16x9", BANNER_SIZE):
        for image_type in (ImageType.BANNER, ImageType.LANDSCAPE):
            images.append(
                MediaItemImage(
                    provider=instance_id,
                    type=image_type,
                    path=banner,
                    remotely_accessible=True,
                )
            )

    metadata = MediaItemMetadata(images=images if images else None)
    metadata.description = channel.description
    if channel.genre:
        metadata.genres = {channel.genre}
    radio.metadata = metadata

    # The channel number is what listeners actually know a station by, so keep
    # it searchable/sortable even though it isn't part of the name.
    if channel.channel_number:
        radio.metadata.links = {
            MediaItemLink(
                type=LinkType.WEBSITE,
                url=f"https://www.siriusxm.com/channels/{channel.channel_number}",
            )
        }

    return radio


def queue_item_id(entity_type: str, entity_id: str) -> str:
    """Build the MA id for a station or Xtra channel."""
    return f"{entity_type}{ID_SEPARATOR}{entity_id}"


def split_queue_item_id(item_id: str) -> tuple[str, str] | None:
    """Split a station id back into its SiriusXM entity type and id."""
    entity_type, _, entity_id = item_id.partition(ID_SEPARATOR)
    if entity_type in TRACK_QUEUE_TYPES and entity_id:
        return entity_type, entity_id
    return None


def track_item_id(entity_type: str, entity_id: str, track_id: str) -> str:
    """
    Build the MA id for one track of a station's queue.

    The id carries the parent station, which is needed to resolve the track.
    """
    return f"{entity_type}{ID_SEPARATOR}{entity_id}{ID_SEPARATOR}{track_id}"


def split_track_item_id(item_id: str) -> tuple[str, str, str] | None:
    """Split a track id back into (entity_type, entity_id, track_id)."""
    parts = item_id.split(ID_SEPARATOR)
    if len(parts) == 3 and parts[0] in TRACK_QUEUE_TYPES and all(parts):
        return parts[0], parts[1], parts[2]
    return None


def parse_station(station: ArtistStation, instance_id: str, provider_domain: str) -> Playlist:
    """
    Create a dynamic Playlist from a SiriusXM artist station.

    :param station: The artist station to parse.
    :param instance_id: The provider instance id.
    :param provider_domain: The provider domain string.
    """
    # An artist station is an endless, algorithmically built run of tracks — the
    # same shape as Deezer's Flow — so it is a dynamic playlist rather than
    # radio. Modelling it as radio would lose per-track seeking and skipping,
    # which MA only gives to real track items.
    item_id = queue_item_id("artist-station", station.id)
    playlist = Playlist(
        provider=instance_id,
        item_id=item_id,
        name=station.title,
        owner="SiriusXM",
        is_editable=False,
        is_dynamic=True,
        provider_mappings={
            ProviderMapping(
                provider_domain=provider_domain,
                provider_instance=instance_id,
                item_id=item_id,
            )
        },
    )
    playlist.metadata = _image_metadata(
        station.image_url(*THUMB_SIZE), instance_id, description=station.description
    )
    return playlist


def parse_xtra_playlist(
    channel: Channel, instance_id: str, provider_domain: str, *, number_prefix: bool = False
) -> Playlist:
    """
    Create a dynamic Playlist from an Xtra channel.

    :param channel: The Xtra channel to parse.
    :param instance_id: The provider instance id.
    :param provider_domain: The provider domain string.
    :param number_prefix: Prefix the name with the channel number.
    """
    # Xtra channels are curated music runs, not broadcasts: each tune hands back
    # discrete tracks with their own artwork and durations.
    item_id = queue_item_id(channel.type, channel.id)
    name = channel.title
    if number_prefix and channel.channel_number:
        name = f"{channel.channel_number} - {name}"
    playlist = Playlist(
        provider=instance_id,
        item_id=item_id,
        name=name,
        owner="SiriusXM",
        is_editable=False,
        is_dynamic=True,
        provider_mappings={
            ProviderMapping(
                provider_domain=provider_domain,
                provider_instance=instance_id,
                item_id=item_id,
                available=not channel.off_air and not channel.unentitled,
            )
        },
    )
    playlist.metadata = _image_metadata(
        channel.image_url("tile", "1x1", THUMB_SIZE),
        instance_id,
        description=channel.description,
    )
    if channel.genre:
        playlist.metadata.genres = {channel.genre}
    return playlist


def parse_track(
    track: SxmTrack,
    entity_type: str,
    entity_id: str,
    instance_id: str,
    provider_domain: str,
) -> Track:
    """
    Create a Track from one entry of a station's queue.

    :param track: The queued track.
    :param entity_type: The SiriusXM entity type of the parent station.
    :param entity_id: The SiriusXM entity id of the parent station.
    :param instance_id: The provider instance id.
    :param provider_domain: The provider domain string.
    """
    item_id = track_item_id(entity_type, entity_id, track.id)
    mass_track = Track(
        provider=instance_id,
        item_id=item_id,
        name=track.title,
        duration=int(track.duration) if track.duration else 0,
        provider_mappings={
            ProviderMapping(
                provider_domain=provider_domain,
                provider_instance=instance_id,
                item_id=item_id,
            )
        },
    )
    if track.artist:
        mass_track.artists = UniqueList(
            [
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=track.artist,
                    provider=instance_id,
                    name=track.artist,
                )
            ]
        )
    # No album: a queued track carries an album name but SiriusXM has no album
    # entity behind it, so there is nothing to browse or play. The artwork comes
    # off the track itself.
    mass_track.metadata = _image_metadata(track.image_url(*THUMB_SIZE), instance_id)
    return mass_track


def _image_metadata(
    image_url: str | None, instance_id: str, description: str | None = None
) -> MediaItemMetadata:
    """Build metadata carrying a single thumbnail, when there is one."""
    images: UniqueList[MediaItemImage] = UniqueList()
    if image_url:
        for image_type in (ImageType.THUMB, ImageType.LOGO):
            images.append(
                MediaItemImage(
                    provider=instance_id,
                    type=image_type,
                    path=image_url,
                    remotely_accessible=True,
                )
            )
    metadata = MediaItemMetadata(images=images if images else None)
    if description:
        metadata.description = description
    return metadata


def parse_stream_metadata(
    now_playing: NowPlaying, fallback_image: str | None = None
) -> StreamMetadata | None:
    """
    Build live stream metadata from a now-playing entry.

    :param now_playing: The live track data for the channel.
    :param fallback_image: Channel artwork to use when the track has none.
    """
    if not now_playing.title or now_playing.is_ad:
        # During ad breaks SiriusXM keeps publishing a cut; showing it would
        # replace the station name with an advert in the player UI.
        return None
    return StreamMetadata(
        title=now_playing.title,
        artist=now_playing.artist,
        # SiriusXM reports the show (e.g. "The Weekend Countdown") rather than
        # an album; it is the more useful third line for a radio stream.
        album=now_playing.show,
        image_url=now_playing.image_url(*THUMB_SIZE) or fallback_image,
    )
