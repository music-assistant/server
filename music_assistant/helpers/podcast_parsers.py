"""Podcastfeed -> Mass."""

from datetime import datetime
from io import BytesIO
from typing import Any

import aiohttp
import podcastparser
from aiohttp.client import ClientError
from music_assistant_models.enums import ContentType, ImageType, MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    UniqueList,
)


async def get_podcastparser_dict(
    *, session: aiohttp.ClientSession, feed_url: str, max_episodes: int = 0
) -> dict[str, Any]:
    """Get feed parsed by podcastparser by providing the url.

    max_episodes = 0 does not limit the returned episodes.
    """
    response: aiohttp.ClientResponse | None = None
    # without user agent, some feeds can not be retrieved
    # https://github.com/music-assistant/support/issues/3596
    # but, reports on discord show, that also the opposite may be true
    for headers in [{"User-Agent": "Mozilla/5.0"}, {}]:
        # raises ClientError on status failure
        # ClientError is the base class of all possible Error, i.e. not authorized,
        # url doesn't exist etc.
        try:
            response = await session.get(feed_url, headers=headers, raise_for_status=True)
        except ClientError:
            continue
        break
    if response is None:
        # we did not get a single acceptable response
        raise MediaNotFoundError(
            f"Did not get acceptable response while trying to access {feed_url}."
        )
    feed_data = await response.read()
    feed_stream = BytesIO(feed_data)
    try:
        return podcastparser.parse(feed_url, feed_stream, max_episodes=max_episodes)  # type: ignore[no-any-return]
    except podcastparser.FeedParseError:
        raise MediaNotFoundError(f"The url at {feed_url} returns invalid RSS data.")


def parse_podcast(
    *,
    feed_url: str,
    parsed_feed: dict[str, Any],
    instance_id: str,
    domain: str,
    mass_item_id: str | None = None,
) -> Podcast:
    """Podcast -> Mass Podcast.

    The item_id is the feed url by default, or the optional mass_item_id instead.
    """
    publisher = parsed_feed.get("author") or parsed_feed.get("itunes_author", "NO_AUTHOR")
    item_id = feed_url if mass_item_id is None else mass_item_id
    mass_podcast = Podcast(
        item_id=item_id,
        name=parsed_feed.get("title", "NO_TITLE"),
        publisher=publisher,
        provider=instance_id,
        uri=parsed_feed.get("link"),
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
    )
    genres: list[str] = []
    if _genres := parsed_feed.get("itunes_categories"):
        for _sub_genre in _genres:
            if isinstance(_sub_genre, list):
                genres.extend(x for x in _sub_genre if isinstance(x, str))
            elif isinstance(_sub_genre, str):
                genres.append(_sub_genre)

    mass_podcast.metadata.genres = set(genres)
    mass_podcast.metadata.description = parsed_feed.get("description", "")
    mass_podcast.metadata.explicit = parsed_feed.get("explicit", False)
    language = parsed_feed.get("language")
    if language is not None:
        mass_podcast.metadata.languages = UniqueList([language])
    episodes = parsed_feed.get("episodes", [])
    mass_podcast.total_episodes = len(episodes)
    podcast_cover = parsed_feed.get("cover_url")
    if podcast_cover is not None:
        mass_podcast.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=podcast_cover,
                    provider=instance_id,
                    remotely_accessible=True,
                )
            ]
        )
    return mass_podcast


def get_stream_url_and_guid_from_episode(*, episode: dict[str, Any]) -> tuple[str, str | None]:
    """Give episode's stream url and guid, if it exists."""
    episode_enclosures = episode.get("enclosures", [])
    if len(episode_enclosures) < 1:
        raise ValueError("Episode enclosure is missing")
    if stream_url := episode_enclosures[0].get("url"):
        guid = episode.get("guid")
        if guid is not None:
            # The media's item_id is {prov_podcast_id} {guid_or_stream_url}
            # see parse_podcast_episode.
            # However, the guid must not contain a space, otherwise it is invalid.
            # We cannot check, if it is a proper guid (uuid.UUID4(...)), as some podcast feeds
            # do not follow the standard.
            guid = None if len(guid.split(" ")) > 1 else guid
        return stream_url, guid
    raise ValueError("Stream URL is missing.")


def parse_podcast_episode(
    *,
    episode: dict[str, Any],
    prov_podcast_id: str,
    episode_cnt: int,
    podcast_cover: str | None = None,
    instance_id: str,
    domain: str,
    mass_item_id: str | None = None,
) -> PodcastEpisode | None:
    """Podcast Episode -> Mass Podcast Episode.

    The item_id is {prov_podcast_id} {guid_or_stream_url} by default, or the optional mass_item_id
    instead. The podcast_cover is used, if the episode should not have its own cover.

    The function returns None, if the episode enclosure is missing, i.e. there is no stream
    information present.
    """
    episode_duration = episode.get("total_time", 0.0)
    episode_title = episode.get("title", "NO_EPISODE_TITLE")
    episode_cover = episode.get("episode_art_url", podcast_cover)

    # this is unix epoch in s, and 0 if unknown
    episode_published: int | None = episode.get("published")
    if episode_published == 0:
        episode_published = None

    try:
        stream_url, guid = get_stream_url_and_guid_from_episode(episode=episode)
    except ValueError:
        # we are missing the episode enclosure or stream information
        return None
    # We treat a guid as invalid if contains a space.
    guid_or_stream_url = guid if guid is not None and len(guid.split(" ")) == 1 else stream_url

    # Default episode id. A guid is preferred as identification.
    episode_id = f"{prov_podcast_id} {guid_or_stream_url}" if mass_item_id is None else mass_item_id
    mass_episode = PodcastEpisode(
        item_id=episode_id,
        provider=instance_id,
        name=episode_title,
        duration=int(episode_duration),
        position=episode_cnt,
        podcast=ItemMapping(
            item_id=prov_podcast_id,
            provider=instance_id,
            name=episode_title,
            media_type=MediaType.PODCAST,
        ),
        provider_mappings={
            ProviderMapping(
                item_id=episode_id,
                provider_domain=domain,
                provider_instance=instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.try_parse(stream_url),
                ),
                url=stream_url,
            )
        },
    )
    if episode_published is not None:
        mass_episode.metadata.release_date = datetime.fromtimestamp(episode_published)

    # chapter
    if chapters := episode.get("chapters"):
        _chapters = []
        for cnt, chapter in enumerate(chapters):
            if not isinstance(chapter, dict):
                continue
            title = chapter.get("title")
            start = chapter.get("start")
            if title and start:
                _chapters.append(MediaItemChapter(position=cnt + 1, name=title, start=start))

    # cover image
    if episode_cover is not None:
        mass_episode.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=episode_cover,
                    provider=instance_id,
                    remotely_accessible=True,
                )
            ]
        )

    return mass_episode
