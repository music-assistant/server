"""Tests for sidebar shortcut cleanup on provider removal and library pruning."""

from __future__ import annotations

import asyncio
import logging
import pathlib
import threading
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import UserRole

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.music.controller import MusicController
from music_assistant.controllers.webserver.auth import (
    PREF_SIDEBAR_SHORTCUTS,
    AuthenticationManager,
)
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.mass import MusicAssistant


@pytest.fixture
async def mass_minimal(tmp_path: pathlib.Path) -> AsyncGenerator[MusicAssistant]:
    """Create a minimal Music Assistant instance for shortcut cleanup testing."""
    storage_path = tmp_path / "data"
    cache_path = tmp_path / "cache"
    storage_path.mkdir(parents=True)
    cache_path.mkdir(parents=True)

    logging.getLogger("aiosqlite").level = logging.INFO

    mass_instance = MusicAssistant(str(storage_path), str(cache_path))
    mass_instance.loop = asyncio.get_running_loop()
    mass_instance.loop_thread_id = threading.get_ident()

    mass_instance.config = ConfigController(mass_instance)
    await mass_instance.config.setup()

    webserver = WebserverController(mass_instance)
    mass_instance.webserver = webserver

    webserver_config = await mass_instance.config.get_core_config("webserver")
    webserver.config = webserver_config

    await webserver.auth.setup()

    try:
        yield mass_instance
    finally:
        await webserver.auth.close()
        await mass_instance.config.close()


@pytest.fixture
async def auth(mass_minimal: MusicAssistant) -> AuthenticationManager:
    """Get the auth manager."""
    return mass_minimal.webserver.auth


async def _set_shortcuts(auth: AuthenticationManager, user_id: str, uris: list[str]) -> None:
    """Write sidebar shortcuts directly into the user's preferences."""
    prefs = {PREF_SIDEBAR_SHORTCUTS: uris}
    await auth.database.update(
        "users",
        {"user_id": user_id},
        {"preferences": json_dumps(prefs)},
    )


async def _get_shortcuts(auth: AuthenticationManager, user_id: str) -> list[str]:
    """Read sidebar shortcuts from a user's preferences."""
    row = await auth.database.get_row("users", {"user_id": user_id})
    assert row is not None
    prefs: dict[str, list[str]] = json_loads(row["preferences"]) if row["preferences"] else {}
    return prefs.get(PREF_SIDEBAR_SHORTCUTS, [])


async def test_cleanup_drops_shortcuts(auth: AuthenticationManager) -> None:
    """Shortcuts whose rewrite callback returns None are removed."""
    user = await auth.create_user(username="alice", role=UserRole.USER)
    await _set_shortcuts(
        auth,
        user.user_id,
        [
            "spotify_1://track/abc",
            "library://track/42",
            "tidal_1://album/xyz",
        ],
    )

    async def _drop_spotify(uri: str) -> str | None:
        if uri.startswith("spotify_1://"):
            return None
        return uri

    await auth.cleanup_user_shortcuts(_drop_spotify)

    result = await _get_shortcuts(auth, user.user_id)
    assert result == ["library://track/42", "tidal_1://album/xyz"]


async def test_cleanup_rewrites_shortcuts(auth: AuthenticationManager) -> None:
    """Shortcuts whose rewrite callback returns a new URI are rewritten."""
    user = await auth.create_user(username="bob", role=UserRole.USER)
    await _set_shortcuts(
        auth,
        user.user_id,
        [
            "spotify_1://track/abc",
            "library://album/10",
        ],
    )

    async def _rewrite_to_library(uri: str) -> str | None:
        if uri == "spotify_1://track/abc":
            return "library://track/99"
        return uri

    await auth.cleanup_user_shortcuts(_rewrite_to_library)

    result = await _get_shortcuts(auth, user.user_id)
    assert result == ["library://track/99", "library://album/10"]


