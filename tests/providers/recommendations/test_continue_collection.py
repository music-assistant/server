"""Tests for the continue collection option of the library recommendations provider."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from music_assistant_models.auth import User, UserRole

from music_assistant.mass import MusicAssistant
from music_assistant.providers.recommendations import (
    CONF_CONTINUE_COLLECTION,
    SUPPORTED_FEATURES,
    LibraryRecommendationsProvider,
    LibraryRowID,
)
from tests.controllers.music.helpers import add_collection_audiobook

GET_CURRENT_USER = "music_assistant.controllers.music.controller.get_current_user"
USER_A = User(user_id="user-a", username="user-a", role=UserRole.USER)


@pytest.mark.parametrize(
    ("option", "row_listed", "in_progress_row", "own_row"),
    [
        (None, False, ["2"], []),
        ("off", False, ["2"], []),
        ("own_row", True, ["2"], ["4"]),
        ("in_progress_row", False, ["4", "2"], []),
    ],
)
async def test_continue_collection_option(
    music_mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    option: str | None,
    row_listed: bool,
    in_progress_row: list[str],
    own_row: list[str],
) -> None:
    """The option decides which row, if any, shows the next audiobook of a collection."""
    monkeypatch.setattr(music_mass.music, "get_active_provider_instances", lambda: ["local_1"])
    await add_collection_audiobook(music_mass, 1, 1, "finished", collection="Standalone")
    await add_collection_audiobook(music_mass, 2, 2, "in_progress", collection="Other", played_at=1)
    await add_collection_audiobook(music_mass, 3, 1, "finished", played_at=2)
    await add_collection_audiobook(music_mass, 4, 2)
    manifest = Mock()
    manifest.domain = "recommendations"
    config = Mock()
    config.instance_id = "recommendations"
    values = {"log_level": "INFO"} | ({CONF_CONTINUE_COLLECTION: option} if option else {})
    config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    provider = LibraryRecommendationsProvider(music_mass, manifest, config, SUPPORTED_FEATURES)

    entries = await provider.get_config_entries()
    rows = [row.item_id for row in await provider.get_recommendations()]
    with patch(GET_CURRENT_USER, return_value=USER_A):
        in_progress_items = await provider.get_recommendation_items(LibraryRowID.IN_PROGRESS)
        own_row_items = await provider.get_recommendation_items(LibraryRowID.CONTINUE_COLLECTION)

    assert [(x.key, x.default_value) for x in entries] == [(CONF_CONTINUE_COLLECTION, "off")]
    assert (LibraryRowID.CONTINUE_COLLECTION in rows) is row_listed
    assert [item.item_id for item in in_progress_items] == in_progress_row
    assert [item.item_id for item in own_row_items] == own_row
