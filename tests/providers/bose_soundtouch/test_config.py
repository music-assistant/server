"""Tests for the Bose SoundTouch provider preset config entries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import ConfigEntryType, MediaType
from music_assistant_models.media_items import Playlist, SearchResults

from music_assistant.providers.bose_soundtouch.config import (
    build_preset_config_entries,
    parse_preset_action,
    preset_media_key,
    preset_media_type_key,
    preset_search_key,
    preset_selected_media_key,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from music_assistant.providers.bose_soundtouch.provider import BoseSoundTouchProvider


class _MusicStub:
    """Records the preset media searches performed while building the entries."""

    def __init__(self) -> None:
        self.searches: list[tuple[str, list[MediaType]]] = []

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int,
        library_only: bool,
    ) -> SearchResults:
        """Return a single playlist result for any query."""
        self.searches.append((search_query, media_types))
        return SearchResults(
            playlists=[
                Playlist(
                    item_id="42",
                    provider="demo",
                    name="Morning Mix",
                    uri="demo://playlist/42",
                    provider_mappings=set(),
                )
            ]
        )


class _ProviderStub:
    """Minimal stand-in for the provider: stored config values plus a music controller."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self._values = values or {}
        self.music = _MusicStub()
        self.mass = self

    def get_config_value(self, key: str, default: Any = None) -> Any:
        """Return the stored value for the given config key."""
        return self._values.get(key, default)


def _as_provider(stub: _ProviderStub) -> BoseSoundTouchProvider:
    """Present the stub as a provider for the entry builder."""
    return cast("BoseSoundTouchProvider", stub)


def _entries_by_key(entries: list[ConfigEntry]) -> dict[str, ConfigEntry]:
    """Index the built entries by their config key."""
    return {entry.key: entry for entry in entries}


def test_parse_preset_action_search() -> None:
    """A preset search action maps to its preset id with is_select False."""
    assert parse_preset_action("preset_3_search_media") == (3, False)
    assert parse_preset_action("preset_1_search_media") == (1, False)
    assert parse_preset_action("preset_6_search_media") == (6, False)


def test_parse_preset_action_select() -> None:
    """A preset select action maps to its preset id with is_select True."""
    assert parse_preset_action("preset_2_select_media") == (2, True)
    assert parse_preset_action("preset_6_select_media") == (6, True)


def test_parse_preset_action_unknown() -> None:
    """Non-action ids (incl. the button entry keys and out-of-range presets) don't match."""
    assert parse_preset_action("preset_3_do_search") == (None, False)
    assert parse_preset_action("preset_7_search_media") == (None, False)
    assert parse_preset_action("preset_0_select_media") == (None, False)
    assert parse_preset_action("some_other_action") == (None, False)
    assert parse_preset_action("") == (None, False)


async def test_build_preset_entries_without_stored_values() -> None:
    """A plain render exposes all six presets and runs no search."""
    stub = _ProviderStub()
    entries = _entries_by_key(await build_preset_config_entries(_as_provider(stub)))

    for preset_id in range(1, 7):
        assert entries[preset_media_key(preset_id)].type == ConfigEntryType.STRING
        assert entries[preset_media_type_key(preset_id)].default_value == MediaType.PLAYLIST.value
        assert entries[preset_search_key(preset_id)].type == ConfigEntryType.STRING
        assert entries[f"preset_{preset_id}_do_search"].action == f"preset_{preset_id}_search_media"
        # no search ran, so there is nothing to select yet
        assert preset_selected_media_key(preset_id) not in entries
    assert stub.music.searches == []


async def test_build_preset_entries_only_searches_refreshed_preset() -> None:
    """Only the preset whose search button was pressed queries the music controller."""
    stub = _ProviderStub(
        {
            preset_search_key(2): "  morning  ",
            preset_media_type_key(2): MediaType.PLAYLIST.value,
            preset_search_key(5): "evening",
        }
    )
    entries = _entries_by_key(
        await build_preset_config_entries(_as_provider(stub), refresh_preset_id=2)
    )

    assert stub.music.searches == [("morning", [MediaType.PLAYLIST])]
    selection = entries[preset_selected_media_key(2)]
    assert selection.options is not None
    assert [option.value for option in selection.options] == ["demo://playlist/42"]
    assert entries["preset_2_do_select"].action == "preset_2_select_media"
    # the untouched preset got no result dropdown
    assert preset_selected_media_key(5) not in entries


async def test_build_preset_entries_keeps_current_selection() -> None:
    """A stored selection stays selectable when its search results are not refreshed."""
    stub = _ProviderStub({preset_selected_media_key(4): "demo://playlist/7"})
    entries = _entries_by_key(await build_preset_config_entries(_as_provider(stub)))

    selection = entries[preset_selected_media_key(4)]
    assert selection.options is not None
    assert [option.value for option in selection.options] == ["demo://playlist/7"]
    assert stub.music.searches == []


async def test_build_preset_entries_falls_back_to_default_media_type() -> None:
    """An unsupported stored media type falls back to the default (playlist) search."""
    stub = _ProviderStub(
        {preset_media_type_key(1): MediaType.FOLDER.value, preset_search_key(1): "jazz"}
    )
    await build_preset_config_entries(_as_provider(stub), refresh_preset_id=1)

    assert stub.music.searches == [("jazz", [MediaType.PLAYLIST])]
