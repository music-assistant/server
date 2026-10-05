"""
Connect Wizard onboarding UI for MCP-aware AI clients.

Provides a single-page web UI mounted under ``<mcp-mount>/connect`` that mints
per-client long-lived MA tokens (``"MCP — <Client>"``) and renders ready-to-paste
reviewed configuration methods and share-URLs for supported MCP clients.
"""

from __future__ import annotations

from .actions import handle_open_connect_action
from .mount import mount_connect_wizard

__all__ = ["handle_open_connect_action", "mount_connect_wizard"]
