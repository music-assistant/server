"""Tests for the providers that moved from the server to the community catalog."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from music_assistant_models.enums import ProviderStatus
from music_assistant_models.errors import ActionUnavailable

from music_assistant.controllers.community_catalog import controller as controller_module
from music_assistant.controllers.community_catalog.constants import CONF_ENABLED
from music_assistant.controllers.community_catalog.controller import _INDEX_CHECK
from music_assistant.controllers.community_catalog.gate import _REASONS, Verdict
from music_assistant.controllers.community_catalog.models import (
    CatalogState,
    CommunityProviderState,
)
from tests.controllers.community_catalog.test_controller import (  # noqa: F401 - a fixture
    _assert_loaded,
    _events,
    _index,
    _publish,
    _seed_instance,
    _server,
    _settings_entry,
    _state,
    catalog,
)
from tests.controllers.community_catalog.test_settings import _switch

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant.mass import MusicAssistant
    from tests.controllers.community_catalog.conftest import GitHub, Registry

MOVED = "moved_one"
pytestmark = pytest.mark.usefixtures("catalog")


@pytest.fixture
def moved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make MOVED a provider that moved to the community catalog."""
    monkeypatch.setattr(controller_module, "MOVED_PROVIDER_DOMAINS", frozenset({MOVED}))


