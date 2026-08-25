"""Tests for PlaylistController.import_playlist's matching hand-off."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.media_items import Playlist, ProviderMapping

from music_assistant.controllers.music.media.playlists import (
    PlaylistController,
    PlaylistMatchPolicy,
)


def _make_controller() -> PlaylistController:
    """Create a minimal PlaylistController with a mocked mass, bypassing __init__."""
    ctrl = object.__new__(PlaylistController)
    ctrl.mass = MagicMock()
    return ctrl


def _make_playlist(item_id: str = "playlist_1") -> Playlist:
    """Build a minimal builtin Playlist."""
    return Playlist(
        item_id=item_id,
        provider="builtin",
        name="Imported Playlist",
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="builtin", provider_instance="builtin")
        },
    )


def _make_provider_mock(instance_id: str, domain: str) -> MagicMock:
    """Build a mock provider exposing instance_id and domain."""
    provider = MagicMock()
    provider.instance_id = instance_id
    provider.domain = domain
    return provider


async def test_import_without_match_policy_skips_background_task() -> None:
    """No background task is scheduled when match_policy is omitted."""
    ctrl = _make_controller()
    ctrl_any = cast("Any", ctrl)
    builtin_prov = MagicMock()
    builtin_prov.import_playlist = AsyncMock(return_value=_make_playlist())
    ctrl_any.mass.get_provider = MagicMock(return_value=builtin_prov)
    ctrl_any.add_item_to_library = AsyncMock(return_value=_make_playlist())

    with patch("music_assistant.controllers.music.media.playlists.MusicProvider", MagicMock):
        result = await ctrl.import_playlist("#EXTM3U\n")

    assert result.item_id == "playlist_1"
    ctrl_any.mass.tasks.run_background_task.assert_not_called()


async def test_import_with_match_policy_snapshots_allowed_providers() -> None:
    """The background task receives a frozen snapshot of the user's allowed providers."""
    ctrl = _make_controller()
    ctrl_any = cast("Any", ctrl)
    builtin_prov = MagicMock()
    builtin_prov.import_playlist = AsyncMock(return_value=_make_playlist())
    builtin_prov.match_imported_playlist_tracks = AsyncMock()
    ctrl_any.mass.get_provider = MagicMock(return_value=builtin_prov)
    ctrl_any.add_item_to_library = AsyncMock(return_value=_make_playlist())
    ctrl_any.mass.music.providers = [
        _make_provider_mock("spotify--1", "spotify"),
        _make_provider_mock("qobuz--1", "qobuz"),
    ]

    with (
        patch("music_assistant.controllers.music.media.playlists.MusicProvider", MagicMock),
        patch(
            "music_assistant.controllers.music.media.playlists.get_current_user",
            return_value=None,
        ),
    ):
        await ctrl.import_playlist("#EXTM3U\n", match_policy=PlaylistMatchPolicy.SAME_RECORDING)

    ctrl_any.mass.tasks.run_background_task.assert_called_once()
    call_kwargs = ctrl_any.mass.tasks.run_background_task.call_args.kwargs
    assert call_kwargs["metadata"]["match_policy"] == "same_recording"

    # invoking the deferred handler must call the builtin provider with the snapshot,
    # independent of mass.music.providers at call time (simulating a later, unrelated user)
    ctrl_any.mass.music.providers = []
    await call_kwargs["handler"]()
    builtin_prov.match_imported_playlist_tracks.assert_awaited_once_with(
        "playlist_1",
        PlaylistMatchPolicy.SAME_RECORDING,
        ("qobuz--1", "spotify--1"),
    )


