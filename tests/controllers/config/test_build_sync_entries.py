"""Tests for the library-sync config-entry builder in the config controller."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature, ProviderType

from music_assistant.constants import (
    CONF_ENTRY_LIBRARY_SYNC_ARTISTS,
    CONF_ENTRY_LIBRARY_SYNC_BACK,
    CONF_ENTRY_LIBRARY_SYNC_DELETIONS,
    CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
)
from music_assistant.controllers.config import ConfigController
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry


def _controller() -> ConfigController:
    """Build a bare ConfigController (the builder is pure and needs no mass)."""
    return ConfigController.__new__(ConfigController)


def _keys(entries: list[ConfigEntry]) -> set[str]:
    return {entry.key for entry in entries}


def test_non_music_provider_has_no_sync_entries() -> None:
    """Sync options only apply to music providers."""
    manifest = SimpleNamespace(type=ProviderType.PLAYER)

    entries = _controller()._build_sync_entries(
        manifest, {ProviderFeature.LIBRARY_TRACKS}, provider=None
    )

    assert entries == []


def test_library_features_map_to_sync_entries() -> None:
    """Each supported library feature contributes its matching sync entry."""
    manifest = SimpleNamespace(type=ProviderType.MUSIC)
    features = {ProviderFeature.LIBRARY_ARTISTS, ProviderFeature.LIBRARY_PODCASTS}

    entries = _controller()._build_sync_entries(manifest, features, provider=None)

    keys = _keys(entries)
    assert CONF_ENTRY_LIBRARY_SYNC_ARTISTS.key in keys
    assert CONF_ENTRY_LIBRARY_SYNC_PODCASTS.key in keys
    assert CONF_ENTRY_LIBRARY_SYNC_TRACKS.key not in keys


def test_edit_feature_adds_sync_back_entry() -> None:
    """Any *_EDIT feature surfaces the sync-back (export) option."""
    manifest = SimpleNamespace(type=ProviderType.MUSIC)

    entries = _controller()._build_sync_entries(
        manifest, {ProviderFeature.LIBRARY_TRACKS_EDIT}, provider=None
    )

    assert CONF_ENTRY_LIBRARY_SYNC_BACK.key in _keys(entries)


def test_no_features_yields_no_entries() -> None:
    """A music provider with no library features gets no sync entries."""
    manifest = SimpleNamespace(type=ProviderType.MUSIC)

    entries = _controller()._build_sync_entries(manifest, set(), provider=None)

    assert entries == []


def _music_provider() -> MusicProvider:
    """Build a bare MusicProvider (is_streaming_provider defaults to True)."""
    return MusicProvider.__new__(MusicProvider)


def test_streaming_provider_with_library_sync_gets_deletions_entry() -> None:
    """The deletions option shows for streaming providers that sync a library."""
    manifest = SimpleNamespace(type=ProviderType.MUSIC)

    entries = _controller()._build_sync_entries(
        manifest, {ProviderFeature.LIBRARY_TRACKS}, provider=_music_provider()
    )

    assert CONF_ENTRY_LIBRARY_SYNC_DELETIONS.key in _keys(entries)


def test_streaming_provider_without_library_sync_has_no_deletions_entry() -> None:
    """Without any library feature there is nothing to sync, so no deletions option."""
    manifest = SimpleNamespace(type=ProviderType.MUSIC)

    entries = _controller()._build_sync_entries(
        manifest, {ProviderFeature.BROWSE, ProviderFeature.SEARCH}, provider=_music_provider()
    )

    assert CONF_ENTRY_LIBRARY_SYNC_DELETIONS.key not in _keys(entries)
