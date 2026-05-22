"""Shared pytest fixtures for ma-provider-mcp tests.

Most tests run without a real Music Assistant install — they exercise pure
logic (URI parsing, tag mapping, config entries shape) or use ``MagicMock``
for ``mass``.
"""
# ruff: noqa: D401, PLR0915
#   D401: fixture docstrings describe *what is returned* ("A stub …"), not
#         imperative actions; rephrasing to "Build / Return …" hurts grep-ability.
#   PLR0915: ``mock_mass`` builds a tall MagicMock surface — splitting it across
#            helpers obscures the test contract.

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Make the provider/ package importable as a top-level "provider" module without
# requiring a full ``pip install -e .`` step in ad-hoc test runs.
# Guard: only add when a "provider/" sibling directory exists so that the
# synced copy at tests/providers/fastmcp_server/conftest.py does NOT add
# tests/providers/ to sys.path and shadow installed packages.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if (_REPO_ROOT / "provider").is_dir() and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class FakeWebserver:
    """Captures every dynamic-route registration so tests can drive them through aiohttp.

    Mirrors the surface of ``mass.webserver`` that this plugin uses, without
    depending on a real Music Assistant install. Exposed via the
    :func:`fake_webserver` fixture and :func:`build_aiohttp_app` helper.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8095",
        publish_ip: str = "127.0.0.1",
    ) -> None:
        """Initialise an empty registry with the given advertised endpoints."""
        self.routes: list[tuple[str, Any, str]] = []
        self.base_url = base_url
        self.publish_ip = publish_ip

    def register_dynamic_route(self, path: str, handler: Any, method: str = "*") -> Any:
        """Mirror ``mass.webserver.register_dynamic_route``: store + return unregister."""
        import contextlib  # noqa: PLC0415 - keep stdlib import inside method to mirror runtime

        self.routes.append((path, handler, method))

        def _unregister() -> None:
            with contextlib.suppress(ValueError):
                self.routes.remove((path, handler, method))

        return _unregister

    @property
    def handler(self) -> Any:
        """Return the single registered handler (convenience for one-route tests)."""
        return self.routes[0][1] if self.routes else None


def build_aiohttp_app(fake_ws: FakeWebserver) -> Any:
    """Translate captured ``(path, handler, method)`` tuples into an aiohttp app.

    Mirrors MA's real dynamic-route matching
    (``helpers/webserver.py::_handle_catch_all``): a path registered as
    ``"<stem>/*"`` matches BOTH the bare ``<stem>`` (no trailing slash) and
    any descendant ``<stem>/...``. Aiohttp's ``{tail:.*}`` pattern requires
    the slash, so we add an explicit route for the bare stem alongside the
    wildcard. Without that, the harness silently misses the
    wizard-advertised MCP entry-point URL (``<base_url>/mcp/v1`` — no
    trailing slash) that real clients connect to.
    """
    from aiohttp import web  # noqa: PLC0415 - aiohttp only needed by HTTP-level tests

    app = web.Application()
    for path, handler, method in fake_ws.routes:
        if path.endswith("/*"):
            stem = path[:-2]
            app.router.add_route(method, stem, handler)
            app.router.add_route(method, f"{stem}/{{tail:.*}}", handler)
        else:
            app.router.add_route(method, path, handler)
    return app


@pytest.fixture
def fake_webserver() -> FakeWebserver:
    """Fresh ``FakeWebserver`` instance per test."""
    return FakeWebserver()


@pytest.fixture
def mock_user() -> MagicMock:
    """A minimal stand-in for an MA ``User`` object."""
    user = MagicMock()
    user.user_id = "u1"
    user.username = "tester"
    user.role = MagicMock(value="admin")
    user.enabled = True
    return user


@pytest.fixture
def mock_mass(mock_user: MagicMock) -> MagicMock:
    """A MusicAssistant stub with the surface area we touch."""
    mass = MagicMock()
    mass.webserver = MagicMock()
    mass.webserver.base_url = "http://localhost:8095"
    mass.webserver.publish_ip = "127.0.0.1"
    mass.webserver.auth = MagicMock()
    mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=mock_user)
    mass.webserver.register_dynamic_route = MagicMock(return_value=lambda: None)

    mass.music = MagicMock()
    mass.music.search = AsyncMock()
    mass.music.recently_added_tracks = AsyncMock(return_value=[])
    mass.music.recently_played = AsyncMock(return_value=[])
    mass.music.recommendations = AsyncMock(return_value=[])
    mass.music.get_item_by_uri = AsyncMock()

    mass.music.tracks.library_items = AsyncMock(return_value=[])
    mass.music.tracks.get_library_item = AsyncMock()
    mass.music.albums.library_items = AsyncMock(return_value=[])
    mass.music.albums.get_library_item = AsyncMock()
    mass.music.artists.library_items = AsyncMock(return_value=[])
    mass.music.artists.get_library_item = AsyncMock()
    mass.music.playlists.library_items = AsyncMock(return_value=[])
    mass.music.playlists.get_library_item = AsyncMock()
    mass.music.playlists.create_playlist = AsyncMock()
    mass.music.playlists.add_playlist_track = AsyncMock()
    mass.music.playlists.add_playlist_tracks = AsyncMock()
    mass.music.playlists.remove_playlist_tracks = AsyncMock()
    mass.music.radio.library_items = AsyncMock(return_value=[])
    mass.music.radio.get_library_item = AsyncMock()
    mass.music.add_item_to_favorites = AsyncMock()
    mass.music.remove_item_from_favorites = AsyncMock()
    mass.music.add_item_to_library = AsyncMock()
    mass.music.remove_item_from_library = AsyncMock()
    mass.music.mark_item_played = AsyncMock()

    mass.player_queues = MagicMock()
    mass.player_queues.get_active_queue = MagicMock(return_value=None)
    mass.player_queues.get = MagicMock(return_value=None)
    mass.player_queues.items = MagicMock(return_value=[])
    mass.player_queues.play_media = AsyncMock()
    mass.player_queues.play_pause = AsyncMock()
    mass.player_queues.stop = AsyncMock()
    mass.player_queues.next = AsyncMock()
    mass.player_queues.previous = AsyncMock()
    mass.player_queues.skip = AsyncMock()
    mass.player_queues.seek = AsyncMock()
    mass.player_queues.play_index = AsyncMock()
    mass.player_queues.set_shuffle = AsyncMock()
    mass.player_queues.transfer_queue = AsyncMock()
    mass.player_queues.clear = MagicMock()

    mass.players = MagicMock()
    mass.players.all_players = MagicMock(return_value=[])
    mass.players.get_player = MagicMock(return_value=None)
    mass.players.cmd_power = AsyncMock()
    mass.players.cmd_group = AsyncMock()
    mass.players.cmd_volume_set = AsyncMock()
    mass.players.cmd_volume_up = AsyncMock()
    mass.players.cmd_volume_down = AsyncMock()
    mass.players.cmd_volume_mute = AsyncMock()
    mass.players.cmd_group_volume = AsyncMock()
    mass.players.play_announcement = AsyncMock()

    return mass


@pytest.fixture
def mock_config() -> MagicMock:
    """A ProviderConfig stub. ``get_value`` returns whatever is set in ``_values``."""
    config = MagicMock()
    config._values = {
        # Defaults match build_config_entries
        "require_auth": True,
        "mount_path": "/mcp/v1",
        "extra_allowed_origins": "",
        "enforce_audience": False,
        "require_confirmation": True,
        "query_library": True,
        "query_queue": True,
        "query_players": True,
        "query_metadata": True,
        "control_playback": False,
        "control_volume": False,
        "control_players": False,
        "control_media": False,
        "edit_library": False,
        "edit_queue": False,
        "edit_playlists": False,
        "edit_favorites": False,
        "delete_library": False,
        "delete_queue": False,
        "delete_playlists": False,
        "delete_favorites": False,
        "res_library": True,
        "res_player": True,
        "res_prompts": True,
    }

    def _get(key: str, default: Any = None) -> Any:
        return config._values.get(key, default)

    config.get_value = MagicMock(side_effect=_get)
    return config


@pytest.fixture
def have_fastmcp() -> bool:
    """True if ``fastmcp`` is importable in the current environment."""
    return importlib.util.find_spec("fastmcp") is not None
