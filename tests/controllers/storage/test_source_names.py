"""Tests for the names under which a storage location lists the music sources around it."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderType
from music_assistant_models.errors import ActionUnavailable
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_PATH, CONF_PROVIDERS, DEFAULT_PROVIDER_CONFIG_ENTRIES
from music_assistant.controllers.storage import StorageController, StorageLocation
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider
from tests.controllers.storage.conftest import store_source

DOMAIN = "filesystem_local"
FIRST = f"{DOMAIN}--first"
OFFLINE = f"{DOMAIN}--offline"


class _LocalFiles(MusicProvider):
    """Stand-in for a loaded Local files source, named after its folder like the real one."""

    @property
    def instance_name_postfix(self) -> str | None:
        """Return the name of the folder of the source."""
        return Path(str(self.get_setup_value(CONF_PATH))).name


@pytest.fixture
def local_files(storage: StorageController) -> MusicAssistant:
    """
    Let the server add and remove Local files sources, without a library behind them.

    :param storage: The storage controller.
    """
    mass = storage.mass
    mass._provider_manifests[DOMAIN] = ProviderManifest(
        type=ProviderType.MUSIC,
        domain=DOMAIN,
        name="Local files",
        description="",
        codeowners=[],
        multi_instance=True,
    )
    mass.music = MagicMock()
    for method in ("cleanup_provider_shortcuts", "cleanup_provider", "cleanup_library_shortcuts"):
        setattr(mass.music, method, AsyncMock())
    return mass


def _load(mass: MusicAssistant, config: ProviderConfig) -> None:
    """
    Load a stand-in for a Local files source and store its name, as a real load does.

    :param mass: The server.
    :param config: The config of the source.
    """
    provider = _LocalFiles(mass, mass.get_provider_manifest(DOMAIN), config)
    mass._providers[config.instance_id] = provider
    mass.config.set_provider_default_name(config.instance_id, provider.default_name)


def _load_stored(mass: MusicAssistant, instance_id: str) -> None:
    """
    Load a stand-in for a stored Local files source.

    :param mass: The server.
    :param instance_id: The instance id of the source.
    """
    raw = mass.config.get(f"{CONF_PROVIDERS}/{instance_id}")
    _load(mass, cast("ProviderConfig", ProviderConfig.parse(DEFAULT_PROVIDER_CONFIG_ENTRIES, raw)))


async def _add_source(mass: MusicAssistant, folder: Path) -> str:
    """
    Add a Local files source on a folder the way the app does, and return its instance id.

    :param mass: The server.
    :param folder: The folder of the source.
    """

    async def load(config: ProviderConfig) -> None:
        _load(mass, config)

    setup_data = {CONF_PATH: mass.config.encrypt_string(str(folder))}
    with patch.object(mass, "load_provider_config", AsyncMock(side_effect=load)):
        config = await mass.config._create_provider_instance(DOMAIN, {}, setup_data)
    return config.instance_id


async def _remove_source(mass: MusicAssistant, instance_id: str) -> None:
    """
    Remove a music source the way the app does.

    :param mass: The server.
    :param instance_id: The instance id of the source.
    """

    async def unload(instance_id: str, _is_removed: bool = False) -> None:
        mass._providers.pop(instance_id, None)

    with patch.object(mass, "unload_provider", AsyncMock(side_effect=unload)):
        await mass.config.remove_provider_config(instance_id)


def _location(locations: list[StorageLocation], path: Path) -> StorageLocation:
    """
    Return the location on a path.

    :param locations: The locations to pick from.
    :param path: The path of the location.
    """
    return next(loc for loc in locations if loc.path == str(path))


@pytest.mark.parametrize(("name", "shown"), [(None, "Local files [Music]"), ("Mine", "Mine")])
async def test_sources_named_as_the_app_names_them(
    storage: StorageController,
    local_files: MusicAssistant,
    tmp_path: Path,
    name: str | None,
    shown: str,
) -> None:
    """
    A location names a loaded source as the app does while a second source comes and goes.

    With more than one Local files source the default name carries the folder, without a
    reload of the first source. A name the user gave is kept.
    """
    music = tmp_path / "Music"
    radiohead = music / "Radiohead"
    radiohead.mkdir(parents=True)
    await storage.add_local_folder(str(music))
    await storage.add_local_folder(str(radiohead))
    store_source(storage, music, FIRST, name)
    _load_stored(local_files, FIRST)

    second = await _add_source(local_files, radiohead)
    info = await storage.get_info()
    with pytest.raises(ActionUnavailable) as exc_info:
        await storage.remove_local_folder(str(music))

    used_by = sorted([shown, "Local files [Radiohead]"], key=str.casefold)
    assert _location(info.locations, music).used_by == used_by
    assert exc_info.value.translation_args == [used_by[0]]
    inner = _location(info.locations, radiohead)
    assert (inner.used_by, inner.read_by) == (["Local files [Radiohead]"], [shown])

    await _remove_source(local_files, second)
    info = await storage.get_info()

    alone = name or "Local files"
    assert _location(info.locations, music).used_by == [alone]
    inner = _location(info.locations, radiohead)
    assert (inner.used_by, inner.read_by) == ([], [alone])
    # the removed source does not come back through its name
    assert local_files.config.get(f"{CONF_PROVIDERS}/{second}") is None


async def test_source_that_is_not_loaded_is_listed(
    storage: StorageController, local_files: MusicAssistant, tmp_path: Path
) -> None:
    """
    A source that is not loaded, e.g. because its share is not connected, is still listed.

    It keeps the name stored when it last loaded, which the app shows for it too, and nothing
    is loaded to name it. It still counts as an instance of its kind.
    """
    music = tmp_path / "Music"
    offline = tmp_path / "Offline"
    (music / "Radiohead").mkdir(parents=True)
    offline.mkdir()
    await storage.add_local_folder(str(music))
    await storage.add_local_folder(str(offline))
    store_source(storage, offline, OFFLINE, None)
    store_source(storage, music, FIRST, None)
    _load_stored(local_files, FIRST)

    second = await _add_source(local_files, music / "Radiohead")
    await _remove_source(local_files, second)
    info = await storage.get_info()

    assert _location(info.locations, music).used_by == ["Local files [Music]"]
    assert _location(info.locations, offline).used_by == ["Local files"]
    assert OFFLINE not in local_files._providers
