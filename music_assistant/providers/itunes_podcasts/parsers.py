"""Podcastfeed -> Mass."""

from typing import Any

from music_assistant_models.enums import ContentType, ImageType, MediaType
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


def parse_podcast(
    *, feed_url: str, parsed_feed: dict[str, Any], lookup_key: str, domain: str, instance_id: str
) -> Podcast:
    """Podcast -> Mass Podcast."""
    publisher = parsed_feed.get("author") or parsed_feed.get("itunes_author", "NO_AUTHOR")
    mass_podcast = Podcast(
        item_id=feed_url,
        name=parsed_feed.get("title", "NO_TITLE"),
        publisher=publisher,
        provider=lookup_key,
        uri=parsed_feed.get("link"),
        provider_mappings={
            ProviderMapping(
                item_id=feed_url,
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
                    provider=lookup_key,
                    remotely_accessible=True,
                )
            ]
        )
    return mass_podcast


def parse_podcast_episode(
    *,
    episode: dict[str, Any],
    prov_podcast_id: str,
    episode_cnt: int,
    podcast_cover: str | None = None,
    lookup_key: str,
    domain: str,
    instance_id: str,
) -> PodcastEpisode:
    """Podcast Episode -> Mass Podcast Episode."""
    episode_duration = episode.get("total_time", 0.0)
    episode_title = episode.get("title", "NO_EPISODE_TITLE")
    episode_cover = episode.get("episode_art_url", podcast_cover)
    episode_published = episode.get("published")
    episode_enclosures = episode.get("enclosures", [])
    if len(episode_enclosures) < 1:
        raise RuntimeError
    stream_url = episode_enclosures[0].get("url", None)
    # not all feeds have a guid, but a guid is preferred as identification
    guid_or_stream_url = episode.get("guid", stream_url)

    episode_id = f"{prov_podcast_id} {guid_or_stream_url}"
    mass_episode = PodcastEpisode(
        item_id=episode_id,
        provider=lookup_key,
        name=episode_title,
        duration=int(episode_duration),
        position=episode_cnt,
        podcast=ItemMapping(
            item_id=prov_podcast_id,
            provider=lookup_key,
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
    mass_episode.metadata.release_date = episode_published

    # chapter
    if chapters := episode.get("chapters"):
        _chapters = []
        for cnt, chapter in chapters:
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
                    provider=lookup_key,
                    remotely_accessible=True,
                )
            ]
        )

    return mass_episode
