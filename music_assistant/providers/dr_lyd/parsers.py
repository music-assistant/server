"""
Parsers of the DR LYD provider.

The documents of the radio API of DR are turned into the channels of the provider and into
Music Assistant items.
"""

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

from .constants import PUBLISHER
from .helpers import get_audio, get_image_url, get_stream
from .models import Channel

if TYPE_CHECKING:
    from .models import DRChannel, DREpisode, DRSeries


def parse_channels(documents: list[DRChannel]) -> list[Channel]:
    """
    Return the live radio channels, with each regional channel listed on its own.

    :param documents: The channels of the channels document of DR.
    """
    channels = []
    for document in documents:
        for item in document.districts or [document]:
            # only the flow channels play round the clock, the others are events and web feeds
            if not item.is_flow_channel or not (stream := get_stream(item.audio_assets)):
                continue
            channels.append(
                Channel(
                    slug=item.slug,
                    name=item.title,
                    urn=item.id,
                    stream_url=stream.url,
                    hls=stream.format == "HLS",
                    image_url=get_image_url(item.channel_logos)
                    or get_image_url(item.image_assets, "SquareImage"),
                )
            )
    return channels


def parse_radio(channel: Channel, instance_id: str, domain: str) -> Radio:
    """
    Return the Music Assistant radio of a channel.

    :param channel: The channel to turn into a radio.
    :param instance_id: The instance id of the provider.
    :param domain: The domain of the provider.
    """
    radio = Radio(
        item_id=channel.slug,
        name=channel.name,
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=channel.slug, provider_domain=domain, provider_instance=instance_id
            )
        },
    )
    radio.metadata.images = _thumbnails(channel.image_url, instance_id)
    return radio


def parse_podcast(series: DRSeries, instance_id: str, domain: str) -> Podcast:
    """
    Return the Music Assistant podcast of a series.

    :param series: The series to turn into a podcast.
    :param instance_id: The instance id of the provider.
    :param domain: The domain of the provider.
    """
    podcast = Podcast(
        item_id=series.production_number,
        name=series.title,
        publisher=PUBLISHER,
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=series.production_number,
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
    )
    podcast.metadata.description = series.description
    podcast.metadata.explicit = series.explicit_content
    podcast.metadata.genres = set(series.categories) or {DEFAULT_AUDIOBOOK_PODCAST_GENRE}
    podcast.metadata.images = _thumbnails(
        get_image_url(series.image_assets, "Podcast", "SquareImage"), instance_id
    )
    podcast.metadata.links = {MediaItemLink(type=LinkType.WEBSITE, url=series.presentation_url)}
    return podcast


def parse_podcast_episode(
    episode: DREpisode, podcast: ItemMapping, position: int, instance_id: str, domain: str
) -> PodcastEpisode:
    """
    Return the Music Assistant episode of an episode.

    :param episode: The episode to turn into a Music Assistant episode.
    :param podcast: The podcast the episode is part of, its picture is the one of an episode
        that has none.
    :param position: The listing position of the episode, oldest to newest.
    :param instance_id: The instance id of the provider.
    :param domain: The domain of the provider.
    """
    audio = get_audio(episode.audio_assets)
    mass_episode = PodcastEpisode(
        item_id=episode.production_number,
        provider=instance_id,
        name=episode.title,
        duration=episode.duration_milliseconds // 1000,
        position=position,
        podcast=podcast,
        # the address of the audio stays out of the item, it is only given to the stream details
        provider_mappings={
            ProviderMapping(
                item_id=episode.production_number,
                provider_domain=domain,
                provider_instance=instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.AAC
                    if audio and audio.format == "HLS"
                    else ContentType.MP3
                ),
            )
        },
    )
    mass_episode.metadata.release_date = episode.start_time
    mass_episode.metadata.description = episode.description
    mass_episode.metadata.explicit = episode.explicit_content
    cover = podcast.image.path if podcast.image else None
    mass_episode.metadata.images = _thumbnails(
        get_image_url(episode.image_assets, "Podcast", "SquareImage") or cover, instance_id
    )
    mass_episode.metadata.links = {
        MediaItemLink(type=LinkType.WEBSITE, url=episode.presentation_url)
    }
    return mass_episode


def _thumbnails(url: str | None, instance_id: str) -> UniqueList[MediaItemImage]:
    """Return the images of an item that has the picture at the url, if it has one."""
    if not url:
        return UniqueList()
    return UniqueList(
        [
            MediaItemImage(
                type=ImageType.THUMB,
                path=url,
                provider=instance_id,
                remotely_accessible=True,
            )
        ]
    )
