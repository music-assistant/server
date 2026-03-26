"""E2E tests for playback queue management and state transitions."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature, QueueOption

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
    # Enable native play_media routing so QueueOption.REPLACE can complete via player.play_media().
    # Also inject the provider into MA's registry so get_provider_manifest("mock_player") resolves
    # during registration (required when a player advertises PlayerFeature.PLAY_MEDIA).
    player._attr_supported_features = player._attr_supported_features | {PlayerFeature.PLAY_MEDIA}
    player._cache.clear()
    music_provider = MockMusicProvider(harness.mass, instance_id="pb_music_provider", tracks=tracks)
    await harness.add_provider(music_provider)
    provider.available = True  # type: ignore[attr-defined]  # required by mass.get_provider() availability check
    harness.mass._providers[provider.instance_id] = provider  # type: ignore[assignment]
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
    player, music_provider = player_and_provider

    # Given the music library is synced so REPLACE can resolve full track metadata from the DB
    await harness.sync_library(music_provider.instance_id)

    # And a queue that already has one track
    first_track = make_track(
        item_id="pb-track-0", name="Playback Track 0", provider_domain=MOCK_PROVIDER_DOMAIN
    )
    await harness.mass.player_queues.play_media(
        player.player_id, first_track, option=QueueOption.ADD
    )
    queue = harness.mass.player_queues.get(player.player_id)
    assert queue is not None
    assert queue.items >= 1

    # When a second track is loaded with QueueOption.REPLACE (clears existing items, then plays)
    second_track = make_track(
        item_id="pb-track-1", name="Playback Track 1", provider_domain=MOCK_PROVIDER_DOMAIN
    )
    await harness.mass.player_queues.play_media(
        player.player_id, second_track, option=QueueOption.REPLACE
    )

    # Then the queue contains exactly the replacement item (queue item name includes artist prefix)
    queue_items = harness.mass.player_queues.items(player.player_id)
    assert len(queue_items) == 1
    assert "Playback Track 1" in queue_items[0].name


@pytest.mark.asyncio
async def test_stop_command_propagates_to_player(
    harness: MusicAssistantHarness,
    player_and_provider: tuple[TrackingMockPlayer, MockMusicProvider],
) -> None:
    """Given a playing player, when MA issues a stop command, the player transitions to idle."""
    player, _ = player_and_provider

    # Given a track in the queue and the player set to playing state as a precondition
    track = make_track(
        item_id="pb-track-0", name="Playback Track 0", provider_domain=MOCK_PROVIDER_DOMAIN
    )
    await harness.mass.player_queues.play_media(player.player_id, track, option=QueueOption.ADD)
    player.simulate_play("pb-track-0")
    await harness.mass.players.register_or_update(player)

    # When stop is issued through the MA player queue API
    await harness.mass.player_queues.stop(player.player_id)

    # Then the player is in idle state, confirming the stop command reached the player
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
