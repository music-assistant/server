"""
Tests for the "latest" / "newest" podcast start item.

The keyword must resolve to the newest episode on every provider. Providers list
their episodes in whatever order their API returns, so the episode positions
(oldest = lowest) are what decides which one is the newest.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING
from uuid import uuid4

from music_assistant_models.media_items import Podcast, PodcastEpisode, ProviderMapping

from music_assistant.mass import MusicAssistant

if TYPE_CHECKING:
    import pytest

PROVIDER = "test_podcast_prov"


def _provider_mapping() -> set[ProviderMapping]:
    """Create a single provider mapping with a unique item id."""
    return {
        ProviderMapping(
            item_id=uuid4().hex,
            provider_domain=PROVIDER,
            provider_instance=PROVIDER,
        )
    }


def _podcast() -> Podcast:
    """Create a podcast to resolve episodes for."""
    return Podcast(
        item_id="show-latest-001",
        provider=PROVIDER,
        name="Latest Episode Show",
        provider_mappings=_provider_mapping(),
    )


def _episode(podcast: Podcast, number: int, position: int) -> PodcastEpisode:
    """Create an episode carrying the given position."""
    return PodcastEpisode(
        item_id=f"ep-latest-{number:03d}",
        provider=PROVIDER,
        name=f"Episode {number}",
        provider_mappings=_provider_mapping(),
        position=position,
        podcast=podcast,
    )


def _serve(
    monkeypatch: pytest.MonkeyPatch, mass: MusicAssistant, episodes: list[PodcastEpisode]
) -> None:
    """Let the podcasts controller list the given episodes, in the given order."""

    async def _episodes(_item_id: str, _provider: str) -> AsyncGenerator[PodcastEpisode]:
        for episode in episodes:
            yield episode

    monkeypatch.setattr(mass.music.podcasts, "episodes", _episodes)


async def test_latest_picks_highest_position_from_oldest_first_listing(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that lists its episodes oldest-first still resolves to the newest one."""
    podcast = _podcast()
    episodes = [_episode(podcast, number, position=number) for number in (1, 2, 3)]
    _serve(monkeypatch, mass, episodes)

    resolved = await mass.player_queues._media_resolver.get_next_podcast_episodes(podcast, "latest")

    assert [x.item_id for x in resolved] == ["ep-latest-003"]


async def test_latest_picks_highest_position_from_newest_first_listing(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that lists its episodes newest-first resolves to the same episode."""
    podcast = _podcast()
    episodes = [_episode(podcast, number, position=number) for number in (3, 2, 1)]
    _serve(monkeypatch, mass, episodes)

    resolved = await mass.player_queues._media_resolver.get_next_podcast_episodes(podcast, "newest")

    assert [x.item_id for x in resolved] == ["ep-latest-003"]


async def test_latest_falls_back_to_first_listed_without_positions(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without positions to rank on, the episode the provider listed first is used."""
    podcast = _podcast()
    episodes = [_episode(podcast, number, position=0) for number in (3, 2, 1)]
    _serve(monkeypatch, mass, episodes)

    resolved = await mass.player_queues._media_resolver.get_next_podcast_episodes(podcast, "latest")

    assert [x.item_id for x in resolved] == ["ep-latest-003"]


async def test_a_chosen_episode_still_queues_the_newer_ones(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picking an episode queues it plus everything newer, oldest to newest."""
    podcast = _podcast()
    episodes = [_episode(podcast, number, position=number) for number in (1, 2, 3)]
    _serve(monkeypatch, mass, episodes)

    resolved = await mass.player_queues._media_resolver.get_next_podcast_episodes(
        podcast, episodes[1]
    )

    assert [x.item_id for x in resolved] == ["ep-latest-002", "ep-latest-003"]