@pytest.mark.usefixtures("moved")
async def test_installed_with_the_switch_off(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """A moved provider with a settings entry is installed at start, loads and has the notice."""
    folder = tmp_path / "data" / "community_providers"
    registry.document = _index(
        {MOVED: _publish(github, MOVED), "other_one": _publish(github, "other_one")}
    )
    _seed_instance(tmp_path, MOVED)

    async with _server(tmp_path) as mass:
        assert mass.community_catalog.config.get_value(CONF_ENABLED) is False
        _assert_loaded(mass, folder, MOVED)
        providers = await mass.community_catalog.get_providers()
        assert [(p.domain, p.state, p.notice) for p in providers] == [
            (MOVED, CommunityProviderState.INSTALLED, "moved")
        ]
        state = _state(folder)
        assert state["moved"] == [MOVED]
        assert state["installed"][MOVED]["notice"] == "moved"

    async with _server(tmp_path) as mass:
        _assert_loaded(mass, folder, MOVED)
        assert _state(folder) == state


@pytest.mark.usefixtures("moved")
async def test_switch_goes_off_with_a_moved_provider(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """What the server installed by itself lets the switch go off, and keeps the index checked."""
    folder = tmp_path / "data" / "community_providers"
    await _move(tmp_path, registry, github)

    async with _server(tmp_path) as mass:
        await _switch(mass, enabled=True)
        await _switch(mass, enabled=False)

        assert mass.community_catalog.config.get_value(CONF_ENABLED) is False
        _assert_loaded(mass, folder, MOVED)
    requests = len(registry.requests)

    async with _server(tmp_path) as mass:
        assert len(registry.requests) == requests + 1
        assert _INDEX_CHECK in mass._tracked_timers
        _assert_loaded(mass, folder, MOVED)


@pytest.mark.usefixtures("moved")
async def test_switch_leaves_a_running_check_to_finish(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """Turning the switch never cuts a check short: it may be stopping a provider."""
    await _move(tmp_path, registry, github)

    async with _server(tmp_path) as mass:
        registry.delay = 0.2
        requests = len(registry.requests)
        check = mass.create_task(mass.community_catalog._check_index(), task_id=_INDEX_CHECK)
        await asyncio.sleep(0)

        await mass.config.save_core_config("community_catalog", {CONF_ENABLED: True})
        await mass.config.save_core_config("community_catalog", {CONF_ENABLED: False})

        assert mass.get_task(_INDEX_CHECK) is check
        await check
        assert len(registry.requests) == requests + 1


@pytest.mark.usefixtures("moved")
async def test_dismiss_notice(tmp_path: Path, registry: Registry, github: GitHub) -> None:
    """A dismissed notice is gone for good; dismissing again or for an unknown domain does nothing."""
    folder = tmp_path / "data" / "community_providers"
    await _move(tmp_path, registry, github)

    async with _server(tmp_path) as mass:
        with patch.object(mass, "signal_event", wraps=mass.signal_event) as signal_event:
            await mass.community_catalog.dismiss_notice(MOVED)
        assert _events(signal_event) == 1
        assert await _notice(mass) is None

        with patch.object(mass, "signal_event", wraps=mass.signal_event) as signal_event:
            await mass.community_catalog.dismiss_notice(MOVED)
            await mass.community_catalog.dismiss_notice("unknown_one")
        assert _events(signal_event) == 0

    async with _server(tmp_path) as mass:
        _assert_loaded(mass, folder, MOVED)
        assert await _notice(mass) is None
        assert _state(folder)["installed"][MOVED]["notice"] is None


@pytest.mark.usefixtures("moved")
@pytest.mark.parametrize(
    ("cause", "why"),
    [
        ("not_offered", "does not offer"),
        ("registry_unreachable", "does not offer"),
        ("github_unreachable", "503"),
    ],
)
async def test_waits_until_it_can_be_installed(
    tmp_path: Path,
    registry: Registry,
    github: GitHub,
    caplog: pytest.LogCaptureFixture,
    cause: str,
    why: str,
) -> None:
    """A moved provider that cannot be installed waits and says why; a later refresh installs it."""
    folder = tmp_path / "data" / "community_providers"
    entry = _publish(github, MOVED)
    registry.document = _index({MOVED: entry})
    _seed_instance(tmp_path, MOVED)
    if cause == "not_offered":
        registry.document = _index({})
    elif cause == "registry_unreachable":
        registry.status = 503
    else:
        github.status = 503

    with caplog.at_level(logging.WARNING):
        async with _server(tmp_path) as mass:
            assert registry.requests
            assert _INDEX_CHECK in mass._tracked_timers
            assert mass.get_provider(MOVED) is None
            assert not (folder / MOVED).exists()
            assert await mass.community_catalog.get_providers() == []
            conf = _settings_entry(
                await mass.config.get_provider_configs(include_unavailable=True), MOVED
            )
            assert conf.status == ProviderStatus.UNAVAILABLE
            assert conf.last_error is not None
            assert conf.last_error.translation_key == "community_provider_move_failed"
            assert conf.last_error.message.startswith(_REASONS[Verdict.MOVE_FAILED].split(" (")[0])
            assert why in conf.last_error.message
            assert sum("where it moved" in r.getMessage() for r in caplog.records) == 1

            registry.status, github.status = 200, 200
            registry.document, registry.etag = _index({MOVED: entry}), '"two"'
            if cause == "github_unreachable":
                # the index does not change, a refresh tries all the same
                registry.etag = '"one"'
            await mass.community_catalog.refresh()

            _assert_loaded(mass, folder, MOVED)
            assert await _notice(mass) == "moved"


@pytest.mark.usefixtures("moved")
async def test_not_installed_again_after_a_remove(
    tmp_path: Path, registry: Registry, github: GitHub
) -> None:
    """A moved provider the admin removed is not installed again; its entry is a plain one."""
    folder = tmp_path / "data" / "community_providers"
    await _move(tmp_path, registry, github)
    async with _server(tmp_path) as mass:
        await mass.community_catalog.remove_provider(MOVED)
    requests = len(registry.requests)

    async with _server(tmp_path) as mass:
        assert mass.get_provider(MOVED) is None
        assert not (folder / MOVED).exists()
        assert _state(folder)["installed"] == {}
        conf = _settings_entry(
            await mass.config.get_provider_configs(include_unavailable=True), MOVED
        )
        assert conf.status == ProviderStatus.UNAVAILABLE
        assert conf.last_error is not None
        assert conf.last_error.translation_key == "provider_no_longer_available"
    assert len(registry.requests) == requests


@pytest.mark.parametrize("domain", [MOVED, "builtin"])
async def test_nothing_without_a_waiting_move(
    tmp_path: Path,
    registry: Registry,
    github: GitHub,
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
) -> None:
    """A moved domain without a settings entry, or with a provider of the server, is left alone."""
    # the server creates the settings entry of its builtin provider itself
    monkeypatch.setattr(controller_module, "MOVED_PROVIDER_DOMAINS", frozenset({domain}))
    registry.document = _index({domain: _publish(github, domain)})

    async with _server(tmp_path) as mass:
        assert _INDEX_CHECK not in mass._tracked_timers
        assert await mass.community_catalog.get_providers() == []
        assert mass.community_catalog.unavailable_reason(domain) is None

    assert registry.requests == []
    assert not (tmp_path / "data" / "community_providers").exists()


@pytest.mark.usefixtures("moved")
async def test_safe_mode(tmp_path: Path, registry: Registry, github: GitHub) -> None:
    """In safe mode a moved provider is not installed and dismissing a notice refuses."""
    registry.document = _index({MOVED: _publish(github, MOVED)})
    _seed_instance(tmp_path, MOVED)

    async with _server(tmp_path, safe_mode=True) as mass:
        assert mass.get_provider(MOVED) is None
        with pytest.raises(ActionUnavailable, match="safe mode"):
            await mass.community_catalog.dismiss_notice(MOVED)
        # nothing was tried, so there is no cause to tell
        reason = mass.community_catalog.unavailable_reason(MOVED)
        assert reason is not None
        assert reason.message == _REASONS[Verdict.MOVED]

    assert registry.requests == []
    assert not (tmp_path / "data" / "community_providers").exists()


async def test_no_request_without_a_waiting_move(tmp_path: Path, registry: Registry) -> None:
    """Without a moved provider that waits, an entry without a provider makes no request."""
    _seed_instance(tmp_path, MOVED)

    async with _server(tmp_path) as mass:
        assert _INDEX_CHECK not in mass._tracked_timers
        assert mass.community_catalog.unavailable_reason(MOVED) is None

    assert registry.requests == []
    assert not (tmp_path / "data" / "community_providers").exists()


def test_state_without_the_moved_fields() -> None:
    """A state written before the moved providers reads with no move and no notice."""
    installed = {"version": "1.0.0", "repository": "someone/x", "commit": "c", "content_hash": "h"}

    state = CatalogState.from_dict({"schema": 1, "installed": {"x": installed}})

    assert state.moved == []
    assert state.installed["x"].notice is None


async def _move(tmp_path: Path, registry: Registry, github: GitHub) -> None:
    """Boot a server that installs the moved provider by itself, with an enabled entry; stop."""
    registry.document = _index({MOVED: _publish(github, MOVED)})
    _seed_instance(tmp_path, MOVED)
    async with _server(tmp_path) as mass:
        assert mass.get_provider(MOVED) is not None


async def _notice(mass: MusicAssistant) -> str | None:
    """Return the notice of the moved provider in the list of the community catalog."""
    providers = await mass.community_catalog.get_providers()
    return next(provider.notice for provider in providers if provider.domain == MOVED)
