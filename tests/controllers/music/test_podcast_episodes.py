"""Tests for the paged podcast episodes listing and the episode iterator."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import MediaType, ProviderType, SortField
from music_assistant_models.errors import InvalidDataError, ResourceTemporarilyUnavailable
from music_assistant_models.media_items import Podcast, PodcastEpisode, ProviderMapping

from music_assistant.constants import DB_TABLE_MEDIA_PROGRESS
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType

    from music_assistant.mass import MusicAssistant

INSTANCE = "rss_1"
PODCAST_ID = "show-1"
USER_A = User(user_id="user-a", username="user-a", role=UserRole.USER)
USER_B = User(user_id="user-b", username="user-b", role=UserRole.USER)


class _StubPodcastProvider(MusicProvider):
    """Music provider listing the episodes of one podcast, counting how often it is asked."""

    def __init__(self, episodes: list[PodcastEpisode]) -> None:
        """
        Initialize the stub provider.

        :param episodes: The episodes of the podcast, as the provider lists them.
        """
        self.episodes = episodes
        self.failing_after: int | None = None
        self.listings = 0
        self.played: list[tuple[str, bool]] = []
        self.config = MagicMock()
        self.config.instance_id = INSTANCE
        self.manifest = MagicMock()
        self.manifest.domain = "rss"
        self.manifest.type = ProviderType.MUSIC
        self.logger = MagicMock()
        self.available = True

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Yield new copies of the episodes, failing part way when the test says so."""
        self.listings += 1
        for index, episode in enumerate(self.episodes):
            if index == self.failing_after:
                raise ResourceTemporarilyUnavailable("rate limited")
            yield deepcopy(episode)

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Take a play report in as progress of its own, as Audiobookshelf does."""
        self.played.append((prov_item_id, fully_played))
        for episode in self.episodes:
            if episode.item_id == prov_item_id:
                episode.fully_played = fully_played
                episode.resume_position_ms = position * 1000


@pytest.fixture(name="mass")
def mass_fixture(music_mass_with_cache: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance with a cache store: episode listings are cached."""
    return music_mass_with_cache


def _podcast() -> Podcast:
    return Podcast(
        item_id=PODCAST_ID,
        provider=INSTANCE,
        name="My Podcast",
        provider_mappings={
            ProviderMapping(item_id=PODCAST_ID, provider_domain="rss", provider_instance=INSTANCE)
        },
    )


def _episodes(count: int) -> list[PodcastEpisode]:
    """Return the episodes of a podcast, the newest (highest position) listed first."""
    episodes = []
    for position in range(count, 0, -1):
        item_id = f"ep{position}"
        episodes.append(
            PodcastEpisode(
                item_id=item_id,
                provider=INSTANCE,
                name=f"Episode {(position * 7) % count:02d}",
                duration=1000 + (position * 3) % count,
                provider_mappings={
                    ProviderMapping(
                        item_id=item_id, provider_domain="rss", provider_instance=INSTANCE
                    )
                },
                position=position,
                podcast=_podcast(),
            )
        )
    return episodes


def _with_provider(mass: MusicAssistant, provider: _StubPodcastProvider) -> Any:
    """Load the given provider as the only one, under its instance id and its domain."""

    def _get_provider(provider_id: str, *_args: Any, **_kwargs: Any) -> Any:
        return provider if provider_id in (provider.instance_id, provider.domain) else None

    return patch.object(mass, "get_provider", side_effect=_get_provider)


@contextmanager
def _held_play_reports(mass: MusicAssistant) -> Iterator[Callable[[], Awaitable[None]]]:
    """Hold the play reports to the providers until the function this yields delivers them."""
    reports: list[Coroutine[Any, Any, None]] = []

    async def _deliver() -> None:
        while reports:
            await reports.pop(0)

    with patch.object(
        mass, "create_task", side_effect=lambda coro, *_a, **_k: reports.append(coro)
    ):
        yield _deliver


def _as_user(user: User | None) -> Any:
    return patch(
        "music_assistant.controllers.music.media.podcasts.get_current_user", return_value=user
    )


async def _add_progress(
    mass: MusicAssistant, item_id: str, userid: str, seconds_played: int, fully_played: bool
) -> None:
    """Store the playlog row a user's listening leaves for an episode."""
    await mass.music.database.insert(
        DB_TABLE_MEDIA_PROGRESS,
        {
            "item_id": item_id,
            "provider": INSTANCE,
            "media_type": MediaType.PODCAST_EPISODE.value,
            "name": item_id,
            "userid": userid,
            "seconds_played": seconds_played,
            "fully_played": fully_played,
            "timestamp": 1000,
        },
        allow_replace=True,
    )