async def test_import_with_library_matching_true_defaults_to_best_effort() -> None:
    """The deprecated library_matching=True still schedules matching, at BEST_EFFORT."""
    ctrl = _make_controller()
    ctrl_any = cast("Any", ctrl)
    builtin_prov = MagicMock()
    builtin_prov.import_playlist = AsyncMock(return_value=_make_playlist())
    builtin_prov.match_imported_playlist_tracks = AsyncMock()
    ctrl_any.mass.get_provider = MagicMock(return_value=builtin_prov)
    ctrl_any.add_item_to_library = AsyncMock(return_value=_make_playlist())
    ctrl_any.mass.music.providers = [_make_provider_mock("qobuz--1", "qobuz")]

    with (
        patch("music_assistant.controllers.music.media.playlists.MusicProvider", MagicMock),
        patch(
            "music_assistant.controllers.music.media.playlists.get_current_user",
            return_value=None,
        ),
    ):
        await ctrl.import_playlist("#EXTM3U\n", library_matching=True)

    ctrl_any.mass.tasks.run_background_task.assert_called_once()
    call_kwargs = ctrl_any.mass.tasks.run_background_task.call_args.kwargs
    assert call_kwargs["metadata"]["match_policy"] == "best_effort"
    await call_kwargs["handler"]()
    builtin_prov.match_imported_playlist_tracks.assert_awaited_once_with(
        "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, ("qobuz--1",)
    )


async def test_import_with_explicit_match_policy_overrides_library_matching() -> None:
    """An explicit match_policy takes precedence over the deprecated library_matching flag."""
    ctrl = _make_controller()
    ctrl_any = cast("Any", ctrl)
    builtin_prov = MagicMock()
    builtin_prov.import_playlist = AsyncMock(return_value=_make_playlist())
    builtin_prov.match_imported_playlist_tracks = AsyncMock()
    ctrl_any.mass.get_provider = MagicMock(return_value=builtin_prov)
    ctrl_any.add_item_to_library = AsyncMock(return_value=_make_playlist())
    ctrl_any.mass.music.providers = [_make_provider_mock("qobuz--1", "qobuz")]

    with (
        patch("music_assistant.controllers.music.media.playlists.MusicProvider", MagicMock),
        patch(
            "music_assistant.controllers.music.media.playlists.get_current_user",
            return_value=None,
        ),
    ):
        await ctrl.import_playlist(
            "#EXTM3U\n", library_matching=True, match_policy=PlaylistMatchPolicy.EXACT
        )

    call_kwargs = ctrl_any.mass.tasks.run_background_task.call_args.kwargs
    assert call_kwargs["metadata"]["match_policy"] == "exact"


async def test_import_with_match_providers_narrows_snapshot() -> None:
    """match_providers narrows the snapshot to the requested instances/domains."""
    ctrl = _make_controller()
    ctrl_any = cast("Any", ctrl)
    builtin_prov = MagicMock()
    builtin_prov.import_playlist = AsyncMock(return_value=_make_playlist())
    builtin_prov.match_imported_playlist_tracks = AsyncMock()
    ctrl_any.mass.get_provider = MagicMock(return_value=builtin_prov)
    ctrl_any.add_item_to_library = AsyncMock(return_value=_make_playlist())
    ctrl_any.mass.music.providers = [
        _make_provider_mock("spotify--1", "spotify"),
        _make_provider_mock("qobuz--1", "qobuz"),
    ]

    with (
        patch("music_assistant.controllers.music.media.playlists.MusicProvider", MagicMock),
        patch(
            "music_assistant.controllers.music.media.playlists.get_current_user",
            return_value=None,
        ),
    ):
        await ctrl.import_playlist(
            "#EXTM3U\n",
            match_policy=PlaylistMatchPolicy.BEST_EFFORT,
            match_providers=["qobuz"],
        )

    call_kwargs = ctrl_any.mass.tasks.run_background_task.call_args.kwargs
    await call_kwargs["handler"]()
    builtin_prov.match_imported_playlist_tracks.assert_awaited_once_with(
        "playlist_1",
        PlaylistMatchPolicy.BEST_EFFORT,
        ("qobuz--1",),
    )
