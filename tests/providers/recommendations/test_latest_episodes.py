"""Tests for the Latest episodes row of the library recommendations provider."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import ItemMapping, MediaItemMetadata, PodcastEpisode

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.recommendations import (
    LATEST_EPISODES_TASK_ID,
    SUPPORTED_FEATURES,
    LibraryRecommendationsProvider,
    LibraryRowID,
)


def _episode(
    item_id: str, provider: str, position: int, release_date: datetime | None = None
) -> PodcastEpisode:
    """Build a podcast episode with optional release date and played state set."""
    return PodcastEpisode(
        item_id=item_id,
        provider=provider,
        name=item_id,
        position=position,
        podcast=ItemMapping(
            media_type=MediaType.PODCAST, item_id="pod", provider=provider, name="Pod"
        ),
        provider_mappings=set(),
        metadata=MediaItemMetadata(release_date=release_date),
        fully_played=True,
        resume_position_ms=1000,
    )


def _podcast(item_id: str, provider_instances: list[str]) -> Mock:
    """Build a library podcast stand-in mapped on the given provider instances."""
    podcast = Mock()
    podcast.item_id = item_id
    podcast.provider = "library"
    podcast.name = item_id
    podcast.provider_mappings = [
        Mock(provider_instance=x, item_id=f"{x}_{item_id}") for x in provider_instances
    ]
    return podcast


@pytest.fixture
def provider() -> LibraryRecommendationsProvider:
    """Return the provider with a mocked mass, nothing cached and every provider loaded."""
    mass = Mock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    manifest = Mock()
    manifest.domain = "recommendations"
    config = Mock()
    config.instance_id = "recommendations"
    config.get_value.side_effect = lambda key, default=None: {"log_level": "INFO"}.get(key, default)
    return LibraryRecommendationsProvider(mass, manifest, config, SUPPORTED_FEATURES)


def _mass(provider: LibraryRecommendationsProvider) -> Mock:
    """Return the mocked mass of the provider."""
    return cast("Mock", provider.mass)


def _set_library(
    provider: LibraryRecommendationsProvider,
    library: dict[str, object],
    mappings: dict[str, list[str]] | None = None,
    active: list[str] | None = None,
) -> None:
    """
    Make the library hold podcasts mapped to their episodes, or to an error to raise.

    :param provider: The provider under test.
    :param library: Podcast id mapped to its episodes, or to an error its provider raises.
    :param mappings: Podcast id mapped to the provider instances it is linked on.
    :param active: The provider instances that are loaded and available.
    """
    mappings = mappings or {}
    active = active if active is not None else ["prov"]

    async def iter_library_items() -> AsyncIterator[Mock]:
        for item_id in library:
            yield _podcast(item_id, mappings.get(item_id, ["prov"]))

    async def get_podcast_episodes(prov_item_id: str) -> AsyncIterator[PodcastEpisode]:
        result = library[prov_item_id.split("_", 1)[1]]
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, list)
        for episode in result:
            yield episode

    music_provider = Mock(spec=MusicProvider)
    music_provider.get_podcast_episodes = get_podcast_episodes
    _mass(provider).get_provider.return_value = music_provider
    _mass(provider).music.get_active_provider_instances.return_value = active
    _mass(provider).music.podcasts.iter_library_items = iter_library_items


async def test_task_registered_every_six_hours(provider: LibraryRecommendationsProvider) -> None:
    """The refresh task runs every 6 hours, with a startup delay for an overdue run."""
    await provider.handle_async_init()
    kwargs = _mass(provider).tasks.register_scheduled_task.call_args.kwargs
    assert kwargs["task_id"] == LATEST_EPISODES_TASK_ID
    assert kwargs["schedule"] == TaskSchedule.hourly(every=6)
    assert kwargs["initial_delay"] == 20


async def test_cached_row_loaded_at_startup(provider: LibraryRecommendationsProvider) -> None:
    """A cached row is served from startup on."""
    episode = _episode("e1", "prov", 1)
    _mass(provider).cache.get.return_value = [{"podcast_id": "pod", "episode": episode.to_dict()}]
    await provider.handle_async_init()
    assert provider._latest_episodes == [("pod", episode)]


async def test_refresh_picks_latest_and_orders_by_release_date(
    provider: LibraryRecommendationsProvider,
) -> None:
    """The highest position episode of each podcast is kept, newest release first, undated last."""
    _set_library(
        provider,
        {
            "undated": [_episode("u1", "prov", 1), _episode("u2", "prov", 2)],
            "old": [
                _episode("o2", "prov", 2, datetime(2025, 1, 1, tzinfo=UTC)),
                _episode("o1", "prov", 1, datetime(2026, 6, 1, tzinfo=UTC)),
            ],
            "new": [_episode("n1", "prov", 5, datetime(2026, 3, 1, tzinfo=UTC))],
            "empty": [],
        },
    )
    await provider._refresh_latest_episodes()
    assert [x[1].item_id for x in provider._latest_episodes] == ["n1", "o2", "u2"]


async def test_refresh_skips_failing_podcast(provider: LibraryRecommendationsProvider) -> None:
    """A podcast whose provider fails is left out while the others are kept."""
    _set_library(
        provider,
        {"broken": ProviderUnavailableError("gone"), "ok": [_episode("e1", "prov", 1)]},
    )
    await provider._refresh_latest_episodes()
    assert [x[1].item_id for x in provider._latest_episodes] == ["e1"]


async def test_refresh_uses_an_available_mapping(provider: LibraryRecommendationsProvider) -> None:
    """A podcast is read through a provider that is available, and skipped when none is."""
    _set_library(
        provider,
        {"linked": [_episode("e1", "prov", 1)], "offline": [_episode("e2", "prov", 1)]},
        mappings={"linked": ["down", "prov"], "offline": ["down"]},
    )
    await provider._refresh_latest_episodes()
    assert [x[1].item_id for x in provider._latest_episodes] == ["e1"]
    _mass(provider).get_provider.assert_called_once_with("prov")


async def test_play_history_wins_over_provider_state(
    provider: LibraryRecommendationsProvider,
) -> None:
    """MA's play history is used when it has an entry, without changing the stored row."""
    _set_library(provider, {"pod": [_episode("e1", "prov", 1)]})
    await provider._refresh_latest_episodes()

    async def restore(episode: PodcastEpisode, provider_instance_id: str) -> None:
        assert provider_instance_id == "prov"
        assert episode.fully_played is None
        episode.fully_played = False
        episode.resume_position_ms = 5000

    _mass(provider).music.podcasts.restore_resume_position = restore
    items = await provider.get_recommendation_items(LibraryRowID.LATEST_EPISODES)
    assert isinstance(items[0], PodcastEpisode)
    assert (items[0].fully_played, items[0].resume_position_ms) == (False, 5000)
    assert (
        provider._latest_episodes[0][1].fully_played,
        provider._latest_episodes[0][1].resume_position_ms,
    ) == (True, 1000)


