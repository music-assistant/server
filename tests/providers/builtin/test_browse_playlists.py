"""Tests for browsing the playlists of the builtin provider."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.media_items import Playlist, ProviderMapping

from music_assistant.providers.builtin import BuiltinProvider


def _make_provider(playlists_dir: Path, library_playlists: list[Playlist]) -> BuiltinProvider:
    """Create a minimal BuiltinProvider whose library lists the given playlists."""
    prov = object.__new__(BuiltinProvider)
    mass: Any = MagicMock()
    mass.music.playlists.library_items = AsyncMock(return_value=library_playlists)
    prov.mass = mass
    prov.logger = MagicMock()
    prov.manifest = MagicMock(domain="builtin")
    prov.config = MagicMock(instance_id="builtin_1")
    prov._playlists_dir = str(playlists_dir)
    prov._playlist_lock = asyncio.Lock()
    return prov


@pytest.mark.asyncio
async def test_browse_playlists_never_falls_back_to_the_files_on_disk(tmp_path: Path) -> None:
    """An empty (access-filtered) listing is served as is, the M3U files stay hidden."""
    (tmp_path / "private.m3u").write_text("#EXTM3U\n#PLAYLIST:Private\n", encoding="utf-8")
    prov = _make_provider(tmp_path, [])

    assert await prov.browse("builtin_1://playlists") == []
    listing = prov.mass.music.playlists.library_items
    assert isinstance(listing, AsyncMock)
    listing.assert_awaited_once_with(provider="builtin_1", summary=False)


@pytest.mark.asyncio
async def test_browse_playlists_serves_the_library_listing(tmp_path: Path) -> None:
    """The playlists the caller may see come straight from the library listing."""
    playlist = Playlist(
        item_id="1",
        provider="library",
        name="Mine",
        provider_mappings={
            ProviderMapping(
                item_id="mine", provider_domain="builtin", provider_instance="builtin_1"
            )
        },
    )
    prov = _make_provider(tmp_path, [playlist])

    assert await prov.browse("builtin_1://playlists") == [playlist]
