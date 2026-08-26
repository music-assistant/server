"""
Tests for the playlog updated event.

The integration tests use the ``mass`` fixture from ``tests/conftest.py`` which
creates a full MusicAssistant instance with a real SQLite database in a
temporary directory.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

from music_assistant_models.enums import EventType, MediaType
from music_assistant_models.media_items import (
    Artist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Track,
)
from music_assistant_models.playlog_update import PlaylogUpdate
from music_assistant_models.unique_list import UniqueList

from music_assistant.mass import MusicAssistant

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent


def _provider_mapping(provider: str = "test_prov") -> set[ProviderMapping]:
    """Create a single provider mapping with a unique item id."""
    return {
        ProviderMapping(
            item_id=uuid4().hex,
            provider_domain=provider,
            provider_instance=provider,
        )
    }


def _collect_events(mass: MusicAssistant) -> list[MassEvent]:
    """Return a list that collects the playlog updated events as they are signalled."""
    events: list[MassEvent] = []
    mass.subscribe(events.append, EventType.PLAYLOG_UPDATED)
    return events


async def _updates(events: list[MassEvent]) -> list[PlaylogUpdate]:
    """Let the pending event callbacks run and return the collected payloads."""
    await asyncio.sleep(0)
    return [event.data for event in events]


async def test_mark_played_signals_playlog_updated(mass: MusicAssistant) -> None:
    """Marking an item played signals the new playlog state for that item."""
    user = await mass.webserver.auth.create_user("playloguser")
    track = Track(
        item_id="track-001",
        provider="test_prov",
        name="Test Track",
        provider_mappings=_provider_mapping(),
        duration=200,
    )
    assert track.uri is not None
    events = _collect_events(mass)

    await mass.music.mark_item_played(track, fully_played=True, userid=user.user_id)

    updates = await _updates(events)
    assert len(updates) == 1
    assert events[0].object_id == track.uri
    assert updates[0] == PlaylogUpdate(
        uri=track.uri,
        media_type=MediaType.TRACK,
        fully_played=True,
        seconds_played=0,
        userid=user.user_id,
    )


async def test_progress_report_signals_resume_position(mass: MusicAssistant) -> None:
    """A partial play reports the resume position, so clients can update an in progress row."""
    user = await mass.webserver.auth.create_user("playlogpartial")
    episode = PodcastEpisode(
        item_id="ep-010",
        provider="test_podcast_prov",
        name="Episode 10",
        provider_mappings=_provider_mapping("test_podcast_prov"),
        position=1,
        podcast=Podcast(
            item_id="show-011",
            provider="test_podcast_prov",
            name="Progress Show",
            provider_mappings=_provider_mapping("test_podcast_prov"),
        ),
    )
    assert episode.uri is not None
    events = _collect_events(mass)

    await mass.music.mark_item_played(
        episode, fully_played=False, seconds_played=120, userid=user.user_id
    )

    updates = await _updates(events)
    assert updates == [
        PlaylogUpdate(
            uri=episode.uri,
            media_type=MediaType.PODCAST_EPISODE,
            fully_played=False,
            seconds_played=120,
            userid=user.user_id,
        )
    ]


async def test_playing_progress_report_signals_nothing(mass: MusicAssistant) -> None:
    """The frequent progress reports during playback do not touch the playlog, so stay quiet."""
    user = await mass.webserver.auth.create_user("playlogplaying")
    track = Track(
        item_id="track-002",
        provider="test_prov",
        name="Playing Track",
        provider_mappings=_provider_mapping(),
        duration=200,
    )
    events = _collect_events(mass)

    await mass.music.mark_item_played(
        track, fully_played=False, seconds_played=30, is_playing=True, userid=user.user_id
    )

    assert await _updates(events) == []


async def test_mark_unplayed_signals_playlog_updated(mass: MusicAssistant) -> None:
    """Marking an item unplayed signals it as neither played nor in progress."""
    user = await mass.webserver.auth.create_user("playlogunplayed")
    episode = PodcastEpisode(
        item_id="ep-011",
        provider="test_podcast_prov",
        name="Episode 11",
        provider_mappings=_provider_mapping("test_podcast_prov"),
        position=1,
        podcast=Podcast(
            item_id="show-012",
            provider="test_podcast_prov",
            name="Unplayed Show",
            provider_mappings=_provider_mapping("test_podcast_prov"),
        ),
    )
    assert episode.uri is not None
    await mass.music.mark_item_played(
        episode, fully_played=False, seconds_played=120, userid=user.user_id
    )
    events = _collect_events(mass)

    await mass.music.mark_item_unplayed(episode, userid=user.user_id)

    assert await _updates(events) == [
        PlaylogUpdate(
            uri=episode.uri,
            media_type=MediaType.PODCAST_EPISODE,
            fully_played=False,
            seconds_played=0,
            userid=user.user_id,
        )
    ]


async def test_mark_played_without_user_applies_to_all_users(mass: MusicAssistant) -> None:
    """Without a determinable user the playlog changes for everyone, so userid is None."""
    track = Track(
        item_id="track-003",
        provider="test_prov",
        name="Shared Track",
        provider_mappings=_provider_mapping(),
        duration=200,
    )
    events = _collect_events(mass)

    await mass.music.mark_item_played(track, fully_played=True)

    updates = await _updates(events)
    assert len(updates) == 1
    assert updates[0].userid is None


async def test_credited_artist_signals_playlog_updated(mass: MusicAssistant) -> None:
    """An artist credited by a played track gets its own playlog update."""
    user = await mass.webserver.auth.create_user("playlogartist")
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0", provider="library", name="Credited", provider_mappings=_provider_mapping()
        )
    )
    added = await mass.music.tracks.add_item_to_library(
        Track(
            item_id="0",
            provider="library",
            name="Credited Track",
            provider_mappings=_provider_mapping(),
            artists=UniqueList([artist]),
        )
    )
    track = await mass.music.tracks.get_library_item(added.item_id)
    events = _collect_events(mass)

    await mass.music.mark_item_played(track, fully_played=True, userid=user.user_id)

    updates = await _updates(events)
    assert [update.uri for update in updates] == [track.uri, artist.uri]
    assert updates[1].media_type == MediaType.ARTIST
    assert updates[1].userid == user.user_id


async def test_credited_podcast_signals_playlog_updated(mass: MusicAssistant) -> None:
    """The parent podcast credited by a played episode gets its own playlog update."""
    user = await mass.webserver.auth.create_user("playlogpodcast")
    podcast = Podcast(
        item_id="show-010",
        provider="test_podcast_prov",
        name="Credited Show",
        provider_mappings=_provider_mapping("test_podcast_prov"),
    )
    episode = PodcastEpisode(
        item_id="ep-012",
        provider="test_podcast_prov",
        name="Episode 12",
        provider_mappings=_provider_mapping("test_podcast_prov"),
        position=1,
        podcast=podcast,
    )
    events = _collect_events(mass)

    await mass.music.mark_item_played(episode, fully_played=True, userid=user.user_id)

    updates = await _updates(events)
    assert [update.uri for update in updates] == [episode.uri, podcast.uri]
    assert updates[1].media_type == MediaType.PODCAST
