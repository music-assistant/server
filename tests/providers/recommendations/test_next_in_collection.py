"""Tests for the next in collection option of the library recommendations provider."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from music_assistant.providers.recommendations import (
    CONF_NEXT_IN_COLLECTION,
    SUPPORTED_FEATURES,
    LibraryRecommendationsProvider,
    LibraryRowID,
    NextInCollectionMode,
)


@pytest.mark.parametrize(
    ("mode", "expected_rows"),
    [
        (NextInCollectionMode.OFF, [LibraryRowID.IN_PROGRESS, LibraryRowID.RECENTLY_PLAYED]),
        (
            NextInCollectionMode.IN_PROGRESS_ROW,
            [LibraryRowID.IN_PROGRESS, LibraryRowID.RECENTLY_PLAYED],
        ),
        (
            NextInCollectionMode.OWN_ROW,
            [LibraryRowID.IN_PROGRESS, LibraryRowID.NEXT_IN_COLLECTION],
        ),
    ],
)
async def test_continue_collection_row_follows_mode(
    mode: NextInCollectionMode, expected_rows: list[LibraryRowID]
) -> None:
    """Only the own row mode lists the Continue collection row, right after In progress."""
    manifest = Mock()
    manifest.domain = "recommendations"
    config = Mock()
    config.instance_id = "recommendations"
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "INFO",
        CONF_NEXT_IN_COLLECTION: mode.value,
    }.get(key, default)
    provider = LibraryRecommendationsProvider(Mock(), manifest, config, SUPPORTED_FEATURES)

    rows = await provider.get_recommendations()

    assert [row.item_id for row in rows[:2]] == expected_rows
