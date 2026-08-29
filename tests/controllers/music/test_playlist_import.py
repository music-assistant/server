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


def _make_provider_config_mock(instance_id: str, domain: str, enabled: bool = True) -> MagicMock:
    """Build a mock ProviderConfig exposing instance_id, domain, and enabled."""
    config = MagicMock()
    config.instance_id = instance_id
    config.domain = domain
    config.enabled = enabled
    return config


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
    ctrl_any.mass.config.get_provider_configs = AsyncMock(
        return_value=[
            _make_provider_config_mock("spotify--1", "spotify"),
            _make_provider_config_mock("qobuz--1", "qobuz"),
        ]
    )

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
        (("qobuz--1", "qobuz"), ("spotify--1", "spotify")),
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
    ctrl_any.mass.config.get_provider_configs = AsyncMock(
        return_value=[_make_provider_config_mock("qobuz--1", "qobuz")]
    )

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
        "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, (("qobuz--1", "qobuz"),), ("qobuz--1",)
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
    ctrl_any.mass.config.get_provider_configs = AsyncMock(
        return_value=[_make_provider_config_mock("qobuz--1", "qobuz")]
    )

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


async def test_import_with_match_providers_narrows_search_only() -> None:
    """match_providers narrows only the search targets, not the source-validation snapshot."""
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
    ctrl_any.mass.config.get_provider_configs = AsyncMock(
        return_value=[
            _make_provider_config_mock("spotify--1", "spotify"),
            _make_provider_config_mock("qobuz--1", "qobuz"),
        ]
    )

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
    # source validation keeps the user's full snapshot (a playable spotify original must
    # not look unavailable just because match_providers narrows the search), while the
    # search target set is narrowed to the requested provider
    builtin_prov.match_imported_playlist_tracks.assert_awaited_once_with(
        "playlist_1",
        PlaylistMatchPolicy.BEST_EFFORT,
        (("qobuz--1", "qobuz"), ("spotify--1", "spotify")),
        ("qobuz--1",),
    )


async def test_import_with_empty_match_providers_narrows_search_to_nothing() -> None:
    """An explicit empty match_providers list deselects every provider, not every provider."""
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
    ctrl_any.mass.config.get_provider_configs = AsyncMock(
        return_value=[
            _make_provider_config_mock("spotify--1", "spotify"),
            _make_provider_config_mock("qobuz--1", "qobuz"),
        ]
    )

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
            match_providers=[],
        )

    call_kwargs = ctrl_any.mass.tasks.run_background_task.call_args.kwargs
    await call_kwargs["handler"]()
    # an explicit [] means "nothing selected", so the search target set must be empty,
    # while source validation still keeps the user's full snapshot
    builtin_prov.match_imported_playlist_tracks.assert_awaited_once_with(
        "playlist_1",
        PlaylistMatchPolicy.BEST_EFFORT,
        (("qobuz--1", "qobuz"), ("spotify--1", "spotify")),
        (),
    )


async def test_import_source_validation_includes_configured_but_unloaded_provider() -> None:
    """A configured provider that failed to load still counts for source validation."""
    ctrl = _make_controller()
    ctrl_any = cast("Any", ctrl)
    builtin_prov = MagicMock()
    builtin_prov.import_playlist = AsyncMock(return_value=_make_playlist())
    builtin_prov.match_imported_playlist_tracks = AsyncMock()
    ctrl_any.mass.get_provider = MagicMock(return_value=builtin_prov)
    ctrl_any.add_item_to_library = AsyncMock(return_value=_make_playlist())
    # spotify--1 failed setup (or is temporarily down) and is therefore not loaded,
    # but it is still configured and enabled
    ctrl_any.mass.music.providers = [_make_provider_mock("qobuz--1", "qobuz")]
    ctrl_any.mass.config.get_provider_configs = AsyncMock(
        return_value=[
            _make_provider_config_mock("spotify--1", "spotify"),
            _make_provider_config_mock("qobuz--1", "qobuz"),
        ]
    )

    with (
        patch("music_assistant.controllers.music.media.playlists.MusicProvider", MagicMock),
        patch(
            "music_assistant.controllers.music.media.playlists.get_current_user",
            return_value=None,
        ),
    ):
        await ctrl.import_playlist("#EXTM3U\n", match_policy=PlaylistMatchPolicy.BEST_EFFORT)

    call_kwargs = ctrl_any.mass.tasks.run_background_task.call_args.kwargs
    await call_kwargs["handler"]()
    # the unloaded spotify--1 is still part of the source-validation snapshot (so its
    # tracks are not mistaken for removed), but it is excluded from the search targets
    # since nothing can be searched on a provider that isn't loaded
    builtin_prov.match_imported_playlist_tracks.assert_awaited_once_with(
        "playlist_1",
        PlaylistMatchPolicy.BEST_EFFORT,
        (("qobuz--1", "qobuz"), ("spotify--1", "spotify")),
        ("qobuz--1",),
    )


