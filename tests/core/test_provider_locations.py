"""Tests for providers that load from a second folder, next to the server's own."""

from __future__ import annotations

import json
import shutil
import sys
from importlib.machinery import PathFinder
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import ProviderIconVariant, ProviderTier
from music_assistant_models.errors import AlreadyRegisteredError, InvalidDataError

from music_assistant import providers as providers_package
from music_assistant.constants import CONF_PROVIDERS
from music_assistant.helpers.util import load_provider_module, purge_provider_modules
from music_assistant.mass import PROVIDERS_PATH

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import ModuleType

    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

PACKAGE = "music_assistant.providers"
ICON = "<svg/>"


def _write_provider(
    location: Path,
    domain: str,
    *,
    folder: str | None = None,
    version: str = "1",
    icon: bool = False,
    **manifest_keys: object,
) -> Path:
    """
    Write a provider that uses relative and absolute imports of its own modules.

    :param location: Folder that holds the provider folders.
    :param domain: Domain in the manifest.
    :param folder: Name of the provider folder, the domain when omitted.
    :param version: Value of the constant the provider's modules pass around.
    :param icon: Whether the provider ships an icon.
    :param manifest_keys: Extra keys for the manifest.
    """
    provider_dir = location / (folder or domain)
    (provider_dir / "sub").mkdir(parents=True, exist_ok=True)
    (provider_dir / "manifest.json").write_text(
        json.dumps(
            {
                "type": "music",
                "domain": domain,
                "name": domain,
                "description": "test provider",
                "codeowners": [],
                **manifest_keys,
            }
        )
    )
    (provider_dir / "const.py").write_text(f'VERSION = "{version}"\n')
    (provider_dir / "__init__.py").write_text(
        f"from {PACKAGE}.{domain}.const import VERSION as ABSOLUTE\n"
        "from .const import VERSION as RELATIVE\n"
        "from .sub.deep import NESTED\n"
    )
    (provider_dir / "sub" / "__init__.py").write_text("")
    (provider_dir / "sub" / "deep.py").write_text("from ..const import VERSION as NESTED\n")
    if icon:
        (provider_dir / "icon.svg").write_text(ICON)
    return provider_dir


async def _import(manifest: ProviderManifest) -> ModuleType:
    """Import a provider the way the server does."""
    return cast("ModuleType", await load_provider_module(manifest))


@pytest.fixture
def location(tmp_path: Path) -> Iterator[Path]:
    """Return a folder to add as provider location; what a test imports from it is undone."""
    folder = tmp_path / "community_providers"
    folder.mkdir()
    original_path = list(providers_package.__path__)
    imported_before = set(sys.modules)
    yield folder
    providers_package.__path__[:] = original_path
    for provider_dir in folder.iterdir():
        # a shipped provider of the same name, imported elsewhere, stays imported
        if f"{PACKAGE}.{provider_dir.name}" not in imported_before:
            purge_provider_modules(provider_dir.name)


async def test_provider_imports_from_added_location(
    mass_minimal: MusicAssistant, location: Path
) -> None:
    """A provider in an added location imports under the name a shipped provider would have."""
    provider_dir = _write_provider(location, "second_location")
    mass_minimal.add_provider_location(str(location))
    manifest = await mass_minimal.register_provider_manifest(
        str(provider_dir), ProviderTier.COMMUNITY
    )

    module = await _import(manifest)

    assert module.__name__ == f"{PACKAGE}.second_location"
    assert module.__file__ == str(provider_dir / "__init__.py")
    assert (module.ABSOLUTE, module.RELATIVE, module.NESTED) == ("1", "1", "1")


async def test_shipped_provider_wins_over_added_location(
    mass_minimal: MusicAssistant, location: Path
) -> None:
    """A folder in an added location never takes the place of a shipped provider."""
    _write_provider(location, "builtin")
    original_path = list(providers_package.__path__)

    mass_minimal.add_provider_location(str(location))
    mass_minimal.add_provider_location(str(location))

    assert providers_package.__path__ == [*original_path, str(location)]
    spec = PathFinder.find_spec(f"{PACKAGE}.builtin", providers_package.__path__)
    assert spec is not None
    assert spec.origin is not None
    assert spec.origin.startswith(PROVIDERS_PATH)


async def test_community_provider_can_not_take_shipped_domain(
    mass_minimal: MusicAssistant, location: Path, tmp_path: Path
) -> None:
    """A community provider is refused when a shipped provider has its domain."""
    shipped_dir = _write_provider(tmp_path / "shipped", "taken", icon=True)
    shipped = await mass_minimal.register_provider_manifest(str(shipped_dir), ProviderTier.CORE)
    community_dir = _write_provider(location, "taken")

    with pytest.raises(AlreadyRegisteredError):
        await mass_minimal.register_provider_manifest(str(community_dir), ProviderTier.COMMUNITY)

    assert mass_minimal.get_provider_manifest("taken") is shipped
    assert shipped.tier == ProviderTier.CORE
    # the shipped provider keeps its icon, the community folder has none
    assert mass_minimal.get_provider_icon("taken") == ("image/svg+xml", ICON.encode())


async def test_community_provider_can_not_take_core_controller_domain(
    mass_minimal: MusicAssistant, location: Path
) -> None:
    """A community provider is refused when a core controller has its domain."""
    controller_manifest = mass_minimal.discovery.manifest
    mass_minimal._provider_manifests["discovery"] = controller_manifest
    community_dir = _write_provider(location, "discovery")

    with pytest.raises(AlreadyRegisteredError):
        await mass_minimal.register_provider_manifest(str(community_dir), ProviderTier.COMMUNITY)

    assert mass_minimal.get_provider_manifest("discovery") is controller_manifest


