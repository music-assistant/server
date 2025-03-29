"""Parser for ABS -> MASS."""

from contextlib import suppress
from datetime import datetime

from aioaudiobookshelf.schema.library import (
    LibraryItemExpandedBook as AbsLibraryItemExpandedBook,
)
from aioaudiobookshelf.schema.library import (
    LibraryItemExpandedPodcast as AbsLibraryItemExpandedPodcast,
)
from aioaudiobookshelf.schema.library import (
    LibraryItemMinifiedBook as AbsLibraryItemMinifiedBook,
)
from aioaudiobookshelf.schema.library import (
    LibraryItemMinifiedPodcast as AbsLibraryItemMinifiedPodcast,
)
from aioaudiobookshelf.schema.library import (
    LibraryItemPodcast as AbsLibraryItemPodcast,
)
from aioaudiobookshelf.schema.media_progress import MediaProgress as AbsMediaProgress
from aioaudiobookshelf.schema.podcast import PodcastEpisode as AbsPodcastEpisode
from aioaudiobookshelf.schema.podcast import (
    PodcastEpisodeExpanded as AbsPodcastEpisodeExpanded,
)
from music_assistant_models.enums import ContentType, ImageType, MediaType
from music_assistant_models.media_items import Audiobook as MassAudiobook
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    ProviderMapping,
    UniqueList,
)
from music_assistant_models.media_items import Podcast as MassPodcast
from music_assistant_models.media_items import PodcastEpisode as MassPodcastEpisode


