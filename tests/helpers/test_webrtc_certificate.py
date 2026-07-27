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
    get_or_create_remote_id,
    get_or_create_webrtc_certificate_pems,
)

if TYPE_CHECKING:
    from pathlib import Path

# Frozen fixtures derived from _generate_certificate()/_remote_id_from_certificate(),
# pinned as literals so these tests keep guarding the derivation even if the
# implementation changes later.
FIXTURE_CERT_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIBQTCB6KADAgECAhRuYKuRIszhzAOCq29MyuvEOzJ9EDAKBggqhkjOPQQDAjAh\n"
    "MR8wHQYDVQQDDBZNdXNpYyBBc3Npc3RhbnQgV2ViUlRDMB4XDTI2MDcxOTEwMjIw\n"
    "OFoXDTM2MDcxNzEwMjIwOFowITEfMB0GA1UEAwwWTXVzaWMgQXNzaXN0YW50IFdl\n"
    "YlJUQzBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABAU/5JpGlyTQ2pexaev5083F\n"
    "1/dkvNKxCCRWQbfZOvk1vRhJP4jsT710Un7jzfMx25rtMVsv1lzvCmerRFCsacsw\n"
    "CgYIKoZIzj0EAwIDSAAwRQIgOTVzTaqOXVyaD2NDNIVjXlX3hDOxEgbacy7Q7Lfj\n"
    "61QCIQCq3RN0iyR9qfuo28VjkWceEcHkDwynS1vFKr2oqffO6g==\n"
    "-----END CERTIFICATE-----\n"
)
FIXTURE_KEY_PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgrrFr6dcpp+Sr1j+e\n"
    "dY0oUp/WYDjh/xpceu3sIxmldUWhRANCAAQFP+SaRpck0NqXsWnr+dPNxdf3ZLzS\n"
    "sQgkVkG32Tr5Nb0YST+I7E+9dFJ+483zMdua7TFbL9Zc7wpnq0RQrGnL\n"
    "-----END PRIVATE KEY-----\n"
)
FIXTURE_REMOTE_ID = "QCPJJTJMI3IDBNURU6XJJENRYU"


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


def test_private_key_permissions_tightened_on_load(tmp_path: Path) -> None:
    """Loading a valid pair tightens a loose key file to owner-only access."""
    _, cert = _get_or_create_certificate(str(tmp_path))
    key_path = tmp_path / KEY_FILENAME
    key_path.chmod(0o644)
    _, cert2 = _get_or_create_certificate(str(tmp_path))
    assert cert2 == cert
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_remote_id_and_pems_stable_across_frozen_fixture(tmp_path: Path) -> None:
    """A pre-existing certificate pair yields the frozen Remote ID and is returned as-is."""
    (tmp_path / CERT_FILENAME).write_text(FIXTURE_CERT_PEM)
    (tmp_path / KEY_FILENAME).write_text(FIXTURE_KEY_PEM)

    assert get_or_create_remote_id(str(tmp_path)) == FIXTURE_REMOTE_ID
    assert get_or_create_webrtc_certificate_pems(str(tmp_path)) == (
        FIXTURE_CERT_PEM,
        FIXTURE_KEY_PEM,
    )


def test_get_or_create_webrtc_certificate_pems_persists(tmp_path: Path) -> None:
    """A fresh call creates the certificate files and a second call returns the same PEMs."""
    cert_pem, key_pem = get_or_create_webrtc_certificate_pems(str(tmp_path))
    cert_pem2, key_pem2 = get_or_create_webrtc_certificate_pems(str(tmp_path))
    assert cert_pem2 == cert_pem
    assert key_pem2 == key_pem