def _ids(episodes: list[PodcastEpisode]) -> list[str]:
    return [episode.item_id for episode in episodes]


async def test_podcast_episodes_follow_the_listing_contract(mass: MusicAssistant) -> None:
    """The episodes are searched, filtered, sorted and paged on the server, from one walk."""
    provider = _StubPodcastProvider(_episodes(6))
    db_podcast = await mass.music.podcasts.add_item_to_library(_podcast())
    await _add_progress(mass, "ep2", USER_A.user_id, seconds_played=60, fully_played=True)
    await _add_progress(mass, "ep3", USER_A.user_id, seconds_played=30, fully_played=False)

    async def _episodes_of(**kwargs: Any) -> list[str]:
        with _as_user(USER_A):
            episodes = await mass.music.podcasts.episodes(db_podcast.item_id, "library", **kwargs)
        return _ids(episodes)

    with _with_provider(mass, provider):
        # the newest first
        assert await _episodes_of() == ["ep6", "ep5", "ep4", "ep3", "ep2", "ep1"]
        assert await _episodes_of(sort_field=SortField.NAME) == [
            "ep6",
            "ep1",
            "ep2",
            "ep3",
            "ep4",
            "ep5",
        ]
        assert await _episodes_of(sort_field=SortField.DURATION, limit=2, offset=1) == [
            "ep4",
            "ep2",
        ]
        assert await _episodes_of(search="episode 01") == ["ep1"]
        assert await _episodes_of(fully_played=True) == ["ep2"]
        # a part-played episode is not played to the end
        assert await _episodes_of(fully_played=False) == ["ep6", "ep5", "ep4", "ep3", "ep1"]
        assert await _episodes_of(limit=0) == ["ep6", "ep5", "ep4", "ep3", "ep2", "ep1"]
        assert await _episodes_of(exclude_item_id="ep5", limit=2, offset=1) == [
            "ep4",
            "ep3",
        ]
        with pytest.raises(InvalidDataError):
            await _episodes_of(sort_field=SortField.YEAR)
    assert provider.listings == 1


async def test_podcast_episodes_serve_a_first_page_of_500(mass: MusicAssistant) -> None:
    """Without a limit a caller gets the 500 newest episodes; 0 returns them all."""
    provider = _StubPodcastProvider(_episodes(600))
    with _with_provider(mass, provider):
        first_page = await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE)
        everything = await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE, limit=0)
    assert _ids(first_page) == [f"ep{position}" for position in range(600, 100, -1)]
    assert len(everything) == 600
    assert provider.listings == 1


async def test_an_episode_played_within_the_cache_window_shows_as_played(
    mass: MusicAssistant,
) -> None:
    """The progress a user makes after the listing was cached shows on the next request."""
    provider = _StubPodcastProvider(_episodes(3))
    with _with_provider(mass, provider), _as_user(USER_A):
        before = await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE)
        await _add_progress(mass, "ep2", USER_A.user_id, seconds_played=90, fully_played=True)
        after = await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE)
        unplayed = await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE, fully_played=False)
    assert [episode.fully_played for episode in before] == [None, None, None]
    assert [episode.fully_played for episode in after] == [None, True, None]
    assert after[1].resume_position_ms == 90000
    assert _ids(unplayed) == ["ep3", "ep1"]
    assert provider.listings == 1


async def test_progress_on_a_cached_listing_is_the_calling_users(mass: MusicAssistant) -> None:
    """A listing one user filled carries the progress of whoever asks for it."""
    provider = _StubPodcastProvider(_episodes(2))
    await _add_progress(mass, "ep1", USER_A.user_id, seconds_played=45, fully_played=False)
    with _with_provider(mass, provider):
        with _as_user(USER_A):
            for_a = await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE)
        with _as_user(USER_B):
            for_b = await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE)
        with _as_user(USER_A):
            for_a_again = await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE)
    assert [episode.resume_position_ms for episode in for_a] == [None, 45000]
    assert [episode.resume_position_ms for episode in for_b] == [None, None]
    assert [episode.resume_position_ms for episode in for_a_again] == [None, 45000]
    assert provider.listings == 1


