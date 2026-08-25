"""Parser for ABS -> MASS."""

from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING

from aioaudiobookshelf.schema.author import AuthorExpanded as AbsAuthorExpanded
from aioaudiobookshelf.schema.author import AuthorMinified as AbsAuthorMinified
from aioaudiobookshelf.schema.author import Narrator as AbsNarrator
from aioaudiobookshelf.schema.library import (
    LibraryItemExpandedBook as AbsLibraryItemExpandedBook,
)
from aioaudiobookshelf.schema.library import (
    LibraryItemExpandedPodcast as AbsLibraryItemExpandedPodcast,
)
from aioaudiobookshelf.schema.library import (
    LibraryItemMinifiedPodcast as AbsLibraryItemMinifiedPodcast,
)
from aioaudiobookshelf.schema.library import (
    LibraryItemPodcast as AbsLibraryItemPodcast,
)
from aioaudiobookshelf.schema.podcast import PodcastEpisode as AbsPodcastEpisode
from aioaudiobookshelf.schema.podcast import (
    PodcastEpisodeExpanded as AbsPodcastEpisodeExpanded,
)
from music_assistant_models.enums import ArtistType, ContentType, ImageType, MediaType
from music_assistant_models.media_items import Artist as MassArtist
from music_assistant_models.media_items import Audiobook as MassAudiobook
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    MediaItemChapter,
    MediaItemCollection,
    MediaItemImage,
    ProviderMapping,
    UniqueList,
)
from music_assistant_models.media_items import Playlist as MassPlaylist
from music_assistant_models.media_items import Podcast as MassPodcast
from music_assistant_models.media_items import PodcastEpisode as MassPodcastEpisode

from music_assistant.helpers.datetime import from_utc_timestamp
from music_assistant.providers.audiobookshelf.helpers import NarratorHelper

if TYPE_CHECKING:
    from aioaudiobookshelf.schema.media_progress import MediaProgress as AbsMediaProgress
    from aioaudiobookshelf.schema.playlist import PlaylistExpanded as AbsPlaylistExpanded


def _build_cover_url(*, base_url: str, item_id: str, token: str, version: int | None = None) -> str:
    """
    Build the cover url for an Audiobookshelf library item.

    :param base_url: Base url of the Audiobookshelf server.
    :param item_id: Id of the library item to fetch the cover for.
    :param token: Access token for the Audiobookshelf api.
    :param version: Last-updated timestamp of the item.
    """
    # the cover endpoint is static and keeps its filename when artwork is
    # replaced in place, so append the last-updated timestamp to bust caches
    cover_url = f"{base_url}/api/items/{item_id}/cover?token={token}"
    if version is not None:
        cover_url += f"&ts={version}"
    return cover_url


def parse_playlist(
    *,
    abs_playlist: AbsPlaylistExpanded,
    instance_id: str,
    domain: str,
    token: str,
    base_url: str,
    owner: str,
    media_type: MediaType,
) -> MassPlaylist:
    """Translate AbsPlaylist to MassPlaylist."""
    mass_playlist = MassPlaylist(
        item_id=abs_playlist.id_,
        provider=instance_id,
        name=abs_playlist.name,
        sort_name=abs_playlist.name,
        provider_mappings={
            ProviderMapping(
                item_id=abs_playlist.id_, provider_domain=domain, provider_instance=instance_id
            )
        },
        supported_mediatypes={media_type},
        is_editable=True,
        owner=owner,
    )
    # cover
    if abs_playlist.cover_path is not None:
        cover_url = _build_cover_url(
            base_url=base_url,
            item_id=abs_playlist.id_,
            token=token,
            version=abs_playlist.last_update,
        )
        mass_playlist.metadata.images = UniqueList(
            [MediaItemImage(type=ImageType.THUMB, path=cover_url, provider=instance_id)]
        )
    else:
        mass_playlist.metadata.images = UniqueList([])
    return mass_playlist


