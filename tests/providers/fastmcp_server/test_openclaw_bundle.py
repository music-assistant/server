"""
Static validation of the OpenClaw distribution bundle.

The bundle under ``packaging/openclaw/`` is an installable Claude-format
plugin that pre-declares the Music Assistant MCP server. These tests guard
the artifact against silent drift — a malformed ``.mcp.json`` or a transport
typo would only surface as a failed install on a user's machine otherwise.
"""
# `assert` is the pytest convention.
# mypy: disable-error-code="type-arg, no-any-return"
#   The fixtures return decoded JSON; precise typing adds no safety to a
#   static-artifact validator.

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = REPO_ROOT / "packaging" / "openclaw"

# The bundle lives under ``packaging/`` in the provider repo, which is NOT part of
# the surface synced into Music Assistant core (only ``provider/`` and ``tests/``
# are). When these tests run inlined upstream the bundle is absent, so skip the
# whole module there — the guard stays meaningful in the provider repo, its home.
pytestmark = pytest.mark.skipif(
    not BUNDLE_DIR.is_dir(),
    reason="OpenClaw bundle (packaging/) is not synced into the upstream tree",
)


@pytest.fixture
def mcp_json() -> dict:
    """Return the bundle's ``.mcp.json`` parsed as a dict."""
    return json.loads((BUNDLE_DIR / ".mcp.json").read_text(encoding="utf-8"))


@pytest.fixture
def plugin_json() -> dict:
    """Return the bundle's ``.claude-plugin/plugin.json`` parsed as a dict."""
    return json.loads((BUNDLE_DIR / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))


def test_bundle_layout_present() -> None:
    """The bundle ships the manifest, the MCP declaration, and a skill guide."""
    assert (BUNDLE_DIR / ".claude-plugin" / "plugin.json").is_file()
    assert (BUNDLE_DIR / ".mcp.json").is_file()
    assert (BUNDLE_DIR / "skills" / "music-assistant" / "SKILL.md").is_file()
    assert (BUNDLE_DIR / "README.md").is_file()


def test_mcp_json_declares_streamable_http_server(mcp_json: dict) -> None:
    """``.mcp.json`` declares the ``ma`` server over streamable-HTTP."""
    server = mcp_json["mcpServers"]["ma"]
    assert server["transport"] == "streamable-http"
    assert server["url"].endswith("/mcp/v1")


def test_mcp_json_uses_env_token_header(mcp_json: dict) -> None:
    """
    The bearer token is supplied via the ``${MA_TOKEN}`` env var, never inlined.

    OpenClaw interpolates ``${VAR}`` in ``headers`` values (but not in ``url``),
    so the token stays out of the committed artifact and out of bundle config.
    """
    auth = mcp_json["mcpServers"]["ma"]["headers"]["Authorization"]
    assert auth == "Bearer ${MA_TOKEN}"


def test_plugin_manifest_has_name(plugin_json: dict) -> None:
    """The Claude-format manifest carries a non-empty ``name``."""
    assert plugin_json.get("name")


def test_plugin_version_tracks_provider_version(plugin_json: dict) -> None:
    """
    The bundle manifest version stays in lockstep with ``provider/VERSION``.

    Nothing auto-syncs the two, so this guard forces a version bump to update
    both files together rather than letting the bundle advertise a stale
    version forever.
    """
    provider_version = (REPO_ROOT / "provider" / "VERSION").read_text(encoding="utf-8").strip()
    assert plugin_json["version"] == provider_version