async def test_the_users_progress_wins_over_what_the_provider_reports(
    mass: MusicAssistant,
) -> None:
    """The progress Music Assistant recorded for the user wins over the provider's own."""
    provider = _StubPodcastProvider(_episodes(1))
    provider.episodes[0].fully_played = False
    provider.episodes[0].resume_position_ms = 5000
    await _add_progress(mass, "ep1", USER_A.user_id, seconds_played=90, fully_played=True)
    with _with_provider(mass, provider):
        for _ in range(2):
            with _as_user(USER_A):
                (episode,) = await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE)
            assert episode.fully_played is True
            assert episode.resume_position_ms == 90000
        # a user without progress of their own sees what the provider reports
        with _as_user(USER_B):
            (episode,) = await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE)
        assert episode.fully_played is False
        assert episode.resume_position_ms == 5000
    assert provider.listings == 1


async def test_a_marked_episode_shows_so_on_the_next_request(mass: MusicAssistant) -> None:
    """
    An episode marked played or unplayed shows so on the very next request.

    Marking returns once the provider, which keeps progress of its own, has taken it in.
    """
    provider = _StubPodcastProvider(_episodes(2))
    provider.episodes[0].fully_played = False
    provider.episodes[0].resume_position_ms = 5000
    episode = provider.episodes[0]

    async def _listed_episode() -> PodcastEpisode:
        return (await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE))[0]

    def _provider_played() -> bool | None:
        return episode.fully_played

    with (
        _with_provider(mass, provider),
        # nothing is left to the background: a report held there never reaches the provider
        _held_play_reports(mass),
        _as_user(USER_A),
        patch.object(mass.webserver.auth, "get_user", AsyncMock(return_value=USER_A)),
        # the user may play from the provider, which is not configured here
        patch(
            "music_assistant.controllers.music.controller.visible_playback_sources",
            return_value=None,
        ),
    ):
        assert (await _listed_episode()).fully_played is False
        await mass.music.mark_item_played(episode, fully_played=True, userid=USER_A.user_id)
        assert _provider_played() is True
        assert (await _listed_episode()).fully_played is True
        await mass.music.mark_item_unplayed(episode, userid=USER_A.user_id)
        assert _provider_played() is False
        # what the provider lists now shows, as the user's progress is gone
        assert (await _listed_episode()).fully_played is False
    assert provider.played == [("ep2", True), ("ep2", False)]
    assert provider.listings == 3


async def test_the_progress_of_a_playing_episode_keeps_the_cached_listing(
    mass: MusicAssistant,
) -> None:
    """The progress reports while an episode plays keep the listing; a stop drops it."""
    provider = _StubPodcastProvider(_episodes(2))
    with _with_provider(mass, provider), _held_play_reports(mass) as deliver_reports:
        await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE)
        await mass.music.mark_item_played(
            provider.episodes[0], fully_played=False, seconds_played=30, is_playing=True
        )
        await deliver_reports()
        await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE)
        assert provider.listings == 1
        # the provider takes a stop in before the call returns
        await mass.music.mark_item_played(
            provider.episodes[0], fully_played=False, seconds_played=60
        )
        await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE)
    assert provider.played == [("ep2", False), ("ep2", False)]
    assert provider.listings == 2


async def test_a_podcast_a_provider_delivers_in_part_is_not_kept(mass: MusicAssistant) -> None:
    """A listing that fails part way is served as far as it got, and fetched again next time."""
    provider = _StubPodcastProvider(_episodes(3))
    provider.failing_after = 2
    with _with_provider(mass, provider):
        for _ in range(2):
            assert _ids(await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE)) == [
                "ep3",
                "ep2",
            ]
        # without anything to serve the failure surfaces
        provider.failing_after = 0
        with pytest.raises(ResourceTemporarilyUnavailable):
            await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE)
    assert provider.listings == 3


async def test_podcast_episodes_of_a_provider_not_loaded(mass: MusicAssistant) -> None:
    """Without the provider there is nothing to list, and nothing is kept as the listing."""
    assert await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE) == []
    provider = _StubPodcastProvider(_episodes(1))
    with _with_provider(mass, provider):
        assert _ids(await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE)) == ["ep1"]


async def test_iter_episodes_walks_the_provider_with_the_users_progress(
    mass: MusicAssistant,
) -> None:
    """The iterator yields every episode as the provider lists it, past the listing cache."""
    provider = _StubPodcastProvider(_episodes(3))
    db_podcast = await mass.music.podcasts.add_item_to_library(_podcast())
    await _add_progress(mass, "ep2", USER_A.user_id, seconds_played=30, fully_played=False)
    with _with_provider(mass, provider), _as_user(USER_A):
        await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE)
        walked = [x async for x in mass.music.podcasts.iter_episodes(db_podcast.item_id, "library")]
    assert _ids(walked) == ["ep3", "ep2", "ep1"]
    assert walked[1].resume_position_ms == 30000
    assert provider.listings == 2
