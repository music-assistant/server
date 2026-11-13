"""Tests for the local folder of providers that a server in dev mode loads."""

from __future__ import annotations

import json
import logging
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import EventType, ProviderTier

from music_assistant import providers as providers_package
from music_assistant.constants import CONF_PROVIDERS
from music_assistant.controllers.community_catalog import CommunityCatalogController, dependencies
from music_assistant.controllers.community_catalog.dependencies import CommunityDependencies
from music_assistant.helpers.util import purge_provider_modules
from tests.conftest import full_mass_context
from tests.controllers.community_catalog.test_dependencies import _build_wheel

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from music_assistant.mass import MusicAssistant

# a package that only the test wheels have
REQUIREMENT = "dev-folder-requirement"
PROVIDER_CODE = f"""
from {REQUIREMENT.replace("-", "_")} import VERSION as REQUIREMENT_VERSION

from music_assistant.models.plugin import PluginProvider


async def setup(mass, manifest, config):
    return PluginProvider(mass, manifest, config)
"""


@pytest.fixture
def location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """
    Return the dev providers folder, with uv kept to the test wheels.

    What a test adds to the import paths or imports from the folder is undone.
    """
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    _build_wheel(wheels, REQUIREMENT, "1.0")
    monkeypatch.setattr(dependencies, "_UV_INDEX_ARGS", ["--no-index", "--find-links", str(wheels)])
    monkeypatch.setattr(sys, "path", list(sys.path))
    folder = tmp_path / "dev_providers"
    folder.mkdir()
    original_path = list(providers_package.__path__)
    yield folder
    providers_package.__path__[:] = original_path
    sys.modules.pop(REQUIREMENT.replace("-", "_"), None)
    for provider_dir in folder.iterdir():
        module = sys.modules.get(f"{providers_package.__name__}.{provider_dir.name}")
        # a folder named after a shipped provider never replaced it
        if module is not None and str(module.__file__).startswith(str(folder)):
            purge_provider_modules(provider_dir.name)


async def test_dev_provider_loads(tmp_path: Path, location: Path) -> None:
    """A provider in the folder loads as community provider, with its texts and requirements."""
    _write_provider(location, "dev_one", requirements=[REQUIREMENT])
    (location / "dev_one" / "strings.json").write_text(json.dumps({"manifest": {"name": "One"}}))
    _seed_instance(tmp_path, "dev_one")

    async with full_mass_context(tmp_path) as mass:
        # the server's own load passed the instance over: its domain had no manifest yet
        assert mass.config.get(f"{CONF_PROVIDERS}/dev_one/last_error") is None
        assert "load_provider_dev_one" not in mass._tracked_timers
        mass.dev_mode = True
        mass.dev_providers_path = str(location)

        with patch.object(mass, "signal_event", wraps=mass.signal_event) as signal_event:
            await mass.community_catalog.start()

        assert mass.get_provider_manifest("dev_one").tier == ProviderTier.COMMUNITY
        provider = mass.get_provider("dev_one")
        assert provider is not None
        assert provider.available
        module = sys.modules[f"{providers_package.__name__}.dev_one"]
        assert module.__file__ == str(location / "dev_one" / "__init__.py")
        assert module.REQUIREMENT_VERSION == "1.0"
        assert mass.translations.get_translation("provider.dev_one.manifest.name") == "One"
        events = [c.args[0] for c in signal_event.call_args_list]
        assert events.count(EventType.COMMUNITY_CATALOG_UPDATED) == 1


