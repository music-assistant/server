"""Tests for the Plex setup-flow library selection (claim exclusion + smart defaults)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from music_assistant.providers.plex.constants import CONF_LIBRARY_ID
from music_assistant.providers.plex.helpers import (
    CONF_LIBRARY_TYPE,
    LIBRARY_TYPE_AUDIOBOOKS,
    LIBRARY_TYPE_MUSIC,
    LIBRARY_TYPE_PODCASTS,
    PlexSectionInfo,
)
from music_assistant.providers.plex.setup_flow import (
    _claimed_libraries,
    _default_library_selection,
)


def _section(name: str, tracking: bool) -> PlexSectionInfo:
    """Build a PlexSectionInfo for the given display name and tracking flag."""
    return PlexSectionInfo(
        display_name=name,
        section_title=name.rsplit(" / ", maxsplit=1)[-1],
        server_name="Server",
        section_type="artist",
        is_tracking_progress=tracking,
    )


def _make_mock_mass(providers_config: dict[str, Any] | None = None) -> MagicMock:
    """Build a MusicAssistant stub whose config.get('providers') returns the raw config."""
    mass = MagicMock()
    mass.config.get = MagicMock(return_value=providers_config or {})
    return mass


def test_claimed_libraries_reads_raw_config_without_recursion() -> None:
    """
    Claimed libraries are read from the raw stored config, not via get_provider_configs.

    Regression: using get_provider_configs(include_values=True) recursed because each
    call triggered get_config_entries for every other plex instance.
    """
    raw_config = {
        "plex-1": {
            "domain": "plex",
            "values": {
                CONF_LIBRARY_ID: "Server / Audiobooks",
                CONF_LIBRARY_TYPE: LIBRARY_TYPE_AUDIOBOOKS,
            },
        },
        "plex-2": {
            "domain": "plex",
            "setup_data": {
                CONF_LIBRARY_ID: "Server / Music",
                CONF_LIBRARY_TYPE: LIBRARY_TYPE_MUSIC,
            },
        },
        "other": {"domain": "spotify", "values": {CONF_LIBRARY_ID: "ignored"}},
    }
    mass = _make_mock_mass(raw_config)

    used, has_audiobook_provider = _claimed_libraries(mass, instance_id=None)

    assert used == {"Server / Audiobooks", "Server / Music"}
    assert has_audiobook_provider is True
    # the raw provider config is read directly; the recursive helper is never used
    mass.config.get.assert_called_once_with("providers", {})
    mass.config.get_provider_configs.assert_not_called()


def test_default_selection_prefers_unclaimed_non_tracking_library() -> None:
    """The default library is the first non-tracking (music) library, A-Z."""
    sections = [
        _section("Server / Audiobooks", tracking=True),
        _section("Server / Music", tracking=False),
        _section("Server / Podcasts", tracking=True),
    ]
    library, library_type = _default_library_selection(
        sections,
        used_libraries=set(),
        has_audiobook_provider=False,
        prefilled_library=None,
    )
    # the non-tracking (music) library sorts ahead of the tracking ones
    assert library == "Server / Music"
    assert library_type == LIBRARY_TYPE_MUSIC


def test_default_selection_skips_claimed_libraries() -> None:
    """Claimed libraries are excluded, so the default falls to the remaining one."""
    sections = [
        _section("Server / Music", tracking=False),
        _section("Server / Podcasts", tracking=True),
    ]
    # Music is claimed and an audiobook provider already exists
    library, library_type = _default_library_selection(
        sections,
        used_libraries={"Server / Music"},
        has_audiobook_provider=True,
        prefilled_library=None,
    )
    # only Podcasts is unclaimed; it is a tracking library and an audiobook provider
    # already exists, so the type resolves to Podcasts
    assert library == "Server / Podcasts"
    assert library_type == LIBRARY_TYPE_PODCASTS


def test_default_selection_tracking_library_without_audiobook_provider() -> None:
    """A tracking default library maps to the Audiobooks type when none exists yet."""
    sections = [_section("Server / Audiobooks", tracking=True)]
    library, library_type = _default_library_selection(
        sections,
        used_libraries=set(),
        has_audiobook_provider=False,
        prefilled_library=None,
    )
    assert library == "Server / Audiobooks"
    assert library_type == LIBRARY_TYPE_AUDIOBOOKS


def test_default_selection_preserves_prefilled_choice() -> None:
    """A previously selected library (reconfigure) is kept as the default."""
    sections = [
        _section("Server / Music", tracking=False),
        _section("Server / Audiobooks", tracking=True),
    ]
    library, library_type = _default_library_selection(
        sections,
        used_libraries=set(),
        has_audiobook_provider=False,
        prefilled_library="Server / Music",
    )
    assert library == "Server / Music"
    assert library_type == LIBRARY_TYPE_MUSIC
