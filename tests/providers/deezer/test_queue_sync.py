"""Test that a Deezer track playing in Music Assistant becomes the queue the Deezer apps continue."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from deezer_python_gql.generated.push_queue_state import (
    PushQueueStatePushQueueStatePushQueueStateError,
    PushQueueStatePushQueueStatePushQueueStateOutput,
)
from music_assistant_models.enums import MediaType, RepeatMode
from music_assistant_models.media_items import ItemMapping

from music_assistant.providers.deezer.streaming import DeezerStreamingManager

INSTANCE = "deezer--test123"
PLAYLIST = ItemMapping(
    media_type=MediaType.PLAYLIST, item_id="85", provider="library", name="Peaceful Piano"
)


def _output(version: str) -> PushQueueStatePushQueueStatePushQueueStateOutput:
    return PushQueueStatePushQueueStatePushQueueStateOutput.model_validate(
        {"__typename": "PushQueueStateOutput", "version": version}
    )


def _item(queue_item_id: str, *mappings: tuple[str, str], streamed: str | None = None) -> Any:
    """Return a queue item played from the playlist, with the given provider mappings."""
    return SimpleNamespace(
        queue_item_id=queue_item_id,
        media_type=MediaType.TRACK,
        streamdetails=SimpleNamespace(provider=INSTANCE, item_id=streamed) if streamed else None,
        media_item=SimpleNamespace(
            provider_mappings=[
                SimpleNamespace(provider_instance=instance, item_id=item_id, available=True)
                for instance, item_id in mappings
            ]
        ),
        origin=SimpleNamespace(container=PLAYLIST),
    )


@pytest.fixture
def items() -> list[Any]:
    """Return a queue of a playing Deezer track, another one, a Tidal track and an upload."""
    return [
        _item("q1", (INSTANCE, "10"), streamed="10"),
        _item("q2", (INSTANCE, "11"), ("tidal--test", "abc")),
        _item("q3", ("tidal--test", "def")),
        _item("q4", (INSTANCE, "-5")),
    ]


@pytest.fixture
def provider(items: list[Any]) -> Mock:
    """Return a provider with queue sync enabled and one queue playing the first item."""
    provider = Mock(instance_id=INSTANCE)
    provider.config.get_value.side_effect = lambda key: key == "sync_queue"
    provider.gql_client.push_queue_state = AsyncMock(return_value=_output("v1"))
    queue = SimpleNamespace(
        queue_id="queue", current_index=0, current_item=items[0], repeat_mode=RepeatMode.OFF
    )
    provider.mass.player_queues.all.return_value = (queue,)
    provider.mass.player_queues.items.side_effect = lambda _queue_id, limit, offset: items[
        offset : offset + limit
    ]
    provider.mass.music.get_controller.return_value.get_library_item = AsyncMock(
        return_value=SimpleNamespace(
            provider_mappings=[SimpleNamespace(provider_instance=INSTANCE, item_id="777")]
        )
    )
    return provider


async def _report(provider: Mock, track_id: str, position: int, *, is_playing: bool) -> None:
    await DeezerStreamingManager(provider).on_played(
        MediaType.TRACK, track_id, False, position, Mock(), is_playing=is_playing
    )


async def test_playing_track_saves_its_deezer_queue(provider: Mock) -> None:
    """The Deezer tracks from the playing one on are saved with the playlist they came from."""
    await _report(provider, "10", 42, is_playing=True)

    state = provider.gql_client.push_queue_state.call_args.kwargs["input"]
    assert [(m.id, m.local_queue_item_id, m.context_id) for m in state.media_list] == [
        ("10", "q1", "0"),
        ("11", "q2", "0"),
    ]
    context = json.loads(state.contexts[0].content)
    assert (context["ctxT"], context["ctxId"]) == ("playlist_page", "777")
    assert (state.position, state.progress_ms, state.previous_version) == (0, 42000, None)


async def test_playing_track_saves_once_and_pause_saves_the_position(provider: Mock) -> None:
    """The reports every 30 seconds push nothing new, a pause pushes where it stopped."""
    streaming = DeezerStreamingManager(provider)
    for position, is_playing in ((0, True), (30, True), (51, False)):
        await streaming.on_played(
            MediaType.TRACK, "10", False, position, Mock(), is_playing=is_playing
        )

    states = [call.kwargs["input"] for call in provider.gql_client.push_queue_state.call_args_list]
    assert [(s.progress_ms, s.previous_version) for s in states] == [(0, None), (51000, "v1")]


async def test_conflict_pushes_on_top_of_the_app_state(provider: Mock) -> None:
    """When a Deezer app saved its queue meanwhile, the push is repeated on its version."""
    conflict = PushQueueStatePushQueueStatePushQueueStateError.model_validate(
        {
            "__typename": "PushQueueStateError",
            "isBadRequest": False,
            "conflict": {"current": {"version": "app"}},
        }
    )
    provider.gql_client.push_queue_state.side_effect = [conflict, _output("v2")]

    await _report(provider, "10", 0, is_playing=True)

    states = [call.kwargs["input"] for call in provider.gql_client.push_queue_state.call_args_list]
    assert [s.previous_version for s in states] == [None, "app"]


@pytest.mark.parametrize(
    ("enabled", "track_id"),
    [(False, "10"), (True, "11")],
    ids=["setting off", "track not streamed by this account"],
)
async def test_queue_is_not_saved(provider: Mock, enabled: bool, track_id: str) -> None:
    """Nothing is saved with the setting off, or for a track this account is not playing."""
    provider.config.get_value.side_effect = lambda key: enabled and key == "sync_queue"

    await _report(provider, track_id, 0, is_playing=True)

    provider.gql_client.push_queue_state.assert_not_called()
