"""
Parsers of the DR LYD provider.

Documents of the radio API of DR are read as JSON and turned into the models of the provider,
which are turned into Music Assistant items. Anything unexpected in a document leaves out the
item it is part of.
"""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import ContentType, ImageType, LinkType, MediaType
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

from .constants import PUBLISHER
from .helpers import clean_text, get_image_url, get_items, parse_id, parse_time
from .models import Channel, Episode, Programme, Series, Track


def parse_series(item: Any) -> Series | None:
    """
    Return the series that an item of the series documents of DR describes, if it is available.

    :param item: An item of a list of series, or the document of one series.
    """
    if not isinstance(item, dict) or item.get("isAvailableOnDemand") is False:
        return None
    series_id = parse_id(item.get("productionNumber"))
    title = clean_text(item.get("title"))
    if series_id is None or title is None:
        return None
    episodes = item.get("numberOfEpisodes")
    categories = item.get("categories")
    genres = [clean_text(name) for name in categories] if isinstance(categories, list) else []
    return Series(
        id=series_id,
        title=title,
        description=clean_text(item.get("description")),
        episodes=max(episodes, 0) if isinstance(episodes, int) else 0,
        latest=parse_time(item.get("latestEpisodeStartTime")),
        image_url=get_image_url(item.get("imageAssets"), "Podcast", "SquareImage"),
        genres=tuple(dict.fromkeys(name for name in genres if name)),
        explicit=item.get("explicitContent") is True,
        page_url=clean_text(item.get("presentationUrl")),
    )


def parse_episode(item: Any) -> Episode | None:
    """
    Return the episode that an item of the episode documents of DR describes, if it can be played.

    :param item: An item of a list of episodes, or the document of one episode.
    """
    if not isinstance(item, dict) or item.get("isAvailableOnDemand") is False:
        return None
    episode_id = parse_id(item.get("productionNumber"))
    title = clean_text(item.get("title"))
    audio = _parse_audio(item.get("audioAssets"))
    if episode_id is None or title is None or audio is None:
        return None
    series = item.get("series")
    if not isinstance(series, dict):
        series = {}
    duration = item.get("durationMilliseconds")
    published = parse_time(item.get("startTime"))
    if published is None:
        published = parse_time(item.get("publishTime"))
    return Episode(
        id=episode_id,
        title=title,
        description=clean_text(item.get("description")),
        duration=int(duration / 1000) if isinstance(duration, int | float) and duration > 0 else 0,
        published=published,
        audio_url=audio[0],
        hls=audio[1],
        image_url=get_image_url(item.get("imageAssets"), "Podcast", "SquareImage"),
        explicit=item.get("explicitContent") is True,
        page_url=clean_text(item.get("presentationUrl")),
        series_id=parse_id(series.get("productionNumber")),
        series_title=clean_text(series.get("title")),
    )


def parse_channels(document: Any) -> list[Channel]:
    """
    Return the live radio channels of the channels document, in the order of DR.

    This lists the districts of P4 and P5 in place of P4 and P5 themselves, and only the flow
    channels, which are the stations that play round the clock. The rest of the channels are
    events and web feeds, or have no stream.

    :param document: The document of the channels endpoint.
    """
    channels: dict[str, Channel] = {}
    for entry in document if isinstance(document, list) else []:
        districts = entry.get("districts") if isinstance(entry, dict) else None
        for item in districts if isinstance(districts, list) and districts else [entry]:
            if channel := _parse_channel(item):
                channels.setdefault(channel.slug, channel)
    return list(channels.values())


def parse_tracks(document: Any) -> list[Track]:
    """
    Return the tracks of a live index points document.

    Items that are not tracks, or that lack a title, a start or a duration, are left out.

    :param document: The document of the live index points endpoint of a channel.
    """
    tracks = []
    for item in get_items(document):
        if not isinstance(item, dict) or item.get("type") != "Track":
            continue
        title = clean_text(item.get("title"))
        start = parse_time(item.get("playedTime"))
        duration = item.get("durationMilliseconds")
        if title is None or start is None or not isinstance(duration, int | float) or duration <= 0:
            continue
        tracks.append(Track(title, _parse_artist(item), start, start + duration / 1000))
    return tracks


def parse_programmes(document: Any) -> list[Programme]:
    """
    Return the programmes on air now, from the document of the schedules endpoint.

    :param document: The document of the endpoint that lists the programme of every channel.
    """
    programmes = []
    for entry in document if isinstance(document, list) else []:
        if not isinstance(entry, dict):
            continue
        channel = entry.get("channel")
        slug = clean_text(channel.get("slug")) if isinstance(channel, dict) else None
        title = clean_text(entry.get("title"))
        end = parse_time(entry.get("endTime"))
        if slug is None or title is None or end is None:
            continue
        picture = get_image_url(entry.get("imageAssets"), "SquareImage")
        programmes.append(Programme(slug, title, picture, end))
    return programmes


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


def parse_podcast(series: Series, instance_id: str, domain: str) -> Podcast:
    """
    Return the Music Assistant podcast of a series.

    :param series: The series to turn into a podcast.
    :param instance_id: The instance id of the provider.
    :param domain: The domain of the provider.
    """
    podcast = Podcast(
        item_id=series.id,
        name=series.title,
        publisher=PUBLISHER,
        provider=instance_id,
        total_episodes=series.episodes,
        provider_mappings={
            ProviderMapping(
                item_id=series.id, provider_domain=domain, provider_instance=instance_id
            )
        },
    )
    podcast.metadata.description = series.description
    podcast.metadata.explicit = series.explicit
    podcast.metadata.genres = set(series.genres) or {DEFAULT_AUDIOBOOK_PODCAST_GENRE}
    podcast.metadata.images = _thumbnails(series.image_url, instance_id)
    if series.page_url:
        podcast.metadata.links = {MediaItemLink(type=LinkType.WEBSITE, url=series.page_url)}
    return podcast


