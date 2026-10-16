"""Tests for removing a single playlist item right away."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.auth import User, UserRole

from music_assistant.controllers.music.media.playlists import PlaylistController

MEMBER = User(user_id="user-member", username="member", role=UserRole.USER)


async def test_remove_playlist_track_runs_without_a_background_task() -> None:
    """The item is removed in the call itself, as the calling user, and no task is queued."""
    playlists = PlaylistController.__new__(PlaylistController)
    playlists.mass = MagicMock()
    with (
        patch(
            "music_assistant.controllers.music.media.playlists.get_current_user",
            return_value=MEMBER,
        ),
        patch.object(
            PlaylistController, "_handle_remove_playlist_tracks", new_callable=AsyncMock
        ) as handle_remove,
    ):
        await playlists.remove_playlist_track("12", 3)
    handle_remove.assert_awaited_once_with("12", (3,), MEMBER.user_id)
    playlists.mass.tasks.run_background_task.assert_not_called()
