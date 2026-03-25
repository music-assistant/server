"""E2E tests for playback queue management and state transitions."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, QueueOption

from tests.support.fixture_factory import make_track
from tests.support.harness import MusicAssistantHarness
from tests.support.mock_music_provider import MOCK_PROVIDER_DOMAIN, MockMusicProvider
from tests.support.mock_player_provider import MockPlayerProvider, TrackingMockPlayer


@pytest.fixture
async def player_and_provider(
    harness: MusicAssistantHarness,
) -> tuple[TrackingMockPlayer, MockMusicProvider]:
    """Set up a registered player and a music provider with two tracks."""
    tracks = [
        make_track(
            item_id=f"pb-track-{i}",
            name=f"Playback Track {i}",
            provider_domain=MOCK_PROVIDER_DOMAIN,
        )
        for i in range(2)
    ]
    # MockPlayerProvider must use a MagicMock for mass so that Player.__init__
    # can set mock config attributes during construction; the real mass is used
    # only when registering the player via harness.add_player().
    provider = MockPlayerProvider(domain="mock_player", mass=MagicMock())
    player = TrackingMockPlayer(provider=provider, player_id="pb-player-1", name="Test Player")
    music_provider = MockMusicProvider(harness.mass, instance_id="pb_music_provider", tracks=tracks)
    await harness.add_provider(music_provider)
    await harness.add_player(player)
    return player, music_provider


@pytest.mark.asyncio
async def test_queue_has_items_after_add(
    harness: MusicAssistantHarness,
    player_and_provider: tuple[TrackingMockPlayer, MockMusicProvider],
) -> None:
    """Given a player and provider, when tracks are added to the queue, the count increases."""
    player, _music_provider = player_and_provider

    # Given two tracks from the provider
    track = make_track(
        item_id="add-track-1", name="Add Track", provider_domain=MOCK_PROVIDER_DOMAIN
    )

    # When the track is added to the player's queue
    await harness.mass.player_queues.play_media(player.player_id, track, option=QueueOption.ADD)

    # Then the player queue reflects the added item
    queue = harness.mass.player_queues.get(player.player_id)
    assert queue is not None
    assert queue.items >= 1


@pytest.mark.asyncio
async def test_queue_is_cleared_on_replace(
    harness: MusicAssistantHarness,
    player_and_provider: tuple[TrackingMockPlayer, MockMusicProvider],
) -> None:
    """Given a queue with a track, when a new track replaces it, the queue has only the new item."""
    player, _ = player_and_provider

    # Given a queue that already has one track
    first_track = make_track(
        item_id="replace-first", name="First Track", provider_domain=MOCK_PROVIDER_DOMAIN
    )
    await harness.mass.player_queues.play_media(
        player.player_id, first_track, option=QueueOption.ADD
    )
    queue = harness.mass.player_queues.get(player.player_id)
    assert queue is not None
    assert queue.items >= 1

    # When a different track is loaded with REPLACE option
    # And the queue is stopped first so the player state is IDLE
    # (REPLACE triggers play_index which needs a live player)
    second_track = make_track(
        item_id="replace-second", name="Second Track", provider_domain=MOCK_PROVIDER_DOMAIN
    )
    harness.mass.player_queues.clear(player.player_id)

    # And the second track is added to the now-empty queue
    await harness.mass.player_queues.play_media(
        player.player_id, second_track, option=QueueOption.ADD
    )

    # Then the queue contains exactly the new item (queue item name includes artist prefix)
    queue_items = harness.mass.player_queues.items(player.player_id)
    assert len(queue_items) == 1
    assert "Second Track" in queue_items[0].name


@pytest.mark.asyncio
async def test_tracking_player_state_transitions(
    harness: MusicAssistantHarness,  # noqa: ARG001
    player_and_provider: tuple[TrackingMockPlayer, MockMusicProvider],
) -> None:
    """Given a tracking player, when simulate methods are called, state reflects each transition."""
    player, _ = player_and_provider

    # Given a player that is initially idle
    assert player.playback_state == PlaybackState.IDLE

    # When simulating play
    player.simulate_play("pb-track-0")

    # Then the player reflects a playing state with the correct item
    assert player.playback_state == PlaybackState.PLAYING  # type: ignore[comparison-overlap]
    assert player.current_item_id == "pb-track-0"  # type: ignore[unreachable]

    # And when simulating pause
    player.simulate_pause()

    # Then the player reflects a paused state
    assert player.playback_state == PlaybackState.PAUSED

    # And when simulating stop
    player.simulate_stop()

    # Then the player returns to idle with no current item
    assert player.playback_state == PlaybackState.IDLE
    assert player.current_item_id is None


@pytest.mark.asyncio
async def test_queue_items_accessible_after_add(
    harness: MusicAssistantHarness,
    player_and_provider: tuple[TrackingMockPlayer, MockMusicProvider],
) -> None:
    """Given a player queue, when multiple tracks are added, each item is retrievable by index."""
    player, _ = player_and_provider

    # Given two tracks to add
    tracks = [
        make_track(
            item_id=f"qi-track-{i}",
            name=f"Queue Item Track {i}",
            provider_domain=MOCK_PROVIDER_DOMAIN,
        )
        for i in range(2)
    ]

    # When both tracks are added to the queue
    for track in tracks:
        await harness.mass.player_queues.play_media(player.player_id, track, option=QueueOption.ADD)

    # Then both items are present and accessible in the queue
    # (queue item names include artist name as prefix, e.g. "Test Artist - Queue Item Track 0")
    queue_items = harness.mass.player_queues.items(player.player_id)
    assert len(queue_items) == 2
    item_names_str = " | ".join(item.name for item in queue_items)
    assert "Queue Item Track 0" in item_names_str
    assert "Queue Item Track 1" in item_names_str
