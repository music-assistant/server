"""Tests for the WebRTC DTLS certificate persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from music_assistant.helpers.webrtc_certificate import (
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
