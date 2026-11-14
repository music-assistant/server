"""Tests for installing and removing community providers, and for which of them load."""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from music_assistant_models.enums import EventType, ProviderStatus, ProviderTier
from music_assistant_models.errors import ActionUnavailable

import music_assistant
from music_assistant import providers as providers_package
from music_assistant.constants import CONF_PROVIDERS
from music_assistant.controllers.community_catalog import CommunityCatalogController, dependencies
from music_assistant.controllers.community_catalog.constants import CONF_ENABLED
from music_assistant.controllers.community_catalog.dependencies import DependencyConflict
from music_assistant.controllers.community_catalog.gate import _REASONS, Verdict
from music_assistant.controllers.community_catalog.models import CommunityProviderState
from music_assistant.helpers.util import purge_provider_modules
from tests.conftest import full_mass_context
from tests.controllers.community_catalog.conftest import ADDED, RELEASED, build_archive
from tests.controllers.community_catalog.test_dependencies import _build_wheel
from tests.controllers.community_catalog.test_installer import _hash
from tests.controllers.community_catalog.test_settings import _switch, _wait_for_start

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator, Sequence

    from music_assistant.mass import MusicAssistant
    from tests.controllers.community_catalog.conftest import GitHub, Registry

DOMAIN = "comm_one"
VERSION = "1.0.0"
# packages that only the test wheels have
REQUIREMENT = "catalog-requirement"
DEV_REQUIREMENT = "dev-requirement"
PROVIDER_CODE = """
from music_assistant.models.plugin import PluginProvider
{imports}

async def setup(mass, manifest, config):
    return PluginProvider(mass, manifest, config)
"""
WHY = "https://example.org/why"


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """
    Return the folder of the installed community providers, with uv kept to the test wheels.

    What a test adds to the import paths or imports from its folders is undone.
    """
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    for name, version in ((REQUIREMENT, "1.0"), (REQUIREMENT, "2.0"), (DEV_REQUIREMENT, "1.0")):
        _build_wheel(wheels, name, version)
    monkeypatch.setattr(dependencies, "_UV_INDEX_ARGS", ["--no-index", "--find-links", str(wheels)])
    monkeypatch.setattr(sys, "path", list(sys.path))
    original_path = list(providers_package.__path__)
    yield tmp_path / "data" / "community_providers"
    providers_package.__path__[:] = original_path
    _forget_imports(tmp_path)


async def test_install(tmp_path: Path, catalog: Path, registry: Registry, github: GitHub) -> None:
    """An installed provider is live and registered, and its settings entry loads, also later."""
    registry.document = _index({DOMAIN: _publish(github, DOMAIN, requirements=[REQUIREMENT])})
    _seed_instance(tmp_path, DOMAIN)

    async with _server(tmp_path) as mass:
        await _switch(mass, enabled=True)
        assert mass.get_provider(DOMAIN) is None
        with patch.object(mass, "signal_event", wraps=mass.signal_event) as signal_event:
            provider = await mass.community_catalog.install_provider(DOMAIN)

        assert provider.state == CommunityProviderState.INSTALLED
        assert provider.installed_version == VERSION
        assert provider.latest_version == VERSION
        assert provider.repository == f"someone/{DOMAIN}"
        assert (catalog / DOMAIN / "manifest.json").exists()
        assert not (catalog / ".staging" / DOMAIN).exists()
        assert _state(catalog)["installed"] == {
            DOMAIN: {
                "version": VERSION,
                "repository": f"someone/{DOMAIN}",
                "commit": f"{DOMAIN}-{VERSION}",
                "content_hash": _hash(_provider_files(DOMAIN, [REQUIREMENT])),
                "verified": True,
                "previous_version": None,
                "pending_version": None,
                "failed_version": None,
                "notice": None,
            }
        }
        manifest = mass.get_provider_manifest(DOMAIN)
        assert manifest.tier == ProviderTier.COMMUNITY
        assert manifest.builtin is False
        assert manifest.support_url == f"https://github.com/someone/{DOMAIN}"
        assert mass.translations.get_translation(f"provider.{DOMAIN}.manifest.name") == "One"
        assert _events(signal_event) == 1
        _assert_loaded(mass, catalog, DOMAIN)
        assert sys.modules[f"{providers_package.__name__}.{DOMAIN}"].REQUIREMENT_VERSION == "2.0"
        assert _states(await mass.community_catalog.get_providers()) == {
            DOMAIN: CommunityProviderState.INSTALLED
        }

    async with _server(tmp_path) as mass:
        _assert_loaded(mass, catalog, DOMAIN)
        manifest = mass.get_provider_manifest(DOMAIN)
        assert manifest.support_url == f"https://github.com/someone/{DOMAIN}"


