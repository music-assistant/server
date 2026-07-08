"""Tests for the sendspin server identity persistence."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

import pytest

from music_assistant.providers.sendspin.security import (
    IDENTITY_FILENAME,
    get_or_create_server_identity,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_identity_created_with_restrictive_permissions(tmp_path: Path) -> None:
    """A fresh identity is generated with a 0600 key file."""
    storage_dir = tmp_path / "sendspin"
    identity = get_or_create_server_identity(storage_dir)
    assert len(identity.peer_id) == 43
    assert stat.S_IMODE((storage_dir / IDENTITY_FILENAME).stat().st_mode) == 0o600


def test_identity_is_persistent(tmp_path: Path) -> None:
    """Repeated calls return the same identity."""
    storage_dir = tmp_path / "sendspin"
    first = get_or_create_server_identity(storage_dir)
    second = get_or_create_server_identity(storage_dir)
    assert first == second


def test_identity_corrupt_file_raises(tmp_path: Path) -> None:
    """A corrupt key file raises instead of silently minting a new identity."""
    storage_dir = tmp_path / "sendspin"
    get_or_create_server_identity(storage_dir)
    key_path = storage_dir / IDENTITY_FILENAME
    key_path.write_text("garbage")
    with pytest.raises(ValueError, match="32 bytes"):
        get_or_create_server_identity(storage_dir)
    assert key_path.read_text() == "garbage"
