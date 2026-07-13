"""
Integration tests for the PlaylistController.

Uses a full MusicAssistant instance with a real SQLite database in a temporary
directory (mirroring ``tests/controllers/music/test_genres.py``) to verify that a
playlist's ``translation_key`` survives the library round-trip.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

import pytest
from music_assistant_models.media_items import Playlist, ProviderMapping

from music_assistant.controllers.music.media.playlists import PlaylistController
from music_assistant.mass import MusicAssistant


@pytest.fixture(scope="class")
async def mass(tmp_path_factory: pytest.TempPathFactory) -> AsyncGenerator[MusicAssistant]:
    """Class-scoped MusicAssistant instance (one per test class)."""
    tmp_path = tmp_path_factory.mktemp("playlist_tests")
    storage_path = tmp_path / "data"
    cache_path = tmp_path / "cache"
    storage_path.mkdir(parents=True)
    cache_path.mkdir(parents=True)
    logging.getLogger("aiosqlite").level = logging.INFO
    mass_instance = MusicAssistant(str(storage_path), str(cache_path))
    await mass_instance.start()
    try:
        yield mass_instance
    finally:
        await mass_instance.stop()


@pytest.fixture(scope="class")
async def playlist_ctrl(mass: MusicAssistant) -> PlaylistController:
    """Get the playlist controller from a running MusicAssistant instance."""
    return mass.music.playlists


def _make_playlist(
    item_id: str,
    name: str,
    *,
    translation_key: str | None = None,
    translation_params: list[str] | None = None,
) -> Playlist:
    """Create a provider-mapped Playlist for adding to the library."""
    return Playlist(
        item_id=item_id,
        provider="builtin",
        name=name,
        translation_key=translation_key,
        translation_params=translation_params,
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="builtin", provider_instance="builtin")
        },
        owner="Music Assistant",
        is_editable=False,
    )


class TestPlaylistTranslationKey:
    """
    The translation_key column survives the library round-trip (mirrors genres).

    Item ids and names must not collide with BUILTIN_PLAYLISTS: the builtin provider is
    auto-loaded by the mass fixture and its startup sync adds those playlists to the
    library with a translation_key already set, racing with (and matching) our rows.
    """

    async def test_parameterless_key_survives_round_trip(
        self, playlist_ctrl: PlaylistController
    ) -> None:
        """A static (parameterless) translation_key is persisted and read back."""
        created = await playlist_ctrl.add_item_to_library(
            _make_playlist("static_key_mix", "Static Key Mix", translation_key="static_key_mix")
        )
        fetched = await playlist_ctrl.get_library_item(int(created.item_id))
        assert fetched.translation_key == "static_key_mix"

    async def test_key_and_params_survive_round_trip(
        self, playlist_ctrl: PlaylistController
    ) -> None:
        """A key + translation_params (e.g. Spotify's per-account Liked Songs) both persist."""
        created = await playlist_ctrl.add_item_to_library(
            _make_playlist(
                "liked_songs",
                "Liked Songs Alice",
                translation_key="liked_songs",
                translation_params=["Alice"],
            )
        )
        fetched = await playlist_ctrl.get_library_item(int(created.item_id))
        assert fetched.translation_key == "liked_songs"
        assert fetched.translation_params == ["Alice"]

    async def test_update_backfills_key_on_resync(self, playlist_ctrl: PlaylistController) -> None:
        """A row added without a key adopts one when the provider later supplies it."""
        created = await playlist_ctrl.add_item_to_library(
            _make_playlist("backfill_mix", "Backfill Mix")
        )
        assert (await playlist_ctrl.get_library_item(int(created.item_id))).translation_key is None
        # re-sync: same provider item, now carrying a translation_key -> update path adopts it
        await playlist_ctrl.add_item_to_library(
            _make_playlist("backfill_mix", "Backfill Mix", translation_key="backfill_mix")
        )
        fetched = await playlist_ctrl.get_library_item(int(created.item_id))
        assert fetched.translation_key == "backfill_mix"
