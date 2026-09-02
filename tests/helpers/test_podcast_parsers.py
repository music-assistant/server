"""Tests for the podcastfeed -> Mass parsing helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from aiohttp.client import ClientError
from music_assistant_models.enums import LinkType

from music_assistant.helpers.podcast_parsers import (
    enrich_episode_chapters,
    find_episode_stream_url,
    get_cached_podcast,
    get_episode_positions,
    get_podcastparser_dict,
    get_stream_url_from_episode,
    parse_chapters_from_json,
    parse_podcast_episode,
    parse_podcast_persons,
    rank_episodes_by_date,
    refresh_cached_podcast,
)

if TYPE_CHECKING:
    import aiohttp
    from music_assistant_models.media_items import PodcastEpisode

    from music_assistant.mass import MusicAssistant


def _episode(**overrides: Any) -> dict[str, Any]:
    """Return a minimal podcastparser episode dict with a valid enclosure."""
    episode: dict[str, Any] = {
        "title": "Episode 1",
        "enclosures": [{"url": "https://example.com/ep1.mp3"}],
    }
    episode.update(overrides)
    return episode


def _parse(episode: dict[str, Any]) -> PodcastEpisode | None:
    """Parse an episode dict with the required boilerplate args."""
    return parse_podcast_episode(
        episode=episode,
        prov_podcast_id="podcast-1",
        position=1,
        instance_id="podcastfeed--test",
        domain="podcastfeed",
    )


class _FakeResponse:
    """Minimal stand-in for an aiohttp response yielding a fixed JSON payload."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def json(self, **kwargs: Any) -> Any:
        return self._payload


