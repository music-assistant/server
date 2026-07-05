"""
Tests for TracksController.similar_tracks provider dispatch.

Guards the core contract: similarity sources are consulted by the provider's
``priority`` attribute (lower = more preferred, the track's own music provider
wins ties), later sources top up the remainder, and the combined result is
de-duplicated up to the limit. A plugin that claims a lower priority (e.g.
AudioMuse-AI at 25) therefore leads the track's own music provider (default 50).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ProviderFeature

from music_assistant.controllers.music.media.tracks import TracksController
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    import pytest

SEED_URI = "library://track/seed"


def _track(uri: str) -> MagicMock:
    track = MagicMock()
    track.uri = uri
    track.duration = 200
    return track


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    plugin_tracks: list[MagicMock],
    music_tracks: list[MagicMock],
) -> tuple[TracksController, Any, Any]:
    """
    Build a real TracksController wired to one plugin and one music provider.

    The provider mocks use spec= so the isinstance checks inside similar_tracks
    treat them exactly like real PluginProvider / MusicProvider instances.
    """
    ref_item = _track(SEED_URI)
    ref_item.provider = "library"
    ref_item.provider_mappings = [
        SimpleNamespace(provider_instance="jellyfin--1", item_id="jf-seed", quality=10)
    ]

    plugin = MagicMock(spec=PluginProvider)
    plugin.get_similar_tracks = AsyncMock(return_value=plugin_tracks)
    # claim priority over the music provider's default of 50, as AudioMuse-AI does
    plugin.priority = 25

    music = MagicMock(spec=MusicProvider)
    music.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    music.get_similar_tracks = AsyncMock(return_value=music_tracks)

    ctrl = TracksController.__new__(TracksController)
    mass = MagicMock()
    mass.get_providers_supporting_feature = MagicMock(return_value=[plugin])
    mass.get_provider = MagicMock(return_value=music)
    ctrl.mass = mass
    monkeypatch.setattr(ctrl, "get", AsyncMock(return_value=ref_item))
    return ctrl, plugin, music


async def test_plugin_results_first_music_provider_tops_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plugin picks lead the result; the music provider fills the remainder, deduped."""
    ctrl, plugin, music = _controller(
        monkeypatch,
        plugin_tracks=[_track("t/p1"), _track("t/p2"), _track("t/shared")],
        music_tracks=[_track("t/shared"), _track(SEED_URI), _track("t/m1")],
    )

    result = await ctrl.similar_tracks("seed", "library", limit=10)

    # plugin first, top-up after; 'shared' deduped, the seed itself dropped
    assert [t.uri for t in result] == ["t/p1", "t/p2", "t/shared", "t/m1"]
    plugin.get_similar_tracks.assert_awaited_once()
    music.get_similar_tracks.assert_awaited_once_with(prov_track_id="jf-seed", limit=10)


async def test_plugin_satisfying_limit_skips_music_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the plugin already fills the limit, the music provider is not queried."""
    ctrl, _plugin, music = _controller(
        monkeypatch,
        plugin_tracks=[_track("t/p1"), _track("t/p2"), _track("t/p3")],
        music_tracks=[_track("t/m1")],
    )

    result = await ctrl.similar_tracks("seed", "library", limit=3)

    assert [t.uri for t in result] == ["t/p1", "t/p2", "t/p3"]
    music.get_similar_tracks.assert_not_awaited()


async def test_no_plugin_falls_back_to_music_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without any plugin provider the music provider still serves results."""
    ctrl, _plugin, music = _controller(
        monkeypatch,
        plugin_tracks=[],
        music_tracks=[_track("t/m1"), _track("t/m2")],
    )
    monkeypatch.setattr(ctrl.mass, "get_providers_supporting_feature", MagicMock(return_value=[]))

    result = await ctrl.similar_tracks("seed", "library", limit=10)

    assert [t.uri for t in result] == ["t/m1", "t/m2"]
    music.get_similar_tracks.assert_awaited_once()


async def test_default_priority_keeps_music_provider_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin at the default priority does NOT beat the track's own music provider."""
    ctrl, plugin, _music = _controller(
        monkeypatch,
        plugin_tracks=[_track("t/p1")],
        music_tracks=[_track("t/m1"), _track("t/m2")],
    )
    plugin.priority = 50  # no explicit claim -> same as the music-provider default

    result = await ctrl.similar_tracks("seed", "library", limit=10)

    assert [t.uri for t in result] == ["t/m1", "t/m2", "t/p1"]
