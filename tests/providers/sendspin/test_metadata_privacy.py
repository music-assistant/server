"""Tests for Sendspin metadata sourced from concealed queue presentation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.providers.sendspin.player import SendspinPlayer


def _projected_media() -> tuple[MagicMock, PlayerMedia, PlayerMedia]:
    """Build concealed and revealed media from a real queue presentation lease."""
    mass = MagicMock()
    mass.players.get_player.return_value = None
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.mass = mass
    controller._queue_data = {
        "q1": PlayerQueueData(
            queue=PlayerQueue(
                queue_id="q1",
                active=True,
                display_name="Queue",
                available=True,
                items=1,
            )
        )
    }
    mass.player_queues = controller
    source = PlayerMedia(
        uri="provider://track/secret",
        media_type=MediaType.TRACK,
        title="Secret title",
        artist="Secret artist",
        album="Secret album",
        album_artist="Secret album artist",
        image_url="https://example.test/secret.jpg",
        palette=MagicMock(),
        duration=123,
        source_id="q1",
        queue_item_id="secret-id",
        elapsed_time=17,
        elapsed_time_last_updated=1234.5,
    )
    owner = object()
    controller.set_media_presentation("q1", owner, "Music Quiz")
    concealed = controller.apply_media_presentation("q1", source)
    controller.clear_media_presentation("q1", owner)
    revealed = controller.apply_media_presentation("q1", source)
    return mass, concealed, revealed


def _sendspin_player(mass: MagicMock, media: PlayerMedia) -> MagicMock:
    """Build the Sendspin collaborators used by metadata publication."""
    player = MagicMock()
    player.available = True
    player.state.current_media = media
    player.state.playback_state = PlaybackState.PLAYING
    player._metadata_role = MagicMock()
    player._controller_role = MagicMock()
    player._color_role = MagicMock()
    player._send_album_artwork = AsyncMock()
    player._send_artist_artwork = AsyncMock()
    player._compute_track_progress_ms.return_value = 17_000
    player._send_beat_schedule = AsyncMock()
    player.mass = mass
    return player


async def test_concealed_sendspin_metadata_is_neutral_without_relookup() -> None:
    """Direct Sendspin publication exposes only the neutral title before reveal."""
    mass, concealed, _ = _projected_media()
    player = _sendspin_player(mass, concealed)

    await SendspinPlayer.send_current_media_metadata(player)

    metadata = player._metadata_role.set_metadata.call_args.args[0]
    assert metadata.title == "Music Quiz"
    assert metadata.artist is None
    assert metadata.album is None
    assert metadata.album_artist is None
    assert metadata.artwork_url is None
    assert metadata.year is None
    assert metadata.track is None
    assert metadata.track_duration is None
    assert "secret" not in repr(metadata).lower()
    mass.music.get_library_item_by_prov_id.assert_not_called()
    player._send_artist_artwork.assert_awaited_once_with(None)
    player._send_color_palette.assert_called_once_with(player._color_role, None)
    player._send_beat_schedule.assert_awaited_once()
    queue, queue_item, *_ = player._send_beat_schedule.await_args.args
    assert queue is None
    assert queue_item is None


async def test_missing_queue_item_clears_stale_artist_artwork() -> None:
    """Concealed metadata clears artist artwork retained from the previous item."""
    player = MagicMock()
    player.last_sent_artist_artwork_url = "https://example.test/secret-artist.jpg"
    player._artwork_role = MagicMock()
    player._artwork_role.set_artist_artwork = AsyncMock()

    await SendspinPlayer._send_artist_artwork(player, None)

    assert player.last_sent_artist_artwork_url is None
    player._artwork_role.set_artist_artwork.assert_awaited_once_with(None)


async def test_reveal_and_late_listener_use_current_presentation_state() -> None:
    """Metadata becomes real after reveal while a late pre-reveal role remains neutral."""
    mass, concealed, revealed = _projected_media()
    player = _sendspin_player(mass, concealed)
    late_metadata_role = MagicMock()
    player._metadata_role = late_metadata_role

    await SendspinPlayer.send_current_media_metadata(player)

    late_metadata = late_metadata_role.set_metadata.call_args.args[0]
    assert late_metadata.title == "Music Quiz"
    assert late_metadata.artist is None
    assert late_metadata.track_duration is None

    player.state.current_media = revealed
    await SendspinPlayer.send_current_media_metadata(player)

    public_metadata = late_metadata_role.set_metadata.call_args.args[0]
    assert public_metadata.title == "Secret title"
    assert public_metadata.artist == "Secret artist"
    assert public_metadata.album == "Secret album"
    assert public_metadata.artwork_url == "https://example.test/secret.jpg"
    assert public_metadata.track_duration == 123_000
