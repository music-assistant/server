"""Tests for the names under which a storage location lists the music sources around it."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderType
from music_assistant_models.errors import ActionUnavailable, SetupFailedError
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


class _Numbered(MusicProvider):
    """Stand-in for a loaded source of a kind that tells its instances apart by number."""


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


def _load(
    mass: MusicAssistant, config: ProviderConfig, provider_class: type[MusicProvider] = _LocalFiles
) -> None:
    """
    Load a stand-in for a source and store its name, as a real load does.

    :param mass: The server.
    :param config: The config of the source.
    :param provider_class: The stand-in to load.
    """
    provider = provider_class(mass, mass.get_provider_manifest(DOMAIN), config)
    mass._providers[config.instance_id] = provider
    mass.config.set_provider_default_name(config.instance_id, provider.default_name)


def _load_stored(
    mass: MusicAssistant, instance_id: str, provider_class: type[MusicProvider] = _LocalFiles
) -> None:
    """
    Load a stand-in for a stored source.

    :param mass: The server.
    :param instance_id: The instance id of the source.
    :param provider_class: The stand-in to load.
    """
    raw = mass.config.get(f"{CONF_PROVIDERS}/{instance_id}")
    config = cast("ProviderConfig", ProviderConfig.parse(DEFAULT_PROVIDER_CONFIG_ENTRIES, raw))
    _load(mass, config, provider_class)


def _stored_names(mass: MusicAssistant, instance_ids: list[str]) -> list[str | None]:
    """
    Return the stored default name of each source.

    :param mass: The server.
    :param instance_ids: The instance ids of the sources.
    """
    return [mass.config.get(f"{CONF_PROVIDERS}/{i}/default_name") for i in instance_ids]


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


async def test_source_that_fails_to_load_leaves_the_names(
    storage: StorageController, local_files: MusicAssistant, tmp_path: Path
) -> None:
    """
    A source that fails to load leaves the name of the other source as it was before.

    While it loads it counts as a second instance of its kind, so the other source, when it
    stores its name in the meantime, e.g. because it reloads, stores the name it would have
    next to it. The error of the load reaches the caller as it was.
    """
    music = tmp_path / "Music"
    music.mkdir()
    store_source(storage, music, FIRST, None)
    _load_stored(local_files, FIRST)
    error = SetupFailedError("The folder can not be read")

    async def fail(_config: ProviderConfig) -> None:
        _load_stored(local_files, FIRST)
        raise error

    setup_data = {CONF_PATH: local_files.config.encrypt_string(str(tmp_path / "Radiohead"))}
    with (
        patch.object(local_files, "load_provider_config", AsyncMock(side_effect=fail)),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        await local_files.config._create_provider_instance(DOMAIN, {}, setup_data)

    assert exc_info.value is error
    assert _stored_names(local_files, [FIRST]) == ["Local files"]
    assert list(local_files.config.get(CONF_PROVIDERS)) == [FIRST]


async def test_removing_one_of_three_numbered_sources(
    storage: StorageController, local_files: MusicAssistant, tmp_path: Path
) -> None:
    """
    Removing one of three sources told apart by number renumbers the other two.

    The removed source is still loaded after its config is gone, and is left out.
    """
    sources = [f"{DOMAIN}--{letter}" for letter in "abc"]
    for instance_id in sources:
        store_source(storage, tmp_path, instance_id, None)
    for instance_id in sources:
        _load_stored(local_files, instance_id, _Numbered)
    assert _stored_names(local_files, sources) == [
        "Local files [1]",
        "Local files [2]",
        "Local files [3]",
    ]

    await _remove_source(local_files, sources[0])

    assert _stored_names(local_files, sources[1:]) == ["Local files [1]", "Local files [2]"]
    assert local_files.config.get(f"{CONF_PROVIDERS}/{sources[0]}") is None
