"""Tests for the Bose SoundTouch provider preset config entries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ConfigEntryType, MediaType
from music_assistant_models.media_items import Playlist, SearchResults

from music_assistant.providers.bose_soundtouch.config import (
    ACTION_ASSIGN,
    ACTION_SEARCH,
    CONF_SEARCH_MEDIA_TYPE,
    CONF_SEARCH_QUERY,
    CONF_SEARCH_RESULT,
    CONF_SEARCH_TARGET,
    build_preset_config_entries,
    preset_media_key,
)
from music_assistant.providers.bose_soundtouch.provider import (
    BoseSoundTouchProvider,
    _search_values_to_reset,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry


class _MusicStub:
    """Record preset media searches and return a single result."""

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
    """Provide stored config values and the music search controller."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self._values = values or {}
        self.music = _MusicStub()
        self.mass = self
        self.instance_id = "bose_soundtouch"
        self.config_updates: list[tuple[str, Any, bool]] = []

    def get_config_value(self, key: str, default: Any = None) -> Any:
        """Return a stored value."""
        return self._values.get(key, default)

    def _update_config_value(self, key: str, value: Any, immediate: bool = False) -> None:
        """Record and apply a provider-owned config update."""
        self.config_updates.append((key, value, immediate))
        self._values[key] = value

    def _reset_search_values(self, *keys: str) -> None:
        """Reset persisted search fields."""
        for key in keys:
            self._update_config_value(key, "", immediate=True)


def _as_provider(stub: _ProviderStub) -> BoseSoundTouchProvider:
    """Present the stub as a provider for the entry builder."""
    return cast("BoseSoundTouchProvider", stub)


def _entries_by_key(entries: list[ConfigEntry]) -> dict[str, ConfigEntry]:
    """Index config entries by key."""
    return {entry.key: entry for entry in entries}


async def test_plain_render_has_one_search_engine_and_six_presets() -> None:
    """A plain render exposes one search flow and all six preset mappings."""
    stub = _ProviderStub()
    entries = _entries_by_key(await build_preset_config_entries(_as_provider(stub)))

    assert entries[CONF_SEARCH_MEDIA_TYPE].default_value == MediaType.PLAYLIST.value
    assert entries[CONF_SEARCH_MEDIA_TYPE].immediate_apply
    assert entries[CONF_SEARCH_QUERY].immediate_apply
    assert entries["preset_do_search"].action == ACTION_SEARCH
    assert entries[CONF_SEARCH_RESULT].hidden
    assert entries[CONF_SEARCH_RESULT].immediate_apply
    assert entries[CONF_SEARCH_TARGET].hidden
    assert entries[CONF_SEARCH_TARGET].immediate_apply
    assert entries[CONF_SEARCH_TARGET].default_value == ""
    assert entries["preset_do_assign"].hidden
    for preset_id in range(1, 7):
        preset_entry = entries[preset_media_key(preset_id)]
        assert preset_entry.type == ConfigEntryType.STRING
        assert preset_entry.category == "presets"
    assert stub.music.searches == []


async def test_hidden_search_fields_accept_immediate_config_save() -> None:
    """The server accepts search selections when rebuilding plain config entries."""
    stub = _ProviderStub()
    entries = await build_preset_config_entries(_as_provider(stub))
    config = cast(
        "ProviderConfig",
        ProviderConfig.parse(
            entries,
            {
                "type": "player",
                "domain": "bose_soundtouch",
                "instance_id": "bose_soundtouch",
                "values": {},
            },
        ),
    )
    selected_uri = "radiobrowser://radio/france-inter"

    changed_keys = config.update({CONF_SEARCH_RESULT: selected_uri, CONF_SEARCH_TARGET: "4"})

    assert changed_keys == {
        f"values/{CONF_SEARCH_RESULT}",
        f"values/{CONF_SEARCH_TARGET}",
    }
    assert config.values[CONF_SEARCH_RESULT].value == selected_uri
    assert config.values[CONF_SEARCH_TARGET].value == "4"


async def test_search_refreshes_shared_results_once() -> None:
    """The shared search action performs a single media search."""
    stub = _ProviderStub(
        {
            CONF_SEARCH_QUERY: "  morning  ",
            CONF_SEARCH_MEDIA_TYPE: MediaType.PLAYLIST.value,
        }
    )
    entries = _entries_by_key(
        await build_preset_config_entries(_as_provider(stub), refresh_results=True)
    )

    assert stub.music.searches == [("morning", [MediaType.PLAYLIST])]
    selection = entries[CONF_SEARCH_RESULT]
    assert selection.options is not None
    assert [option.value for option in selection.options] == ["demo://playlist/42"]
    assert not selection.hidden
    target = entries[CONF_SEARCH_TARGET]
    assert not target.hidden
    assert target.options is not None
    assert [option.value for option in target.options] == [
        "",
        *[str(value) for value in range(1, 7)],
    ]
    assert target.options[0].disabled
    assert target.value == ""
    assert entries["preset_do_assign"].hidden