async def test_ignored_outside_dev_mode(
    location: Path, mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Outside dev mode the folder is only warned about."""
    _write_provider(location, "dev_one")
    mass.dev_mode = False
    mass.dev_providers_path = str(location)

    with patch.object(dependencies, "check_output", AsyncMock(return_value=(0, b""))) as uv:
        await _start(mass, caplog)

    warnings = _warnings(mass, caplog)
    assert len(warnings) == 1
    assert "dev mode" in warnings[0]
    _assert_nothing_loaded(mass, location, uv)


async def test_nothing_happens_without_the_folder(
    location: Path, mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Without the option the server does not look for providers to load."""
    mass.dev_mode = True

    with patch.object(dependencies, "check_output", AsyncMock(return_value=(0, b""))) as uv:
        await _start(mass, caplog)

    assert not _warnings(mass, caplog)
    _assert_nothing_loaded(mass, location, uv)


@pytest.mark.parametrize(
    ("folder", "domain", "reason"),
    [
        ("misnamed", "other_domain", "not named after its domain"),
        ("builtin", "builtin", "already has a provider named builtin"),
    ],
)
async def test_refused_provider(
    location: Path,
    mass: MusicAssistant,
    caplog: pytest.LogCaptureFixture,
    folder: str,
    domain: str,
    reason: str,
) -> None:
    """A provider that may not take its domain is refused before its requirements are seen."""
    _write_provider(location, "dev_one")
    _write_provider(location, domain, folder=folder, requirements=[REQUIREMENT])
    mass.dev_mode = True
    mass.dev_providers_path = str(location)

    with patch.object(
        CommunityDependencies, "resolve", autospec=True, side_effect=CommunityDependencies.resolve
    ) as resolve:
        await _start(mass, caplog)

    assert mass.get_provider_manifest("dev_one").tier == ProviderTier.COMMUNITY
    assert mass.get_provider_manifest("builtin").tier == ProviderTier.CORE
    with pytest.raises(KeyError):
        mass.get_provider_manifest("other_domain")
    assert [list(c.args[1]) for c in resolve.call_args_list] == [["dev_one"]]
    deps_path = location.parent / "cache" / "community_deps"
    assert not (deps_path / REQUIREMENT.replace("-", "_")).exists()
    warnings = _warnings(mass, caplog)
    assert len(warnings) == 1
    assert str(location / folder) in warnings[0]
    assert reason in warnings[0]


async def test_provider_with_refused_requirements(
    location: Path, mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A provider whose requirements cannot be installed is not registered; the others are."""
    _write_provider(location, "dev_one", requirements=[REQUIREMENT])
    _write_provider(location, "dev_two", requirements=["--find-links /tmp"])
    mass.dev_mode = True
    mass.dev_providers_path = str(location)

    await _start(mass, caplog)

    assert mass.get_provider_manifest("dev_one").tier == ProviderTier.COMMUNITY
    with pytest.raises(KeyError):
        mass.get_provider_manifest("dev_two")
    assert mass.translations.get_translation("provider.dev_two.manifest.name") is None
    warnings = _warnings(mass, caplog)
    assert len(warnings) == 1
    assert "Requirements of dev_two cannot be installed" in warnings[0]


@pytest.mark.parametrize("safe_mode", [False, True])
async def test_not_started_in_safe_mode(tmp_path: Path, safe_mode: bool) -> None:
    """The server starts the community catalog after its own providers, except in safe mode."""
    with patch.object(CommunityCatalogController, "start", AsyncMock()) as start:
        async with full_mass_context(tmp_path, safe_mode=safe_mode) as mass:
            assert isinstance(mass.community_catalog, CommunityCatalogController)

    assert start.await_count == (0 if safe_mode else 1)


@pytest.mark.parametrize("dev_mode", [False, True])
async def test_dev_mode_in_server_info(mass: MusicAssistant, dev_mode: bool) -> None:
    """The server info tells a client whether the server runs in dev mode."""
    mass.dev_mode = dev_mode

    assert mass.get_server_info().dev_mode is dev_mode


def _write_provider(
    location: Path, domain: str, *, folder: str | None = None, requirements: list[str] | None = None
) -> None:
    """
    Write a plugin provider that imports the test requirement when it is one of its requirements.

    :param location: Folder that holds the provider folders.
    :param domain: Domain in the manifest.
    :param folder: Name of the provider folder, the domain when omitted.
    :param requirements: Requirements in the manifest.
    """
    provider_dir = location / (folder or domain)
    provider_dir.mkdir()
    manifest = {
        "type": "plugin",
        "domain": domain,
        "name": domain,
        "description": "test provider",
        "codeowners": [],
        "requirements": requirements or [],
    }
    (provider_dir / "manifest.json").write_text(json.dumps(manifest))
    (provider_dir / "__init__.py").write_text(PROVIDER_CODE)


def _seed_instance(tmp_path: Path, domain: str) -> None:
    """Write the settings of a server with one enabled instance of a provider."""
    storage_path = tmp_path / "data"
    storage_path.mkdir()
    instance = {
        "type": "plugin",
        "domain": domain,
        "instance_id": domain,
        "enabled": True,
        "name": domain,
        "values": {},
    }
    settings = {CONF_PROVIDERS: {domain: instance}}
    (storage_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


async def _start(mass: MusicAssistant, caplog: pytest.LogCaptureFixture) -> None:
    """Start the community catalog, capturing only what it logs from here on."""
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=mass.community_catalog.logger.name):
        await mass.community_catalog.start()


def _warnings(mass: MusicAssistant, caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the warnings the community catalog logged."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == mass.community_catalog.logger.name and r.levelno == logging.WARNING
    ]


def _assert_nothing_loaded(mass: MusicAssistant, location: Path, uv: AsyncMock) -> None:
    """Assert that nothing of the folder was registered, installed or added to a path."""
    with pytest.raises(KeyError):
        mass.get_provider_manifest("dev_one")
    assert str(location) not in providers_package.__path__
    assert not any(path.endswith("community_deps") for path in sys.path)
    assert not (location.parent / "cache" / "community_deps").exists()
    uv.assert_not_called()