def parse_podcast(
    *,
    abs_podcast: AbsLibraryItemExpandedPodcast
    | AbsLibraryItemMinifiedPodcast
    | AbsLibraryItemPodcast,
    instance_id: str,
    domain: str,
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
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=abs_podcast.id_,
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
    )
    mass_podcast.metadata.description = abs_podcast.media.metadata.description
    if token is not None and abs_podcast.media.cover_path is not None:
        image_url = _build_cover_url(
            base_url=base_url,
            item_id=abs_podcast.id_,
            token=token,
            version=abs_podcast.updated_at,
        )
        mass_podcast.metadata.images = UniqueList(
            [MediaItemImage(type=ImageType.THUMB, path=image_url, provider=instance_id)]
        )
    elif abs_podcast.media.cover_path is None:
        mass_podcast.metadata.images = UniqueList([])

    mass_podcast.metadata.explicit = abs_podcast.media.metadata.explicit
    mass_podcast.metadata.languages = (
        UniqueList([abs_podcast.media.metadata.language])
        if abs_podcast.media.metadata.language is not None
        else UniqueList([])
    )
    mass_podcast.metadata.genres = (
        set(abs_podcast.media.metadata.genres)
        if abs_podcast.media.metadata.genres is not None
        else set()
    )

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
    prov_podcast_name: str | None,
    fallback_episode_cnt: int | None = None,
    instance_id: str,
    domain: str,
    token: str | None,
    base_url: str,
    media_progress: AbsMediaProgress | None = None,
    cover_path: str | None = None,
    cover_version: int | None = None,
) -> MassPodcastEpisode:
    """
    Translate ABSPodcastEpisode to MassPodcastEpisode.

    For an episode the id is set to f"{podcast_id} {episode_id}".
    ABS ids have no spaces, so we can split at a space to retrieve both
    in other functions.

    NOTE: We should always use a PodcastEpisodeExpanded when possible.
    A PodcastEpisode has only limited information, and is currently only used
    within the recommendations.
    """
    # ruff: noqa: PLR0913 (too many arguments)
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
        position = episode.published_at
        # abs published_at is ms epoch; leave the date unset if it is out of range
        with suppress(ValueError, OverflowError, OSError):
            release_date = from_utc_timestamp(episode.published_at / 1000)
    else:
        position = 0
        if fallback_episode_cnt is not None:
            position = fallback_episode_cnt
    mass_episode = MassPodcastEpisode(
        item_id=episode_id,
        provider=instance_id,
        name=episode.title,
        duration=duration,
        position=position,
        podcast=ItemMapping(
            item_id=prov_podcast_id,
            provider=instance_id,
            name=prov_podcast_name or episode.title,
            media_type=MediaType.PODCAST,
        ),
        provider_mappings=provider_mappings,
    )

    mass_episode.metadata.release_date = release_date
    if episode.description:
        mass_episode.metadata.description = episode.description

    # cover image
    if token is not None and cover_path:
        url_cover = _build_cover_url(
            base_url=base_url,
            item_id=prov_podcast_id,
            token=token,
            version=cover_version,
        )
        mass_episode.metadata.images = UniqueList(
            [MediaItemImage(type=ImageType.THUMB, path=url_cover, provider=instance_id)]
        )

    if media_progress is not None and media_progress.current_time is not None:
        mass_episode.resume_position_ms = int(media_progress.current_time * 1000)
        mass_episode.fully_played = media_progress.is_finished

    mass_episode.metadata.chapters = [
        MediaItemChapter(
            position=position,
            name=chapter.title,
            start=chapter.start,
            end=chapter.end,
        )
        for position, chapter in enumerate(episode.chapters, 1)
    ]

    return mass_episode


