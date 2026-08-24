"""Behavior tests for retained resource briefs and their public converters."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from music_assistant.providers.fastmcp_server.models import PlayerBrief, QueueBrief, QueueItemBrief
from music_assistant.providers.fastmcp_server.resource_helpers import (
    safe_active_queue,
    to_brief_player,
    to_brief_queue,
    to_resource_text,
)


def test_to_brief_player_prefers_canonical_state_fields() -> None:
    """Canonical state wins over stale raw power/media and supplies display state."""
    player = SimpleNamespace(
        player_id="kitchen",
        display_name="Kitchen",
        name="Fallback",
        playback_state=SimpleNamespace(value="playing"),
        powered=False,
        current_media=SimpleNamespace(title="stale", uri="old://track"),
        volume_level=42,
        state=SimpleNamespace(
            powered=True,
            current_media=SimpleNamespace(title="Song", uri="library://track/1"),
            volume_muted=False,
            group_volume=40,
            group_volume_muted=True,
            active_group=None,
            synced_to=None,
        ),
    )

    brief = to_brief_player(player)

    assert brief == PlayerBrief(
        player_id="kitchen",
        name="Kitchen",
        state="playing",
        volume_level=42,
        powered=True,
        current_item="Song",
        volume_muted=False,
        group_volume=40,
        group_volume_muted=True,
    )


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"available": False}, "unavailable"),
        ({"enabled": False}, "disabled"),
        ({"needs_setup": True}, "needs_setup"),
        ({"synced_to": "leader"}, "synced"),
    ],
)
def test_to_brief_player_synthesizes_blocker_state(
    overrides: dict[str, object], expected: str
) -> None:
    """Availability/setup/group blockers remain visible to resource clients."""
    values: dict[str, object] = {
        "player_id": "p1",
        "name": "P1",
        "playback_state": SimpleNamespace(value="idle"),
        "current_media": None,
        "powered": True,
        "available": True,
        "enabled": True,
        "needs_setup": False,
        "synced_to": None,
        "active_group": None,
    }
    values.update(overrides)
    assert to_brief_player(SimpleNamespace(**values)).state == expected


def test_to_brief_player_surfaces_external_audio_source() -> None:
    """Active external sources override idle state and the stale player title."""
    player = SimpleNamespace(
        player_id="p1",
        name="P1",
        playback_state=SimpleNamespace(value="idle"),
        current_media=None,
        powered=True,
    )
    queue = SimpleNamespace(
        state=SimpleNamespace(value="playing"),
        current_item=SimpleNamespace(
            streamdetails=SimpleNamespace(
                media_type=SimpleNamespace(value="audio_source"),
                provider="external--1",
                stream_metadata=SimpleNamespace(title="External Song"),
            )
        ),
    )

    brief = to_brief_player(player, queue)

    assert brief.state == "playing"
    assert brief.external_source == "external--1"
    assert brief.current_item == "External Song"


def test_to_brief_queue_preserves_absolute_indices_and_insertion_floor() -> None:
    """Queue pages retain canonical totals and absolute insertion coordinates."""
    queue = SimpleNamespace(
        queue_id="q1",
        current_index=3,
        index_in_buffer=5,
        items=12,
        shuffle_enabled=True,
        repeat_mode=SimpleNamespace(value="all"),
        available=True,
    )
    item = SimpleNamespace(
        queue_item_id="row-1",
        name="Track",
        duration=180,
        media_item=SimpleNamespace(artists=[SimpleNamespace(name="Artist")]),
    )

    brief = to_brief_queue(queue, [item], items_offset=8)

    assert brief == QueueBrief(
        queue_id="q1",
        current_index=3,
        item_count=12,
        shuffle=True,
        repeat="all",
        items=[
            QueueItemBrief(
                item_id="row-1",
                name="Track",
                index=8,
                duration=180,
                artists=["Artist"],
            )
        ],
        index_in_buffer=5,
        next_insertable_index=6,
        items_start_index=8,
    )


def test_to_brief_queue_does_not_infer_total_from_partial_page() -> None:
    """Missing upstream totals remain unknown instead of using page length."""
    queue = SimpleNamespace(
        queue_id="q1",
        current_index=None,
        shuffle_enabled=False,
        repeat_mode=None,
    )
    assert (
        to_brief_queue(queue, [SimpleNamespace(queue_item_id="1", name="One")]).item_count is None
    )


def test_safe_active_queue_swallows_upstream_lookup_errors() -> None:
    """Player resources remain readable when active-queue lookup races teardown."""
    mass = MagicMock()
    mass.player_queues.get_active_queue.side_effect = RuntimeError("gone")
    assert safe_active_queue(mass, "p1") is None


def test_to_resource_text_serializes_briefs_and_ma_objects() -> None:
    """Resource handlers return UTF-8 JSON text for both retained input shapes."""
    brief = PlayerBrief(player_id="p1", name="Кухня", state="idle")
    assert json.loads(to_resource_text(brief) or "null")["name"] == "Кухня"

    ma_object = SimpleNamespace(to_dict=lambda: {"uri": "library://track/1"})
    assert json.loads(to_resource_text(ma_object) or "null") == {"uri": "library://track/1"}
    assert to_resource_text(None) is None
