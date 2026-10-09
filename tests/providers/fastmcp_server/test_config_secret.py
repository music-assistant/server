"""Unit tests for the SECURE_STRING write gate."""
#   Fixtures import provider modules lazily (inside the test body). A file-level
#   suppression is used instead of per-line directives so the intent survives the
#   upstream import-path rewrite, which lengthens ``from music_assistant.providers.fastmcp_server.X`` lines and
#   reflows them — detaching any trailing per-line directive.

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.providers.fastmcp_server.config_io.secret_handler import (
    gate_secret_writes,
    is_secret_key,
)


def _entries() -> dict[str, ConfigEntry]:
    return {
        "log_level": ConfigEntry(key="log_level", type=ConfigEntryType.STRING, label="L"),
        "token": ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="T"),
    }


def test_nonsecret_key_passes_without_secret_tag() -> None:
    """Non-secret keys pass the gate without secret capability enabled."""
    gate_secret_writes(_entries(), {"log_level": "DEBUG"}, secret_capability_enabled=False)


def test_secret_key_blocked_without_secret_tag() -> None:
    """Secret keys are blocked without secret capability enabled."""
    with pytest.raises(ToolError, match="config:write:secret"):
        gate_secret_writes(_entries(), {"token": "abc"}, secret_capability_enabled=False)


def test_secret_key_allowed_with_secret_tag() -> None:
    """Secret keys are allowed when secret capability is enabled."""
    gate_secret_writes(_entries(), {"token": "abc"}, secret_capability_enabled=True)


def test_mixed_payload_blocked_atomically_names_secret_key() -> None:
    """Mixed payload is blocked atomically, naming the secret key."""
    with pytest.raises(ToolError, match="token"):
        gate_secret_writes(
            _entries(), {"log_level": "DEBUG", "token": "abc"}, secret_capability_enabled=False
        )


def test_is_secret_key() -> None:
    """is_secret_key correctly identifies SECURE_STRING entries."""
    assert is_secret_key(_entries(), "token") is True
    assert is_secret_key(_entries(), "log_level") is False
    assert is_secret_key(_entries(), "missing") is False
