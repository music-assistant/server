"""Tests for editing playlist items right away."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.auth import User, UserRole

from music_assistant.controllers.music.media.playlists import PlaylistController

MEMBER = User(user_id="user-member", username="member", role=UserRole.USER)


async def test_edit_playlist_tracks_runs_without_a_background_task() -> None:
    """The items are removed, then added, in the call itself, as the calling user."""
    playlists = PlaylistController.__new__(PlaylistController)
    playlists.mass = MagicMock()
    calls = MagicMock()
    with (
        patch(
            "music_assistant.controllers.music.media.playlists.get_current_user",
            return_value=MEMBER,
        ),
        patch.object(
            PlaylistController, "_handle_remove_playlist_tracks", new_callable=AsyncMock
        ) as handle_remove,
        patch.object(
            PlaylistController, "_handle_add_playlist_tracks", new_callable=AsyncMock
        ) as handle_add,
    ):
        calls.attach_mock(handle_remove, "remove")
        calls.attach_mock(handle_add, "add")
        await playlists.edit_playlist_tracks("12", (5, 3), ["library://track/7"])
    assert [name for name, _, _ in calls.mock_calls] == ["remove", "add"]
    handle_remove.assert_awaited_once_with("12", (5, 3), MEMBER.user_id)
    handle_add.assert_awaited_once_with("12", ["library://track/7"], MEMBER.user_id)
    playlists.mass.tasks.run_background_task.assert_not_called()


async def test_edit_playlist_tracks_skips_the_empty_side() -> None:
    """Nothing to remove or nothing to add does not reach the provider."""
    playlists = PlaylistController.__new__(PlaylistController)
    playlists.mass = MagicMock()
    with (
        patch(
            "music_assistant.controllers.music.media.playlists.get_current_user",
            return_value=None,
        ),
        patch.object(
            PlaylistController, "_handle_remove_playlist_tracks", new_callable=AsyncMock
        ) as handle_remove,
        patch.object(
            PlaylistController, "_handle_add_playlist_tracks", new_callable=AsyncMock
        ) as handle_add,
    ):
        await playlists.edit_playlist_tracks("12", uris_to_add=["library://track/7"])
        await playlists.edit_playlist_tracks("12", (2,))
    handle_remove.assert_awaited_once_with("12", (2,), None)
    handle_add.assert_awaited_once_with("12", ["library://track/7"], None)
