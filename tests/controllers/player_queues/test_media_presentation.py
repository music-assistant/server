"""Tests for runtime queue media presentation leases."""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.errors import InvalidCommand, PlayerUnavailableError
from music_assistant_models.media_items import MediaItemImage, ProviderMapping, Track
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


class _AlwaysEqual:
    """Token whose equality cannot be used to establish lease ownership."""

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return 0


def _controller() -> tuple[PlayerQueuesController, MagicMock]:
    """Build a controller with one queue and owner player."""
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.mass = MagicMock()
    player = MagicMock()
    player.state.active_group = None
    player.state.synced_to = None
    controller.mass.players.get_player.return_value = player
    controller._managed_pool = MagicMock()
    controller._queue_data = {
        "q1": PlayerQueueData(
            queue=PlayerQueue(
                queue_id="q1",
                active=True,
                display_name="Queue",
                available=True,
                items=0,
            )
        )
    }
    return controller, player


def _media(uri: str = "provider://track/secret", queue_item_id: str = "secret-id") -> PlayerMedia:
    """Build fully populated media whose descriptive fields must be concealed."""
    return PlayerMedia(
        uri=uri,
        media_type=MediaType.TRACK,
        title="Secret title",
        artist="Secret artist",
        album="Secret album",
        album_artist="Secret album artist",
        image_url="https://example.test/secret.jpg",
        palette=MagicMock(),
        duration=123,
        source_id="q1",
        queue_item_id=queue_item_id,
        custom_data={"session_id": "session", "original_uri": uri},
        elapsed_time=17,
        elapsed_time_last_updated=1234.5,
    )


