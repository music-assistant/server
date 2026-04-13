"""WebRTC DTLS Certificate Management.

This module provides persistent DTLS certificate management for WebRTC connections.
The certificate is generated once and stored persistently, enabling client-side
certificate pinning for authentication.
"""

from __future__ import annotations

import base64
import logging
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiortc import RTCConfiguration, RTCPeerConnection
from aiortc.rtcdtlstransport import RTCCertificate
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

LOGGER = logging.getLogger(__name__)

CERT_FILENAME = "webrtc_certificate.pem"
KEY_FILENAME = "webrtc_private_key.pem"

CERT_VALIDITY_DAYS = 3650  # 10 years

CERT_RENEWAL_THRESHOLD_DAYS = 30


def _generate_certificate() -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """Generate a new ECDSA certificate for WebRTC DTLS.

    :return: Tuple of (private_key, certificate).
    """
    # Generate ECDSA key (SECP256R1 - same as aiortc default)
    private_key = ec.generate_private_key(ec.SECP256R1())

    now = datetime.now(UTC)
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
    """Save certificate and private key to disk.

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
    key_path.write_bytes(key_pem)

    # Set restrictive permissions on private key (owner read/write only)
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _load_certificate(
    storage_path: str,
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate] | None:
    """Load certificate and private key from disk.

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
    """Check if certificate is still valid with enough time remaining.

    :param cert: The X.509 certificate to check.
    :return: True if certificate is valid and has sufficient time remaining.
    """
    now = datetime.now(UTC)
    not_after = cert.not_valid_after_utc

    if now >= not_after:
        return False

    days_remaining = (not_after - now).days
    return not days_remaining < CERT_RENEWAL_THRESHOLD_DAYS


def get_or_create_webrtc_certificate(storage_path: str) -> RTCCertificate:
    """Get or create a persistent WebRTC DTLS certificate.

    Loads an existing certificate from disk if available and valid.
    Otherwise, generates a new certificate and saves it.

    :param storage_path: Directory to store/load the certificate files.
    :return: RTCCertificate instance for use with WebRTC.
    """
    loaded = _load_certificate(storage_path)

    if loaded is not None:
        private_key, cert = loaded

        if _is_certificate_valid(cert):
            return RTCCertificate(key=private_key, cert=cert)

    LOGGER.debug("Generating new WebRTC DTLS certificate (valid for %d days)", CERT_VALIDITY_DAYS)
    private_key, cert = _generate_certificate()
    _save_certificate(storage_path, private_key, cert)

    return RTCCertificate(key=private_key, cert=cert)


def _get_certificate_fingerprint(certificate: RTCCertificate) -> str:
    """Get the SHA-256 fingerprint of a certificate.

    :param certificate: The RTCCertificate to get the fingerprint for.
    :return: SHA-256 fingerprint as colon-separated hex string (e.g., "A1:B2:C3:...").
    """
    fingerprints = certificate.getFingerprints()
    for fp in fingerprints:
        if fp.algorithm == "sha-256":
            return fp.value
    raise ValueError("SHA-256 fingerprint not found in certificate")


def get_remote_id_from_certificate(certificate: RTCCertificate) -> str:
    """Generate a remote ID from the certificate fingerprint.

    Uses base32-encoded 128-bit truncation of the SHA-256 fingerprint.
    This creates a deterministic remote ID tied to the certificate.

    :param certificate: The RTCCertificate to derive the remote ID from.
    :return: Custom base32-encoded (with 9s instead of 2s) remote ID string
        (26 characters, uppercase, no-padding).
    """
    fingerprint = _get_certificate_fingerprint(certificate)

    # Parse the colon-separated hex fingerprint to bytes
    # Format: "A1:B2:C3:D4:..." -> bytes
    fingerprint_bytes = bytes.fromhex(fingerprint.replace(":", ""))

    # Take first 128 bits (16 bytes) of SHA-256
    truncated = fingerprint_bytes[:16]

    # Base32 encode (with 9s instead of 2s) and return (uppercase) without padding
    return base64.b32encode(truncated).decode("ascii").rstrip("=").replace("2", "9")


def create_peer_connection_with_certificate(
    certificate: RTCCertificate,
    configuration: RTCConfiguration | None = None,
) -> RTCPeerConnection:
    """Create an RTCPeerConnection with a custom persistent certificate.

    :param certificate: The RTCCertificate to use for DTLS.
    :param configuration: Optional RTCConfiguration with ICE servers.
    :return: RTCPeerConnection configured with the provided certificate.
    """
    pc = RTCPeerConnection(configuration=configuration)
    # Replace the auto-generated certificate with our persistent one
    # Uses name-mangled private attribute access
    pc._RTCPeerConnection__certificates = [certificate]  # type: ignore[attr-defined]
    return pc