async def test_restart_without_the_registry(
    tmp_path: Path, catalog: Path, registry: Registry, github: GitHub
) -> None:
    """A restart while the registry cannot be reached loads from the kept copy."""
    await _install(tmp_path, registry, github)
    registry.status = 503

    async with _server(tmp_path) as mass:
        assert registry.requests[-1][0] == "nightly"
        _assert_loaded(mass, catalog, DOMAIN)


async def test_changed_folder_is_installed_again(
    tmp_path: Path, catalog: Path, registry: Registry, github: GitHub
) -> None:
    """A live folder changed by hand is installed again at start; without GitHub it does not load."""
    await _install(tmp_path, registry, github)
    original = (catalog / DOMAIN / "__init__.py").read_bytes()
    (catalog / DOMAIN / "__init__.py").write_bytes(original + b"\nCHANGED = True\n")

    async with _server(tmp_path) as mass:
        assert (catalog / DOMAIN / "__init__.py").read_bytes() == original
        _assert_loaded(mass, catalog, DOMAIN)

    (catalog / DOMAIN / "extra.py").write_text("")
    github.status = 503

    async with _server(tmp_path) as mass:
        assert (catalog / DOMAIN / "extra.py").exists()
        reason = _REASONS[Verdict.REINSTALL_FAILED].split(" (")[0]
        await _assert_not_loaded(mass, DOMAIN, CommunityProviderState.UNAVAILABLE, reason)
        provider = _provider(await mass.community_catalog.get_providers())
        assert "503" in provider.error.message


async def test_folder_not_in_the_state(
    tmp_path: Path, catalog: Path, registry: Registry, github: GitHub
) -> None:
    """A provider folder that is not in the state does not load and is not listed."""
    registry.document = _index({DOMAIN: _publish(github, DOMAIN)})
    _seed_instance(tmp_path, DOMAIN, "stray_one")
    async with _server(tmp_path) as mass:
        await _switch(mass, enabled=True)
        await mass.community_catalog.install_provider(DOMAIN)
    stray = catalog / "stray_one"
    stray.mkdir()
    for name, content in _provider_files("stray_one").items():
        (stray / name).write_bytes(content)

    async with _server(tmp_path) as mass:
        _assert_loaded(mass, catalog, DOMAIN)
        with pytest.raises(KeyError):
            mass.get_provider_manifest("stray_one")
        assert mass.get_provider("stray_one") is None
        assert f"{providers_package.__name__}.stray_one" not in sys.modules
        assert "stray_one" not in _states(await mass.community_catalog.get_providers())


@pytest.mark.parametrize("block", ["provider", "version"])
async def test_blocked_by_a_refresh(
    tmp_path: Path, catalog: Path, registry: Registry, github: GitHub, block: str
) -> None:
    """A block found by a refresh stops the provider; its files, state and settings entry stay."""
    await _install(tmp_path, registry, github)
    entry = _publish(github, DOMAIN)
    if block == "provider":
        entry.update(status="blocked", reason="Sends the library elsewhere", reason_url=WHY)
    else:
        entry.update(blocked_versions={VERSION: "Sends the library elsewhere"}, reason_url=WHY)

    async with _server(tmp_path) as mass:
        _assert_loaded(mass, catalog, DOMAIN)
        registry.document = _index({DOMAIN: entry})
        registry.etag = '"two"'
        with patch.object(mass, "signal_event", wraps=mass.signal_event) as signal_event:
            await mass.community_catalog.refresh()

        assert _events(signal_event) == 1
        await _assert_blocked(mass, catalog)

    async with _server(tmp_path) as mass:
        await _assert_blocked(mass, catalog)


