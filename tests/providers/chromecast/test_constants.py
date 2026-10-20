"""Tests for Chromecast provider constants."""

from __future__ import annotations

from music_assistant.constants import CONF_HTTP_PROFILE
from music_assistant.providers.chromecast.constants import CAST_PLAYER_CONFIG_ENTRIES


def test_chromecast_defaults_to_http_profile_without_content_length() -> None:
    """Cast players default to no Content-Length, newer receivers stop at ~24 MiB with one."""
    entry = next(entry for entry in CAST_PLAYER_CONFIG_ENTRIES if entry.key == CONF_HTTP_PROFILE)

    assert entry.default_value == "no_content_length"
