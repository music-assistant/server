"""Tests for the track information (player, queue item, timing) sent to the skill."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from music_assistant_models.config_entries import PlayerConfig
from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.alexa import AlexaPlayer
from tests.common import MockProvider

PLAYER_ID = "Kitchen Echo"
ANSWER = '{"status": "ok"}'


@pytest.fixture
def alexa() -> Iterator[tuple[AlexaPlayer, AsyncMock, MagicMock]]:
    """Return a real AlexaPlayer, a mock for the skill's API and the active queue."""
    provider = cast("Any", MockProvider("alexa", instance_id="alexa--1"))
    provider.mass.config.get_base_protocol_config.return_value = PlayerConfig(
        values={}, provider="alexa--1", player_id=PLAYER_ID, default_name=PLAYER_ID
    )
    provider.mass.streams.resolve_stream_url = AsyncMock(return_value="http://ma/flow/1.mp3")
    provider.config = MagicMock()
    provider.config.get_value.return_value = "en-US"
    queue = MagicMock()
    queue.current_item.queue_item_id = "item-2"
    queue.elapsed_time = 1800
    queue.corrected_elapsed_time = 1800.0
    provider.mass.player_queues.get_active_queue = MagicMock(return_value=queue)
    player = AlexaPlayer(provider, PLAYER_ID, MagicMock())
    alexa_api = MagicMock()
    alexa_api.run_custom = AsyncMock()
    skill_api = AsyncMock(return_value=ANSWER)
    with (
        patch.object(player, "update_state"),
        patch.object(AlexaPlayer, "api", new_callable=PropertyMock, return_value=alexa_api),
        patch("music_assistant.providers.alexa.api_request", skill_api),
    ):
        yield player, skill_api, queue


def _last_payload(skill_api: AsyncMock) -> dict[str, Any]:
    assert skill_api.await_args is not None
    return cast("dict[str, Any]", skill_api.await_args.kwargs["json_data"])


async def _upload(player: AlexaPlayer, media: PlayerMedia) -> None:
    attached = MagicMock()
    attached.player_id = PLAYER_ID
    attached.current_media = media
    player._player = attached
    player.on_player_media_updated()
    create_task = cast("MagicMock", player.mass.create_task)
    if create_task.call_args is not None:
        await create_task.call_args.args[0]
        create_task.reset_mock()


async def test_new_track_carries_its_queue_item_and_timing(
    alexa: tuple[AlexaPlayer, AsyncMock, MagicMock],
) -> None:
    """A new track tells the skill its queue item, duration and position in the item."""
    player, skill_api, _queue = alexa
    await player.play_media(
        PlayerMedia(uri="x", title="t", queue_item_id="item-2", duration=225, elapsed_time=42)
    )
    payload = _last_payload(skill_api)
    assert payload["queueItemId"] == "item-2"
    assert payload["durationMs"] == 225000
    assert payload["elapsedMs"] == 42000


async def test_radio_song_gets_its_own_position_not_the_station_s(
    alexa: tuple[AlexaPlayer, AsyncMock, MagicMock],
) -> None:
    """A radio song with a duration is at its own position, not at how long the station played."""
    player, skill_api, _queue = alexa  # the queue has played the station for 30 minutes
    player._last_stream_url = "http://ma/flow/1.mp3"
    await _upload(
        player,
        PlayerMedia(uri="r", title="Song", queue_item_id="item-2", duration=200, elapsed_time=12),
    )
    payload = _last_payload(skill_api)
    assert payload["durationMs"] == 200000
    assert payload["elapsedMs"] == 12000


async def test_position_runs_on_only_while_playing(
    alexa: tuple[AlexaPlayer, AsyncMock, MagicMock],
) -> None:
    """While the Echo plays, the position is carried forward from when it was last updated."""
    player, skill_api, _queue = alexa
    player._last_stream_url = "http://ma/flow/1.mp3"
    updated = time.time() - 5
    for item, state, low, high in (
        ("item-2", PlaybackState.PLAYING, 14900, 15500),
        ("item-3", PlaybackState.PAUSED, 10000, 10000),
    ):
        media = PlayerMedia(
            uri="x",
            title="t",
            queue_item_id=item,
            duration=225,
            elapsed_time=10,
            elapsed_time_last_updated=updated,
        )
        with patch.object(
            AlexaPlayer, "playback_state", new_callable=PropertyMock, return_value=state
        ):
            await _upload(player, media)
        assert low <= _last_payload(skill_api)["elapsedMs"] <= high


async def test_media_outside_a_queue_sends_no_timing(
    alexa: tuple[AlexaPlayer, AsyncMock, MagicMock],
) -> None:
    """Media without a queue item or duration sends the same fields as before."""
    player, skill_api, _queue = alexa
    await player.play_media(PlayerMedia(uri="x", title="t"))
    payload = _last_payload(skill_api)
    assert not {"queueItemId", "durationMs", "elapsedMs"} & payload.keys()


async def test_metadata_upload_names_its_player_and_carries_timing(
    alexa: tuple[AlexaPlayer, AsyncMock, MagicMock],
) -> None:
    """A track change in the flow stream tells the skill which Echo it is for, and its timing."""
    player, skill_api, _queue = alexa
    player._last_stream_url = "http://ma/flow/1.mp3"
    await _upload(
        player,
        PlayerMedia(uri="y", title="Next", queue_item_id="item-2", duration=180, elapsed_time=3),
    )
    payload = _last_payload(skill_api)
    assert payload["playerId"] == PLAYER_ID
    assert payload["queueItemId"] == "item-2"
    assert payload["durationMs"] == 180000
    assert payload["elapsedMs"] == 3000


async def test_same_metadata_for_a_new_queue_item_is_uploaded_again(
    alexa: tuple[AlexaPlayer, AsyncMock, MagicMock],
) -> None:
    """A track queued twice in a row is a new item: its timing starts over, so it is sent."""
    player, skill_api, _queue = alexa
    player._last_stream_url = "http://ma/flow/1.mp3"
    await _upload(player, PlayerMedia(uri="y", title="Same", queue_item_id="item-2"))
    await _upload(player, PlayerMedia(uri="y", title="Same", queue_item_id="item-2"))
    assert skill_api.await_count == 1
    await _upload(player, PlayerMedia(uri="y", title="Same", queue_item_id="item-3"))
    assert skill_api.await_count == 2
