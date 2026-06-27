"""Tests for get_config_entries to prevent recursion and validate defaults."""

from __future__ import annotations

from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.plex import CONF_LIBRARY_ID, get_config_entries
from music_assistant.providers.plex.helpers import (
    CONF_LIBRARY_TYPE,
    LIBRARY_TYPE_AUDIOBOOKS,
    LIBRARY_TYPE_MUSIC,
    LIBRARY_TYPE_PODCASTS,
    PlexSectionInfo,
)


def _make_mock_mass(
    providers_config: dict[str, Any] | None = None,
    _sections: list[PlexSectionInfo] | None = None,
) -> MagicMock:
    """Build a MusicAssistant stub with configurable providers and section info."""
    mass = MagicMock()
    mass.config.get = MagicMock(return_value=providers_config or {})
    mass.config.decrypt_string = MagicMock(return_value="token")
    mass.cache.get = AsyncMock(return_value=None)
    return mass


async def test_get_config_entries_no_recursion_with_multiple_plex_instances() -> None:
    """
    Calling get_config_entries should not recurse when multiple plex instances exist.

    Regression test: using get_provider_configs(include_values=True) caused
    infinite recursion because each call triggered get_config_entries for all
    other plex instances.
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
            "values": {
                CONF_LIBRARY_ID: "Server / Music",
                CONF_LIBRARY_TYPE: LIBRARY_TYPE_MUSIC,
            },
        },
    }

    sections = [
        PlexSectionInfo(
            display_name="Server / Audiobooks",
            section_title="Audiobooks",
            server_name="Server",
            section_type="artist",
            is_tracking_progress=True,
        ),
        PlexSectionInfo(
            display_name="Server / Music",
            section_title="Music",
            server_name="Server",
            section_type="artist",
            is_tracking_progress=False,
        ),
        PlexSectionInfo(
            display_name="Server / Podcasts",
            section_title="Podcasts",
            server_name="Server",
            section_type="artist",
            is_tracking_progress=True,
        ),
    ]

    mass = _make_mock_mass(raw_config, sections)

    with mock.patch(
        "music_assistant.providers.plex.get_section_info",
        new_callable=AsyncMock,
        return_value=sections,
    ):
        # Call for a *new* instance (no instance_id) — this must not recurse
        entries = await get_config_entries(
            mass,
            values={
                "token": "encrypted",
                "local_server_ip": "10.0.4.33",
                "local_server_port": 32400,
                "local_server_ssl": False,
                "local_server_verify_cert": True,
            },
        )

    # Verify we got entries back without hitting recursion
    by_key = {e.key: e for e in entries}
    assert CONF_LIBRARY_ID in by_key
    assert CONF_LIBRARY_TYPE in by_key

    library_entry = by_key[CONF_LIBRARY_ID]
    # Default library should be the first *unclaimed* non-tracking library, A-Z
    # Server / Music is claimed by plex-2, so Podcasts is the only unclaimed option
    assert library_entry.default_value == "Server / Podcasts"

    type_entry = by_key[CONF_LIBRARY_TYPE]
    # Podcasts is a tracking library and an audiobook provider already exists -> Podcasts type
    assert type_entry.default_value == LIBRARY_TYPE_PODCASTS


async def test_get_config_entries_default_type_progress_tracking() -> None:
    """Progress-tracking libraries default to Audiobooks (or Podcasts if one exists)."""
    raw_config = {
        "plex-1": {
            "domain": "plex",
            "values": {
                CONF_LIBRARY_ID: "Server / Audiobooks",
                CONF_LIBRARY_TYPE: LIBRARY_TYPE_AUDIOBOOKS,
            },
        },
    }

    sections = [
        PlexSectionInfo(
            display_name="Server / Audiobooks",
            section_title="Audiobooks",
            server_name="Server",
            section_type="artist",
            is_tracking_progress=True,
        ),
        PlexSectionInfo(
            display_name="Server / More Audios",
            section_title="More Audios",
            server_name="Server",
            section_type="artist",
            is_tracking_progress=True,
        ),
    ]

    mass = _make_mock_mass(raw_config, sections)

    with mock.patch(
        "music_assistant.providers.plex.get_section_info",
        new_callable=AsyncMock,
        return_value=sections,
    ):
        entries = await get_config_entries(
            mass,
            values={
                "token": "encrypted",
                "local_server_ip": "10.0.4.33",
                "local_server_port": 32400,
                "local_server_ssl": False,
                "local_server_verify_cert": True,
            },
        )

    by_key = {e.key: e for e in entries}

    # Only unclaimed tracking library is "More Audios"
    library_entry = by_key[CONF_LIBRARY_ID]
    assert library_entry.default_value == "Server / More Audios"

    # Audiobook provider already exists -> Podcasts
    type_entry = by_key[CONF_LIBRARY_TYPE]
    assert type_entry.default_value == LIBRARY_TYPE_PODCASTS


async def test_get_config_entries_preserves_user_library_choice() -> None:
    """When user has already selected a library, defaults are not overwritten."""
    raw_config: dict[str, Any] = {}

    sections = [
        PlexSectionInfo(
            display_name="Server / Music",
            section_title="Music",
            server_name="Server",
            section_type="artist",
            is_tracking_progress=False,
        ),
    ]

    mass = _make_mock_mass(raw_config, sections)

    with mock.patch(
        "music_assistant.providers.plex.get_section_info",
        new_callable=AsyncMock,
        return_value=sections,
    ):
        entries = await get_config_entries(
            mass,
            instance_id="plex_test",
            values={
                "token": "encrypted",
                "local_server_ip": "10.0.4.33",
                "local_server_port": 32400,
                "local_server_ssl": False,
                "local_server_verify_cert": True,
                CONF_LIBRARY_ID: "Server / Music",
                CONF_LIBRARY_TYPE: LIBRARY_TYPE_MUSIC,
            },
        )

    by_key = {e.key: e for e in entries}
    library_entry = by_key[CONF_LIBRARY_ID]
    type_entry = by_key[CONF_LIBRARY_TYPE]

    assert library_entry.value == "Server / Music"
    assert type_entry.value == LIBRARY_TYPE_MUSIC
