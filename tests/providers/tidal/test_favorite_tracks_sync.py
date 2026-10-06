"""Tests for the library sync of the virtual Tidal favorite tracks playlist."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest

from music_assistant.controllers.music.media.base import SUPPRESS_MEDIA_ITEM_UPDATES
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.tidal.parsers import parse_favorite_tracks_playlist

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.media_items import Playlist

    from music_assistant.mass import MusicAssistant

# the server fixture is module scoped, so the tests must share its event loop
pytestmark = pytest.mark.asyncio(loop_scope="module")


def _tidal_account(instance_id: str, profile_name: str) -> Mock:
    """Return a mock Tidal provider instance logged in to the given account."""
    provider = Mock()
    provider.domain = "tidal"
    provider.instance_id = instance_id
    provider.auth.user_id = instance_id
    provider.auth.user.profile_name = profile_name
    return provider


async def _sync(mass: MusicAssistant, instance_id: str, prov_item: Playlist) -> None:
    """Run the playlist library sync of a Tidal instance that lists the given playlist."""
    provider = MusicProvider.__new__(MusicProvider)
    provider.mass = mass
    provider.config = Mock(instance_id=instance_id, get_value=Mock(return_value=[]))
    provider.manifest = Mock(domain="tidal")
    provider.logger = Mock()

    async def get_library_playlists() -> AsyncGenerator[Playlist]:
        yield prov_item

    token = SUPPRESS_MEDIA_ITEM_UPDATES.set(True)
    try:
        with patch.multiple(
            provider,
            get_library_playlists=get_library_playlists,
            _update_sync_task_item_status=Mock(),
            _handle_sync_item_failure=Mock(),
        ):
            await provider._sync_library_playlists()
    finally:
        SUPPRESS_MEDIA_ITEM_UPDATES.reset(token)


async def test_favorite_tracks_of_two_accounts_stay_separate(
    music_mass_module: MusicAssistant,
) -> None:
    """Each Tidal account gets its own favorite tracks playlist in the library."""
    playlists = music_mass_module.music.playlists
    fav_a = parse_favorite_tracks_playlist(_tidal_account("tidal--a", "Alice"))
    fav_b = parse_favorite_tracks_playlist(_tidal_account("tidal--b", "Bob"))

    await _sync(music_mass_module, "tidal--a", fav_a)
    await _sync(music_mass_module, "tidal--b", fav_b)

    lib_a = await playlists.get_library_item_by_prov_id(fav_a.item_id, "tidal--a")
    lib_b = await playlists.get_library_item_by_prov_id(fav_b.item_id, "tidal--b")
    assert lib_a is not None
    assert lib_b is not None
    assert lib_a.item_id != lib_b.item_id
    assert lib_a.owner == "Alice"
    assert lib_b.owner == "Bob"
