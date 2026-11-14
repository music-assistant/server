"""Tests for updating community providers, by hand and automatically, and for going back."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import EventType, PlaybackState
from music_assistant_models.errors import ActionUnavailable

from music_assistant import providers as providers_package
from music_assistant.controllers.community_catalog import dependencies
from music_assistant.controllers.community_catalog.controller import (
    _INDEX_CHECK,
    _REASONS,
    _UPDATE_RETRY,
)
from music_assistant.controllers.community_catalog.dependencies import DependencyConflict
from music_assistant.controllers.community_catalog.gate import Verdict
from music_assistant.controllers.community_catalog.installer import InstallError
from music_assistant.controllers.community_catalog.models import CommunityProviderState
from tests.controllers.community_catalog.conftest import ADDED, RELEASED, build_archive
from tests.controllers.community_catalog.test_controller import (  # noqa: F401 - a fixture
    DEV_REQUIREMENT,
    REQUIREMENT,
    _assert_not_loaded,
    _index,
    _seed_instance,
    _server,
    _settings_entry,
    _state,
    _states,
    catalog,
)
from tests.controllers.community_catalog.test_installer import _hash
from tests.controllers.community_catalog.test_settings import _switch, _wait_for_start

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from music_assistant.mass import MusicAssistant
    from tests.controllers.community_catalog.conftest import GitHub, Registry

DOMAIN = "comm_one"
OTHER = "comm_two"
OLD, NEW, NEWER = "1.0.0", "2.0.0", "3.0.0"
PROVIDER_CODE = """
from music_assistant.models.plugin import PluginProvider
{imports}

CODE_VERSION = {version!r}


async def setup(mass, manifest, config):
    {setup}
    return PluginProvider(mass, manifest, config)
"""
pytestmark = pytest.mark.usefixtures("catalog")


async def test_update(tmp_path: Path, registry: Registry, github: GitHub) -> None:
    """An update by hand makes the new version live and runs it; another provider keeps running."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry, OTHER: _entry(github, OTHER)})
    folder = _catalog(tmp_path)

    async with _server(tmp_path) as mass:
        other_module = sys.modules[f"{providers_package.__name__}.{OTHER}"]
        other_instance = mass.get_provider(OTHER)
        await _publish(mass, registry, github, {NEW: _files(DOMAIN, NEW)}, entry)
        assert _states(await mass.community_catalog.get_providers())[DOMAIN] == (
            CommunityProviderState.UPDATE_AVAILABLE
        )
        with patch.object(mass, "signal_event", wraps=mass.signal_event) as signal_event:
            providers = await mass.community_catalog.update_providers(DOMAIN)

        assert _states(providers)[DOMAIN] == CommunityProviderState.INSTALLED
        assert EventType.COMMUNITY_CATALOG_UPDATED in [c.args[0] for c in signal_event.mock_calls]
        _assert_runs(mass, DOMAIN, NEW)
        assert _code_version(folder / ".previous" / DOMAIN) == OLD
        assert _installed(folder) == {
            "version": NEW,
            "repository": f"someone/{DOMAIN}",
            "commit": f"{DOMAIN}-{NEW}",
            "content_hash": _hash(_files(DOMAIN, NEW)),
            "verified": True,
            "previous_version": OLD,
            "pending_version": None,
            "failed_version": None,
            "notice": None,
        }
        assert sys.modules[f"{providers_package.__name__}.{OTHER}"] is other_module
        assert mass.get_provider(OTHER) is other_instance


