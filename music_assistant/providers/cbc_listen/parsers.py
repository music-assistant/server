"""Parsers turning CBC Listen API models into Music Assistant media items."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, ImageType, LinkType
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemLink,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    UniqueList,
)

from music_assistant.constants import DEFAULT_AUDIOBOOK_PODCAST_GENRE
from music_assistant.helpers.datetime import from_utc_timestamp

from .constants import DEFAULT_IMAGE_URL, NETWORK_LOGOS, PUBLISHER
from .helpers import format_image_url
from .models import Channel

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .models import CBCClip, CBCNetwork, CBCShow


def parse_channels(networks: Iterable[CBCNetwork]) -> list[Channel]:
    """
    Flatten CBC live radio networks into individual regional channels.

    :param networks: The live radio network documents from CBC Listen.
    """
    channels: list[Channel] = []
    for network in networks:
        for stream in network.streams:
            if not stream.stream_url:
                continue
            channels.append(
                Channel(
                    station_id=str(stream.id),
                    name=" ".join(stream.full_title.split()),
                    network_id=network.network_id,
                    location_key=stream.location_key,
                    province=stream.province,
                    call_sign=stream.call_sign,
                    stream_url=stream.stream_url,
                )
            )
    return sorted(channels, key=lambda item: (item.network_id, item.name.casefold()))


def parse_radio(channel: Channel, instance_id: str, domain: str) -> Radio:
    """
    Convert a CBC ``Channel`` into a Music Assistant ``Radio`` item.

    :param channel: The channel to convert.
    :param instance_id: The provider instance ID.
    :param domain: The provider domain.
    """
    radio = Radio(
        item_id=channel.station_id,
        name=channel.name,
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=channel.station_id,
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
    )
    radio.metadata.images = _thumbnails(
        NETWORK_LOGOS.get(channel.network_id, DEFAULT_IMAGE_URL), instance_id
    )
    return radio


def parse_podcast(show: CBCShow, instance_id: str, domain: str) -> Podcast:
    """
    Convert a ``CBCShow`` into a Music Assistant ``Podcast`` item.

    :param show: The CBC podcast or radio show to convert.
    :param instance_id: The provider instance ID.
    :param domain: The provider domain.
    """
    item_id = str(show.id)
    podcast = Podcast(
        item_id=item_id,
        name=show.title.strip(),
        publisher=PUBLISHER,
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
    )
    hosts = show.hosts.strip()
    description = show.description.strip()
    podcast.metadata.description = (
        f"Hosted by {hosts}.\n\n{description}".strip() if hosts else description
    )
    podcast.metadata.genres = (
        {show.category.strip()}
        if show.category and show.category.strip()
        else {DEFAULT_AUDIOBOOK_PODCAST_GENRE}
    )
    podcast.metadata.images = _thumbnails(
        format_image_url(show.image) or DEFAULT_IMAGE_URL, instance_id
    )
    if show.web_url:
        podcast.metadata.links = {MediaItemLink(type=LinkType.WEBSITE, url=show.web_url)}
    return podcast


def parse_podcast_episode(
    clip: CBCClip,
    podcast: ItemMapping,
    position: int,
    instance_id: str,
    domain: str,
) -> PodcastEpisode:
    """
    Convert a ``CBCClip`` into a Music Assistant ``PodcastEpisode`` item.

    :param clip: The CBC clip to convert.
    :param podcast: ItemMapping of the parent podcast/show.
    :param position: The listing position of the episode, oldest to newest.
    :param instance_id: The provider instance ID.
    :param domain: The provider domain.
    """
    item_id = str(clip.clip_id)
    episode = PodcastEpisode(
        item_id=item_id,
        provider=instance_id,
        name=clip.title.strip(),
        duration=clip.duration,
        position=position,
        podcast=podcast,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=domain,
                provider_instance=instance_id,
                audio_format=AudioFormat(content_type=ContentType.MP3),
            )
        },
    )
    if clip.released_at > 0:
        episode.metadata.release_date = from_utc_timestamp(clip.released_at / 1000)
    episode.metadata.description = clip.description.strip()
    cover = podcast.image.path if podcast.image else DEFAULT_IMAGE_URL
    episode.metadata.images = _thumbnails(
        format_image_url(clip.image_url) or cover,
        instance_id,
    )
    if clip.web_url:
        episode.metadata.links = {MediaItemLink(type=LinkType.WEBSITE, url=clip.web_url)}
    return episode


def _thumbnails(url: str, instance_id: str) -> UniqueList[MediaItemImage]:
    """Return a ``UniqueList`` containing the thumbnail image for ``url``."""
    return UniqueList(
        [
            MediaItemImage(
                type=ImageType.THUMB,
                path=url,
                provider=instance_id,
                remotely_accessible=False,
            )
        ]
    )