@pytest.mark.usefixtures("catalog")
async def test_no_longer_listed(tmp_path: Path, registry: Registry, github: GitHub) -> None:
    """A provider that leaves the index stops, and stays stopped after a restart."""
    await _install(tmp_path, registry, github)
    reason = _REASONS[Verdict.NOT_LISTED]

    async with _server(tmp_path) as mass:
        registry.document = _index({})
        registry.etag = '"two"'
        await mass.community_catalog.refresh()
        await _assert_not_loaded(mass, DOMAIN, CommunityProviderState.UNAVAILABLE, reason)

    async with _server(tmp_path) as mass:
        await _assert_not_loaded(mass, DOMAIN, CommunityProviderState.UNAVAILABLE, reason)


async def test_copy_of_another_line(
    tmp_path: Path, catalog: Path, registry: Registry, github: GitHub
) -> None:
    """With only a copy of another line the provider loads, nothing is offered, a block applies."""
    await _install(tmp_path, registry, github)
    registry.status = 503
    kept = json.loads((catalog / "index.json").read_bytes())
    kept["server_line"] = "2.11"
    kept["providers"]["other_one"] = _publish(github, "other_one")
    (catalog / "index.json").write_text(json.dumps(kept))

    async with _server(tmp_path) as mass:
        _assert_loaded(mass, catalog, DOMAIN)
        assert _states(await mass.community_catalog.get_providers()) == {
            DOMAIN: CommunityProviderState.INSTALLED
        }
        with pytest.raises(ActionUnavailable, match="does not offer other_one"):
            await mass.community_catalog.install_provider("other_one")

    kept["providers"][DOMAIN]["status"] = "blocked"
    kept["providers"][DOMAIN]["reason"] = "Sends the library elsewhere"
    (catalog / "index.json").write_text(json.dumps(kept))

    async with _server(tmp_path) as mass:
        await _assert_not_loaded(
            mass, DOMAIN, CommunityProviderState.BLOCKED, "Sends the library elsewhere"
        )


async def test_no_kept_copy(
    tmp_path: Path, catalog: Path, registry: Registry, github: GitHub
) -> None:
    """Without any copy of the index nothing from the catalog loads."""
    await _install(tmp_path, registry, github)
    registry.status = 503
    (catalog / "index.json").unlink()

    async with _server(tmp_path) as mass:
        await _assert_not_loaded(
            mass,
            DOMAIN,
            CommunityProviderState.UNAVAILABLE,
            _REASONS[Verdict.CATALOG_UNREACHABLE],
        )


@pytest.mark.parametrize(
    ("domain", "message"),
    [
        ("switched_off", "Switch the community catalog on"),
        ("blocked_one", "does not offer blocked_one"),
        ("blocked_latest_one", "does not offer blocked_latest_one"),
        ("unknown_one", "does not offer unknown_one"),
        (DOMAIN, "installed already"),
        ("builtin", "already has a provider named builtin"),
    ],
)
async def test_install_refused(
    tmp_path: Path,
    catalog: Path,
    registry: Registry,
    github: GitHub,
    domain: str,
    message: str,
) -> None:
    """A refused install stages nothing and changes nothing live or in the state."""
    blocked = _publish(github, "blocked_one")
    blocked.update(status="blocked", reason="Sends the library elsewhere")
    blocked_latest = _publish(github, "blocked_latest_one")
    blocked_latest.update(blocked_versions={VERSION: "Sends the library elsewhere"})
    providers = {
        DOMAIN: _publish(github, DOMAIN),
        "switched_off": _publish(github, "switched_off"),
        "blocked_one": blocked,
        "blocked_latest_one": blocked_latest,
        "builtin": _publish(github, "builtin"),
    }
    registry.document = _index(providers)
    async with _server(tmp_path) as mass:
        await _switch(mass, enabled=True)
        if domain == "switched_off":
            await _switch(mass, enabled=False)
        else:
            await mass.community_catalog.install_provider(DOMAIN)
        before = _snapshot(catalog)

        with pytest.raises(ActionUnavailable, match=message):
            await mass.community_catalog.install_provider(domain)

        assert _snapshot(catalog) == before
        assert mass.get_provider_manifest("builtin").tier == ProviderTier.CORE


