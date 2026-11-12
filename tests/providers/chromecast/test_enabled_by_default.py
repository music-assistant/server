"""Tests for the Chromecast players that are disabled by default."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pychromecast.const import CAST_TYPE_AUDIO, CAST_TYPE_CHROMECAST, CAST_TYPE_GROUP

from music_assistant.providers.chromecast.helpers import ChromecastInfo
from music_assistant.providers.chromecast.player import ChromecastPlayer


@pytest.mark.parametrize(
    ("friendly_name", "cast_type", "expected"),
    [
        ("Living Room TV", CAST_TYPE_CHROMECAST, False),
        ("TV Room", CAST_TYPE_AUDIO, True),
        ("TV Room speakers", CAST_TYPE_GROUP, True),
        ("Living Room TV", None, True),
    ],
    ids=["video-device", "speaker", "group", "unknown-type"],
)
def test_only_video_devices_are_disabled_by_their_name(
    friendly_name: str, cast_type: str | None, expected: bool
) -> None:
    """A TV name disables a video device by default, never a speaker, a group or an unknown type."""
    cast_info = ChromecastInfo(
        services=set(),
        uuid=uuid4(),
        model_name="Test Model",
        friendly_name=friendly_name,
        host="10.0.0.10",
        port=8009,
        cast_type=cast_type,
        manufacturer="Test Manufacturer",
    )
    player = ChromecastPlayer(MagicMock(), str(cast_info.uuid), cast_info)

    assert player.enabled_by_default is expected
