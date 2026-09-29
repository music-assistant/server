"""Shared fixtures for the Local files provider tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.translations import TRANSLATION_RESOLVER

from music_assistant.controllers.storage import StorageController
from music_assistant.controllers.translations import TranslationController
from music_assistant.providers.filesystem_local import LocalFileSystemProvider

if TYPE_CHECKING:
    from mashumaro import DataClassDictMixin

    from music_assistant.mass import MusicAssistant


@pytest.fixture
async def storage(mass_minimal: MusicAssistant) -> AsyncGenerator[StorageController]:
    """
    Provide a storage controller on a minimal server, not set up (no background refresh).

    Hold locations with the helpers of tests/controllers/storage/conftest.py.

    :param mass_minimal: The minimal server to attach the controller to.
    """
    controller = StorageController(mass_minimal)
    mass_minimal.storage = controller
    try:
        yield controller
    finally:
        await controller.close()


@pytest.fixture
async def localize(
    mass_minimal: MusicAssistant,
) -> Callable[[DataClassDictMixin], dict[str, Any]]:
    """
    Provide a serializer that fills in the English strings of the repository, as a client gets them.

    :param mass_minimal: The minimal server the translations load on.
    """
    translations = TranslationController(mass_minimal)
    await translations.setup(None)  # type: ignore[arg-type]

    def _localize(item: DataClassDictMixin) -> dict[str, Any]:
        token = TRANSLATION_RESOLVER.set(partial(translations.get_translation, locale=None))
        try:
            return item.to_dict()
        finally:
            TRANSLATION_RESOLVER.reset(token)

    return _localize


def make_provider(mass: MusicAssistant, base_path: Path) -> LocalFileSystemProvider:
    """
    Build a Local files source that reads its files from a folder.

    :param mass: The server the source belongs to.
    :param base_path: The folder of the source.
    """
    config = MagicMock()
    config.instance_id = "filesystem_local--test"
    config.values = {}
    config.get_value = MagicMock(side_effect=lambda _key, default=None: default)
    manifest = MagicMock()
    manifest.domain = "filesystem_local"
    return LocalFileSystemProvider(mass, manifest, config, base_path=str(base_path))