@pytest.mark.parametrize(
    ("requirement", "error", "message"),
    [
        (f"{REQUIREMENT}==2.0", DependencyConflict, f"comm_two clash with {DOMAIN}"),
        # no clash, but the version the loaded provider imported would be replaced
        (REQUIREMENT, ActionUnavailable, "cannot be changed while they run"),
    ],
)
async def test_install_refused_on_a_clash(
    tmp_path: Path,
    catalog: Path,
    registry: Registry,
    github: GitHub,
    requirement: str,
    error: type[Exception],
    message: str,
) -> None:
    """Requirements that clash with a loaded provider, or replace a package it uses, are refused."""
    registry.document = _index(
        {
            DOMAIN: _publish(github, DOMAIN, requirements=[requirement]),
            "comm_two": _publish(github, "comm_two", requirements=[f"{REQUIREMENT}==1.0"]),
        }
    )
    _seed_instance(tmp_path, DOMAIN)
    lock = tmp_path / "cache" / "community_deps" / "requirements.lock"
    async with _server(tmp_path) as mass:
        await _switch(mass, enabled=True)
        await mass.community_catalog.install_provider(DOMAIN)
        before = _snapshot(catalog), lock.read_bytes()

        with pytest.raises(error, match=message):
            await mass.community_catalog.install_provider("comm_two")

        assert (_snapshot(catalog), lock.read_bytes()) == before
        with pytest.raises(KeyError):
            mass.get_provider_manifest("comm_two")
        _assert_loaded(mass, catalog, DOMAIN)


async def test_requirements_clash_at_start(
    tmp_path: Path, catalog: Path, registry: Registry, github: GitHub
) -> None:
    """Of two installed providers whose requirements clash, only the one installed first loads."""
    one = _publish(github, DOMAIN, requirements=[f"{REQUIREMENT}==1.0"])
    two = _publish(github, "comm_two", requirements=[f"{REQUIREMENT}==2.0"])
    registry.document = _index({DOMAIN: one, "comm_two": two})
    _seed_instance(tmp_path, DOMAIN, "comm_two")
    async with _server(tmp_path) as mass:
        await _switch(mass, enabled=True)
        await mass.community_catalog.install_provider(DOMAIN)
    # while the first one is blocked, nothing refuses the install of the second
    blocked = one | {"status": "blocked", "reason": "Sends the library elsewhere"}
    registry.document, registry.etag = _index({DOMAIN: blocked, "comm_two": two}), '"two"'
    async with _server(tmp_path) as mass:
        await mass.community_catalog.install_provider("comm_two")
        _assert_loaded(mass, catalog, "comm_two")
    registry.document, registry.etag = _index({DOMAIN: one, "comm_two": two}), '"three"'

    async with _server(tmp_path) as mass:
        _assert_loaded(mass, catalog, DOMAIN)
        await _assert_not_loaded(
            mass,
            "comm_two",
            CommunityProviderState.UNAVAILABLE,
            _REASONS[Verdict.REQUIREMENTS_CLASH],
        )


async def test_remove(tmp_path: Path, catalog: Path, registry: Registry, github: GitHub) -> None:
    """A removed provider is unloaded and deleted; its settings entry stays, without a provider."""
    await _install(tmp_path, registry, github)

    async with _server(tmp_path) as mass:
        _assert_loaded(mass, catalog, DOMAIN)
        with patch.object(mass, "signal_event", wraps=mass.signal_event) as signal_event:
            await mass.community_catalog.remove_provider(DOMAIN)

        assert _events(signal_event) == 1
        assert mass.get_provider(DOMAIN) is None
        with pytest.raises(KeyError):
            mass.get_provider_manifest(DOMAIN)
        assert f"{providers_package.__name__}.{DOMAIN}" not in sys.modules
        assert not (catalog / DOMAIN).exists()
        assert not (catalog / ".previous" / DOMAIN).exists()
        assert _state(catalog)["installed"] == {}
        conf = _settings_entry(
            await mass.config.get_provider_configs(include_unavailable=True), DOMAIN
        )
        assert conf.status == ProviderStatus.UNAVAILABLE
        assert conf.last_error is not None
        assert conf.last_error.translation_key == "provider_no_longer_available"
        assert _states(await mass.community_catalog.get_providers()) == {
            DOMAIN: CommunityProviderState.AVAILABLE
        }
        with pytest.raises(ActionUnavailable, match="not installed"):
            await mass.community_catalog.remove_provider(DOMAIN)


