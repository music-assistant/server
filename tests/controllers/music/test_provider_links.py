"""Public links for existing library mappings are derived without changing stored rows."""

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.controllers.music.media.albums import AlbumsController
from music_assistant.helpers.json import serialize_to_json


@pytest.mark.parametrize("stored_url", [None, "https://music.youtube.com/playlist?list=None"])
def test_library_album_links_are_derived_on_read(stored_url: str | None) -> None:
    """Full details and summaries both expose a link using the provider album ID."""
    controller = AlbumsController(MagicMock())
    raw_mappings = serialize_to_json(
        [
            {
                "item_id": "MPREb_pRx4LBJiFWR",
                "provider_domain": "ytmusic",
                "provider_instance": "ytmusic--account",
                "url": stored_url,
            }
        ]
    )
    row = {
        "item_id": 4,
        "favorite": None,
        "timestamp_added": 0,
        "provider_mappings": raw_mappings,
    }
    expected = "https://music.youtube.com/browse/MPREb_pRx4LBJiFWR"

    details = controller._parse_db_row(row, set())
    summaries = controller._parse_summary_provider_mappings(row)

    assert controller.media_type == MediaType.ALBUM
    assert details["provider_mappings"][0]["url"] == expected
    assert next(iter(summaries)).url == expected
    assert row["provider_mappings"] == raw_mappings