async def test_cleanup_no_change_skips_write(auth: AuthenticationManager) -> None:
    """When no shortcuts change, the database row is not updated."""
    user = await auth.create_user(username="carol", role=UserRole.USER)
    uris = ["library://track/1", "library://track/2"]
    await _set_shortcuts(auth, user.user_id, uris)

    async def _keep_all(uri: str) -> str | None:
        return uri

    await auth.cleanup_user_shortcuts(_keep_all)

    result = await _get_shortcuts(auth, user.user_id)
    assert result == uris


async def test_cleanup_empty_shortcuts_skipped(auth: AuthenticationManager) -> None:
    """Users with no shortcuts are not touched."""
    await auth.create_user(username="dave", role=UserRole.USER)

    call_count = 0

    async def _counter(uri: str) -> str | None:
        nonlocal call_count
        call_count += 1
        return uri

    await auth.cleanup_user_shortcuts(_counter)
    assert call_count == 0


async def test_cleanup_multiple_users(auth: AuthenticationManager) -> None:
    """Cleanup iterates over all users."""
    user_a = await auth.create_user(username="eve", role=UserRole.USER)
    user_b = await auth.create_user(username="frank", role=UserRole.USER)
    await _set_shortcuts(auth, user_a.user_id, ["old_provider://track/1"])
    await _set_shortcuts(auth, user_b.user_id, ["old_provider://track/2", "library://track/5"])

    async def _drop_old(uri: str) -> str | None:
        if uri.startswith("old_provider://"):
            return None
        return uri

    await auth.cleanup_user_shortcuts(_drop_old)

    assert await _get_shortcuts(auth, user_a.user_id) == []
    assert await _get_shortcuts(auth, user_b.user_id) == ["library://track/5"]


async def test_cleanup_preserves_other_preferences(auth: AuthenticationManager) -> None:
    """Shortcut cleanup does not clobber unrelated preference keys."""
    user = await auth.create_user(username="grace", role=UserRole.USER)
    prefs = {
        "frontend.settings.theme": "dark",
        PREF_SIDEBAR_SHORTCUTS: ["old://track/1"],
    }
    await auth.database.update(
        "users",
        {"user_id": user.user_id},
        {"preferences": json_dumps(prefs)},
    )

    async def _drop_all(_uri: str) -> str | None:
        return None

    await auth.cleanup_user_shortcuts(_drop_all)

    row = await auth.database.get_row("users", {"user_id": user.user_id})
    assert row is not None
    result_prefs: dict[str, Any] = json_loads(row["preferences"])
    assert result_prefs["frontend.settings.theme"] == "dark"
    assert result_prefs[PREF_SIDEBAR_SHORTCUTS] == []


def _music_controller(
    mass: MusicAssistant, library_hit: bool, lookup_error: type[Exception] | None = None
) -> MusicController:
    """
    Build a MusicController that resolves provider items to a fixed library id.

    :param mass: The MusicAssistant instance to bind the controller to.
    :param library_hit: Whether the media controller finds a library item for the shortcut.
    :param lookup_error: Optional error the library lookup raises when it is reached.
    """
    music = MusicController.__new__(MusicController)
    music.mass = mass
    music.logger = logging.getLogger("test.music")
    media_ctrl = MagicMock()
    media_ctrl.get_library_item_by_prov_id = AsyncMock(
        return_value=MagicMock(item_id="7") if library_hit else None,
        side_effect=lookup_error,
    )
    music.get_controller = MagicMock(return_value=media_ctrl)  # type: ignore[method-assign]
    return music


async def test_stale_provider_shortcut_follows_library_item(
    mass_minimal: MusicAssistant, auth: AuthenticationManager
) -> None:
    """A shortcut on a removed provider instance follows the library copy of its item."""
    user = await auth.create_user(username="dave", role=UserRole.USER)
    await _set_shortcuts(auth, user.user_id, ["ytmusic--old://playlist/abc"])
    mass_minimal.config.set(CONF_PROVIDERS, {"ytmusic--new": {"domain": "ytmusic"}})

    await _music_controller(mass_minimal, library_hit=True).cleanup_stale_provider_shortcuts()

    assert await _get_shortcuts(auth, user.user_id) == ["library://playlist/7"]


