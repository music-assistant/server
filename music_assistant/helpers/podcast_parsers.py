"""Podcastfeed -> Mass."""

import logging
from datetime import UTC, datetime
from io import BytesIO
from math import isfinite
from typing import TYPE_CHECKING, Any

import podcastparser
from aiohttp.client import ClientError, ClientTimeout
from music_assistant_models.enums import ContentType, ImageType, LinkType, MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    MediaItemLink,
    MediaItemTranscriptCue,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    UniqueList,
)

from music_assistant.helpers.transcripts import (
    cues_to_text,
    document_to_text,
    parse_transcript_cues,
)

if TYPE_CHECKING:
    import aiohttp

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)

# best-effort enrichment must never stall episode resolution, so cap the chapter fetch
_CHAPTERS_FETCH_TIMEOUT = ClientTimeout(total=10)
_TRANSCRIPT_FETCH_TIMEOUT = ClientTimeout(total=15)

# some podcast hosts and CDNs reject anything that does not look like a browser, so these
# fetches override the Music Assistant user agent that the session sends by default
# https://github.com/music-assistant/support/issues/3596
_BROWSER_UA_HEADERS = {"User-Agent": "Mozilla/5.0"}

# transcript formats carrying timings, most preferred first. Anything else is still
# usable, but only as untimed text.
_TIMED_TRANSCRIPT_TYPES = ("text/vtt", "application/x-subrip", "application/srt", "text/srt")

# defaults for the parsed-feed cache shared by the podcast providers
CACHE_CATEGORY_PODCAST_FEED = 0
PODCAST_FEED_CACHE_EXPIRATION = 24 * 3600

# a published transcript does not change, so it is cached far longer than the feed to keep
# repeat views of an episode off the publisher's server
CACHE_CATEGORY_PODCAST_TRANSCRIPT = 1
PODCAST_TRANSCRIPT_CACHE_EXPIRATION = 3600 * 24 * 180


async def get_podcastparser_dict(
    *, session: aiohttp.ClientSession, feed_url: str, max_episodes: int = 0
) -> dict[str, Any]:
    """
    Get feed parsed by podcastparser by providing the url.

    max_episodes = 0 does not limit the returned episodes.
    """
    feed_data: bytes | None = None
    # reports on discord show that some feeds need the opposite, so also try without any UA
    for headers in [_BROWSER_UA_HEADERS, {}]:
        # raises ClientError on status failure
        # ClientError is the base class of all possible Error, i.e. not authorized,
        # url doesn't exist etc. The body is read inside the context manager, so a
        # connection is never left open when a feed fails midway.
        try:
            async with session.get(feed_url, headers=headers, raise_for_status=True) as response:
                feed_data = await response.read()
        except ClientError:
            continue
        break
    if feed_data is None:
        # we did not get a single acceptable response
        raise MediaNotFoundError(
            f"Did not get acceptable response while trying to access {feed_url}."
        )
    feed_stream = BytesIO(feed_data)
    try:
        return podcastparser.parse(feed_url, feed_stream, max_episodes=max_episodes)  # type: ignore[no-any-return]
    except podcastparser.FeedParseError:
        raise MediaNotFoundError(f"The url at {feed_url} returns invalid RSS data.")