def _track_queue_item() -> QueueItem:
    """Build a track queue item with artwork."""
    track = Track(
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
    track.metadata.images = UniqueList(
        [
            MediaItemImage(
                type=ImageType.THUMB,
                path="secret.jpg",
                provider="test",
                remotely_accessible=False,
            )
        ]
    )
    return QueueItem.from_media_item("q1", track)


def test_set_requires_existing_queue() -> None:
    """Setting a presentation on an unknown queue fails precisely."""
    controller, _ = _controller()

    with pytest.raises(PlayerUnavailableError, match="Queue missing is not available"):
        controller.set_media_presentation("missing", object(), "Music Quiz")


def test_owner_identity_conflict_and_clear_isolation() -> None:
    """Only the identical owner token can replace or clear an active lease."""
    controller, player = _controller()
    owner = _AlwaysEqual()
    other_owner = _AlwaysEqual()

    controller.set_media_presentation("q1", owner, "Music Quiz")

    with pytest.raises(InvalidCommand, match="active media presentation"):
        controller.set_media_presentation("q1", other_owner, "Other")
    assert controller.has_media_presentation("q1", owner) is True
    assert controller.has_media_presentation("q1", other_owner) is False
    assert controller.clear_media_presentation("q1", other_owner) is False
    assert controller.clear_media_presentation("missing", owner) is False
    assert controller.clear_media_presentation("q1", owner) is True
    assert controller.clear_media_presentation("q1", owner) is False
    assert player.refresh_state.call_count == 2
    get_player = cast("MagicMock", controller.mass.players.get_player)
    get_player.assert_called_with("q1")


def test_same_owner_rotates_identity_and_queue_removal_drops_lease() -> None:
    """A round reset rotates the key and removing the queue drops runtime state."""
    controller, player = _controller()
    owner = object()

    controller.set_media_presentation("q1", owner, "Music Quiz")
    first_key = controller._queue_data["q1"].media_presentation
    controller.set_media_presentation("q1", owner, "Music Quiz")
    second_key = controller._queue_data["q1"].media_presentation

    assert first_key is not None
    assert second_key is not None
    assert first_key.identity_key != second_key.identity_key
    assert player.refresh_state.call_count == 2

    controller.on_player_remove("q1", permanent=False)

    assert controller.has_media_presentation("q1", owner) is False


def test_presentation_refreshes_redirected_playback_owner() -> None:
    """Presentation changes refresh the parent that publishes the queue media."""
    controller, queue_player = _controller()
    parent_player = MagicMock()
    queue_player.state.active_group = None
    queue_player.state.synced_to = "parent"
    get_player = cast("MagicMock", controller.mass.players.get_player)
    get_player.side_effect = lambda player_id: queue_player if player_id == "q1" else parent_player

    owner = object()
    controller.set_media_presentation("q1", owner, "Music Quiz")
    controller.clear_media_presentation("q1", owner)

    assert parent_player.refresh_state.call_count == 2
    queue_player.refresh_state.assert_not_called()


def test_default_projection_returns_fresh_equivalent_media() -> None:
    """Queues without a lease retain the complete media presentation."""
    controller, _ = _controller()
    source = _media()

    projected = controller.apply_media_presentation("q1", source)

    assert projected is not source
    for field in (
        "uri",
        "media_type",
        "title",
        "artist",
        "album",
        "album_artist",
        "image_url",
        "palette",
        "duration",
        "source_id",
        "queue_item_id",
        "elapsed_time",
        "elapsed_time_last_updated",
    ):
        assert getattr(projected, field) == getattr(source, field)
    assert projected.custom_data == source.custom_data
    assert projected.custom_data is not source.custom_data


def test_public_projection_is_opaque_stable_and_minimal() -> None:
    """Public media retains only a neutral title, opaque identity, and elapsed anchor."""
    controller, _ = _controller()
    owner = object()
    controller.set_media_presentation("q1", owner, "Music Quiz")

    first = controller.apply_media_presentation("q1", _media())
    repeated = controller.apply_media_presentation("q1", _media())
    next_item = controller.apply_media_presentation(
        "q1", _media("provider://track/other", "other-id")
    )

    assert first.uri == repeated.uri
    assert first.uri != next_item.uri
    assert first.uri.startswith("media-presentation://")
    assert "secret" not in first.uri
    assert first.media_type == MediaType.UNKNOWN
    assert first.title == "Music Quiz"
    assert first.elapsed_time == 17
    assert first.elapsed_time_last_updated == 1234.5
    for field in (
        "artist",
        "album",
        "album_artist",
        "image_url",
        "palette",
        "duration",
        "source_id",
        "queue_item_id",
        "custom_data",
    ):
        assert getattr(first, field) is None

    controller.set_media_presentation("q1", owner, "Music Quiz")
    rotated = controller.apply_media_presentation("q1", _media())
    assert rotated.uri != first.uri


def test_transport_projection_preserves_loading_fields_only() -> None:
    """Device media keeps transport/session fields while removing descriptive metadata."""
    controller, _ = _controller()
    controller.set_media_presentation("q1", object(), "Music Quiz")

    projected = controller.apply_media_presentation("q1", _media(), preserve_playback_data=True)

    assert projected.uri == "provider://track/secret"
    assert projected.media_type == MediaType.TRACK
    assert projected.title == "Music Quiz"
    assert projected.duration == 123
    assert projected.source_id == "q1"
    assert projected.queue_item_id == "secret-id"
    assert projected.custom_data == {
        "session_id": "session",
        "original_uri": "provider://track/secret",
    }
    assert projected.elapsed_time == 17
    assert projected.elapsed_time_last_updated == 1234.5
    assert projected.artist is None
    assert projected.album is None
    assert projected.album_artist is None
    assert projected.image_url is None
    assert projected.palette is None


async def test_outbound_queue_media_skips_artwork_resolution_when_concealed() -> None:
    """Concealed outbound media remains playable without resolving or exposing artwork."""
    controller, _ = _controller()
    queue_data = controller._queue_data["q1"]
    queue_data.session_id = "session"
    queue_item = _track_queue_item()
    controller.set_media_presentation("q1", object(), "Music Quiz")

    media = await controller.player_media_from_queue_item(queue_item)

    assert media.uri == queue_item.uri
    assert media.media_type == MediaType.TRACK
    assert media.title == "Music Quiz"
    assert media.duration == 123
    assert media.source_id == "q1"
    assert media.queue_item_id == queue_item.queue_item_id
    assert media.custom_data == {
        "session_id": "session",
        "original_uri": queue_item.uri,
    }
    assert media.artist is None
    assert media.album is None
    assert media.image_url is None
    metadata = cast("MagicMock", controller.mass.metadata)
    metadata.get_image_url.assert_not_called()


async def test_outbound_queue_media_is_unchanged_without_lease() -> None:
    """Default outbound media retains its existing descriptive metadata and artwork."""
    controller, _ = _controller()
    controller._queue_data["q1"].session_id = "session"
    metadata = cast("MagicMock", controller.mass.metadata)
    metadata.get_image_url.return_value = "https://stream.test/image.jpg"
    queue_item = _track_queue_item()

    media = await controller.player_media_from_queue_item(queue_item)

    assert media.uri == queue_item.uri
    assert media.media_type == MediaType.TRACK
    assert media.title == "Secret title"
    assert media.duration == 123
    assert media.source_id == "q1"
    assert media.queue_item_id == queue_item.queue_item_id
    assert media.image_url == "https://stream.test/image.jpg"
    metadata.get_image_url.assert_called_once()
