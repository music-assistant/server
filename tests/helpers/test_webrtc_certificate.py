"""Tests for the WebRTC DTLS certificate persistence."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from music_assistant.helpers.webrtc_certificate import (
    CERT_FILENAME,
    KEY_FILENAME,
    _get_or_create_certificate,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_certificate_is_persistent(tmp_path: Path) -> None:
    """Repeated calls return the same keypair."""
    key, cert = _get_or_create_certificate(str(tmp_path))
    key2, cert2 = _get_or_create_certificate(str(tmp_path))
    assert cert2 == cert
    assert key2.public_key() == key.public_key()


def test_mismatched_key_regenerates_pair(tmp_path: Path) -> None:
    """A key file not matching the certificate yields a fresh consistent pair."""
    _, cert = _get_or_create_certificate(str(tmp_path))
    stray_key = ec.generate_private_key(ec.SECP256R1())
    (tmp_path / KEY_FILENAME).write_bytes(
        stray_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key2, cert2 = _get_or_create_certificate(str(tmp_path))
    assert key2.public_key() == cert2.public_key()
    assert cert2 != cert


def test_private_key_created_with_restrictive_permissions(tmp_path: Path) -> None:
    """A fresh private key file is owner read/write only."""
    _get_or_create_certificate(str(tmp_path))
    assert stat.S_IMODE((tmp_path / KEY_FILENAME).stat().st_mode) == 0o600


def test_private_key_permissions_tightened_on_regeneration(tmp_path: Path) -> None:
    """Regenerating over a loose-permissions key file restores owner-only access."""
    _get_or_create_certificate(str(tmp_path))
    key_path = tmp_path / KEY_FILENAME
    key_path.chmod(0o644)
    (tmp_path / CERT_FILENAME).unlink()
    _get_or_create_certificate(str(tmp_path))
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
