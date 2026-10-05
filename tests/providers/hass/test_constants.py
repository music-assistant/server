"""Tests for the Home Assistant provider constants/helpers."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from music_assistant.providers.hass.constants import (
    MediaPlayerEntityFeature,
    parse_supported_features,
)

LOGGER = logging.getLogger("test.hass")


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (
            int(MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.VOLUME_SET),
            MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.VOLUME_SET,
        ),
        # a feature flag added in a newer Home Assistant version is kept as-is
        (
            int(MediaPlayerEntityFeature.PLAY_MEDIA) | 1 << 40,
            MediaPlayerEntityFeature(int(MediaPlayerEntityFeature.PLAY_MEDIA) | 1 << 40),
        ),
        (0, MediaPlayerEntityFeature(0)),
    ],
)
def test_valid_supported_features(raw_value: int, expected: MediaPlayerEntityFeature) -> None:
    """Values Home Assistant can report are translated to the matching feature flags."""
    assert parse_supported_features(raw_value, "media_player.test", LOGGER) == expected


@pytest.mark.parametrize(
    "raw_value",
    [
        # an entity that (currently) has no state reports no attributes at all
        None,
        # integrations are free to write anything into the attribute
        [],
        ["PLAY_MEDIA"],
        "512",
        True,
        -1,
    ],
)
def test_invalid_supported_features(raw_value: Any) -> None:
    """A value Music Assistant can not interpret is reported as 'no features supported'."""
    assert parse_supported_features(raw_value, "media_player.test", LOGGER) == (
        MediaPlayerEntityFeature(0)
    )


def test_invalid_supported_features_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    """An entity with an invalid value is named in the log so it can be traced back."""
    parse_supported_features([], "media_player.new_receiver", LOGGER)
    assert "media_player.new_receiver" in caplog.text


def test_missing_supported_features_are_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    """An entity without any state is not reported as misbehaving."""
    parse_supported_features(None, "media_player.new_receiver", LOGGER)
    assert not caplog.text