async def get_cached_podcast(
    *,
    mass: MusicAssistant,
    provider_instance_id: str,
    feed_url: str,
    max_episodes: int = 0,
    cache_category: int = CACHE_CATEGORY_PODCAST_FEED,
    cache_expiration: int = PODCAST_FEED_CACHE_EXPIRATION,
) -> dict[str, Any]:
    """
    Return a podcast's parsed feed, retrieving and caching it when not cached yet.

    :param mass: The MusicAssistant instance holding the cache.
    :param provider_instance_id: Provider instance the cache entry belongs to.
    :param feed_url: The podcast's feed url, also used as the cache key.
    :param max_episodes: Maximum number of episodes to parse, 0 for unlimited.
    :param cache_category: Cache category to store the parsed feed under.
    :param cache_expiration: Time in seconds the cached feed stays valid.
    :raises MediaNotFoundError: If the feed could not be retrieved or parsed.
    """
    parsed_feed = await mass.cache.get(
        key=feed_url,
        provider=provider_instance_id,
        category=cache_category,
        default=None,
    )
    if parsed_feed is None:
        return await refresh_cached_podcast(
            mass=mass,
            provider_instance_id=provider_instance_id,
            feed_url=feed_url,
            max_episodes=max_episodes,
            cache_category=cache_category,
            cache_expiration=cache_expiration,
        )
    # this is a dictionary from podcastparser
    return parsed_feed  # type: ignore[no-any-return]


async def refresh_cached_podcast(
    *,
    mass: MusicAssistant,
    provider_instance_id: str,
    feed_url: str,
    max_episodes: int = 0,
    cache_category: int = CACHE_CATEGORY_PODCAST_FEED,
    cache_expiration: int = PODCAST_FEED_CACHE_EXPIRATION,
) -> dict[str, Any]:
    """
    Retrieve a podcast's feed and store it in the cache, replacing any cached copy.

    Use this on the library sync path: the sync must always refresh the cached feed,
    regardless of whether a (still valid) cache entry exists.

    :param mass: The MusicAssistant instance holding the cache.
    :param provider_instance_id: Provider instance the cache entry belongs to.
    :param feed_url: The podcast's feed url, also used as the cache key.
    :param max_episodes: Maximum number of episodes to parse, 0 for unlimited.
    :param cache_category: Cache category to store the parsed feed under.
    :param cache_expiration: Time in seconds the cached feed stays valid.
    :raises MediaNotFoundError: If the feed could not be retrieved or parsed.
    """
    parsed_feed = await get_podcastparser_dict(
        session=mass.http_session, feed_url=feed_url, max_episodes=max_episodes
    )
    await mass.cache.set(
        key=feed_url,
        provider=provider_instance_id,
        category=cache_category,
        data=parsed_feed,
        expiration=cache_expiration,
    )
    return parsed_feed


def parse_podcast(
    *,
    feed_url: str,
    parsed_feed: dict[str, Any],
    instance_id: str,
    domain: str,
    mass_item_id: str | None = None,
) -> Podcast:
    """
    Podcast -> Mass Podcast.

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


def get_stream_url_from_episode(*, episode: dict[str, Any]) -> str | None:
    """
    Give the url of the episode's playable enclosure, or None if the episode has none.

    Prefers the first audio or video enclosure, falling back to any non-image one.

    :param episode: A single episode dict as returned by podcastparser.
    """
    fallback: str | None = None
    for enclosure in episode.get("enclosures", []):
        url = enclosure.get("url")
        if not url:
            continue
        mime_type = str(enclosure.get("mime_type") or "")
        if mime_type.startswith(("audio/", "video/")):
            return str(url)
        # feeds do declare bogus mime types (e.g. type="file") for real audio, so anything
        # that is not an image stays a candidate in case no audio enclosure is declared
        if fallback is None and not mime_type.startswith("image/"):
            fallback = str(url)
    return fallback


def get_stream_url_and_guid_from_episode(*, episode: dict[str, Any]) -> tuple[str, str | None]:
    """Give episode's stream url and guid, if it exists."""
    stream_url = get_stream_url_from_episode(episode=episode)
    if stream_url is None:
        raise ValueError("Episode has no playable enclosure")
    guid = episode.get("guid")
    if guid is not None:
        # The media's item_id is {prov_podcast_id} {guid_or_stream_url}
        # see parse_podcast_episode.
        # However, the guid must not contain a space, otherwise it is invalid.
        # We cannot check, if it is a proper guid (uuid.UUID4(...)), as some podcast feeds
        # do not follow the standard.
        guid = None if len(guid.split(" ")) > 1 else guid
    return stream_url, guid


