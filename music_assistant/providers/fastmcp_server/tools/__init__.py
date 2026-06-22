"""
Tool sub-server factories.

Each ``build_*_server`` function returns its own :class:`fastmcp.FastMCP`
instance, which is then ``mount()``-ed under a namespace by
:meth:`provider.server.MCPServerRuntime.start`. Tags applied per-tool drive
the ``restrict_tag`` middleware.
"""

from __future__ import annotations

from .config import build_config_server
from .debug import build_debug_server
from .library import build_library_server
from .media import build_media_server
from .metadata import build_metadata_server
from .playback import build_playback_server
from .players import build_players_server
from .playlists import build_playlists_server
from .queue import build_queue_server
from .volume import build_volume_server

__all__ = [
    "build_config_server",
    "build_debug_server",
    "build_library_server",
    "build_media_server",
    "build_metadata_server",
    "build_playback_server",
    "build_players_server",
    "build_playlists_server",
    "build_queue_server",
    "build_volume_server",
]