async def test_provider_state_used_without_play_history(
    provider: LibraryRecommendationsProvider,
) -> None:
    """The provider's reported state is shown when MA has no play history for the episode."""
    _set_library(provider, {"pod": [_episode("e1", "prov", 1)]})
    await provider._refresh_latest_episodes()
    _mass(provider).music.podcasts.restore_resume_position = AsyncMock()
    items = await provider.get_recommendation_items(LibraryRowID.LATEST_EPISODES)
    assert isinstance(items[0], PodcastEpisode)
    assert (items[0].fully_played, items[0].resume_position_ms) == (True, 1000)


async def test_providers_filter_and_allowed_providers(
    provider: LibraryRecommendationsProvider,
) -> None:
    """Only episodes of requested providers that the user may use and that are available."""
    provider._latest_episodes = [
        ("pod_a", _episode("a", "prov_a", 1)),
        ("pod_b", _episode("b", "prov_b", 1)),
        ("pod_hidden", _episode("hidden", "prov_hidden", 1)),
    ]
    _mass(provider).music.podcasts.restore_resume_position = AsyncMock()
    _mass(provider).music.get_active_provider_instances.return_value = ["prov_a", "prov_b"]

    items = await provider.get_recommendation_items(LibraryRowID.LATEST_EPISODES)
    assert [x.item_id for x in items] == ["a", "b"]
    items = await provider.get_recommendation_items(
        LibraryRowID.LATEST_EPISODES, providers=["prov_b", "prov_hidden"]
    )
    assert [x.item_id for x in items] == ["b"]


async def test_podcast_shown_through_any_mapping_the_user_can_use(
    provider: LibraryRecommendationsProvider,
) -> None:
    """A podcast on several providers is kept for each, and shown once through one the user has."""
    episodes = {"mine": _episode("e_mine", "mine", 1), "theirs": _episode("e_theirs", "theirs", 1)}

    def get_provider(instance_id: str) -> Mock:
        async def get_podcast_episodes(_prov_item_id: str) -> AsyncIterator[PodcastEpisode]:
            yield episodes[instance_id]

        music_provider = Mock(spec=MusicProvider)
        music_provider.get_podcast_episodes = get_podcast_episodes
        return music_provider

    async def iter_library_items() -> AsyncIterator[Mock]:
        yield _podcast("pod", ["theirs", "mine"])

    _mass(provider).get_provider.side_effect = get_provider
    _mass(provider).music.podcasts.iter_library_items = iter_library_items
    _mass(provider).music.podcasts.restore_resume_position = AsyncMock()
    _mass(provider).music.get_active_provider_instances.return_value = ["mine", "theirs"]
    await provider._refresh_latest_episodes()
    assert len(provider._latest_episodes) == 2

    items = await provider.get_recommendation_items(LibraryRowID.LATEST_EPISODES)
    assert len(items) == 1
    _mass(provider).music.get_active_provider_instances.return_value = ["mine"]
    items = await provider.get_recommendation_items(LibraryRowID.LATEST_EPISODES)
    assert [x.item_id for x in items] == ["e_mine"]