def parse_podcast_episode(
    episode: Episode,
    podcast: ItemMapping,
    cover: str | None,
    position: int,
    instance_id: str,
    domain: str,
) -> PodcastEpisode:
    """
    Return the Music Assistant episode of an episode.

    The address of the audio is not part of the item, it is only given to the stream details.

    :param episode: The episode to turn into a Music Assistant episode.
    :param podcast: The podcast the episode is part of.
    :param cover: The picture of the podcast, used when the episode has none.
    :param position: The listing position of the episode, oldest to newest.
    :param instance_id: The instance id of the provider.
    :param domain: The domain of the provider.
    """
    mass_episode = PodcastEpisode(
        item_id=episode.id,
        provider=instance_id,
        name=episode.title,
        duration=episode.duration,
        position=position,
        podcast=podcast,
        provider_mappings={
            ProviderMapping(
                item_id=episode.id,
                provider_domain=domain,
                provider_instance=instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.AAC if episode.hls else ContentType.MP3
                ),
            )
        },
    )
    if episode.published is not None:
        mass_episode.metadata.release_date = from_utc_timestamp(episode.published)
    mass_episode.metadata.description = episode.description
    mass_episode.metadata.explicit = episode.explicit
    mass_episode.metadata.images = _thumbnails(episode.image_url or cover, instance_id)
    if episode.page_url:
        mass_episode.metadata.links = {MediaItemLink(type=LinkType.WEBSITE, url=episode.page_url)}
    return mass_episode


def parse_podcast_reference(podcast_id: str, name: str, instance_id: str) -> ItemMapping:
    """
    Return the reference to a podcast that an episode is part of.

    :param podcast_id: The id of the podcast at DR.
    :param name: The name of the podcast.
    :param instance_id: The instance id of the provider.
    """
    return ItemMapping(
        item_id=podcast_id,
        provider=instance_id,
        name=name,
        media_type=MediaType.PODCAST,
    )


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


def _parse_audio(assets: Any) -> tuple[str, bool] | None:
    """Return the url of the audio to play and whether it is HLS: the best mp3, else HLS."""
    mp3: list[tuple[float, str]] = []
    hls: list[str] = []
    for asset in assets if isinstance(assets, list) else []:
        url = clean_text(asset.get("url")) if isinstance(asset, dict) else None
        if not url or not url.startswith(("https://", "http://")):
            continue
        if asset.get("target") == "Progressive" and asset.get("format") == "mp3":
            bitrate = asset.get("bitrate")
            mp3.append((bitrate if isinstance(bitrate, int | float) else 0, url))
        elif asset.get("format") == "HLS":
            hls.append(url)
    if mp3:
        return max(mp3, key=lambda asset: asset[0])[1], False
    return (hls[0], True) if hls else None


def _parse_channel(item: Any) -> Channel | None:
    """Return the channel an item of the channels document describes, if it plays a stream."""
    if not isinstance(item, dict) or not item.get("isFlowChannel"):
        return None
    slug = clean_text(item.get("slug"))
    title = clean_text(item.get("title"))
    urn = clean_text(item.get("id"))
    stream = _parse_stream(item.get("audioAssets"))
    if slug is None or title is None or urn is None or stream is None:
        return None
    picture = get_image_url(item.get("channelLogos")) or get_image_url(
        item.get("imageAssets"), "SquareImage"
    )
    return Channel(
        slug=slug,
        name=_channel_name(slug, title),
        urn=urn,
        stream_url=stream[0],
        hls=stream[1],
        image_url=picture,
        district=bool(item.get("isDistrict")),
    )


def _parse_stream(assets: Any) -> tuple[str, bool] | None:
    """Return the url of the stream to play and whether it is HLS: the HLS one, else best ICY."""
    hls: list[str] = []
    icy: list[tuple[float, str]] = []
    for asset in assets if isinstance(assets, list) else []:
        url = clean_text(asset.get("url")) if isinstance(asset, dict) else None
        if not url or not url.startswith(("https://", "http://")):
            continue
        if asset.get("format") == "HLS":
            hls.append(url)
        elif asset.get("format") == "ICY":
            bitrate = asset.get("bitrate")
            icy.append((bitrate if isinstance(bitrate, int | float) else 0, url))
    if hls:
        return hls[0], True
    return (max(icy, key=lambda asset: asset[0])[1], False) if icy else None


def _channel_name(slug: str, title: str) -> str:
    """
    Return the name of a channel, DR P3 for P3.

    The title of P6 and P8 lacks what their slugs say: p6beat is DR P6 Beat, p8jazz is DR P8 Jazz.
    """
    compact = title.replace(" ", "").casefold()
    suffix = slug[len(compact) :] if slug.casefold().startswith(compact) else ""
    return " ".join(part for part in ("DR", title, suffix.capitalize()) if part)


def _parse_artist(item: dict[str, Any]) -> str | None:
    """
    Return the artist line of a track.

    It is the line of DR, except for classical music, where that is a long list of credits and
    the composers are named instead.
    """
    if item.get("classical") and (composers := _parse_composers(item)):
        return ", ".join(composers)
    return clean_text(item.get("description"))


def _parse_composers(item: dict[str, Any]) -> list[str]:
    """Return the names of the composers among the roles of a track, each one once."""
    roles = item.get("roles")
    names = [
        clean_text(role.get("name"))
        for role in (roles if isinstance(roles, list) else [])
        if isinstance(role, dict) and str(role.get("role")).casefold() == "komponist"
    ]
    return list(dict.fromkeys(name for name in names if name))
