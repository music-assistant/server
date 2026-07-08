"""
Shared pytest fixtures for the FastMCP server provider.

Most tests run without a real Music Assistant install — they exercise pure
logic (URI parsing, tag mapping, config entries shape) or use ``MagicMock``
for ``mass``.
"""
# ruff: noqa: D401, PLC0415, PLR0915
#   D401: fixture docstrings describe *what is returned* ("A stub …"), not
#         imperative actions; rephrasing to "Build / Return …" hurts grep-ability.
#   PLC0415: fixtures import provider modules lazily (inside the fixture body) to
#            keep ``mass``-free tests cheap and to avoid import-time side effects.
#            A file-level suppression is used instead of per-line suppressions so
#            the intent survives the upstream import-path rewrite (which lengthens
#            ``from music_assistant.providers.fastmcp_server.X`` lines and reflows them, detaching trailing
#            per-line directives).
#   PLR0915: ``mock_mass`` builds a tall MagicMock surface — splitting it across
#            helpers obscures the test contract.

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.fastmcp_server.debug import log_reader


class FakeWebserver:
    """
    Captures every dynamic-route registration so tests can drive them through aiohttp.

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
        import contextlib

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
    """
    Translate captured ``(path, handler, method)`` tuples into an aiohttp app.

    Mirrors MA's real dynamic-route matching
    (``helpers/webserver.py::_handle_catch_all``): a path registered as
    ``"<stem>/*"`` matches BOTH the bare ``<stem>`` (no trailing slash) and
    any descendant ``<stem>/...``. Aiohttp's ``{tail:.*}`` pattern requires
    the slash, so we add an explicit route for the bare stem alongside the
    wildcard. Without that, the harness silently misses the
    wizard-advertised MCP entry-point URL (``<base_url>/mcp/v1`` — no
    trailing slash) that real clients connect to.
    """
    from aiohttp import web

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

    mass.translations.get_translation = MagicMock(return_value=None)

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
    mass.music.albums.tracks = AsyncMock(return_value=[])
    mass.music.artists.library_items = AsyncMock(return_value=[])
    mass.music.artists.get_library_item = AsyncMock()
    mass.music.artists.albums = AsyncMock(return_value=[])
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
    mass.player_queues.pause = AsyncMock()
    mass.player_queues.resume = AsyncMock()
    mass.player_queues.stop = AsyncMock()
    mass.player_queues.next = AsyncMock()
    mass.player_queues.previous = AsyncMock()
    mass.player_queues.skip = AsyncMock()
    mass.player_queues.seek = AsyncMock()
    mass.player_queues.play_index = AsyncMock()
    mass.player_queues.set_shuffle = AsyncMock()
    mass.player_queues.set_repeat = MagicMock()
    mass.player_queues.transfer_queue = AsyncMock()
    mass.player_queues.clear = MagicMock()

    mass.players = MagicMock()
    mass.players.all_players = MagicMock(return_value=[])
    mass.players.get_player = MagicMock(return_value=None)
    mass.players.cmd_power = AsyncMock()
    mass.players.cmd_group = AsyncMock()
    mass.players.cmd_ungroup = AsyncMock()
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


@pytest.fixture
def tmp_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_mass: MagicMock) -> Path:
    """
    Sandboxed log root for SafeLogTail tests.

    Copies the fixture log into ``tmp_path`` and (a) patches the class-level
    ``SafeLogTail.ROOT`` so bare ``SafeLogTail()`` callers see ``tmp_path``,
    and (b) sets ``mock_mass.storage_path = str(tmp_path)`` so e2e tests that
    go through ``mounted_debug → build_debug_server(mock_mass) →
    SafeLogTail(mass)`` resolve to the same sandbox. Tests never touch the
    real ``~/.musicassistant/`` directory.
    """
    src = Path(__file__).parent / "fixtures" / "musicassistant.sample.log"
    dst = tmp_path / "musicassistant.log"
    shutil.copyfile(src, dst)

    monkeypatch.setattr(log_reader.SafeLogTail, "ROOT", tmp_path, raising=True)
    mock_mass.storage_path = str(tmp_path)
    return tmp_path


@pytest.fixture
def fake_event_emitter(mock_mass: MagicMock) -> Any:
    """
    Capture the EventBuffer subscriber and let tests emit synthetic events.

    Replaces ``mock_mass.subscribe`` so the first call stores the callback;
    tests then call ``emitter.emit(event)`` to drive it.
    """
    holder: dict[str, Any] = {"cb": None, "removed": False}

    def _subscribe(cb: Any, event_filter: Any = None, id_filter: Any = None) -> Any:  # noqa: ARG001
        holder["cb"] = cb

        def _remove() -> None:
            holder["removed"] = True

        return _remove

    mock_mass.subscribe = MagicMock(side_effect=_subscribe)

    class _Emitter:
        @property
        def cb(self) -> Any:
            return holder["cb"]

        @property
        def removed(self) -> Any:
            return holder["removed"]

        def emit(self, event: Any) -> None:
            if holder["cb"] is None:
                raise AssertionError("no subscriber registered")
            holder["cb"](event)

    return _Emitter()


@pytest.fixture
def mounted_debug(mock_mass: MagicMock) -> Any:
    """Build a root FastMCP with the debug sub-server mounted, all debug tags allowed."""
    import contextlib

    from fastmcp import FastMCP

    from music_assistant.providers.fastmcp_server.tools.debug import build_debug_server

    mcp = FastMCP(name="test")
    mcp.mount(build_debug_server(mock_mass, require_confirmation=False), namespace="debug")

    # No tag-restrict middleware here — by default every tag is visible.
    # mounted_debug_off below applies the restrict filter explicitly.
    try:
        yield mcp
    finally:
        close = getattr(mcp, "close", None) or getattr(mcp, "shutdown", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


@pytest.fixture
def mounted_debug_off(mock_mass: MagicMock) -> Any:
    """
    Debug sub-server mounted with the TagFilterMiddleware allowing zero debug tags.

    Uses the project's own ``TagFilterMiddleware`` (provider/middleware.py),
    not FastMCP's built-in ``restrict_tag`` — the latter is scope-based
    OAuth authorisation while this provider needs config-driven visibility.
    """
    import contextlib

    from fastmcp import FastMCP

    from music_assistant.providers.fastmcp_server.middleware import TagFilterMiddleware
    from music_assistant.providers.fastmcp_server.server import build_tag_lookup
    from music_assistant.providers.fastmcp_server.tools.debug import build_debug_server

    mcp = FastMCP(name="test")
    mcp.mount(build_debug_server(mock_mass, require_confirmation=False), namespace="debug")
    mcp.add_middleware(TagFilterMiddleware(lambda: set(), build_tag_lookup(mcp)))
    try:
        yield mcp
    finally:
        close = getattr(mcp, "close", None) or getattr(mcp, "shutdown", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


@pytest.fixture
def mounted_debug_with_events(mock_mass: MagicMock, fake_event_emitter: Any) -> Any:
    """
    Debug server with a started EventBuffer wired in.

    Yields ``(mcp, buffer, emitter)`` — tests drive synthetic events
    through ``emitter.emit(...)`` and read back via either the MCP
    client or the buffer directly.
    """
    import contextlib

    from fastmcp import FastMCP

    from music_assistant.providers.fastmcp_server.debug.event_buffer import EventBuffer
    from music_assistant.providers.fastmcp_server.tools.debug import build_debug_server

    buf = EventBuffer(mock_mass, capacity=500)
    buf.start()

    mcp = FastMCP(name="test")
    mcp.mount(
        build_debug_server(mock_mass, require_confirmation=False, event_buffer=buf),
        namespace="debug",
    )
    try:
        yield mcp, buf, fake_event_emitter
    finally:
        buf.stop()
        close = getattr(mcp, "close", None) or getattr(mcp, "shutdown", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


@pytest.fixture
def mounted_config(mock_mass: Any) -> Any:
    """Root FastMCP with the config sub-server mounted, all config tags visible."""
    import contextlib

    from fastmcp import FastMCP

    from music_assistant.providers.fastmcp_server.tools.config import build_config_server

    mcp = FastMCP(name="test")
    mcp.mount(build_config_server(mock_mass, require_confirmation=False), namespace="config")
    try:
        yield mcp
    finally:
        close = getattr(mcp, "close", None) or getattr(mcp, "shutdown", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


@pytest.fixture
def mounted_config_off(mock_mass: Any) -> Any:
    """Config sub-server with TagFilterMiddleware allowing zero tags."""
    import contextlib

    from fastmcp import FastMCP

    from music_assistant.providers.fastmcp_server.middleware import TagFilterMiddleware
    from music_assistant.providers.fastmcp_server.server import build_tag_lookup
    from music_assistant.providers.fastmcp_server.tools.config import build_config_server

    mcp = FastMCP(name="test")
    mcp.mount(build_config_server(mock_mass, require_confirmation=False), namespace="config")
    mcp.add_middleware(TagFilterMiddleware(lambda: set(), build_tag_lookup(mcp)))
    try:
        yield mcp
    finally:
        close = getattr(mcp, "close", None) or getattr(mcp, "shutdown", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


@pytest.fixture
def mounted_config_no_secret(mock_mass: Any) -> Any:
    """Config sub-server built with secret writes disabled (value-gate off)."""
    import contextlib

    from fastmcp import FastMCP

    from music_assistant.providers.fastmcp_server.tools.config import build_config_server

    mcp = FastMCP(name="test")
    mcp.mount(
        build_config_server(mock_mass, require_confirmation=False, secret_writes_enabled=False),
        namespace="config",
    )
    try:
        yield mcp
    finally:
        close = getattr(mcp, "close", None) or getattr(mcp, "shutdown", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


@pytest.fixture
def mock_config_targets(mock_mass: Any) -> Any:
    """
    Wire mock_mass.config with provider/core/player config + entries.

    Provides a SECURE_STRING entry (token, requires_reload), an
    INTEGER-with-range (http_port), and a STRING (log_level). to_dict
    mirrors MA's __post_serialize__ (secret already masked).
    """
    from unittest.mock import AsyncMock, MagicMock

    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.enums import ConfigEntryType

    entries = {
        "log_level": ConfigEntry(key="log_level", type=ConfigEntryType.STRING, label="Log level"),
        "http_port": ConfigEntry(
            key="http_port", type=ConfigEntryType.INTEGER, label="Port", range=(1, 65535)
        ),
        "token": ConfigEntry(
            key="token", type=ConfigEntryType.SECURE_STRING, label="Token", requires_reload=True
        ),
    }
    # give each entry a current .value so _resolve_entries current-map works
    entries["log_level"].value = "GLOBAL"
    entries["http_port"].value = 8099
    entries["token"].value = "raw-secret-xyz"

    cfg = MagicMock()
    cfg.domain = "yandex_music"
    cfg.values = entries
    cfg.provider = "yandex_music"
    cfg.to_dict = MagicMock(
        return_value={
            "domain": "yandex_music",
            "values": {
                "log_level": {"key": "log_level", "type": "string", "value": "GLOBAL"},
                "http_port": {"key": "http_port", "type": "integer", "value": 8099},
                "token": {
                    "key": "token",
                    "type": "secure_string",
                    "value": "this_value_is_encrypted",
                },
            },
        }
    )

    summary = MagicMock()
    summary.instance_id = "yandex_music"
    summary.domain = "yandex_music"
    summary.name = "Yandex Music"
    summary.enabled = True
    summary.player_id = "kitchen"
    summary.provider = "local_audio"

    mock_mass.config.get_provider_config = AsyncMock(return_value=cfg)
    mock_mass.config.get_provider_config_entries = AsyncMock(return_value=list(entries.values()))
    mock_mass.config.get_core_config = AsyncMock(return_value=cfg)
    mock_mass.config.get_core_config_entries = AsyncMock(return_value=list(entries.values()))
    mock_mass.config.get_player_config = AsyncMock(return_value=cfg)
    mock_mass.config.get_player_config_entries = AsyncMock(return_value=list(entries.values()))
    mock_mass.config.get_provider_configs = AsyncMock(return_value=[summary])
    mock_mass.config.get_core_configs = AsyncMock(return_value=[summary])
    mock_mass.config.get_player_configs = AsyncMock(return_value=[summary])
    mock_mass.config.save_provider_config = AsyncMock(return_value=cfg)
    mock_mass.config.save_core_config = AsyncMock(return_value=cfg)
    mock_mass.config.save_player_config = AsyncMock(return_value=cfg)
    mock_mass.config.save_dsp_config = AsyncMock()
    mock_mass.config.get_player_dsp_config = MagicMock(
        return_value=MagicMock(
            to_dict=MagicMock(
                return_value={
                    "enabled": True,
                    "input_gain": 0.0,
                    "output_gain": 0.0,
                    "filters": [],
                }
            )
        )
    )
    return mock_mass