def find_episode_stream_url(*, parsed_feed: dict[str, Any], guid_or_stream_url: str) -> str | None:
    """
    Return the stream url of the episode identified by the item_id's episode part.

    :param parsed_feed: The podcastparser dict of the feed holding the episode.
    :param guid_or_stream_url: Episode part of the item_id, see parse_podcast_episode.
    """
    for episode in parsed_feed.get("episodes", []):
        try:
            stream_url, guid = get_stream_url_and_guid_from_episode(episode=episode)
        except ValueError:
            # episode without a playable enclosure carries no stream; skip it instead of
            # aborting the lookup for the (potentially later) requested episode
            continue
        # only a guid rejected as unusable (None) falls back to the stream url, so an
        # empty guid resolves the same way it was turned into an item_id
        if guid_or_stream_url == (stream_url if guid is None else guid):
            return stream_url
    return None


def rank_episodes_by_date(dates: list[Any]) -> list[int]:
    """
    Return the position of every episode, in the order the provider lists them.

    Positions run oldest to newest, so the newest episode has the highest one. Episodes
    without a date rank as the oldest, and when none of them has one the provider is
    assumed to list its episodes newest-first.

    :param dates: The publication dates in listing order, None where an episode has none.
        Any comparable type will do as long as the provider reports them all the same way.
    """
    total = len(dates)
    dated = [idx for idx, date in enumerate(dates) if date is not None]
    if not dated:
        return [total - idx for idx in range(total)]
    undated = [idx for idx, date in enumerate(dates) if date is None]
    positions = [0] * total
    for position, idx in enumerate(undated + sorted(dated, key=lambda idx: dates[idx]), 1):
        positions[idx] = position
    return positions


def get_episode_positions(episodes: list[dict[str, Any]]) -> list[int]:
    """
    Return the position of every episode, in the order the feed lists them.

    Positions run oldest to newest, so the newest episode has the highest one.

    :param episodes: The episodes of a single feed, as parsed by podcastparser.
    """
    # a feed that numbers only part of its episodes, or restarts its numbering every
    # season, mixes incompatible scales, so its numbers are only used when unique
    numbered = all(isinstance(ep.get("number"), int) and ep["number"] > 0 for ep in episodes)
    if numbered and len({ep.get("season") or 0 for ep in episodes}) == 1:
        return [ep["number"] for ep in episodes]
    # podcastparser lists serial feeds oldest-first and all others newest-first,
    # so rank on the publication date instead of the feed order
    return rank_episodes_by_date([ep.get("published") or None for ep in episodes])