async def test_update_reloads_a_dependent(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """A provider that depends on the updated one is loaded again with it."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    dependent = _entry(github, OTHER, {OLD: _files(OTHER, OLD, depends_on=DOMAIN)})
    await _install(tmp_path, registry, {DOMAIN: entry, OTHER: dependent})

    async with _server(tmp_path) as mass:
        module = sys.modules[f"{providers_package.__name__}.{OTHER}"]
        await _publish(mass, registry, github, {NEW: _files(DOMAIN, NEW)}, entry)

        await mass.community_catalog.update_providers(DOMAIN)

        _assert_runs(mass, DOMAIN, NEW)
        _assert_runs(mass, OTHER, OLD)
        assert sys.modules[f"{providers_package.__name__}.{OTHER}"] is not module


async def test_update_of_an_imported_package(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """An update that changes an imported package waits for a restart, which makes it live."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD, [f"{REQUIREMENT}==1.0"])})
    await _install(tmp_path, registry, {DOMAIN: entry})
    folder = _catalog(tmp_path)
    new_files = _files(DOMAIN, NEW, [f"{REQUIREMENT}==2.0"])

    async with _server(tmp_path) as mass:
        await _publish(mass, registry, github, {NEW: new_files}, entry)

        providers = await mass.community_catalog.update_providers(DOMAIN)

        provider = _provider(providers)
        assert provider.state == CommunityProviderState.RESTART_REQUIRED
        assert provider.pending_version == NEW
        assert _installed(folder)["pending_version"] == NEW
        assert _installed(folder)["version"] == OLD
        assert (folder / ".staging" / DOMAIN).is_dir()
        _assert_runs(mass, DOMAIN, OLD)
        with pytest.raises(ActionUnavailable, match="no update"):
            await mass.community_catalog.update_providers(DOMAIN)

    async with _server(tmp_path) as mass:
        _assert_runs(mass, DOMAIN, NEW)
        assert sys.modules[f"{providers_package.__name__}.{DOMAIN}"].REQUIREMENT_VERSION == "2.0"
        assert _installed(folder)["version"] == NEW
        assert _installed(folder)["pending_version"] is None
        assert _installed(folder)["verified"] is True
        assert not (folder / ".staging" / DOMAIN).exists()


async def test_changed_staged_folder_is_dropped(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """A staged version whose folder changed before the restart is dropped; the old one stays."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD, [f"{REQUIREMENT}==1.0"])})
    await _install(tmp_path, registry, {DOMAIN: entry})
    folder = _catalog(tmp_path)

    async with _server(tmp_path) as mass:
        new_files = _files(DOMAIN, NEW, [f"{REQUIREMENT}==2.0"])
        await _publish(mass, registry, github, {NEW: new_files}, entry)
        await mass.community_catalog.update_providers(DOMAIN)
    (folder / ".staging" / DOMAIN / "extra.py").write_text("")

    async with _server(tmp_path) as mass:
        _assert_runs(mass, DOMAIN, OLD)
        assert _installed(folder)["pending_version"] is None
        assert not (folder / ".staging" / DOMAIN).exists()


async def test_update_refused_on_a_clash(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """An update whose requirements clash is refused; nothing stays staged, the old one runs."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    other = _entry(github, OTHER, {OLD: _files(OTHER, OLD, [f"{REQUIREMENT}==2.0"])})
    await _install(tmp_path, registry, {DOMAIN: entry, OTHER: other})
    folder = _catalog(tmp_path)

    async with _server(tmp_path) as mass:
        new_files = _files(DOMAIN, NEW, [f"{REQUIREMENT}==1.0"])
        await _publish(mass, registry, github, {NEW: new_files}, entry)
        before = _installed(folder)

        with pytest.raises(DependencyConflict, match=f"{DOMAIN} clash with {OTHER}"):
            await mass.community_catalog.update_providers(DOMAIN)

        assert not (folder / ".staging" / DOMAIN).exists()
        assert _installed(folder) == before
        _assert_runs(mass, DOMAIN, OLD)


async def test_failed_update_goes_back(tmp_path: Path, registry: Registry, github: GitHub) -> None:
    """A new version that fails with a bug goes back; it is not updated to again, a newer one is."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry})
    folder = _catalog(tmp_path)

    async with _server(tmp_path) as mass:
        broken = _files(DOMAIN, NEW, setup='raise TypeError("broken")')
        await _publish(mass, registry, github, {NEW: broken}, entry)

        providers = await mass.community_catalog.update_providers(DOMAIN)

        _assert_runs(mass, DOMAIN, OLD)
        assert _installed(folder)["version"] == OLD
        assert _installed(folder)["failed_version"] == NEW
        assert _installed(folder)["previous_version"] is None
        assert not (folder / ".previous" / DOMAIN).exists()
        assert _states(providers)[DOMAIN] == CommunityProviderState.INSTALLED
        assert _provider(providers).failed_version == NEW
        await mass.community_catalog.update_providers()
        _assert_runs(mass, DOMAIN, OLD)

        await _publish(mass, registry, github, {NEWER: _files(DOMAIN, NEWER)}, entry, '"three"')
        assert _states(await mass.community_catalog.get_providers())[DOMAIN] == (
            CommunityProviderState.UPDATE_AVAILABLE
        )


async def test_handled_error_does_not_go_back(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """A new version that fails with a handled error stays, and its settings entry shows why."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry})
    folder = _catalog(tmp_path)

    async with _server(tmp_path) as mass:
        failing = _files(
            DOMAIN,
            NEW,
            imports="from music_assistant_models.errors import LoginFailed",
            setup='raise LoginFailed("wrong password")',
        )
        await _publish(mass, registry, github, {NEW: failing}, entry)

        await mass.community_catalog.update_providers(DOMAIN)

        assert mass.get_provider(DOMAIN) is None
        assert _installed(folder)["version"] == NEW
        assert _installed(folder)["verified"] is False
        assert _installed(folder)["failed_version"] is None
        configs = await mass.config.get_provider_configs(include_unavailable=True)
        assert "wrong password" in _settings_entry(configs, DOMAIN).last_error.message


@pytest.mark.parametrize("imports", [True, False])
async def test_without_a_settings_entry(
    tmp_path: Path, registry: Registry, github: GitHub, imports: bool
) -> None:
    """Without an enabled settings entry, a version that imports is verified, one that does not goes back."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry}, instances=())
    folder = _catalog(tmp_path)

    async with _server(tmp_path) as mass:
        new_files = _files(DOMAIN, NEW, imports="" if imports else "import does_not_exist")
        await _publish(mass, registry, github, {NEW: new_files}, entry)

        await mass.community_catalog.update_providers(DOMAIN)

        installed = _installed(folder)
        if imports:
            assert (installed["version"], installed["verified"]) == (NEW, True)
        else:
            assert (installed["version"], installed["failed_version"]) == (OLD, NEW)
            assert _code_version(folder / DOMAIN) == OLD


async def test_going_back_to_a_version_no_longer_listed(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """Going back to a version the folder no longer lists leaves the provider stopped."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry})
    folder = _catalog(tmp_path)

    async with _server(tmp_path) as mass:
        broken = _files(DOMAIN, NEW, setup='raise TypeError("broken")')
        replacement = _entry(github, DOMAIN, {NEW: broken})
        # the switch away from a version that is no longer listed is not left to auto_update
        await _publish(mass, registry, github, {}, replacement)

        assert _installed(folder)["failed_version"] == NEW
        assert _code_version(folder / DOMAIN) == OLD
        reason = _REASONS[Verdict.WENT_BACK].format(NEW)
        await _assert_not_loaded(mass, DOMAIN, CommunityProviderState.UNAVAILABLE, reason)


async def test_switch_at_start(tmp_path: Path, registry: Registry, github: GitHub) -> None:
    """At start a version that is no longer listed is switched to the latest one."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry})
    registry.document = _index({DOMAIN: _entry(github, DOMAIN, {NEW: _files(DOMAIN, NEW)})})
    registry.etag = '"two"'

    async with _server(tmp_path) as mass:
        _assert_runs(mass, DOMAIN, NEW)


@pytest.mark.parametrize("latest", ["missing", "failed"])
async def test_nothing_to_switch_to_at_start(
    tmp_path: Path, registry: Registry, github: GitHub, latest: str
) -> None:
    """At start a version that is no longer listed, without a latest one to switch to, does not load."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry})
    folder = _catalog(tmp_path)
    replacement = _entry(github, DOMAIN, {NEW: _files(DOMAIN, NEW)})
    if latest == "missing":
        replacement["latest"] = None
    else:
        state = _state(folder)
        state["installed"][DOMAIN]["failed_version"] = NEW
        (folder / "state.json").write_text(json.dumps(state))
    registry.document = _index({DOMAIN: replacement})
    registry.etag = '"two"'

    async with _server(tmp_path) as mass:
        reason = _REASONS[Verdict.NOT_FOR_THIS_VERSION]
        await _assert_not_loaded(mass, DOMAIN, CommunityProviderState.UNAVAILABLE, reason)
        assert _code_version(folder / DOMAIN) == OLD


async def test_auto_update_at_start(tmp_path: Path, registry: Registry, github: GitHub) -> None:
    """With automatic updates on, an update on offer is installed at start."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry}, auto_update=True)
    _add_version(github, entry, NEW, _files(DOMAIN, NEW))
    registry.document = _index({DOMAIN: entry})
    registry.etag = '"two"'

    async with _server(tmp_path) as mass:
        _assert_runs(mass, DOMAIN, NEW)


async def test_auto_update_waits_for_playback(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """After a changed fetch an automatic update waits while a player plays, then is done."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry}, auto_update=True)
    player = MagicMock()
    player.state.playback_state = PlaybackState.PLAYING

    async with _server(tmp_path) as mass:
        with patch.object(mass.players, "iter_players", return_value=[player]):
            await _publish(mass, registry, github, {NEW: _files(DOMAIN, NEW)}, entry)

            _assert_runs(mass, DOMAIN, OLD)
            delay = mass._tracked_timers[_UPDATE_RETRY].when() - mass.loop.time()
            assert 3500 < delay <= 3600

        # what the timer runs when it is due
        mass.cancel_timer(_UPDATE_RETRY)
        await mass.community_catalog._apply_updates()

        _assert_runs(mass, DOMAIN, NEW)
        assert _UPDATE_RETRY not in mass._tracked_timers


async def test_auto_update_by_the_daily_check(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """The daily check that finds an update installs it while nothing plays."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry}, auto_update=True)

    async with _server(tmp_path) as mass:
        _add_version(github, entry, NEW, _files(DOMAIN, NEW))
        registry.document = _index({DOMAIN: entry})
        registry.etag = '"two"'
        await mass.create_task(mass.community_catalog._check_index(), task_id=_INDEX_CHECK)

        _assert_runs(mass, DOMAIN, NEW)


async def test_no_update_without_auto_update(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """With automatic updates off nothing updates by itself, not after a fetch nor at start."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry})

    async with _server(tmp_path) as mass:
        await _publish(mass, registry, github, {NEW: _files(DOMAIN, NEW)}, entry)
        _assert_runs(mass, DOMAIN, OLD)

    async with _server(tmp_path) as mass:
        _assert_runs(mass, DOMAIN, OLD)
        assert _states(await mass.community_catalog.get_providers())[DOMAIN] == (
            CommunityProviderState.UPDATE_AVAILABLE
        )


async def test_blocked_while_an_update_is_pending(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """A running version that is blocked while its update waits for a restart is stopped at once."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD, [f"{REQUIREMENT}==1.0"])})
    await _install(tmp_path, registry, {DOMAIN: entry})

    async with _server(tmp_path) as mass:
        new_files = _files(DOMAIN, NEW, [f"{REQUIREMENT}==2.0"])
        await _publish(mass, registry, github, {NEW: new_files}, entry)
        await mass.community_catalog.update_providers(DOMAIN)
        entry["blocked_versions"] = {OLD: "Sends the library elsewhere"}
        registry.document = _index({DOMAIN: entry})
        registry.etag = '"three"'

        await mass.community_catalog.refresh()

        await _assert_not_loaded(
            mass, DOMAIN, CommunityProviderState.BLOCKED, "Sends the library elsewhere"
        )


async def test_update_all(tmp_path: Path, registry: Registry, github: GitHub) -> None:
    """Updating without a domain updates every provider that has an update on offer."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    other = _entry(github, OTHER, {OLD: _files(OTHER, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry, OTHER: other})

    async with _server(tmp_path) as mass:
        _add_version(github, other, NEW, _files(OTHER, NEW))
        await _publish(mass, registry, github, {NEW: _files(DOMAIN, NEW)}, entry)

        providers = await mass.community_catalog.update_providers()

        _assert_runs(mass, DOMAIN, NEW)
        _assert_runs(mass, OTHER, NEW)
        assert set(_states(providers).values()) == {CommunityProviderState.INSTALLED}


async def test_update_refused(tmp_path: Path, registry: Registry, github: GitHub) -> None:
    """Updating a provider that is not installed or has nothing on offer raises, as safe mode does."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry})

    async with _server(tmp_path) as mass:
        with pytest.raises(ActionUnavailable, match="not installed"):
            await mass.community_catalog.update_providers(OTHER)
        with pytest.raises(ActionUnavailable, match="no update"):
            await mass.community_catalog.update_providers(DOMAIN)

    async with _server(tmp_path, safe_mode=True) as mass:
        with pytest.raises(ActionUnavailable, match="safe mode"):
            await mass.community_catalog.update_providers()


async def test_failed_version_at_start_without_requirements(
    tmp_path: Path, registry: Registry, github: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version that fails at a start without its requirements goes back, and is offered again."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD, [f"{REQUIREMENT}==1.0"])})
    await _install(tmp_path, registry, {DOMAIN: entry})
    folder = _catalog(tmp_path)

    async with _server(tmp_path) as mass:
        new_files = _files(
            DOMAIN, NEW, [f"{REQUIREMENT}==2.0", DEV_REQUIREMENT], imports="import dev_requirement"
        )
        await _publish(mass, registry, github, {NEW: new_files}, entry)
        await mass.community_catalog.update_providers(DOMAIN)
        assert _installed(folder)["pending_version"] == NEW
    # the package index cannot be reached
    monkeypatch.setattr(
        dependencies, "_UV_INDEX_ARGS", ["--no-index", "--find-links", str(tmp_path / "missing")]
    )

    async with _server(tmp_path) as mass:
        _assert_runs(mass, DOMAIN, OLD)
        installed = _installed(folder)
        assert (installed["version"], installed["verified"]) == (OLD, True)
        assert installed["failed_version"] is None
        provider = _provider(await mass.community_catalog.get_providers())
        assert provider.state == CommunityProviderState.UPDATE_AVAILABLE
        assert provider.latest_version == NEW


async def test_update_by_name_installs_a_failed_version(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """A version marked as failed is installed only when asked for by name, which drops the mark."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry})
    folder = _catalog(tmp_path)
    _add_version(github, entry, NEW, _files(DOMAIN, NEW))
    registry.document = _index({DOMAIN: entry})
    registry.etag = '"two"'
    state = _state(folder)
    state["installed"][DOMAIN]["failed_version"] = NEW
    (folder / "state.json").write_text(json.dumps(state))

    async with _server(tmp_path) as mass:
        provider = _provider(await mass.community_catalog.get_providers())
        assert (provider.state, provider.failed_version) == (CommunityProviderState.INSTALLED, NEW)
        await mass.community_catalog.update_providers()
        _assert_runs(mass, DOMAIN, OLD)

        providers = await mass.community_catalog.update_providers(DOMAIN)

        _assert_runs(mass, DOMAIN, NEW)
        assert _provider(providers).failed_version is None
        assert _installed(folder)["failed_version"] is None


async def test_reinstall_keeps_the_previous_version(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """Installing a changed folder of a version that is not verified again keeps the version before."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry})
    folder = _catalog(tmp_path)
    failing = _files(
        DOMAIN,
        NEW,
        imports="from music_assistant_models.errors import LoginFailed",
        setup='raise LoginFailed("wrong password")',
    )

    async with _server(tmp_path) as mass:
        await _publish(mass, registry, github, {NEW: failing}, entry)
        await mass.community_catalog.update_providers(DOMAIN)
        assert _installed(folder)["verified"] is False
    (folder / DOMAIN / "extra.py").write_text("")

    async with _server(tmp_path) as mass:
        assert not (folder / DOMAIN / "extra.py").exists()
        assert _code_version(folder / DOMAIN) == NEW
        assert _code_version(folder / ".previous" / DOMAIN) == OLD
        assert _installed(folder)["previous_version"] == OLD


async def test_failed_stage_drops_the_pending_version(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """A pending version that staging a newer one gave up is no longer pending."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD, [f"{REQUIREMENT}==1.0"])})
    await _install(tmp_path, registry, {DOMAIN: entry})
    folder = _catalog(tmp_path)

    async with _server(tmp_path) as mass:
        new_files = _files(DOMAIN, NEW, [f"{REQUIREMENT}==2.0"])
        await _publish(mass, registry, github, {NEW: new_files}, entry)
        await mass.community_catalog.update_providers(DOMAIN)
        await _publish(mass, registry, github, {NEWER: _files(DOMAIN, NEWER)}, entry, '"three"')
        github.status = 503

        with pytest.raises(InstallError, match="503"):
            await mass.community_catalog.update_providers(DOMAIN)

        assert _installed(folder)["pending_version"] is None
        assert not (folder / ".staging" / DOMAIN).exists()
        provider = _provider(await mass.community_catalog.get_providers())
        assert (provider.state, provider.pending_version) == (
            CommunityProviderState.UPDATE_AVAILABLE,
            None,
        )
        _assert_runs(mass, DOMAIN, OLD)


async def test_failed_switch_is_tried_again_by_the_next_check(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """A switch that failed at start is done by the next check, also when the index did not change."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry})
    registry.document = _index({DOMAIN: _entry(github, DOMAIN, {NEW: _files(DOMAIN, NEW)})})
    registry.etag = '"two"'
    github.status = 503

    async with _server(tmp_path) as mass:
        reason = _REASONS[Verdict.NOT_FOR_THIS_VERSION]
        await _assert_not_loaded(mass, DOMAIN, CommunityProviderState.UNAVAILABLE, reason)
        github.status = 200

        await mass.create_task(mass.community_catalog._check_index(), task_id=_INDEX_CHECK)

        assert registry.requests[-1][1] == '"two"'
        _assert_runs(mass, DOMAIN, NEW)


@pytest.mark.parametrize("turned_on", ["auto_update", "catalog_enabled"])
async def test_turning_on_auto_update_installs_the_updates(
    tmp_path: Path, registry: Registry, github: GitHub, turned_on: str
) -> None:
    """Turning on automatic updates, or the switch while they are on, installs the updates on offer."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry})

    async with _server(tmp_path) as mass:
        if turned_on == "catalog_enabled":
            await mass.config.save_core_config(
                "community_catalog", {"catalog_enabled": False, "auto_update": True}
            )
        await _publish(mass, registry, github, {NEW: _files(DOMAIN, NEW)}, entry)
        _assert_runs(mass, DOMAIN, OLD)

        await mass.config.save_core_config(
            "community_catalog", {"catalog_enabled": True, "auto_update": True}
        )
        task = mass.get_task(_UPDATE_RETRY if turned_on == "auto_update" else _INDEX_CHECK)
        assert task is not None
        await task

        _assert_runs(mass, DOMAIN, NEW)


async def test_no_auto_update_while_switched_off(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """Automatic updates saved as on do nothing while the catalog is switched off."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry})

    async with _server(tmp_path) as mass:
        await mass.config.save_core_config(
            "community_catalog", {"catalog_enabled": False, "auto_update": True}
        )
        await _publish(mass, registry, github, {NEW: _files(DOMAIN, NEW)}, entry)
        _assert_runs(mass, DOMAIN, OLD)

    async with _server(tmp_path) as mass:
        _assert_runs(mass, DOMAIN, OLD)
        assert _states(await mass.community_catalog.get_providers())[DOMAIN] == (
            CommunityProviderState.UPDATE_AVAILABLE
        )


async def test_live_switch(tmp_path: Path, registry: Registry, github: GitHub) -> None:
    """A version that leaves the index while it runs is switched to the latest one."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry})
    folder = _catalog(tmp_path)

    async with _server(tmp_path) as mass:
        replacement = _entry(github, DOMAIN, {NEW: _files(DOMAIN, NEW)})

        await _publish(mass, registry, github, {}, replacement)

        _assert_runs(mass, DOMAIN, NEW)
        assert (_installed(folder)["version"], _installed(folder)["previous_version"]) == (NEW, OLD)


async def test_blocked_version_switches_to_the_latest(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """A running version that is blocked is stopped and switched to the version on offer."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry})

    async with _server(tmp_path) as mass:
        entry["blocked_versions"] = {OLD: "Leaks the password"}

        await _publish(mass, registry, github, {NEW: _files(DOMAIN, NEW)}, entry)

        _assert_runs(mass, DOMAIN, NEW)


@pytest.mark.parametrize("how", ["pending", "auto_update"])
async def test_going_back_at_start(
    tmp_path: Path, registry: Registry, github: GitHub, how: str
) -> None:
    """A version made live at start that fails with a bug goes back before the start is done."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD, [f"{REQUIREMENT}==1.0"])})
    await _install(tmp_path, registry, {DOMAIN: entry}, auto_update=how == "auto_update")
    folder = _catalog(tmp_path)
    if how == "pending":
        # no longer needing a package that is imported waits for a restart
        broken = _files(DOMAIN, NEW, setup='raise TypeError("broken")')
        async with _server(tmp_path) as mass:
            await _publish(mass, registry, github, {NEW: broken}, entry)
            await mass.community_catalog.update_providers(DOMAIN)
            assert _installed(folder)["pending_version"] == NEW
    else:
        broken = _files(DOMAIN, NEW, [f"{REQUIREMENT}==1.0"], setup='raise TypeError("broken")')
        _add_version(github, entry, NEW, broken)
        registry.document = _index({DOMAIN: entry})
        registry.etag = '"two"'

    async with _server(tmp_path) as mass:
        _assert_runs(mass, DOMAIN, OLD)
        assert sys.modules[f"{providers_package.__name__}.{DOMAIN}"].REQUIREMENT_VERSION == "1.0"
        installed = _installed(folder)
        assert (installed["version"], installed["failed_version"]) == (OLD, NEW)
        assert installed["pending_version"] is None


async def test_going_back_waits_for_a_restart(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """Going back to requirements that change a package in use stops the provider until a restart."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD, [f"{REQUIREMENT}==1.0"])})
    await _install(tmp_path, registry, {DOMAIN: entry})
    folder = _catalog(tmp_path)

    async with _server(tmp_path) as mass:
        broken = _files(DOMAIN, NEW, [f"{REQUIREMENT}==2.0"], setup='raise TypeError("broken")')
        await _publish(mass, registry, github, {NEW: broken}, entry)
        await mass.community_catalog.update_providers(DOMAIN)

    async with _server(tmp_path) as mass:
        reason = _REASONS[Verdict.RESTART_TO_GO_BACK].format(NEW)
        await _assert_not_loaded(mass, DOMAIN, CommunityProviderState.UNAVAILABLE, reason)
        assert _code_version(folder / DOMAIN) == OLD

    async with _server(tmp_path) as mass:
        _assert_runs(mass, DOMAIN, OLD)
        assert sys.modules[f"{providers_package.__name__}.{DOMAIN}"].REQUIREMENT_VERSION == "1.0"


async def test_switch_of_a_stopped_provider_waits_for_a_restart(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """A switch of a provider that does not run, which changes a package in use, waits for a restart."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD, [f"{REQUIREMENT}==1.0"])})
    other = _entry(github, OTHER, {OLD: _files(OTHER, OLD, [REQUIREMENT])})
    await _install(tmp_path, registry, {DOMAIN: entry, OTHER: other})
    new_files = _files(DOMAIN, NEW, [f"{REQUIREMENT}==2.0"])
    registry.document = _index({DOMAIN: _entry(github, DOMAIN, {NEW: new_files}), OTHER: other})
    registry.etag = '"two"'
    github.status = 503

    async with _server(tmp_path) as mass:
        github.status = 200

        await mass.community_catalog.refresh()

        reason = _REASONS[Verdict.RESTART_TO_UPDATE].format(NEW)
        await _assert_not_loaded(mass, DOMAIN, CommunityProviderState.UNAVAILABLE, reason)
        assert _installed(_catalog(tmp_path))["pending_version"] == NEW
        _assert_runs(mass, OTHER, OLD)

    async with _server(tmp_path) as mass:
        _assert_runs(mass, DOMAIN, NEW)


async def test_going_back_reloads_a_dependent(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """A provider that depends on one that goes back is stopped and loaded again with it."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    dependent = _entry(github, OTHER, {OLD: _files(OTHER, OLD, depends_on=DOMAIN)})
    await _install(tmp_path, registry, {DOMAIN: entry, OTHER: dependent})

    async with _server(tmp_path) as mass:
        broken = _files(DOMAIN, NEW, setup='raise TypeError("broken")')
        await _publish(mass, registry, github, {NEW: broken}, entry)
        catalog = mass.community_catalog
        with patch.object(catalog._loader, "stop", wraps=catalog._loader.stop) as stop:
            await catalog.update_providers(DOMAIN)

        # once for the update, once for going back
        assert [c.args[0] for c in stop.call_args_list].count(OTHER) == 2
        _assert_runs(mass, DOMAIN, OLD)
        _assert_runs(mass, OTHER, OLD)


async def test_going_back_without_the_previous_folder(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """When the previous version cannot be put back, the failed version does not run."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD)})
    await _install(tmp_path, registry, {DOMAIN: entry})

    async with _server(tmp_path) as mass:
        broken = _files(DOMAIN, NEW, setup='raise TypeError("broken")')
        await _publish(mass, registry, github, {NEW: broken}, entry)
        installer = mass.community_catalog._installer
        with patch.object(installer, "restore", side_effect=InstallError("no previous version")):
            await mass.community_catalog.update_providers(DOMAIN)

        reason = _REASONS[Verdict.WENT_BACK].format(NEW)
        await _assert_not_loaded(mass, DOMAIN, CommunityProviderState.UNAVAILABLE, reason)


@pytest.mark.parametrize("kept", ["pending", "previous"])
async def test_remove_with_another_version(
    tmp_path: Path, registry: Registry, github: GitHub, kept: str
) -> None:
    """Removing a provider that keeps a pending or previous version leaves nothing of it behind."""
    entry = _entry(github, DOMAIN, {OLD: _files(DOMAIN, OLD, [f"{REQUIREMENT}==1.0"])})
    await _install(tmp_path, registry, {DOMAIN: entry})
    folder = _catalog(tmp_path)
    requirement = f"{REQUIREMENT}==2.0" if kept == "pending" else f"{REQUIREMENT}==1.0"

    async with _server(tmp_path) as mass:
        await _publish(mass, registry, github, {NEW: _files(DOMAIN, NEW, [requirement])}, entry)
        await mass.community_catalog.update_providers(DOMAIN)
        staging = ".staging" if kept == "pending" else ".previous"
        assert (folder / staging / DOMAIN).is_dir()

        await mass.community_catalog.remove_provider(DOMAIN)

        for location in (folder, folder / ".staging", folder / ".previous"):
            assert not (location / DOMAIN).exists()
        assert DOMAIN not in _state(folder)["installed"]


async def _install(
    tmp_path: Path,
    registry: Registry,
    providers: dict[str, dict[str, Any]],
    *,
    instances: Sequence[str] | None = None,
    auto_update: bool = False,
) -> None:
    """
    Boot a server with the folder on, install providers, stop.

    :param tmp_path: The folder of the server.
    :param registry: The fixture registry.
    :param providers: The entry of each provider in the index, by domain.
    :param instances: The domains with an enabled settings entry; every provider when omitted.
    :param auto_update: Whether updates are installed automatically.
    """
    registry.document = _index(providers)
    _seed_instance(tmp_path, *(providers if instances is None else instances))
    async with _server(tmp_path) as mass:
        await _switch(mass, enabled=True)
        if auto_update:
            await mass.config.save_core_config(
                "community_catalog", {"catalog_enabled": True, "auto_update": True}
            )
        for domain in providers:
            await mass.community_catalog.install_provider(domain)


async def _publish(
    mass: MusicAssistant,
    registry: Registry,
    github: GitHub,
    versions: dict[str, dict[str, bytes]],
    entry: dict[str, Any],
    etag: str = '"two"',
) -> None:
    """
    Add versions to the entry of DOMAIN, the last one latest, and refresh the index.

    The other providers in the index stay as they are.

    :param mass: The server.
    :param registry: The fixture registry.
    :param github: The fixture GitHub.
    :param versions: The files of each new version.
    :param entry: The entry of DOMAIN in the index.
    :param etag: The ETag of the new index.
    """
    for version, files in versions.items():
        _add_version(github, entry, version, files)
    assert isinstance(registry.document, dict)
    registry.document["providers"][DOMAIN] = entry
    registry.etag = etag
    await _wait_for_start(mass)
    await mass.community_catalog.refresh()


def _entry(
    github: GitHub, domain: str, versions: dict[str, dict[str, bytes]] | None = None
) -> dict[str, Any]:
    """
    Serve versions of a provider on the fixture GitHub and return its entry in the index.

    :param github: The fixture GitHub.
    :param domain: The domain of the provider.
    :param versions: The files of each version, the last one latest; one plain version when
        omitted.
    """
    entry: dict[str, Any] = {
        "status": "listed",
        "reason": None,
        "reason_url": None,
        "repository": f"someone/{domain}",
        "maintainers": ["someone"],
        "icon_svg": None,
        "added": ADDED,
        "manifest": {},
        "latest": None,
        "versions": {},
        "blocked_versions": {},
    }
    for version, files in (versions or {OLD: _files(domain, OLD)}).items():
        _add_version(github, entry, version, files)
    return entry


def _add_version(
    github: GitHub, entry: dict[str, Any], version: str, files: dict[str, bytes]
) -> None:
    """
    Serve a version on the fixture GitHub and add it to an entry of the index as the latest.

    :param github: The fixture GitHub.
    :param entry: The entry of the provider in the index.
    :param version: The version.
    :param files: The files of the provider folder.
    """
    manifest = json.loads(files["manifest.json"])
    domain = manifest["domain"]
    commit = f"{domain}-{version}"
    github.archives[commit] = build_archive({f"{domain}/{p}": c for p, c in files.items()})
    entry["manifest"] = manifest
    entry["latest"] = version
    entry["versions"][version] = {
        "commit": commit,
        "folder": domain,
        "content_hash": _hash(files),
        "requirements": manifest["requirements"],
        "released": RELEASED,
    }


def _files(
    domain: str,
    version: str,
    requirements: Sequence[str] = (),
    *,
    depends_on: str | None = None,
    imports: str = "",
    setup: str = "pass",
) -> dict[str, bytes]:
    """
    Return the files of a plugin provider that tells its version in CODE_VERSION.

    :param domain: The domain of the provider.
    :param version: The version the code tells.
    :param requirements: The requirements in its manifest; the first one is imported.
    :param depends_on: The provider it depends on.
    :param imports: Extra lines at the top of its module.
    :param setup: The first line of its setup.
    """
    manifest: dict[str, Any] = {
        "type": "plugin",
        "domain": domain,
        "name": domain,
        "description": "test provider",
        "codeowners": ["@someone"],
        "requirements": list(requirements),
    }
    if depends_on:
        manifest["depends_on"] = depends_on
    if requirements:
        module = requirements[0].split("=")[0].replace("-", "_")
        imports += f"\nfrom {module} import VERSION as REQUIREMENT_VERSION"
    code = PROVIDER_CODE.format(imports=imports, version=version, setup=setup)
    return {
        "manifest.json": json.dumps(manifest).encode(),
        "__init__.py": code.encode(),
    }


def _assert_runs(mass: MusicAssistant, domain: str, version: str) -> None:
    """Assert that the instance of a provider runs and that its module is of a version."""
    provider = mass.get_provider(domain)
    assert provider is not None
    assert provider.available
    assert version == sys.modules[f"{providers_package.__name__}.{domain}"].CODE_VERSION
    assert mass.community_catalog.unavailable_reason(domain) is None


def _catalog(tmp_path: Path) -> Path:
    """Return the folder of the installed community providers."""
    return tmp_path / "data" / "community_providers"


def _installed(folder: Path) -> Any:
    """Return what the state file holds about DOMAIN."""
    return _state(folder)["installed"][DOMAIN]


def _code_version(folder: Path) -> str:
    """Return the version the code in a provider folder tells."""
    for line in (folder / "__init__.py").read_text().splitlines():
        if line.startswith("CODE_VERSION"):
            return str(line.split("=")[1].strip().strip("'\""))
    raise AssertionError(folder)


def _provider(providers: Sequence[Any]) -> Any:
    """Return the provider of DOMAIN in a list of the community folder."""
    return next(provider for provider in providers if provider.domain == DOMAIN)
