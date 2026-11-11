"""Tests for the options page of a Local files source."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant.mass import MusicAssistant
from music_assistant.providers.filesystem_local.constants import CONF_CONTENT_TYPE
from tests.providers.filesystem_local.conftest import make_provider

if TYPE_CHECKING:
    from mashumaro import DataClassDictMixin


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