def parse_podcast_episode(
    *,
    episode: dict[str, Any],
    prov_podcast_id: str,
    position: int,
    podcast_cover: str | None = None,
    podcast_name: str | None = None,
    instance_id: str,
    domain: str,
    mass_item_id: str | None = None,
) -> PodcastEpisode | None:
    """
    Podcast Episode -> Mass Podcast Episode.

    The item_id is {prov_podcast_id} {guid_or_stream_url} by default, or the optional mass_item_id
    instead. The podcast_cover is used, if the episode should not have its own cover. The
    podcast_name names the parent podcast reference (falls back to the episode title if unset).

    The function returns None, if the episode enclosure is missing, i.e. there is no stream
    information present.

    :param position: The episode's listing position, oldest to newest.
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
        position=position,
        podcast=ItemMapping(
            item_id=prov_podcast_id,
            provider=instance_id,
            name=podcast_name or episode_title,
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
        mass_episode.metadata.release_date = datetime.fromtimestamp(episode_published, tz=UTC)

    # description (podcastparser normalizes this to plain text, defaulting to "")
    if description := episode.get("description"):
        mass_episode.metadata.description = description

    # explicit flag (itunes:explicit); only set when the feed actually declared it
    explicit = episode.get("explicit")
    if explicit is not None:
        mass_episode.metadata.explicit = bool(explicit)

    # episode webpage (the item <link>)
    if link := episode.get("link"):
        mass_episode.metadata.links = {MediaItemLink(type=LinkType.WEBSITE, url=link)}

    # hosts/guests (podcast:person), mapped to performer names
    if performers := parse_podcast_persons(episode.get("persons")):
        mass_episode.metadata.performers = set(performers)

    # inline chapters (Podlove Simple Chapters, parsed by podcastparser)
    if chapters := episode.get("chapters"):
        _chapters: list[MediaItemChapter] = []
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            title = chapter.get("title")
            start = chapter.get("start")
            # start may legitimately be 0 (opening chapter), so test against None
            if title and start is not None:
                _chapters.append(
                    MediaItemChapter(position=len(_chapters) + 1, name=title, start=start)
                )
        if _chapters:
            mass_episode.metadata.chapters = _chapters

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


def parse_podcast_persons(persons: Any) -> list[str]:
    """
    Extract performer names from a Podcasting 2.0 ``podcast:person`` collection.

    Accepts the persons list as produced by podcastparser (feeds) or by the Podcast
    Index API. Returns the display names de-duplicated (case-insensitive) in feed
    order; any non-list input yields no names, so callers can pass a raw
    ``.get("persons")`` without guarding.

    :param persons: The raw persons collection, or any value (non-lists yield []).
    """
    names: list[str] = []
    seen: set[str] = set()
    if not isinstance(persons, list):
        return names
    for person in persons:
        name = person.get("name") if isinstance(person, dict) else person
        if not isinstance(name, str) or not (name := name.strip()):
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _coerce_seconds(value: Any) -> float | None:
    """Coerce a chapter time value to float seconds, or None if not parseable."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except TypeError, ValueError:
        return None
    return seconds if isfinite(seconds) else None


def parse_chapters_from_json(data: dict[str, Any]) -> list[MediaItemChapter]:
    """
    Parse a Podcasting 2.0 ``podcast:chapters`` JSON document into chapters.

    Spec: https://github.com/Podcastindex-org/podcast-namespace/blob/main/docs/1.0.md#chapters
    Entries without a usable ``startTime``/``title`` are skipped, as are entries
    explicitly hidden from the table of contents (``toc: false``).
    """
    chapters: list[MediaItemChapter] = []
    raw_chapters = data.get("chapters")
    if not isinstance(raw_chapters, list):
        return chapters
    for raw_chapter in raw_chapters:
        if not isinstance(raw_chapter, dict):
            continue
        if raw_chapter.get("toc") is False:
            continue
        title = raw_chapter.get("title")
        start = _coerce_seconds(raw_chapter.get("startTime"))
        if not isinstance(title, str) or not title or start is None:
            continue
        chapters.append(
            MediaItemChapter(
                position=len(chapters) + 1,
                name=title,
                start=start,
                end=_coerce_seconds(raw_chapter.get("endTime")),
            )
        )
    return chapters


