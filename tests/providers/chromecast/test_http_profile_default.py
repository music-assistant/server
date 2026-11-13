"""Tests for the Chromecast HTTP profile default."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from pychromecast.const import CAST_TYPE_AUDIO, CAST_TYPE_CHROMECAST, CAST_TYPE_GROUP

from music_assistant.constants import CONF_HTTP_PROFILE
from music_assistant.providers.chromecast.helpers import ChromecastInfo
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
    "cast_info",
    [
        _info("Google Nest Mini"),
        _info("Google Home Speaker"),
        _info("Google Nest Hub", CAST_TYPE_CHROMECAST),
        _info("Music Frame", manufacturer="Samsung"),
        _info("SHIELD Android TV", CAST_TYPE_CHROMECAST, manufacturer="NVIDIA"),
        _info("Google Cast Group", CAST_TYPE_GROUP),
        _info("Google Cast Group", CAST_TYPE_GROUP, is_multichannel_group=True),
    ],
    ids=[
        "nest-mini",
        "home-speaker",
        "nest-hub",
        "cast-lite",
        "android-tv",
        "group",
        "stereo-pair",
    ],
)
async def test_cast_players_default_to_no_content_length(cast_info: ChromecastInfo) -> None:
    """
    Every Cast receiver, group and stereo pair defaults to a stream without a fixed length.

    With a length, receivers fetch the stream in windows (10 MiB on Cast Lite speakers, about
    24 MiB on Google and Android TV receivers), and a stream can not continue at a byte offset.
    """
    with patch("music_assistant.providers.chromecast.player.CastStatusListener"):
        player = ChromecastPlayer(MagicMock(), str(cast_info.uuid), cast_info, MagicMock())

    entries = await player.get_config_entries()

    entry = next(entry for entry in entries if entry.key == CONF_HTTP_PROFILE)
    assert entry.default_value == "no_content_length"
