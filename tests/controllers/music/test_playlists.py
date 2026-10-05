"""
Integration tests for the PlaylistController.

Uses a database-only MusicAssistant instance with a real SQLite database in a
temporary directory to verify that a
playlist's ``translation_key`` survives the library round-trip.
"""

from __future__ import annotations

import pytest
from music_assistant_models.media_items import Playlist, ProviderMapping

from music_assistant.controllers.music.media.playlists import PlaylistController
from music_assistant.mass import MusicAssistant


@pytest.fixture(scope="class", name="mass")
def mass_fixture(music_mass_class: MusicAssistant) -> MusicAssistant:
    """Return the class-scoped database-only Music Assistant fixture."""
    return music_mass_class


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
    """The translation_key column survives the library round-trip (mirrors genres)."""

    async def test_parameterless_key_survives_round_trip(
        self, playlist_ctrl: PlaylistController
    ) -> None:
        """A static (parameterless) translation_key is persisted and read back."""
        created = await playlist_ctrl.add_item_to_library(
            _make_playlist("infinite_mix", "Infinite Mix (library)", translation_key="infinite_mix")
        )
        fetched = await playlist_ctrl.get_library_item(int(created.item_id))
        assert fetched.translation_key == "infinite_mix"

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
            _make_playlist("recently_played", "Recently played tracks")
        )
        assert (await playlist_ctrl.get_library_item(int(created.item_id))).translation_key is None
        # re-sync: same provider item, now carrying a translation_key -> update path adopts it
        await playlist_ctrl.add_item_to_library(
            _make_playlist(
                "recently_played", "Recently played tracks", translation_key="recently_played"
            )
        )
        fetched = await playlist_ctrl.get_library_item(int(created.item_id))
        assert fetched.translation_key == "recently_played"
