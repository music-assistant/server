"""
WebRTC DTLS Certificate Management.

This module provides persistent DTLS certificate management for WebRTC connections.
The certificate is generated once and stored persistently, enabling client-side
certificate pinning for authentication.
"""

from __future__ import annotations

import base64
import logging
import os
import stat
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from music_assistant.helpers.datetime import utc

if TYPE_CHECKING:
    from aiortc import RTCConfiguration, RTCPeerConnection
    from aiortc.rtcdtlstransport import RTCCertificate

LOGGER = logging.getLogger(__name__)

CERT_FILENAME = "webrtc_certificate.pem"
KEY_FILENAME = "webrtc_private_key.pem"

CERT_VALIDITY_DAYS = 3650  # 10 years

CERT_RENEWAL_THRESHOLD_DAYS = 30


def _generate_certificate() -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """
    Generate a new ECDSA certificate for WebRTC DTLS.

    :return: Tuple of (private_key, certificate).
    """
    # Generate ECDSA key (SECP256R1 - same as aiortc default)
    private_key = ec.generate_private_key(ec.SECP256R1())

    now = utc()
    not_before = now - timedelta(days=1)
    not_after = now + timedelta(days=CERT_VALIDITY_DAYS)

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Music Assistant WebRTC")])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(private_key, hashes.SHA256())
    )

    return private_key, cert


def _save_certificate(
    storage_path: str,
    private_key: ec.EllipticCurvePrivateKey,
    cert: x509.Certificate,
) -> None:
    """
    Save certificate and private key to disk.

    :param storage_path: Directory to store the files.
    :param private_key: The EC private key.
    :param cert: The X.509 certificate.
    """
    cert_path = Path(storage_path) / CERT_FILENAME
    key_path = Path(storage_path) / KEY_FILENAME

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_path.write_bytes(cert_pem)

    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    fd = os.open(key_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key_pem)

    # Set restrictive permissions on private key (owner read/write only)
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _load_certificate(
    storage_path: str,
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate] | None:
    """
    Load certificate and private key from disk.

    :param storage_path: Directory containing the files.
    :return: Tuple of (private_key, certificate) or None if files don't exist.
    """
    cert_path = Path(storage_path) / CERT_FILENAME
    key_path = Path(storage_path) / KEY_FILENAME

    if not cert_path.exists() or not key_path.exists():
        return None

    try:
        cert_pem = cert_path.read_bytes()
        cert = x509.load_pem_x509_certificate(cert_pem)

        key_pem = key_path.read_bytes()
        private_key = serialization.load_pem_private_key(key_pem, password=None)

        if not isinstance(private_key, ec.EllipticCurvePrivateKey):
            LOGGER.warning("WebRTC private key is not an EC key, will regenerate")
            return None

        return private_key, cert
    except Exception as err:
        LOGGER.warning("Failed to load WebRTC certificate: %s", err)
        return None


def _is_certificate_valid(cert: x509.Certificate) -> bool:
    """
    Check if certificate is still valid with enough time remaining.

    :param cert: The X.509 certificate to check.
    :return: True if certificate is valid and has sufficient time remaining.
    """
    now = utc()
    not_after = cert.not_valid_after_utc

    if now >= not_after:
        return False

    days_remaining = (not_after - now).days
    return not days_remaining < CERT_RENEWAL_THRESHOLD_DAYS


def _get_or_create_certificate(
    storage_path: str,
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """
    Load a valid persisted DTLS keypair, or generate and persist a new one.

    :param storage_path: Directory to store/load the certificate files.
    :return: Tuple of (private_key, certificate).
    """
    loaded = _load_certificate(storage_path)
    if loaded is not None and _is_certificate_valid(loaded[1]):
        return loaded

    LOGGER.debug("Generating new WebRTC DTLS certificate (valid for %d days)", CERT_VALIDITY_DAYS)
    private_key, cert = _generate_certificate()
    _save_certificate(storage_path, private_key, cert)
    return private_key, cert


def _remote_id_from_certificate(cert: x509.Certificate) -> str:
    """
    Derive the deterministic Remote ID from a certificate.

    :param cert: The X.509 certificate to derive the Remote ID from.
    :return: Custom base32-encoded (with 9s instead of 2s) Remote ID string
        (26 characters, uppercase, no-padding).
    """
    # SHA-256 over the DER certificate, matching aiortc's certificate_digest; take the
    # first 128 bits and base32-encode (with 9s instead of 2s) without padding.
    digest = cert.fingerprint(hashes.SHA256())
    return base64.b32encode(digest[:16]).decode("ascii").rstrip("=").replace("2", "9")


def get_or_create_webrtc_certificate(storage_path: str) -> RTCCertificate:
    """
    Get or create a persistent WebRTC DTLS certificate.

    Loads an existing certificate from disk if available and valid, otherwise
    generates a new certificate and saves it.

    :param storage_path: Directory to store/load the certificate files.
    :return: RTCCertificate instance for use with WebRTC.
    """
    # imported here so importing this module never pulls in aiortc/PyAV (~51MB);
    # only the (rarely-used) WebRTC paths actually need it
    from aiortc.rtcdtlstransport import RTCCertificate  # noqa: PLC0415

    private_key, cert = _get_or_create_certificate(storage_path)
    return RTCCertificate(key=private_key, cert=cert)


def get_or_create_remote_id(storage_path: str) -> str:
    """
    Return the stable Remote ID for this instance without importing aiortc.

    Loads (or creates and persists) the WebRTC DTLS certificate and derives the
    Remote ID from it, so the always-on remote_access/info endpoint can report the
    Remote ID even when remote access is disabled and aiortc was never loaded.

    :param storage_path: Directory to store/load the certificate files.
    :return: The Remote ID derived from the persistent certificate.
    """
    _, cert = _get_or_create_certificate(storage_path)
    return _remote_id_from_certificate(cert)


def create_peer_connection_with_certificate(
    certificate: RTCCertificate,
    configuration: RTCConfiguration | None = None,
) -> RTCPeerConnection:
    """
    Create an RTCPeerConnection with a custom persistent certificate.

    :param certificate: The RTCCertificate to use for DTLS.
    :param configuration: Optional RTCConfiguration with ICE servers.
    :return: RTCPeerConnection configured with the provided certificate.
    """
    from aiortc import RTCPeerConnection  # noqa: PLC0415

    pc = RTCPeerConnection(configuration=configuration)
    # Replace the auto-generated certificate with our persistent one
    # Uses name-mangled private attribute access
    pc._RTCPeerConnection__certificates = [certificate]  # type: ignore[attr-defined]
    return pc