def parse_podcast(
    *,
    abs_podcast: AbsLibraryItemExpandedPodcast
    | AbsLibraryItemMinifiedPodcast
    | AbsLibraryItemPodcast,
    lookup_key: str,
    domain: str,
    instance_id: str,
    token: str | None,
    base_url: str,
) -> MassPodcast:
    """Translate ABSPodcast to MassPodcast."""
    title = abs_podcast.media.metadata.title
    # Per API doc title may be None.
    if title is None:
        title = "UNKNOWN"
    mass_podcast = MassPodcast(
        item_id=abs_podcast.id_,
        name=title,
        publisher=abs_podcast.media.metadata.author,
        provider=lookup_key,
        provider_mappings={
            ProviderMapping(
                item_id=abs_podcast.id_,
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
    )
    mass_podcast.metadata.description = abs_podcast.media.metadata.description
    if token is not None:
        image_url = f"{base_url}/api/items/{abs_podcast.id_}/cover?token={token}"
        mass_podcast.metadata.images = UniqueList(
            [MediaItemImage(type=ImageType.THUMB, path=image_url, provider=lookup_key)]
        )
    mass_podcast.metadata.explicit = abs_podcast.media.metadata.explicit
    if abs_podcast.media.metadata.language is not None:
        mass_podcast.metadata.languages = UniqueList([abs_podcast.media.metadata.language])
    if abs_podcast.media.metadata.genres is not None:
        mass_podcast.metadata.genres = set(abs_podcast.media.metadata.genres)

    # podcast object has no published_at int, but an iso string
    if abs_podcast.media.metadata.release_date is not None:
        with suppress(ValueError):
            mass_podcast.metadata.release_date = datetime.fromisoformat(
                abs_podcast.media.metadata.release_date
            )

    if isinstance(abs_podcast, AbsLibraryItemExpandedPodcast | AbsLibraryItemPodcast):
        mass_podcast.total_episodes = len(abs_podcast.media.episodes)
    elif isinstance(abs_podcast, AbsLibraryItemMinifiedPodcast):
        mass_podcast.total_episodes = abs_podcast.media.num_episodes

    return mass_podcast


def parse_podcast_episode(
    *,
    episode: AbsPodcastEpisode | AbsPodcastEpisodeExpanded,
    prov_podcast_id: str,
    fallback_episode_cnt: int | None = None,
    lookup_key: str,
    domain: str,
    instance_id: str,
    token: str | None,
    base_url: str,
    media_progress: AbsMediaProgress | None = None,
) -> MassPodcastEpisode:
    """Translate ABSPodcastEpisode to MassPodcastEpisode.

    For an episode the id is set to f"{podcast_id} {episode_id}".
    ABS ids have no spaces, so we can split at a space to retrieve both
    in other functions.

    NOTE: We should always use a PodcastEpisodeExpanded when possible.
    A PodcastEpisode has only limited information, and is currently only used
    within the recommendations.
    """
    episode_id = f"{prov_podcast_id} {episode.id_}"

    if isinstance(episode, AbsPodcastEpisodeExpanded):
        url = f"{base_url}{episode.audio_track.content_url}"
        duration = int(episode.duration)
        provider_mappings = {
            ProviderMapping(
                item_id=episode_id,
                provider_domain=domain,
                provider_instance=instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.UNKNOWN,
                ),
                url=url,
            )
        }
    else:
        # PodcastEpisode
        duration = 0  # mass default
        provider_mappings = {
            ProviderMapping(
                item_id=episode_id,
                provider_domain=domain,
                provider_instance=instance_id,
            )
        }

    release_date: datetime | None = None
    if episode.published_at is not None:
        position = -episode.published_at
        # abs published_at is ms epoch
        release_date = datetime.fromtimestamp(episode.published_at / 1000)
    else:
        position = 0
        if fallback_episode_cnt is not None:
            position = fallback_episode_cnt
    mass_episode = MassPodcastEpisode(
        item_id=episode_id,
        provider=lookup_key,
        name=episode.title,
        duration=duration,
        position=position,
        podcast=ItemMapping(
            item_id=prov_podcast_id,
            provider=lookup_key,
            name=episode.title,
            media_type=MediaType.PODCAST,
        ),
        provider_mappings=provider_mappings,
    )

    mass_episode.metadata.release_date = release_date

    # cover image
    if token is not None:
        url_api = f"/api/items/{prov_podcast_id}/cover?token={token}"
        url_cover = f"{base_url}{url_api}"
        mass_episode.metadata.images = UniqueList(
            [MediaItemImage(type=ImageType.THUMB, path=url_cover, provider=lookup_key)]
        )

    if media_progress is not None and media_progress.current_time is not None:
        mass_episode.resume_position_ms = int(media_progress.current_time * 1000)
        mass_episode.fully_played = media_progress.is_finished

    return mass_episode


def parse_audiobook(
    *,
    abs_audiobook: AbsLibraryItemExpandedBook | AbsLibraryItemMinifiedBook,
    lookup_key: str,
    domain: str,
    instance_id: str,
    token: str | None,
    base_url: str,
    media_progress: AbsMediaProgress | None = None,
) -> MassAudiobook:
    """Translate AbsBook to Mass Book."""
    title = abs_audiobook.media.metadata.title
    # Per API doc title may be None.
    if title is None:
        title = "UNKNOWN TITLE"
    subtitle = abs_audiobook.media.metadata.subtitle
    if subtitle is not None or subtitle:
        title += f" | {subtitle}"
    mass_audiobook = MassAudiobook(
        item_id=abs_audiobook.id_,
        provider=lookup_key,
        name=title,
        duration=int(abs_audiobook.media.duration),
        provider_mappings={
            ProviderMapping(
                item_id=abs_audiobook.id_,
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
        publisher=abs_audiobook.media.metadata.publisher,
    )
    mass_audiobook.metadata.description = abs_audiobook.media.metadata.description
    if abs_audiobook.media.metadata.language is not None:
        mass_audiobook.metadata.languages = UniqueList([abs_audiobook.media.metadata.language])

    if abs_audiobook.media.metadata.published_date is not None:
        with suppress(ValueError):
            mass_audiobook.metadata.release_date = datetime.fromisoformat(
                abs_audiobook.media.metadata.published_date
            )
    elif abs_audiobook.media.metadata.published_year is not None:
        with suppress(ValueError):
            # ruff: noqa: DTZ001 # ignore tzinfo, this is a fallback attempt
            mass_audiobook.metadata.release_date = datetime(
                year=int(abs_audiobook.media.metadata.published_year), month=1, day=1
            )

    if abs_audiobook.media.metadata.genres is not None:
        mass_audiobook.metadata.genres = set(abs_audiobook.media.metadata.genres)

    mass_audiobook.metadata.explicit = abs_audiobook.media.metadata.explicit

    # cover
    if token is not None:
        api_url = f"/api/items/{abs_audiobook.id_}/cover?token={token}"
        cover_url = f"{base_url}{api_url}"
        mass_audiobook.metadata.images = UniqueList(
            [MediaItemImage(type=ImageType.THUMB, path=cover_url, provider=lookup_key)]
        )

    # expanded version
    if isinstance(abs_audiobook, AbsLibraryItemExpandedBook):
        mass_audiobook.authors.set([x.name for x in abs_audiobook.media.metadata.authors])
        mass_audiobook.narrators.set(abs_audiobook.media.metadata.narrators)
        chapters = []
        for idx, chapter in enumerate(abs_audiobook.media.chapters, 1):
            chapters.append(
                MediaItemChapter(
                    position=idx,
                    name=chapter.title,
                    start=chapter.start,
                    end=chapter.end,
                )
            )
        mass_audiobook.metadata.chapters = chapters

    elif isinstance(abs_audiobook, AbsLibraryItemMinifiedBook):
        mass_audiobook.authors.set([abs_audiobook.media.metadata.author_name])
        mass_audiobook.narrators.set([abs_audiobook.media.metadata.narrator_name])

    if media_progress is not None and media_progress.current_time is not None:
        mass_audiobook.resume_position_ms = int(media_progress.current_time * 1000)
        mass_audiobook.fully_played = media_progress.is_finished

    return mass_audiobook
