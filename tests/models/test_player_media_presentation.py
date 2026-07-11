"""Tests for queue presentation in final player media state."""

from __future__ import annotations

import time
from typing import cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState, PlayerType
from music_assistant_models.media_items import ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from tests.common import MockPlayer, MockProvider


def _mass() -> MagicMock:
    """Build the player and queue controllers needed for final-state projection."""
    mass = MagicMock()
    mass.closing = False
    mass.config.get.return_value = []
    mass.config.get_raw_player_config_value.return_value = "auto"
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    mass.metadata.get_image_url.return_value = "https://example.test/secret.jpg"
    players = PlayerController(mass)
    mass.players = players
    queues = PlayerQueuesController.__new__(PlayerQueuesController)
    queues.mass = mass
    queues._queue_data = {}
    queues._managed_pool = MagicMock()
    mass.player_queues = queues
    return mass


def _track() -> Track:
    """Build a complete track for final-state projection."""
    return Track(
        item_id="secret",
        provider="test",
        name="Secret title",
        duration=123,
        provider_mappings={
            ProviderMapping(
                item_id="secret",
                provider_domain="test",
                provider_instance="test",
            )
        },
    )


def _queue_item(branch: str) -> QueueItem:
    """Build a queue item for one final-current-media branch."""
    if branch == "media_item":
        return QueueItem.from_media_item("q1", _track())
    queue_item = QueueItem(
        queue_id="q1",
        queue_item_id="secret-id",
        name="Secret title",
        duration=123,
    )
    if branch == "stream_metadata":
        stream_metadata = MagicMock(
            title="Secret stream title",
            artist="Secret artist",
            album="Secret album",
            description=None,
            image_url="https://example.test/stream.jpg",
            duration=122,
            elapsed_time=11,
            elapsed_time_last_updated=1234.5,
        )
        queue_item.streamdetails = MagicMock(
            media_type=MediaType.RADIO,
            stream_metadata=stream_metadata,
        )
    return queue_item


def _owner_player(
    mass: MagicMock,
    queue_item: QueueItem,
    player_type: PlayerType = PlayerType.PLAYER,
) -> MockPlayer:
    """Register a queue owner playing the given item."""
    provider = MockProvider("test_provider", instance_id="test", mass=mass)
    player = MockPlayer(provider, "q1", "Queue owner", player_type=player_type)
    player._attr_active_source = "q1"
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 17
    player._attr_elapsed_time_last_updated = time.time()
    player.initialized.set()
    mass.players._players = {"q1": player}
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Queue",
        available=True,
        items=1,
        current_index=0,
        current_item=queue_item,
        elapsed_time=17,
        elapsed_time_last_updated=1234.5,
        state=PlaybackState.PLAYING,
    )
    mass.player_queues._queue_data["q1"] = PlayerQueueData(
        queue=queue,
        items=[queue_item],
    )
    player.update_state(signal_event=False)
    return player


@pytest.mark.parametrize("branch", ["stream_metadata", "media_item", "fallback"])
def test_all_active_queue_media_branches_are_concealed_and_revealed(branch: str) -> None:
    """Every active-queue media branch projects neutral state until the lease clears."""
    mass = _mass()
    player = _owner_player(mass, _queue_item(branch))
    original = player.state.current_media
    assert original is not None
    owner = object()

    mass.player_queues.set_media_presentation("q1", owner, "Music Quiz")

    concealed = player.state.current_media
    assert concealed is not None
    assert concealed.uri.startswith("media-presentation://")
    assert concealed.title == "Music Quiz"
    assert concealed.media_type == MediaType.UNKNOWN
    assert concealed.artist is None
    assert concealed.album is None
    assert concealed.album_artist is None
    assert concealed.image_url is None
    assert concealed.palette is None
    assert concealed.duration is None
    assert concealed.source_id is None
    assert concealed.queue_item_id is None
    assert concealed.custom_data is None
    assert concealed.elapsed_time is not None

    assert mass.player_queues.clear_media_presentation("q1", owner) is True
    revealed = player.state.current_media
    assert revealed is not None
    assert revealed.uri == original.uri
    assert revealed.title == original.title
    assert revealed.media_type == original.media_type
    assert revealed.duration == original.duration
    assert revealed.source_id == "q1"
    assert revealed.queue_item_id == original.queue_item_id


def test_sync_protocol_and_late_join_children_inherit_concealed_media() -> None:
    """Related and newly joined players copy the queue owner's final neutral media."""
    mass = _mass()
    owner_player = _owner_player(mass, _queue_item("media_item"))
    provider = cast("MockProvider", owner_player.provider)
    sync_child = MockPlayer(provider, "sync-child", "Sync child")
    protocol_child = MockPlayer(
        provider,
        "protocol-child",
        "Protocol child",
        player_type=PlayerType.PROTOCOL,
    )
    sync_child.initialized.set()
    protocol_child.initialized.set()
    protocol_child.set_protocol_parent_id("q1")
    mass.players._players.update(
        {
            "sync-child": sync_child,
            "protocol-child": protocol_child,
        }
    )
    owner_player._attr_group_members = ["q1", "sync-child"]
    owner_player.refresh_state(signal_event=False)
    sync_child.refresh_state(signal_event=False)
    protocol_child.refresh_state(signal_event=False)
    presentation_owner = object()

    mass.player_queues.set_media_presentation("q1", presentation_owner, "Music Quiz")
    sync_child.refresh_state(signal_event=False)
    protocol_child.refresh_state(signal_event=False)

    assert sync_child.state.current_media == owner_player.state.current_media
    assert protocol_child.state.current_media == owner_player.state.current_media
    assert sync_child.state.current_media is not None
    assert sync_child.state.current_media.title == "Music Quiz"

    late_child = MockPlayer(provider, "late-child", "Late child")
    late_child.initialized.set()
    mass.players._players["late-child"] = late_child
    owner_player._attr_group_members.append("late-child")
    owner_player.refresh_state(signal_event=False)
    late_child.refresh_state(signal_event=False)

    assert late_child.state.current_media == owner_player.state.current_media
    assert late_child.state.current_media is not None
    assert late_child.state.current_media.source_id is None
    assert late_child.state.current_media.queue_item_id is None


def test_active_group_child_inherits_concealed_media() -> None:
    """A member captured by an active group copies the group's neutral media."""
    mass = _mass()
    group = _owner_player(mass, _queue_item("fallback"), PlayerType.GROUP)
    group._attr_powered = True
    group._attr_group_members = ["member"]
    member = MockPlayer(cast("MockProvider", group.provider), "member", "Member")
    member.initialized.set()
    mass.players._players["member"] = member
    group.refresh_state(signal_event=False)
    member.refresh_state(signal_event=False)

    mass.player_queues.set_media_presentation("q1", object(), "Music Quiz")
    member.refresh_state(signal_event=False)

    assert member.state.current_media == group.state.current_media
    assert member.state.current_media is not None
    assert member.state.current_media.title == "Music Quiz"
    assert member.state.current_media.duration is None