async def test_switch_stays_on_while_a_provider_is_installed(
    tmp_path: Path, catalog: Path, registry: Registry, github: GitHub
) -> None:
    """The switch only goes off once what the admin installed is removed."""
    await _install(tmp_path, registry, github)

    async with _server(tmp_path) as mass:
        with pytest.raises(ActionUnavailable, match="Remove what you installed"):
            await _switch(mass, enabled=False)

        assert mass.community_catalog.config.get_value(CONF_ENABLED) is True
        _assert_loaded(mass, catalog, DOMAIN)

    async with _server(tmp_path) as mass:
        assert mass.community_catalog.config.get_value(CONF_ENABLED) is True
        await mass.community_catalog.remove_provider(DOMAIN)

        await _switch(mass, enabled=False)

        assert mass.community_catalog.config.get_value(CONF_ENABLED) is False


async def test_dev_folder_and_installed_provider(
    tmp_path: Path, catalog: Path, registry: Registry, github: GitHub
) -> None:
    """A dev folder provider and an installed one load together, each with its requirements."""
    await _install(tmp_path, registry, github, requirements=[REQUIREMENT])
    location = tmp_path / "dev_providers"
    (location / "dev_one").mkdir(parents=True)
    for name, content in _provider_files("dev_one", [DEV_REQUIREMENT]).items():
        (location / "dev_one" / name).write_bytes(content)
    original_start = CommunityCatalogController.start

    async def start(self: CommunityCatalogController) -> None:
        self.mass.dev_mode = True
        self.mass.dev_providers_path = str(location)
        await original_start(self)

    with patch.object(CommunityCatalogController, "start", start):
        async with _server(tmp_path) as mass:
            _assert_loaded(mass, catalog, DOMAIN)
            assert mass.get_provider_manifest("dev_one").tier == ProviderTier.COMMUNITY
            deps = tmp_path / "cache" / "community_deps"
            for requirement in (REQUIREMENT, DEV_REQUIREMENT):
                assert (deps / requirement.replace("-", "_") / "__init__.py").exists()


@pytest.mark.usefixtures("catalog")
async def test_safe_mode(tmp_path: Path, registry: Registry, github: GitHub) -> None:
    """In safe mode the commands refuse and nothing of the catalog loads."""
    await _install(tmp_path, registry, github)
    requests = len(registry.requests)

    async with _server(tmp_path, safe_mode=True) as mass:
        with pytest.raises(KeyError):
            mass.get_provider_manifest(DOMAIN)
        assert mass.get_provider(DOMAIN) is None
        conf = _settings_entry(
            await mass.config.get_provider_configs(include_unavailable=True), DOMAIN
        )
        assert conf.status == ProviderStatus.UNAVAILABLE
        assert conf.last_error is not None
        assert conf.last_error.translation_key == "community_provider_safe_mode"
        assert conf.last_error.message == _REASONS[Verdict.SAFE_MODE]
        for command in (
            mass.community_catalog.get_providers(),
            mass.community_catalog.refresh(),
            mass.community_catalog.install_provider("other_one"),
            mass.community_catalog.remove_provider(DOMAIN),
        ):
            with pytest.raises(ActionUnavailable, match="safe mode"):
                await command

    assert len(registry.requests) == requests


async def test_settings_entry_while_the_catalog_starts(
    tmp_path: Path, catalog: Path, registry: Registry, github: GitHub
) -> None:
    """Until the catalog has started, the entry of an installed provider says it is starting."""
    await _install(tmp_path, registry, github)
    registry.delay = 1

    try:
        async with full_mass_context(tmp_path) as mass:
            conf = _settings_entry(
                await mass.config.get_provider_configs(include_unavailable=True), DOMAIN
            )
            assert conf.status == ProviderStatus.UNAVAILABLE
            assert conf.last_error is not None
            assert conf.last_error.translation_key == "community_provider_starting"
            assert conf.last_error.message == _REASONS[Verdict.STARTING]

            await _wait_for_start(mass)
            _assert_loaded(mass, catalog, DOMAIN)
    finally:
        _forget_imports(tmp_path)


