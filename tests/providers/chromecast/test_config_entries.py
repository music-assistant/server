"""Tests for the config entries a Cast player offers."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pychromecast.const import CAST_TYPE_AUDIO, CAST_TYPE_GROUP

from music_assistant.constants import CONF_REQUIRES_FIXED_CONTENT_LENGTH
from music_assistant.providers.chromecast.helpers import ChromecastInfo
from music_assistant.providers.chromecast.player import ChromecastPlayer


@pytest.mark.parametrize(
    ("model_name", "cast_type"),
    [("Google Nest Mini", CAST_TYPE_AUDIO), ("Google Cast Group", CAST_TYPE_GROUP)],
    ids=["device", "group"],
)
async def test_no_fixed_content_length_entry(model_name: str, cast_type: str) -> None:
    """Cast receivers play fine without a fixed length, so the setting is not offered."""
    cast_info = ChromecastInfo(
        services=set(),
        uuid=uuid4(),
        model_name=model_name,
        friendly_name=model_name,
        host="10.0.0.10",
        port=8009,
        cast_type=cast_type,
        manufacturer="Google Inc.",
    )
    player = ChromecastPlayer(MagicMock(), str(cast_info.uuid), cast_info)

    entries = await player.get_config_entries()

    assert CONF_REQUIRES_FIXED_CONTENT_LENGTH not in {entry.key for entry in entries}