async def enrich_episode_chapters(
    *,
    session: aiohttp.ClientSession,
    chapters_json_url: str | None,
    mass_episode: PodcastEpisode,
) -> None:
    """
    Attach ``podcast:chapters`` (external JSON) to an episode lacking inline chapters.

    Transport-agnostic: the caller supplies the chapters JSON URL directly (e.g.
    podcastparser's ``chapters_json_url`` or the Podcast Index API ``chaptersUrl``),
    so any podcast provider can reuse it. Chapter data is supplementary: any
    fetch/parse failure is logged and ignored so it can never break episode
    resolution or playback. Intended for the single-episode path only, to avoid a
    network request per episode when listing a whole podcast.

    :param session: The aiohttp session used for the best-effort fetch.
    :param chapters_json_url: The Podcasting 2.0 chapters JSON URL, or None to skip.
    :param mass_episode: The episode to enrich; untouched if it already has chapters.
    """
    if mass_episode.metadata.chapters:
        return
    url = chapters_json_url
    if not url:
        return
    # chapter data is supplementary, so unlike get_podcastparser_dict this is a single
    # attempt with no UA-less retry.
    # TimeoutError (raised on total-timeout) is not a ClientError, so catch it explicitly
    # to keep this enrichment best-effort. The parse stays inside the try so a malformed
    # document can never break episode resolution either.
    try:
        async with session.get(
            url,
            headers=_BROWSER_UA_HEADERS,
            raise_for_status=True,
            timeout=_CHAPTERS_FETCH_TIMEOUT,
        ) as response:
            data = await response.json(content_type=None)
        if isinstance(data, dict) and (chapters := parse_chapters_from_json(data)):
            mass_episode.metadata.chapters = chapters
    except (ClientError, TimeoutError, ValueError, TypeError) as err:
        LOGGER.warning("Failed to fetch podcast chapters from %s: %s", url, err)


async def get_episode_transcript(
    *,
    mass: MusicAssistant,
    provider_instance_id: str,
    transcripts: list[dict[str, Any]] | None,
) -> tuple[str | None, list[MediaItemTranscriptCue] | None]:
    """
    Return an episode's transcript as (readable text, timed cues).

    Never raises: a transcript is supplementary, so a failure yields (None, None). The
    document is fetched once and cached, so repeat views cost nothing.

    :param mass: The MusicAssistant instance holding the cache.
    :param provider_instance_id: Provider instance the cache entry belongs to.
    :param transcripts: The available transcripts as dicts with a url and optional type,
        or None. The one carrying timings is preferred.
    """
    if not (url := _select_transcript_url(transcripts)):
        return None, None
    raw = await mass.cache.get(
        key=url,
        provider=provider_instance_id,
        category=CACHE_CATEGORY_PODCAST_TRANSCRIPT,
        default=None,
    )
    if raw is None:
        if (raw := await _fetch_transcript(session=mass.http_session, url=url)) is None:
            return None, None
        await mass.cache.set(
            key=url,
            provider=provider_instance_id,
            category=CACHE_CATEGORY_PODCAST_TRANSCRIPT,
            data=raw,
            expiration=PODCAST_TRANSCRIPT_CACHE_EXPIRATION,
        )
    if cues := parse_transcript_cues(raw):
        return cues_to_text(cues), cues
    return document_to_text(raw) or None, None


def _select_transcript_url(transcripts: list[dict[str, Any]] | None) -> str | None:
    """Return the url of the most useful transcript, preferring the formats with timings."""

    def preference(transcript: dict[str, Any]) -> int:
        media_type = str(transcript.get("type") or "").lower()
        if media_type in _TIMED_TRANSCRIPT_TYPES:
            return _TIMED_TRANSCRIPT_TYPES.index(media_type)
        return len(_TIMED_TRANSCRIPT_TYPES)

    if not (candidates := [entry for entry in transcripts or [] if entry.get("url")]):
        return None
    return str(min(candidates, key=preference)["url"])


async def _fetch_transcript(*, session: aiohttp.ClientSession, url: str) -> str | None:
    """Fetch a transcript document, returning None when it cannot be retrieved."""
    # TimeoutError (raised on total-timeout) is not a ClientError, so catch it explicitly.
    try:
        async with session.get(
            url,
            headers=_BROWSER_UA_HEADERS,
            raise_for_status=True,
            timeout=_TRANSCRIPT_FETCH_TIMEOUT,
        ) as response:
            # hosts commonly serve these without a charset (or as octet-stream), so decode
            # explicitly rather than letting aiohttp guess at the encoding
            return (await response.read()).decode("utf-8", errors="replace")
    except (ClientError, TimeoutError) as err:
        LOGGER.warning("Failed to fetch podcast transcript from %s: %s", url, err)
        return None