async def test_import_source_validation_excludes_disabled_provider() -> None:
    """A disabled provider config is not treated as one of the user's own sources."""
    ctrl = _make_controller()
    ctrl_any = cast("Any", ctrl)
    builtin_prov = MagicMock()
    builtin_prov.import_playlist = AsyncMock(return_value=_make_playlist())
    builtin_prov.match_imported_playlist_tracks = AsyncMock()
    ctrl_any.mass.get_provider = MagicMock(return_value=builtin_prov)
    ctrl_any.add_item_to_library = AsyncMock(return_value=_make_playlist())
    ctrl_any.mass.music.providers = [_make_provider_mock("qobuz--1", "qobuz")]
    ctrl_any.mass.config.get_provider_configs = AsyncMock(
        return_value=[
            _make_provider_config_mock("spotify--1", "spotify", enabled=False),
            _make_provider_config_mock("qobuz--1", "qobuz"),
        ]
    )

    with (
        patch("music_assistant.controllers.music.media.playlists.MusicProvider", MagicMock),
        patch(
            "music_assistant.controllers.music.media.playlists.get_current_user",
            return_value=None,
        ),
    ):
        await ctrl.import_playlist("#EXTM3U\n", match_policy=PlaylistMatchPolicy.BEST_EFFORT)

    call_kwargs = ctrl_any.mass.tasks.run_background_task.call_args.kwargs
    await call_kwargs["handler"]()
    builtin_prov.match_imported_playlist_tracks.assert_awaited_once_with(
        "playlist_1",
        PlaylistMatchPolicy.BEST_EFFORT,
        (("qobuz--1", "qobuz"),),
        ("qobuz--1",),
    )


async def test_import_source_validation_respects_user_provider_filter() -> None:
    """A provider outside the requesting user's own filter is never part of the snapshot."""
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
    ctrl_any.mass.config.get_provider_configs = AsyncMock(
        return_value=[
            _make_provider_config_mock("spotify--1", "spotify"),
            _make_provider_config_mock("qobuz--1", "qobuz"),
        ]
    )
    user = MagicMock()
    user.provider_filter = {"qobuz--1"}

    with (
        patch("music_assistant.controllers.music.media.playlists.MusicProvider", MagicMock),
        patch(
            "music_assistant.controllers.music.media.playlists.get_current_user",
            return_value=user,
        ),
    ):
        await ctrl.import_playlist("#EXTM3U\n", match_policy=PlaylistMatchPolicy.BEST_EFFORT)

    call_kwargs = ctrl_any.mass.tasks.run_background_task.call_args.kwargs
    await call_kwargs["handler"]()
    # allowed_provider_instances (source validation) is built from provider configs and
    # explicitly narrowed by the user's own filter; search_provider_instances is read
    # straight from mass.music.providers, which already applies that same filter in
    # production - this double just returns both instances unfiltered
    builtin_prov.match_imported_playlist_tracks.assert_awaited_once_with(
        "playlist_1",
        PlaylistMatchPolicy.BEST_EFFORT,
        (("qobuz--1", "qobuz"),),
        ("qobuz--1", "spotify--1"),
    )
