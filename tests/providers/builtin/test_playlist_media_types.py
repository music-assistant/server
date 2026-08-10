"""Tests for the media types a builtin playlist advertises to clients."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.providers.builtin import BuiltinProvider


def _make_provider(playlists_dir: Path) -> BuiltinProvider:
    """Create a minimal BuiltinProvider that reads playlists from the given directory."""
    prov = object.__new__(BuiltinProvider)
    prov.mass = MagicMock()
    prov.logger = MagicMock()
    prov.manifest = MagicMock(domain="builtin")
    prov.config = MagicMock(instance_id="builtin_1")
    prov._playlists_dir = str(playlists_dir)
    prov._playlist_lock = asyncio.Lock()
    return prov


@pytest.mark.asyncio
async def test_user_playlist_does_not_advertise_sound_effect(tmp_path: Path) -> None:
    """
    A user created playlist must not offer SOUND_EFFECT in its supported media types.

    Clients that do not know this media type yet reject the entire playlist listing
    when they receive it.
    """
    (tmp_path / "my-playlist.m3u").write_text("#EXTM3U\n#PLAYLIST:My Playlist\n", encoding="utf-8")
    prov = _make_provider(tmp_path)

    playlist = await prov.get_playlist("my-playlist")

    assert playlist.supported_mediatypes == {
        MediaType.AUDIOBOOK,
        MediaType.PODCAST_EPISODE,
        MediaType.RADIO,
        MediaType.TRACK,
    }
