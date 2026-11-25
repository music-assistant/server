"""Tests for Sendspin scheduling the next item's metadata at the time its audio starts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosendspin.models.types import ArtworkSource
from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.sendspin.player import SendspinPlayer

_NOW_US = 50_000_000
_NEXT_START_US = 58_000_000


def _media(queue_item_id: str, *, image_url: str | None = None) -> PlayerMedia:
    return PlayerMedia(
        uri=f"library://track/{queue_item_id}",
        title=f"Title {queue_item_id}",
        image_url=image_url,
        duration=200,
        source_id="queue-1",
        queue_item_id=queue_item_id,
    )


def _player() -> MagicMock:
    """Build a player that plays item-1, published, with item-2 sent ahead to start later."""
    player = MagicMock()
    for name in (
        "_schedule_upcoming_metadata",
        "_cancel_upcoming_metadata",
        "_settle_scheduled_artwork",
        "_send_album_artwork",
        "_metadata_publish_allowed",
        "_build_current_media_metadata",
    ):
        setattr(player, name, getattr(SendspinPlayer, name).__get__(player, SendspinPlayer))
    player._metadata_generation = 1
    player._content_takeover_pending = False
    player._metadata_lock = asyncio.Lock()
    player._published_item = ("library://track/item-1", "item-1")
    player._scheduled_item = None
    player._scheduled_artwork = None
    player._metadata_update_pending.return_value = False
    player.last_sent_artwork_url = "art-1"
    player.state = SimpleNamespace(playback_state=PlaybackState.PLAYING)
    player.provider.server_api.clock.now_us.return_value = _NOW_US
    player.playback_session.upcoming_item.return_value = (
        _media("item-1"),
        _media("item-2"),
        _NEXT_START_US,
    )
    player.player.queue_item_media.return_value = _media("item-2", image_url="art-2")
    player.mass.player_queues.get_item.return_value.streamdetails.seek_position = 0
    player._compute_track_progress_ms.return_value = 190_000
    player._album_artist = AsyncMock(return_value=None)
    player._fetch_album_artwork = AsyncMock(return_value="image-2")
    player._publish_artwork = AsyncMock()
    player._artwork_role.set_album_artwork = AsyncMock()
    player._artwork_role.cancel_scheduled = AsyncMock()
    return player


async def test_the_next_item_takes_over_the_metadata_and_artwork_when_it_starts() -> None:
    """The next item is scheduled from its start, and a reschedule does not resend its art."""
    player = _player()

    await player._schedule_upcoming_metadata()

    metadata_role = player._metadata_role
    (metadata,), scheduled = metadata_role.set_metadata.call_args
    assert metadata.title == "Title item-2"
    assert metadata.artwork_url == "art-2"
    assert metadata.track_progress == 0
    assert scheduled == {"timestamp_us": _NEXT_START_US}
    player._artwork_role.set_album_artwork.assert_awaited_once_with(
        "image-2", timestamp_us=_NEXT_START_US
    )

    # publishing the playing item again replaces the scheduled metadata, not the artwork
    player._scheduled_item = None
    await player._schedule_upcoming_metadata()

    assert metadata_role.set_metadata.call_count == 2
    player._artwork_role.set_album_artwork.assert_awaited_once()


async def test_the_next_item_takes_over_from_where_a_crossfade_starts_its_audio() -> None:
    """An item that starts mixed into the one before it shows its position past its start."""
    player = _player()
    await player._schedule_upcoming_metadata()

    player.mass.player_queues.get_item.return_value.streamdetails.seek_position = 8.5
    await player._schedule_upcoming_metadata()

    (metadata,), scheduled = player._metadata_role.set_metadata.call_args
    assert metadata.track_progress == 8_500
    assert scheduled == {"timestamp_us": _NEXT_START_US}


@pytest.mark.parametrize("requires_flow_mode", [True, False])
def test_only_a_player_that_takes_items_one_by_one_schedules_the_next_item(
    requires_flow_mode: bool,
) -> None:
    """Legacy clients, which keep the flow stream, never get the next item's metadata ahead."""
    player = MagicMock()
    player.synced_to = None
    player.requires_flow_mode = requires_flow_mode

    SendspinPlayer.on_upcoming_item_changed(player)

    assert player.mass.create_task.called is not requires_flow_mode


async def test_the_next_item_waits_until_the_playing_item_is_published() -> None:
    """Clients hold one scheduled update, so the next one waits for the playing item."""
    player = _player()
    player._published_item = ("library://track/item-0", "item-0")

    await player._schedule_upcoming_metadata()

    player._metadata_role.set_metadata.assert_not_called()
    player._artwork_role.set_album_artwork.assert_not_awaited()


async def test_the_next_item_waits_while_the_playing_item_is_scheduled() -> None:
    """The playing item's metadata scheduled for its first audio keeps the one slot."""
    player = _player()
    player._metadata_update_pending.return_value = True

    await player._schedule_upcoming_metadata()

    player._metadata_role.set_metadata.assert_not_called()
    player._metadata_role.cancel_scheduled.assert_not_called()


async def test_the_scheduled_artwork_is_withdrawn_when_the_next_item_will_not_play() -> None:
    """Once the session no longer plays the next item, its artwork does not take over."""
    player = _player()
    player._scheduled_item = ("item-2", _NEXT_START_US, 0)
    player._scheduled_artwork = ("art-2", _NEXT_START_US)
    player.playback_session.upcoming_item.return_value = None

    await player._schedule_upcoming_metadata()

    player._artwork_role.cancel_scheduled.assert_awaited_once_with(ArtworkSource.ALBUM)
    assert player.last_sent_artwork_url == "art-1"


async def test_the_scheduled_artwork_is_not_sent_again_once_its_item_plays() -> None:
    """Publishing the item that just started leaves its already scheduled artwork alone."""
    player = _player()
    player._scheduled_artwork = ("art-2", _NEXT_START_US)
    player.provider.server_api.clock.now_us.return_value = _NEXT_START_US + 20_000

    await player._send_album_artwork(_media("item-2", image_url="art-2"))

    player._publish_artwork.assert_not_awaited()
    assert player.last_sent_artwork_url == "art-2"
