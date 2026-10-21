"""Tests for replacing the description stored for a library item."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.media_items import MediaItemMetadata

from music_assistant.controllers.music.media.artists import ArtistsController
from music_assistant.helpers.json import json_loads, serialize_to_json


def _make_controller(metadata: MediaItemMetadata) -> ArtistsController:
    """Build an ArtistsController whose db row carries the given metadata."""
    controller = ArtistsController.__new__(ArtistsController)
    controller.mass = MagicMock()
    raw_metadata = serialize_to_json(metadata)
    controller.mass.music.database.get_row = AsyncMock(return_value={"metadata": raw_metadata})
    controller.mass.music.database.update = AsyncMock()
    controller.get_library_item = AsyncMock(return_value=MagicMock(uri="artist://1"))  # type: ignore[method-assign]
    return controller


async def test_update_description_replaces_a_stored_bio_in_the_same_language() -> None:
    """The new bio and its language replace the stored ones; other metadata stays."""
    controller = _make_controller(
        MediaItemMetadata(description="Wikipedia bio", description_language="en", genres={"Rock"})
    )

    await controller.update_description(1, "Own bio", "en")

    update_call = cast("MagicMock", controller.mass.music.database.update).call_args
    stored = MediaItemMetadata.from_dict(json_loads(update_call.args[2]["metadata"]))
    assert (stored.description, stored.description_language) == ("Own bio", "en")
    assert stored.genres == {"Rock"}
    cast("MagicMock", controller.mass.signal_event).assert_called_once()


async def test_update_description_with_the_stored_bio_is_a_noop() -> None:
    """An unchanged bio writes and signals nothing."""
    controller = _make_controller(
        MediaItemMetadata(description="Own bio", description_language="en")
    )

    await controller.update_description(1, "Own bio", "en")

    cast("AsyncMock", controller.mass.music.database.update).assert_not_called()
    cast("MagicMock", controller.mass.signal_event).assert_not_called()