async def test_remove_leaves_a_provider_of_the_server_alone(
    tmp_path: Path, catalog: Path, registry: Registry, github: GitHub
) -> None:
    """Removing an installed provider whose domain the server ships by now leaves that one running."""
    await _install(tmp_path, registry, github)
    # a new process has only the server's own providers on the package path
    providers_package.__path__.remove(str(catalog))
    shipped = tmp_path / "shipped" / DOMAIN
    shipped.mkdir(parents=True)
    for name, content in _provider_files(DOMAIN).items():
        (shipped / name).write_bytes(content)
    original_start = CommunityCatalogController.start

    async def start(self: CommunityCatalogController) -> None:
        # the server has a provider of its own by this name before the catalog starts
        self.mass.add_provider_location(str(shipped.parent))
        await self.mass.register_provider_manifest(str(shipped), ProviderTier.CORE)
        await self.mass.load_provider(DOMAIN)
        await original_start(self)

    with patch.object(CommunityCatalogController, "start", start):
        async with _server(tmp_path) as mass:
            module = sys.modules[f"{providers_package.__name__}.{DOMAIN}"]
            assert module.__file__ == str(shipped / "__init__.py")

            await mass.community_catalog.remove_provider(DOMAIN)

            assert not (catalog / DOMAIN).exists()
            provider = mass.get_provider(DOMAIN)
            assert provider is not None
            assert provider.available
            assert sys.modules[f"{providers_package.__name__}.{DOMAIN}"] is module


def test_reasons_match_the_texts() -> None:
    """The reasons in the code are the English texts of their translation keys."""
    strings_file = Path(music_assistant.__file__).parent / "strings.json"
    strings = json.loads(strings_file.read_bytes())["errors"]

    for verdict, text in _REASONS.items():
        assert strings[f"community_provider_{verdict}"] == text


async def _install(
    tmp_path: Path, registry: Registry, github: GitHub, *, requirements: Sequence[str] = ()
) -> None:
    """Boot a server with the catalog on, install the provider with an enabled entry, stop."""
    registry.document = _index({DOMAIN: _publish(github, DOMAIN, requirements=requirements)})
    _seed_instance(tmp_path, DOMAIN)
    async with _server(tmp_path) as mass:
        await _switch(mass, enabled=True)
        await mass.community_catalog.install_provider(DOMAIN)


@asynccontextmanager
async def _server(tmp_path: Path, *, safe_mode: bool = False) -> AsyncGenerator[MusicAssistant]:
    """Boot a server and wait until the community catalog has started; stopped like a process."""
    try:
        async with full_mass_context(tmp_path, safe_mode=safe_mode) as mass:
            await _wait_for_start(mass)
            yield mass
    finally:
        _forget_imports(tmp_path)


def _forget_imports(tmp_path: Path) -> None:
    """Forget the modules imported from the folders of a test, as a new process would."""
    prefix = f"{providers_package.__name__}."
    for name, module in list(sys.modules.items()):
        if not str(getattr(module, "__file__", None)).startswith(str(tmp_path)):
            continue
        if name.startswith(prefix):
            purge_provider_modules(name.removeprefix(prefix).split(".")[0])
        else:
            sys.modules.pop(name, None)


def _publish(github: GitHub, domain: str, *, requirements: Sequence[str] = ()) -> dict[str, Any]:
    """
    Serve a provider on the fixture GitHub and return its listed entry in the index.

    :param github: The fixture GitHub.
    :param domain: The domain of the provider.
    :param requirements: The requirements in its manifest.
    """
    files = _provider_files(domain, requirements)
    commit = f"{domain}-{VERSION}"
    github.archives[commit] = build_archive({f"{domain}/{p}": c for p, c in files.items()})
    return {
        "status": "listed",
        "reason": None,
        "reason_url": None,
        "repository": f"someone/{domain}",
        "maintainers": ["someone"],
        "icon_svg": None,
        "added": ADDED,
        "manifest": json.loads(files["manifest.json"]),
        "latest": VERSION,
        "versions": {
            VERSION: {
                "commit": commit,
                "folder": domain,
                "content_hash": _hash(files),
                "requirements": list(requirements),
                "released": RELEASED,
            }
        },
        "blocked_versions": {},
    }


def _provider_files(domain: str, requirements: Sequence[str] = ()) -> dict[str, bytes]:
    """
    Return the files of a plugin provider that imports the version of its first requirement.

    :param domain: The domain of the provider.
    :param requirements: The requirements in its manifest.
    """
    manifest = {
        "type": "plugin",
        "domain": domain,
        "name": domain,
        "description": "test provider",
        "codeowners": ["@someone"],
        "requirements": list(requirements),
    }
    imports = ""
    if requirements:
        module = requirements[0].split("=")[0].replace("-", "_")
        imports = f"from {module} import VERSION as REQUIREMENT_VERSION"
    return {
        "manifest.json": json.dumps(manifest).encode(),
        "__init__.py": PROVIDER_CODE.format(imports=imports).encode(),
        "strings.json": json.dumps({"manifest": {"name": "One"}}).encode(),
    }


