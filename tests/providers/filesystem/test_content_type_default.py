"""Tests for the filesystem provider's media content type resolution."""

from typing import Any
from unittest.mock import MagicMock

from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import (
    CONF_CONTENT_TYPE,
    CONF_ENTRY_CONTENT_TYPE,
)

INSTANCE_ID = "filesystem_smb--test"


def _create_provider(setup_data: dict[str, Any]) -> LocalFileSystemProvider:
    """
    Build a provider through its real __init__ with the given stored setup data.

    :param setup_data: The setup_data dict persisted for this provider instance.
    """
    mass = MagicMock()
    mass.config.get = MagicMock(
        side_effect=lambda key, default=None: setup_data if key.endswith("/setup_data") else default
    )
    mass.config.decrypt_string = MagicMock(side_effect=lambda value: value)
    # nothing persisted for the entry either, so the lookup falls through to the default
    mass.config.get_raw_provider_config_value = MagicMock(
        side_effect=lambda _instance, _key, default=None: default
    )
    config = MagicMock()
    config.instance_id = INSTANCE_ID
    # no entry for the key either, so the lookup behaves like Config.get_value on a miss
    config.values = {}
    config.get_value = MagicMock(side_effect=lambda _key, default=None: default)
    manifest = MagicMock()
    manifest.domain = "filesystem_smb"
    return LocalFileSystemProvider(mass, manifest, config, base_path="/media")


def test_content_type_from_setup_data() -> None:
    """A provider set up through the setup flow uses its stored content type."""
    provider = _create_provider({CONF_CONTENT_TYPE: "audiobooks"})
    assert provider.media_content_type == "audiobooks"


def test_content_type_defaults_to_music_when_missing() -> None:
    """
    An instance whose setup data predates the content type entry falls back to music.

    Without the fallback every branch of the sync silently does nothing, so changed
    files are re-queued on every sync and never converge.
    """
    provider = _create_provider({})
    assert provider.media_content_type == CONF_ENTRY_CONTENT_TYPE.default_value
    assert provider.media_content_type == "music"