async def test_community_manifest_can_not_claim_to_be_builtin(
    mass_minimal: MusicAssistant, location: Path
) -> None:
    """Tier, builtin and allow_disable of a community provider do not come from its manifest."""
    provider_dir = _write_provider(
        location, "claims_builtin", tier="core", builtin=True, allow_disable=False
    )

    manifest = await mass_minimal.register_provider_manifest(
        str(provider_dir), ProviderTier.COMMUNITY
    )

    assert manifest is mass_minimal.get_provider_manifest("claims_builtin")
    assert manifest.tier == ProviderTier.COMMUNITY
    assert manifest.builtin is False
    assert manifest.allow_disable is True


async def test_community_folder_must_be_named_after_domain(
    mass_minimal: MusicAssistant, location: Path
) -> None:
    """A community provider whose folder name differs from its domain is refused."""
    provider_dir = _write_provider(location, "real_domain", folder="other_name", icon=True)

    with pytest.raises(InvalidDataError):
        await mass_minimal.register_provider_manifest(str(provider_dir), ProviderTier.COMMUNITY)

    assert mass_minimal.get_provider_manifests() == []
    assert mass_minimal.get_provider_icon("real_domain") is None


async def test_purged_provider_imports_its_new_code(
    mass_minimal: MusicAssistant, location: Path
) -> None:
    """After a purge, importing a provider again runs the code that is in its folder now."""
    provider_dir = _write_provider(location, "replaced")
    mass_minimal.add_provider_location(str(location))
    manifest = await mass_minimal.register_provider_manifest(
        str(provider_dir), ProviderTier.COMMUNITY
    )
    old_module = await _import(manifest)

    # a new version replaces the folder as a whole
    shutil.rmtree(provider_dir)
    _write_provider(location, "replaced", version="2")
    assert await _import(manifest) is old_module

    purge_provider_modules("replaced")

    assert not [name for name in sys.modules if name.startswith(f"{PACKAGE}.replaced")]
    assert not hasattr(providers_package, "replaced")
    new_module = await _import(manifest)
    assert new_module is not old_module
    assert (new_module.ABSOLUTE, new_module.RELATIVE, new_module.NESTED) == ("2", "2", "2")


async def test_purge_leaves_other_providers_alone(
    mass_minimal: MusicAssistant, location: Path
) -> None:
    """A purge does not touch a provider whose domain only starts with the purged one."""
    mass_minimal.add_provider_location(str(location))
    modules = []
    for domain in ("name", "name_longer"):
        manifest = await mass_minimal.register_provider_manifest(
            str(_write_provider(location, domain)), ProviderTier.COMMUNITY
        )
        modules.append(await _import(manifest))

    purge_provider_modules("name")

    assert f"{PACKAGE}.name" not in sys.modules
    assert sys.modules[f"{PACKAGE}.name_longer"] is modules[1]
    assert sys.modules[f"{PACKAGE}.name_longer.sub.deep"]


async def test_unregister_removes_manifest_icons_and_pending_loads(
    mass_minimal: MusicAssistant, location: Path
) -> None:
    """Unregistering a community provider drops its manifest, its icons and its load retries."""
    provider_dir = _write_provider(location, "removed", icon=True)
    await mass_minimal.register_provider_manifest(str(provider_dir), ProviderTier.COMMUNITY)
    assert mass_minimal.get_provider_icon("removed", ProviderIconVariant.DEFAULT) is not None
    for instance_id, domain in (("removed--1", "removed"), ("removed_not--1", "removed_not")):
        mass_minimal.config.set(
            f"{CONF_PROVIDERS}/{instance_id}", {"domain": domain, "instance_id": instance_id}
        )
    retry = mass_minimal.call_later(3600, print, task_id="load_provider_removed--1")
    other_retry = mass_minimal.call_later(3600, print, task_id="load_provider_removed_not--1")

    await mass_minimal.unregister_provider_manifest("removed")

    assert mass_minimal.get_provider_manifests() == []
    assert mass_minimal.get_provider_icon("removed") is None
    assert retry.cancelled()
    assert "load_provider_removed--1" not in mass_minimal._tracked_timers
    assert not other_retry.cancelled()
    mass_minimal.cancel_timer("load_provider_removed_not--1")


async def test_unregister_leaves_shipped_provider_alone(
    mass_minimal: MusicAssistant, tmp_path: Path
) -> None:
    """Only a community provider can be unregistered."""
    shipped_dir = _write_provider(tmp_path / "shipped", "shipped_one", icon=True)
    shipped = await mass_minimal.register_provider_manifest(str(shipped_dir), ProviderTier.CORE)

    await mass_minimal.unregister_provider_manifest("shipped_one")
    await mass_minimal.unregister_provider_manifest("never_registered")

    assert mass_minimal.get_provider_manifests() == [shipped]
    assert mass_minimal.get_provider_icon("shipped_one") is not None


async def test_mdns_browser_follows_registered_manifests(
    mass_minimal: MusicAssistant, location: Path
) -> None:
    """The mDNS browser is refreshed only for a manifest that asks for mDNS discovery."""
    plain_dir = _write_provider(location, "plain")
    mdns_dir = _write_provider(location, "with_mdns", mdns_discovery=["_test._tcp.local."])

    with patch.object(mass_minimal.discovery, "refresh_mdns_browser", AsyncMock()) as refresh:
        await mass_minimal.register_provider_manifest(str(plain_dir), ProviderTier.COMMUNITY)
        await mass_minimal.unregister_provider_manifest("plain")
        refresh.assert_not_awaited()

        await mass_minimal.register_provider_manifest(str(mdns_dir), ProviderTier.COMMUNITY)
        assert refresh.await_count == 1
        await mass_minimal.unregister_provider_manifest("with_mdns")
        assert refresh.await_count == 2