def _index(providers: dict[str, Any]) -> dict[str, Any]:
    """Return an index of the line of a server run from source."""
    return {
        "schema": 1,
        "server_line": "nightly",
        "generated": "2026-11-14T08:00:00Z",
        "providers": providers,
    }


def _seed_instance(tmp_path: Path, *domains: str) -> None:
    """Write the settings of a server with one enabled instance of each provider."""
    storage_path = tmp_path / "data"
    storage_path.mkdir(exist_ok=True)
    instances = {
        domain: {
            "type": "plugin",
            "domain": domain,
            "instance_id": domain,
            "enabled": True,
            "name": domain,
            "values": {},
        }
        for domain in domains
    }
    settings = {CONF_PROVIDERS: instances}
    (storage_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def _assert_loaded(mass: MusicAssistant, catalog: Path, domain: str) -> None:
    """Assert that the instance of a provider runs the code of its live folder."""
    provider = mass.get_provider(domain)
    assert provider is not None
    assert provider.available
    module = sys.modules[f"{providers_package.__name__}.{domain}"]
    assert module.__file__ == str(catalog / domain / "__init__.py")
    assert mass.community_catalog.unavailable_reason(domain) is None


async def _assert_not_loaded(
    mass: MusicAssistant, domain: str, state: CommunityProviderState, reason: str
) -> None:
    """
    Assert that an installed provider is stopped, and that the list and its entry tell why.

    :param mass: The server.
    :param domain: The domain of the provider.
    :param state: Its state in the list.
    :param reason: A text the reason in the list and on its settings entry hold.
    """
    assert mass.get_provider(domain) is None
    with pytest.raises(KeyError):
        mass.get_provider_manifest(domain)
    assert f"{providers_package.__name__}.{domain}" not in sys.modules
    provider = _provider(await mass.community_catalog.get_providers(), domain)
    assert provider.state == state
    assert provider.error is not None
    assert reason in provider.error.message
    conf = _settings_entry(await mass.config.get_provider_configs(include_unavailable=True), domain)
    assert conf.status == ProviderStatus.UNAVAILABLE
    assert conf.last_error is not None
    assert reason in conf.last_error.message
    assert conf.last_error.translation_key is not None
    assert conf.last_error.translation_key.startswith("community_provider_")


async def _assert_blocked(mass: MusicAssistant, catalog: Path) -> None:
    """Assert that the provider is stopped as blocked, with its files and state kept."""
    await _assert_not_loaded(
        mass, DOMAIN, CommunityProviderState.BLOCKED, "Sends the library elsewhere"
    )
    provider = _provider(await mass.community_catalog.get_providers())
    assert provider.reason == "Sends the library elsewhere"
    assert provider.reason_url == WHY
    assert (catalog / DOMAIN / "__init__.py").exists()
    assert DOMAIN in _state(catalog)["installed"]


def _provider(providers: Sequence[Any], domain: str = DOMAIN) -> Any:
    """Return a provider in a list of the community catalog."""
    return next(provider for provider in providers if provider.domain == domain)


def _settings_entry(configs: Sequence[Any], instance_id: str) -> Any:
    """Return the settings entry of an instance."""
    return next(conf for conf in configs if conf.instance_id == instance_id)


def _states(providers: Sequence[Any]) -> dict[str, CommunityProviderState]:
    """Return the state of each provider in a list of the community catalog."""
    return {provider.domain: provider.state for provider in providers}


def _state(catalog: Path) -> Any:
    """Return the state file of the community catalog."""
    return json.loads((catalog / "state.json").read_bytes())


def _snapshot(catalog: Path) -> dict[str, bytes]:
    """Return every file below the community catalog folder except the kept copy of the index."""
    return {
        path.relative_to(catalog).as_posix(): path.read_bytes()
        for path in catalog.rglob("*")
        if path.is_file() and path.name != "index.json"
    }


def _events(signal_event: Any) -> int:
    """Return how often the list of the community catalog was signalled as changed."""
    return [c.args[0] for c in signal_event.call_args_list].count(
        EventType.COMMUNITY_CATALOG_UPDATED
    )
