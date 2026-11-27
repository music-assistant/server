"""Tests for what a DLNA player tells about the streams it fetches."""

from __future__ import annotations

from music_assistant.constants import PlaybackQuality
from music_assistant.providers.dlna.player import DLNAPlayer


def test_a_renderer_fetches_files() -> None:
    """A renderer is built around files, so it gets a track as a file where that is possible."""
    player = DLNAPlayer.__new__(DLNAPlayer)

    assert player.fetches_files is True


def test_a_renderer_needs_no_content_length() -> None:
    """A stream that is not a file reaches a renderer without a made-up length."""
    player = DLNAPlayer.__new__(DLNAPlayer)

    assert player.requires_content_length is False


def test_a_renderer_defaults_to_lossless_uncompressed() -> None:
    """A renderer the user picked no playback quality for is streamed lossless, uncompressed."""
    player = DLNAPlayer.__new__(DLNAPlayer)

    assert player.default_playback_quality is PlaybackQuality.LOSSLESS_UNCOMPRESSED