def parse_audiobook(
    *,
    abs_audiobook: AbsLibraryItemExpandedBook,
    instance_id: str,
    domain: str,
    token: str | None,
    base_url: str,
    audiobook_narrators: set[AbsNarrator] | set[NarratorHelper],
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
        provider=instance_id,
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
    mass_audiobook.metadata.languages = (
        UniqueList([abs_audiobook.media.metadata.language])
        if abs_audiobook.media.metadata.language is not None
        else UniqueList([])
    )

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

    book_series: list[MediaItemCollection] = []
    for abs_series_sequence in abs_audiobook.media.metadata.series:
        book_series.append(
            MediaItemCollection(
                title=abs_series_sequence.name, sequence=abs_series_sequence.sequence
            )
        )
    mass_audiobook.metadata.collections = UniqueList(book_series)

    mass_audiobook.metadata.genres = (
        set(abs_audiobook.media.metadata.genres)
        if abs_audiobook.media.metadata.genres is not None
        else set()
    )

    mass_audiobook.metadata.explicit = abs_audiobook.media.metadata.explicit

    # cover
    if token is not None and abs_audiobook.media.cover_path is not None:
        cover_url = _build_cover_url(
            base_url=base_url,
            item_id=abs_audiobook.id_,
            token=token,
            version=abs_audiobook.updated_at,
        )
        mass_audiobook.metadata.images = UniqueList(
            [MediaItemImage(type=ImageType.THUMB, path=cover_url, provider=instance_id)]
        )
    elif abs_audiobook.media.cover_path is None:
        mass_audiobook.metadata.images = UniqueList([])

    # expanded version
    mass_audiobook.authors.set(
        [
            parse_author(
                abs_author=author,
                instance_id=instance_id,
                domain=domain,
                token=token,
                base_url=base_url,
            )
            for author in abs_audiobook.media.metadata.authors
        ]
    )

    mass_audiobook.narrators.set(
        [
            parse_narrator(abs_narrator=narrator, instance_id=instance_id, domain=domain)
            for narrator in audiobook_narrators
        ]
    )

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

    if media_progress is not None and media_progress.current_time is not None:
        mass_audiobook.resume_position_ms = int(media_progress.current_time * 1000)
        mass_audiobook.fully_played = media_progress.is_finished

    mass_audiobook.date_added = from_utc_timestamp(abs_audiobook.added_at / 1000)

    return mass_audiobook


def parse_author(
    *,
    abs_author: AbsAuthorExpanded | AbsAuthorMinified,
    instance_id: str,
    domain: str,
    token: str | None,
    base_url: str,
) -> MassArtist:
    """Translate AbsAuthor to MassArtist."""
    mass_artist = MassArtist(
        item_id=abs_author.id_,
        provider=instance_id,
        name=abs_author.name,
        sort_name=abs_author.name,
        provider_mappings={
            ProviderMapping(
                item_id=abs_author.id_, provider_domain=domain, provider_instance=instance_id
            )
        },
        artist_type=ArtistType.AUTHOR,
    )
    # cover
    if (
        isinstance(abs_author, AbsAuthorExpanded)
        and abs_author.image_path is not None
        and token is not None
    ):
        api_url = f"/api/authors/{abs_author.id_}/image?token={token}"
        cover_url = f"{base_url}{api_url}"
        mass_artist.metadata.images = UniqueList(
            [MediaItemImage(type=ImageType.THUMB, path=cover_url, provider=instance_id)]
        )
    elif isinstance(abs_author, AbsAuthorExpanded) and abs_author.image_path is None:
        mass_artist.metadata.images = UniqueList([])

    return mass_artist


def parse_narrator(
    *,
    abs_narrator: AbsNarrator | NarratorHelper,
    instance_id: str,
    domain: str,
) -> MassArtist:
    """Translate AbsNarrator to MassArtist."""
    return MassArtist(
        item_id=abs_narrator.id_,
        provider=instance_id,
        name=abs_narrator.name,
        sort_name=abs_narrator.name,
        provider_mappings={
            ProviderMapping(
                item_id=abs_narrator.id_, provider_domain=domain, provider_instance=instance_id
            )
        },
        artist_type=ArtistType.NARRATOR,
    )