async def test_stale_provider_shortcut_dropped_when_not_in_library(
    mass_minimal: MusicAssistant, auth: AuthenticationManager
) -> None:
    """A shortcut on a removed provider instance is dropped when the item is not in library."""
    user = await auth.create_user(username="erin", role=UserRole.USER)
    await _set_shortcuts(auth, user.user_id, ["ytmusic--old://playlist/abc", "library://track/1"])
    mass_minimal.config.set(CONF_PROVIDERS, {"ytmusic--new": {"domain": "ytmusic"}})

    await _music_controller(mass_minimal, library_hit=False).cleanup_stale_provider_shortcuts()

    assert await _get_shortcuts(auth, user.user_id) == ["library://track/1"]


async def test_live_provider_shortcuts_are_kept(
    mass_minimal: MusicAssistant, auth: AuthenticationManager
) -> None:
    """Shortcuts naming a configured instance, its bare domain or the library are untouched."""
    user = await auth.create_user(username="frank", role=UserRole.USER)
    uris = [
        "ytmusic--new://playlist/abc",
        "ytmusic://track/def",
        "builtin://playlist/ghi",
        "library://track/1",
    ]
    await _set_shortcuts(auth, user.user_id, uris)
    mass_minimal.config.set(
        CONF_PROVIDERS,
        {"ytmusic--new": {"domain": "ytmusic"}, "builtin": {"domain": "builtin"}},
    )

    await _music_controller(mass_minimal, library_hit=False).cleanup_stale_provider_shortcuts()

    assert await _get_shortcuts(auth, user.user_id) == uris


async def test_empty_provider_config_keeps_shortcuts(
    mass_minimal: MusicAssistant, auth: AuthenticationManager
) -> None:
    """An unconfigured install must not be mistaken for every provider having been removed."""
    user = await auth.create_user(username="grace", role=UserRole.USER)
    uris = ["ytmusic--old://playlist/abc"]
    await _set_shortcuts(auth, user.user_id, uris)
    mass_minimal.config.set(CONF_PROVIDERS, {})

    await _music_controller(mass_minimal, library_hit=False).cleanup_stale_provider_shortcuts()

    assert await _get_shortcuts(auth, user.user_id) == uris


async def test_malformed_shortcut_is_dropped_without_raising(
    mass_minimal: MusicAssistant, auth: AuthenticationManager
) -> None:
    """A URI that parses to an empty item id is dropped, not asserted on."""
    user = await auth.create_user(username="heidi", role=UserRole.USER)
    await _set_shortcuts(auth, user.user_id, ["oldprov--x://track/", "library://track/1"])
    mass_minimal.config.set(CONF_PROVIDERS, {"ytmusic--new": {"domain": "ytmusic"}})
    # the real lookup asserts on an empty item id, so it must never be reached
    music = _music_controller(mass_minimal, library_hit=False, lookup_error=AssertionError)

    await music.cleanup_stale_provider_shortcuts()

    assert await _get_shortcuts(auth, user.user_id) == ["library://track/1"]


async def test_sweep_failure_never_aborts_startup(
    mass_minimal: MusicAssistant, auth: AuthenticationManager
) -> None:
    """The sweep runs on the boot path, so a failure must not propagate."""
    user = await auth.create_user(username="ivan", role=UserRole.USER)
    uris = ["ytmusic--old://playlist/abc"]
    await _set_shortcuts(auth, user.user_id, uris)
    mass_minimal.config.set(CONF_PROVIDERS, {"ytmusic--new": {"domain": "ytmusic"}})
    music = _music_controller(mass_minimal, library_hit=False, lookup_error=RuntimeError)

    await music.cleanup_stale_provider_shortcuts()

    assert await _get_shortcuts(auth, user.user_id) == uris