class _FakeGetContext:
    """
    Stand-in for aiohttp's request context manager.

    Mirrors aiohttp where the error (e.g. raise_for_status / timeout) surfaces on
    entering the context, so enrichment must guard the ``async with`` itself.
    """

    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeResponse:
        if self._session._error is not None:
            raise self._session._error
        return _FakeResponse(self._session._payload)

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    """Minimal stand-in for an aiohttp ClientSession used by chapter enrichment."""

    def __init__(self, *, payload: Any = None, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error
        self.calls = 0
        self.last_url: str | None = None

    def get(self, url: str, **kwargs: Any) -> _FakeGetContext:
        self.calls += 1
        self.last_url = url
        return _FakeGetContext(self)


# --- enclosure / stream url selection ---------------------------------------------------------


def test_stream_url_prefers_audio_over_leading_image() -> None:
    """A leading image media:content enclosure is skipped in favor of the audio one (#5920)."""
    audio_url = "https://rss.wbur.org/the-midnight-rebellion/ep1.mp3"
    cover_url = "https://rss.wbur.org/the-midnight-rebellion/cover.jpg"
    episode = _episode(
        enclosures=[
            {"url": cover_url, "mime_type": "image/jpeg"},
            {"url": audio_url, "mime_type": "audio/mpeg"},
            {"url": audio_url, "mime_type": "audio/mpeg"},
        ]
    )
    assert get_stream_url_from_episode(episode=episode) == audio_url


def test_stream_url_accepts_bogus_mime_type() -> None:
    """A real audio enclosure with a bogus declared mime type is still used (#5692)."""
    episode = _episode(
        enclosures=[{"url": "https://example.com/ep1.mp3", "mime_type": "application/octet-stream"}]
    )
    assert get_stream_url_from_episode(episode=episode) == "https://example.com/ep1.mp3"


def test_stream_url_accepts_video_enclosure() -> None:
    """A video/mp4 enclosure is accepted as a playable stream."""
    video_url = "https://example.com/ep1.mp4"
    episode = _episode(enclosures=[{"url": video_url, "mime_type": "video/mp4"}])
    assert get_stream_url_from_episode(episode=episode) == video_url


def test_stream_url_skips_enclosures_without_url() -> None:
    """An enclosure missing a url is skipped in favor of a later usable one."""
    episode = _episode(
        enclosures=[
            {"mime_type": "audio/mpeg"},
            {"url": "https://example.com/ep1.mp3", "mime_type": "audio/mpeg"},
        ]
    )
    assert get_stream_url_from_episode(episode=episode) == "https://example.com/ep1.mp3"


def test_stream_url_image_only_returns_none() -> None:
    """An episode whose only enclosure is an image has no playable stream."""
    episode = _episode(
        enclosures=[{"url": "https://example.com/cover.jpg", "mime_type": "image/jpeg"}]
    )
    assert get_stream_url_from_episode(episode=episode) is None
    assert _parse(episode) is None


def test_stream_url_first_audio_enclosure_wins() -> None:
    """When several audio enclosures are declared, the first one wins."""
    episode = _episode(
        enclosures=[
            {"url": "https://example.com/ep1-first.mp3", "mime_type": "audio/mpeg"},
            {"url": "https://example.com/ep1-second.mp3", "mime_type": "audio/mpeg"},
        ]
    )
    assert get_stream_url_from_episode(episode=episode) == "https://example.com/ep1-first.mp3"


def test_stream_url_missing_mime_type_falls_back_to_url() -> None:
    """The default `_episode()` enclosure has no mime_type key and still resolves (fallback)."""
    assert get_stream_url_from_episode(episode=_episode()) == "https://example.com/ep1.mp3"


# --- description -----------------------------------------------------------------------------


def test_description_is_populated() -> None:
    """A non-empty episode description ends up on the episode metadata."""
    mass_episode = _parse(_episode(description="All about parsing podcasts."))
    assert mass_episode is not None
    assert mass_episode.metadata.description == "All about parsing podcasts."


def test_empty_description_left_unset() -> None:
    """An empty description (podcastparser's default) leaves metadata.description as None."""
    mass_episode = _parse(_episode(description=""))
    assert mass_episode is not None
    assert mass_episode.metadata.description is None


def test_missing_description_left_unset() -> None:
    """An absent description key leaves metadata.description as None."""
    mass_episode = _parse(_episode())
    assert mass_episode is not None
    assert mass_episode.metadata.description is None


# --- parent podcast reference ----------------------------------------------------------------


def test_podcast_reference_uses_podcast_name() -> None:
    """The parent-podcast reference is named after the podcast, not the episode."""
    mass_episode = parse_podcast_episode(
        episode=_episode(title="Episode 1"),
        prov_podcast_id="podcast-1",
        position=1,
        podcast_name="My Show",
        instance_id="podcastfeed--test",
        domain="podcastfeed",
    )
    assert mass_episode is not None
    assert mass_episode.podcast.name == "My Show"
    assert mass_episode.name == "Episode 1"


def test_podcast_reference_falls_back_to_episode_title() -> None:
    """Without a podcast name, the reference falls back to the episode title."""
    mass_episode = _parse(_episode(title="Some Episode"))
    assert mass_episode is not None
    assert mass_episode.podcast.name == "Some Episode"


# --- episode position -----------------------------------------------------------------------


def test_episode_position_uses_caller_supplied_position() -> None:
    """The episode position is determined by the caller."""
    mass_episode = parse_podcast_episode(
        episode=_episode(),
        prov_podcast_id="podcast-1",
        position=5,
        instance_id="podcastfeed--test",
        domain="podcastfeed",
    )
    assert mass_episode is not None
    assert mass_episode.position == 5


def test_positions_use_episode_numbers_when_all_numbered() -> None:
    """A fully numbered feed keeps its own itunes:episode numbers."""
    episodes = [_episode(number=3), _episode(number=1), _episode(number=2)]
    assert get_episode_positions(episodes) == [3, 1, 2]


def test_positions_ignore_partial_numbering() -> None:
    """A feed that numbers only some episodes has all of its numbers ignored."""
    episodes = [
        _episode(number=1, published=300),
        _episode(published=100),
        _episode(number=3, published=200),
    ]
    assert get_episode_positions(episodes) == [3, 1, 2]


def test_positions_rank_newest_highest_by_published() -> None:
    """Dated feeds are ranked oldest to newest regardless of the order they are listed in."""
    episodes = [_episode(published=300), _episode(published=100), _episode(published=200)]
    assert get_episode_positions(episodes) == [3, 1, 2]


def test_positions_rank_serial_feeds_oldest_first() -> None:
    """An oldest-first (itunes:type=serial) feed still gives the newest episode the top spot."""
    episodes = [_episode(published=100), _episode(published=200), _episode(published=300)]
    assert get_episode_positions(episodes) == [1, 2, 3]


def test_positions_ignore_numbering_that_restarts_each_season() -> None:
    """Seasoned feeds restart their numbering, so the dates decide instead."""
    episodes = [
        _episode(number=1, season=2, published=300),
        _episode(number=2, season=1, published=100),
        _episode(number=1, season=1, published=200),
    ]
    assert get_episode_positions(episodes) == [3, 1, 2]


def test_positions_use_numbers_for_a_single_season() -> None:
    """A feed that declares one season keeps its own episode numbers."""
    episodes = [_episode(number=2, season=1), _episode(number=1, season=1)]
    assert get_episode_positions(episodes) == [2, 1]


def test_positions_prefer_numbers_over_dates() -> None:
    """Episode numbers win over the publication dates when the feed carries both."""
    episodes = [_episode(number=7, published=100), _episode(number=9, published=200)]
    assert get_episode_positions(episodes) == [7, 9]


def test_rank_by_date_keeps_feed_order_among_undated() -> None:
    """Undated episodes rank oldest and hold their listing order between themselves."""
    assert rank_episodes_by_date([None, 300, None, 100]) == [1, 4, 2, 3]


def test_rank_by_date_accepts_string_dates() -> None:
    """Providers reporting dates as sortable strings are ranked the same way."""
    assert rank_episodes_by_date(["2026-03-01", "2024-01-01", "2025-02-01"]) == [3, 1, 2]


def test_positions_rank_undated_episodes_as_oldest() -> None:
    """Episodes missing a publication date rank below every dated one."""
    episodes = [_episode(published=200), _episode(), _episode(published=100)]
    assert get_episode_positions(episodes) == [3, 1, 2]


def test_positions_assume_newest_first_without_dates() -> None:
    """Without publication dates the feed order is assumed to be newest-first."""
    episodes = [_episode(), _episode(), _episode()]
    assert get_episode_positions(episodes) == [3, 2, 1]


def test_positions_empty_feed() -> None:
    """An empty feed yields no positions."""
    assert get_episode_positions([]) == []


# --- inline (Podlove Simple Chapters) --------------------------------------------------------


def test_inline_chapters_populated() -> None:
    """Inline chapters are attached, including a chapter that starts at 0."""
    episode = _episode(
        chapters=[
            {"title": "Intro", "start": 0},
            {"title": "Topic", "start": 90.0},
        ]
    )
    mass_episode = _parse(episode)
    assert mass_episode is not None
    chapters = mass_episode.metadata.chapters
    assert chapters is not None
    assert [c.name for c in chapters] == ["Intro", "Topic"]
    # the opening chapter (start == 0) must survive; a truthiness check would drop it
    assert chapters[0].start == 0


def test_inline_chapters_absent_leaves_none() -> None:
    """No inline chapters key leaves metadata.chapters as None."""
    mass_episode = _parse(_episode())
    assert mass_episode is not None
    assert mass_episode.metadata.chapters is None


# --- podcast:chapters JSON parsing -----------------------------------------------------------


def test_parse_chapters_from_json_maps_fields() -> None:
    """Valid chapters JSON maps startTime/endTime/title onto MediaItemChapters."""
    data = {
        "version": "1.2.0",
        "chapters": [
            {"startTime": 0, "title": "Intro"},
            {"startTime": 90, "endTime": 120, "title": "Sponsor"},
        ],
    }
    chapters = parse_chapters_from_json(data)
    assert [(c.position, c.name, c.start, c.end) for c in chapters] == [
        (1, "Intro", 0.0, None),
        (2, "Sponsor", 90.0, 120.0),
    ]


def test_parse_chapters_from_json_skips_invalid_and_hidden() -> None:
    """Entries missing startTime/title, hidden (toc: false), or non-dict are skipped."""
    data = {
        "chapters": [
            {"title": "no start"},
            {"startTime": 10},
            {"startTime": 20, "title": "hidden", "toc": False},
            "not-a-dict",
            {"startTime": 30, "title": "kept"},
        ]
    }
    chapters = parse_chapters_from_json(data)
    assert [c.name for c in chapters] == ["kept"]
    assert chapters[0].position == 1


def test_parse_chapters_from_json_malformed_returns_empty() -> None:
    """A missing or non-list chapters key yields no chapters."""
    assert parse_chapters_from_json({}) == []
    assert parse_chapters_from_json({"chapters": "nope"}) == []


def test_parse_chapters_from_json_skips_malformed_values() -> None:
    """Non-numeric/non-finite startTimes and non-string titles are dropped, not stored."""
    data = {
        "chapters": [
            {"startTime": "01:30", "title": "non-numeric start"},
            {"startTime": float("nan"), "title": "nan start"},
            {"startTime": float("inf"), "title": "inf start"},
            {"startTime": 10, "title": {"x": 1}},
            {"startTime": 20, "title": 123},
            {"startTime": 30, "title": "kept", "endTime": float("nan")},
        ]
    }
    chapters = parse_chapters_from_json(data)
    # only the well-formed entry survives, and its non-finite endTime collapses to None
    assert [(c.name, c.start, c.end) for c in chapters] == [("kept", 30.0, None)]


# --- chapter enrichment (external JSON fetch) ------------------------------------------------


async def test_enrich_fetches_json_chapters() -> None:
    """Chapters are fetched from chapters_json_url when the episode has none inline."""
    mass_episode = _parse(_episode())
    assert mass_episode is not None
    session = _FakeSession(payload={"chapters": [{"startTime": 0, "title": "Intro"}]})
    await enrich_episode_chapters(
        session=cast("aiohttp.ClientSession", session),
        chapters_json_url="https://example.com/ch.json",
        mass_episode=mass_episode,
    )
    assert mass_episode.metadata.chapters is not None
    assert [c.name for c in mass_episode.metadata.chapters] == ["Intro"]


async def test_enrich_swallows_fetch_error() -> None:
    """A failed chapter fetch is ignored and leaves the episode without chapters."""
    mass_episode = _parse(_episode())
    assert mass_episode is not None
    session = _FakeSession(error=ClientError("boom"))
    await enrich_episode_chapters(
        session=cast("aiohttp.ClientSession", session),
        chapters_json_url="https://example.com/ch.json",
        mass_episode=mass_episode,
    )
    assert mass_episode.metadata.chapters is None


async def test_enrich_swallows_timeout() -> None:
    """A timed-out chapter fetch is ignored: TimeoutError is not a ClientError."""
    mass_episode = _parse(_episode())
    assert mass_episode is not None
    session = _FakeSession(error=TimeoutError())
    await enrich_episode_chapters(
        session=cast("aiohttp.ClientSession", session),
        chapters_json_url="https://example.com/ch.json",
        mass_episode=mass_episode,
    )
    assert mass_episode.metadata.chapters is None


async def test_enrich_skips_when_inline_chapters_present() -> None:
    """Inline chapters win: no JSON fetch is performed when chapters already exist."""
    mass_episode = _parse(_episode(chapters=[{"title": "Inline", "start": 0}]))
    assert mass_episode is not None
    session = _FakeSession(payload={"chapters": [{"startTime": 5, "title": "FromJson"}]})
    await enrich_episode_chapters(
        session=cast("aiohttp.ClientSession", session),
        chapters_json_url="https://example.com/ch.json",
        mass_episode=mass_episode,
    )
    assert session.calls == 0
    assert mass_episode.metadata.chapters is not None
    assert [c.name for c in mass_episode.metadata.chapters] == ["Inline"]


async def test_enrich_skips_when_no_url() -> None:
    """A falsy chapters URL is a no-op: no fetch, no chapters."""
    mass_episode = _parse(_episode())
    assert mass_episode is not None
    session = _FakeSession(payload={"chapters": [{"startTime": 0, "title": "Intro"}]})
    await enrich_episode_chapters(
        session=cast("aiohttp.ClientSession", session),
        chapters_json_url=None,
        mass_episode=mass_episode,
    )
    assert session.calls == 0
    assert mass_episode.metadata.chapters is None


async def test_enrich_ignores_non_dict_payload() -> None:
    """A JSON payload that is not an object (e.g. a list) leaves the episode without chapters."""
    mass_episode = _parse(_episode())
    assert mass_episode is not None
    session = _FakeSession(payload=[{"startTime": 0, "title": "Intro"}])
    await enrich_episode_chapters(
        session=cast("aiohttp.ClientSession", session),
        chapters_json_url="https://example.com/ch.json",
        mass_episode=mass_episode,
    )
    assert session.calls == 1
    assert mass_episode.metadata.chapters is None


async def test_enrich_ignores_empty_chapters_payload() -> None:
    """A document whose chapters all filter out leaves metadata.chapters as None."""
    mass_episode = _parse(_episode())
    assert mass_episode is not None
    session = _FakeSession(payload={"chapters": []})
    await enrich_episode_chapters(
        session=cast("aiohttp.ClientSession", session),
        chapters_json_url="https://example.com/ch.json",
        mass_episode=mass_episode,
    )
    assert session.calls == 1
    assert mass_episode.metadata.chapters is None


# --- episode webpage, explicit flag, performers ----------------------------------------------


def test_episode_link_maps_to_website_link() -> None:
    """An item <link> becomes a WEBSITE link on the episode metadata."""
    mass_episode = _parse(_episode(link="https://example.com/ep1"))
    assert mass_episode is not None
    assert mass_episode.metadata.links is not None
    link = next(iter(mass_episode.metadata.links))
    assert (link.type, link.url) == (LinkType.WEBSITE, "https://example.com/ep1")


def test_episode_missing_link_leaves_links_unset() -> None:
    """Without an item <link>, metadata.links stays None."""
    mass_episode = _parse(_episode())
    assert mass_episode is not None
    assert mass_episode.metadata.links is None


def test_episode_explicit_only_set_when_declared() -> None:
    """The explicit flag is set from the feed value, and left untouched when absent."""
    explicit_ep = _parse(_episode(explicit=True))
    assert explicit_ep is not None
    assert explicit_ep.metadata.explicit is True
    not_explicit = _parse(_episode(explicit=False))
    assert not_explicit is not None
    assert not_explicit.metadata.explicit is False
    # absent key must not coerce to False over the model default
    unknown = _parse(_episode())
    assert unknown is not None
    assert unknown.metadata.explicit is None


def test_episode_performers_from_persons() -> None:
    """podcast:person entries land on metadata.performers as names."""
    mass_episode = _parse(
        _episode(persons=[{"name": "Jane Host", "role": "host"}, {"name": "Joe Guest"}])
    )
    assert mass_episode is not None
    assert mass_episode.metadata.performers == {"Jane Host", "Joe Guest"}


# --- parse_podcast_persons -------------------------------------------------------------------


def test_parse_podcast_persons_dedupes_and_trims() -> None:
    """Names are trimmed, de-duplicated case-insensitively, and order-preserved."""
    persons = [{"name": "  Alice "}, {"name": "alice"}, {"name": "Bob"}]
    assert parse_podcast_persons(persons) == ["Alice", "Bob"]


def test_parse_podcast_persons_skips_blank_and_malformed() -> None:
    """Blank names, missing names, and non-dict/non-str entries are skipped."""
    persons = [{"name": ""}, {"role": "host"}, {"name": 123}, "Carol", 7]
    assert parse_podcast_persons(persons) == ["Carol"]


def test_parse_podcast_persons_non_list_returns_empty() -> None:
    """Any non-list input yields no names, so callers need not guard."""
    assert parse_podcast_persons(None) == []
    assert parse_podcast_persons("nope") == []


# --- find_episode_stream_url -----------------------------------------------------------------


def test_find_episode_stream_url_matches_guid() -> None:
    """An episode with a usable guid is found by that guid."""
    feed = {"episodes": [_episode(guid="ep-1"), _episode(guid="ep-2", enclosures=[{"url": "b"}])]}
    assert find_episode_stream_url(parsed_feed=feed, guid_or_stream_url="ep-2") == "b"


def test_find_episode_stream_url_falls_back_to_stream_url() -> None:
    """A guid containing a space is unusable as an id, so the stream url identifies it."""
    feed = {"episodes": [_episode(guid="not a guid")]}
    assert (
        find_episode_stream_url(parsed_feed=feed, guid_or_stream_url="https://example.com/ep1.mp3")
        == "https://example.com/ep1.mp3"
    )
    # the unusable guid must not match
    assert find_episode_stream_url(parsed_feed=feed, guid_or_stream_url="not a guid") is None


def test_find_episode_stream_url_skips_episodes_without_enclosure() -> None:
    """An episode without a playable enclosure does not stop the search."""
    feed = {"episodes": [{"title": "no audio"}, _episode(guid="ep-2", enclosures=[{"url": "b"}])]}
    assert find_episode_stream_url(parsed_feed=feed, guid_or_stream_url="ep-2") == "b"


def test_find_episode_stream_url_unknown_returns_none() -> None:
    """An unknown identifier yields None rather than raising."""
    assert find_episode_stream_url(parsed_feed={"episodes": []}, guid_or_stream_url="x") is None


# --- feed retrieval and caching ----------------------------------------------------------------


FEED_URL = "https://example.com/feed.xml"
FEED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Feed One</title>
<item>
<title>Episode 1</title>
<guid>ep-1</guid>
<enclosure url="https://example.com/ep1.mp3" type="audio/mpeg" length="1"/>
</item>
</channel></rss>
"""


class _FakeFeedResponse:
    """Minimal stand-in for an aiohttp response yielding raw feed bytes."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self) -> bytes:
        return self._body


class _FakeFeedGetContext:
    """Request context manager recording whether the response was released again."""

    def __init__(self, session: _FakeFeedSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeFeedResponse:
        error = self._session.errors.pop(0) if self._session.errors else None
        if error is not None:
            raise error
        return _FakeFeedResponse(self._session.body)

    async def __aexit__(self, *exc_info: object) -> bool:
        self._session.released += 1
        return False


class _FakeFeedSession:
    """Session stand-in serving a fixed feed body, optionally failing the first attempts."""

    def __init__(self, *, body: bytes = FEED_XML, errors: list[Exception] | None = None) -> None:
        self.body = body
        self.errors = errors or []
        self.calls = 0
        self.released = 0
        self.headers: list[dict[str, str]] = []

    def get(self, url: str, headers: dict[str, str], **kwargs: Any) -> _FakeFeedGetContext:
        self.calls += 1
        self.headers.append(headers)
        return _FakeFeedGetContext(self)


class _FakeCache:
    """In-memory stand-in for the cache controller, keyed like the real one."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str, int], Any] = {}
        self.sets = 0

    async def get(self, key: str, provider: str, category: int, default: Any = None) -> Any:
        return self.store.get((key, provider, category), default)

    async def set(self, key: str, provider: str, category: int, data: Any, expiration: int) -> None:
        self.sets += 1
        self.store[(key, provider, category)] = data


class _FakeMass:
    """Stand-in exposing only what the podcast cache helpers use."""

    def __init__(self, session: _FakeFeedSession) -> None:
        self.http_session = session
        self.cache = _FakeCache()


def _fake_mass(session: _FakeFeedSession) -> MusicAssistant:
    return cast("MusicAssistant", _FakeMass(session))


async def test_get_podcastparser_dict_releases_the_response() -> None:
    """The feed response is released again, on the retry path as well."""
    session = _FakeFeedSession(errors=[ClientError("no user agent allowed")])
    parsed_feed = await get_podcastparser_dict(
        session=cast("aiohttp.ClientSession", session), feed_url=FEED_URL
    )
    assert parsed_feed["title"] == "Feed One"
    # the first attempt failed on entering the context, so only the second one is released
    assert session.calls == 2
    assert session.released == 1
    assert session.headers[0] == {"User-Agent": "Mozilla/5.0"}


async def test_get_cached_podcast_stores_and_reuses_the_feed() -> None:
    """A miss retrieves and caches the feed, a subsequent call is served from the cache."""
    session = _FakeFeedSession()
    mass = _fake_mass(session)
    parsed_feed = await get_cached_podcast(
        mass=mass, provider_instance_id="podcastfeed--test", feed_url=FEED_URL
    )
    assert parsed_feed["title"] == "Feed One"
    assert session.calls == 1
    await get_cached_podcast(mass=mass, provider_instance_id="podcastfeed--test", feed_url=FEED_URL)
    assert session.calls == 1


async def test_refresh_cached_podcast_always_updates_the_cache() -> None:
    """A sync must refresh the cached feed, also when a valid entry exists."""
    session = _FakeFeedSession()
    mass = _fake_mass(session)
    await get_cached_podcast(mass=mass, provider_instance_id="podcastfeed--test", feed_url=FEED_URL)
    assert session.calls == 1
    session.body = FEED_XML.replace(b"Feed One", b"Feed Renamed")
    parsed_feed = await refresh_cached_podcast(
        mass=mass, provider_instance_id="podcastfeed--test", feed_url=FEED_URL
    )
    assert session.calls == 2
    assert parsed_feed["title"] == "Feed Renamed"
    # the refreshed feed is what subsequent (cached) reads see
    cached_feed = await get_cached_podcast(
        mass=mass, provider_instance_id="podcastfeed--test", feed_url=FEED_URL
    )
    assert cached_feed["title"] == "Feed Renamed"
    assert session.calls == 2


def test_find_episode_stream_url_matches_empty_guid() -> None:
    """An empty guid is used as episode id by the parser, so it must resolve as one."""
    feed = {"episodes": [_episode(guid=""), _episode(guid="ep-2", enclosures=[{"url": "b"}])]}
    assert find_episode_stream_url(parsed_feed=feed, guid_or_stream_url="") == (
        "https://example.com/ep1.mp3"
    )
