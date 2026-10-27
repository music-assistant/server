"""Tests for the Chromecast HTTP profile default per receiver model."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from pychromecast.const import CAST_TYPE_AUDIO, CAST_TYPE_CHROMECAST, CAST_TYPE_GROUP

from music_assistant.constants import CONF_HTTP_PROFILE
from music_assistant.providers.chromecast.helpers import ChromecastInfo, http_profile_entry
from music_assistant.providers.chromecast.player import ChromecastPlayer


def _info(
    model_name: str,
    cast_type: str = CAST_TYPE_AUDIO,
    is_multichannel_group: bool = False,
    manufacturer: str | None = "Google Inc.",
) -> ChromecastInfo:
    """Build the info of a Cast device of the given model."""
    return ChromecastInfo(
        services=set(),
        uuid=uuid4(),
        model_name=model_name,
        friendly_name=model_name,
        host="10.0.0.10",
        port=8009,
        cast_type=cast_type,
        is_multichannel_group=is_multichannel_group,
        manufacturer=manufacturer,
    )


@pytest.mark.parametrize(
    ("cast_info", "expected"),
    [
        (_info("Google Nest Mini"), "forced_content_length"),
        (_info("Google Home Speaker"), "no_content_length"),
        (_info("Google Cast Group", CAST_TYPE_GROUP), "no_content_length"),
        (
            _info("Google Cast Group", CAST_TYPE_GROUP, is_multichannel_group=True),
            "no_content_length",
        ),
        (
            _info("SHIELD Android TV", CAST_TYPE_CHROMECAST, manufacturer="NVIDIA"),
            "no_content_length",
        ),
        (_info("Google Nest Hub", CAST_TYPE_CHROMECAST), "forced_content_length"),
    ],
    ids=["nest-mini", "home-speaker", "group", "stereo-pair", "android-tv", "nest-hub"],
)
def test_http_profile_default_follows_model(cast_info: ChromecastInfo, expected: str) -> None:
    """Groups, stereo pairs, Android TV and a Google Home Speaker default to no Content-Length."""
    assert http_profile_entry(cast_info).default_value == expected


async def test_group_config_entries_default_to_no_content_length() -> None:
    """A Cast group defaults to no Content-Length whatever its members are."""
    group_info = _info("Google Cast Group", CAST_TYPE_GROUP)
    with patch("music_assistant.providers.chromecast.player.CastStatusListener"):
        group = ChromecastPlayer(MagicMock(), str(group_info.uuid), group_info, MagicMock())

    entries = await group.get_config_entries()

    entry = next(entry for entry in entries if entry.key == CONF_HTTP_PROFILE)
    assert entry.default_value == "no_content_length"