async def test_assign_action_appears_after_result_and_target_selection() -> None:
    """Assignment is unavailable until both the result and target preset are selected."""
    stub = _ProviderStub(
        {
            CONF_SEARCH_RESULT: "demo://playlist/42",
            CONF_SEARCH_TARGET: "4",
        }
    )
    entries = _entries_by_key(await build_preset_config_entries(_as_provider(stub)))

    assign_action = entries["preset_do_assign"]
    assert assign_action.action == ACTION_ASSIGN
    assert assign_action.immediate_apply
    assert assign_action.translation_params == ["4"]
    assert not assign_action.hidden


async def test_plain_render_keeps_current_search_selection() -> None:
    """A stored search result stays selectable without repeating the search."""
    stub = _ProviderStub({CONF_SEARCH_RESULT: "demo://playlist/7"})
    entries = _entries_by_key(await build_preset_config_entries(_as_provider(stub)))

    selection = entries[CONF_SEARCH_RESULT]
    assert selection.options is not None
    assert [option.value for option in selection.options] == ["demo://playlist/7"]
    assert not selection.hidden
    assert stub.music.searches == []


async def test_search_falls_back_to_default_media_type() -> None:
    """An unsupported media type falls back to playlist search."""
    stub = _ProviderStub(
        {CONF_SEARCH_MEDIA_TYPE: MediaType.FOLDER.value, CONF_SEARCH_QUERY: "jazz"}
    )

    await build_preset_config_entries(_as_provider(stub), refresh_results=True)

    assert stub.music.searches == [("jazz", [MediaType.PLAYLIST])]


async def test_search_action_resets_previous_result_and_target() -> None:
    """A new search clears all values produced by the previous search."""
    stub = _ProviderStub(
        {
            CONF_SEARCH_MEDIA_TYPE: MediaType.PLAYLIST.value,
            CONF_SEARCH_QUERY: "morning",
            CONF_SEARCH_RESULT: "demo://playlist/old",
            CONF_SEARCH_TARGET: "5",
        }
    )

    entries = _entries_by_key(
        list(await BoseSoundTouchProvider.handle_config_action(_as_provider(stub), ACTION_SEARCH))
    )

    assert stub.config_updates == [
        (CONF_SEARCH_RESULT, "", True),
        (CONF_SEARCH_TARGET, "", True),
    ]
    assert entries[CONF_SEARCH_RESULT].value == ""
    assert entries[CONF_SEARCH_TARGET].value == ""
    assert entries["preset_do_assign"].hidden
    assert stub.music.searches == [("morning", [MediaType.PLAYLIST])]


def test_search_changes_reset_only_downstream_steps() -> None:
    """Each search step invalidates only the values that follow it."""
    assert _search_values_to_reset({f"values/{CONF_SEARCH_MEDIA_TYPE}"}) == (
        CONF_SEARCH_QUERY,
        CONF_SEARCH_RESULT,
        CONF_SEARCH_TARGET,
    )
    assert _search_values_to_reset({f"values/{CONF_SEARCH_QUERY}"}) == (
        CONF_SEARCH_RESULT,
        CONF_SEARCH_TARGET,
    )
    assert _search_values_to_reset({f"values/{CONF_SEARCH_RESULT}"}) == (CONF_SEARCH_TARGET,)
    assert _search_values_to_reset({f"values/{CONF_SEARCH_TARGET}"}) == ()


async def test_assign_action_copies_result_to_selected_preset() -> None:
    """The assignment action copies the selected URI to the chosen preset."""
    selected_uri = "radiobrowser://radio/france-inter"
    stub = _ProviderStub(
        {
            CONF_SEARCH_MEDIA_TYPE: MediaType.RADIO.value,
            CONF_SEARCH_QUERY: "france inter",
            CONF_SEARCH_RESULT: selected_uri,
            CONF_SEARCH_TARGET: "4",
        }
    )

    entries = _entries_by_key(
        list(await BoseSoundTouchProvider.handle_config_action(_as_provider(stub), ACTION_ASSIGN))
    )

    assert stub._values[preset_media_key(4)] == selected_uri
    assert stub.config_updates == [
        (preset_media_key(4), selected_uri, True),
        (CONF_SEARCH_MEDIA_TYPE, "", True),
        (CONF_SEARCH_QUERY, "", True),
        (CONF_SEARCH_RESULT, "", True),
        (CONF_SEARCH_TARGET, "", True),
    ]
    assert entries[preset_media_key(4)].value == selected_uri
    assert entries[CONF_SEARCH_MEDIA_TYPE].value == MediaType.PLAYLIST.value
    assert entries[CONF_SEARCH_QUERY].value == ""
    assert entries[CONF_SEARCH_RESULT].value == ""
    assert entries[CONF_SEARCH_TARGET].value == ""
    assert entries["preset_do_assign"].hidden
    assert stub.music.searches == []
