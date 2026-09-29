"""Tests for the options page of a Local files source."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.config_entries import UI_ONLY
from music_assistant_models.enums import ConfigEntryType

from music_assistant.mass import MusicAssistant
from music_assistant.providers.filesystem_google_drive.provider import (
    GoogleDriveFileSystemProvider,
)
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import CONF_CONTENT_TYPE
from music_assistant.providers.filesystem_nfs.provider import NFSFileSystemProvider
from music_assistant.providers.filesystem_onedrive.provider import OneDriveFileSystemProvider
from music_assistant.providers.filesystem_smb import SMBFileSystemProvider
from music_assistant.providers.webdav.provider import WebDAVFileSystemProvider
from tests.providers.filesystem_local.conftest import make_provider

if TYPE_CHECKING:
    from mashumaro import DataClassDictMixin


async def test_the_options_show_the_folder_of_the_source(
    mass_minimal: MusicAssistant,
    tmp_path: Path,
    localize: Callable[[DataClassDictMixin], dict[str, Any]],
) -> None:
    """A Local files source shows the folder it reads from, as a line that is never stored."""
    provider = make_provider(mass_minimal, tmp_path / "Music")

    entries = await provider.get_config_entries()

    folder = entries[0]
    assert folder.key == "folder"
    assert folder.type == ConfigEntryType.LABEL
    assert folder.type in UI_ONLY
    shown = localize(replace(folder, translation_owner=provider.translation_owner))
    assert shown["label"] == f"Folder: {tmp_path / 'Music'}"


async def test_the_options_keep_the_content_type_as_it_was(
    mass_minimal: MusicAssistant,
    tmp_path: Path,
    localize: Callable[[DataClassDictMixin], dict[str, Any]],
) -> None:
    """The content type stays a read-only line with its own label, not the setup question."""
    provider = make_provider(mass_minimal, tmp_path / "Music")

    entries = await provider.get_config_entries()

    content_type = next(entry for entry in entries if entry.key == CONF_CONTENT_TYPE)
    shown = localize(replace(content_type, translation_owner=provider.translation_owner))
    assert shown["label"] == "Content type in media folder(s)"
    assert shown["read_only"] is True
    assert shown["expanded_options"] is False


@pytest.mark.parametrize(
    "provider_class",
    [
        SMBFileSystemProvider,
        NFSFileSystemProvider,
        WebDAVFileSystemProvider,
        GoogleDriveFileSystemProvider,
        OneDriveFileSystemProvider,
    ],
    ids=["smb", "nfs", "webdav", "google_drive", "onedrive"],
)
async def test_the_other_file_sources_do_not_show_a_folder(
    provider_class: type[LocalFileSystemProvider],
) -> None:
    """
    The sources built on Local files keep their own options, without the folder line.

    :param provider_class: The provider class of the source.
    """
    provider = provider_class.__new__(provider_class)
    provider.base_path = "/tmp/share"  # noqa: S108
    provider.mass = MagicMock()
    provider.mass.config.get = MagicMock(return_value={})
    provider.config = MagicMock()
    provider.config.instance_id = "test"
    provider.config.values = {}
    provider.config.get_value = MagicMock(side_effect=lambda _key, default=None: default)

    entries = await provider.get_config_entries()

    assert CONF_CONTENT_TYPE in {entry.key for entry in entries}
    assert "folder" not in {entry.key for entry in entries}
