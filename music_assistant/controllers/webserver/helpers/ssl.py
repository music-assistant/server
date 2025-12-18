"""SSL helpers for the webserver controller."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import aiofiles

LOGGER = logging.getLogger(__name__)


@dataclass
class SSLCertificateInfo:
    """Information about an SSL certificate."""

    is_valid: bool
    key_type: str  # "RSA", "ECDSA", or "Unknown"
    subject: str
    expiry: str
    is_expired: bool
    is_expiring_soon: bool  # Within 30 days
    error_message: str | None = None


async def get_ssl_content(value: str) -> str:
    """Get SSL content from either a file path or the raw PEM content.

    :param value: Either an absolute file path or the raw PEM content.
    :return: The PEM content as a string.
    :raises FileNotFoundError: If the file path doesn't exist.
    :raises ValueError: If the path is not a file.
    """
    value = value.strip()
    # Check if this looks like a file path (absolute path starting with /)
    # PEM content always starts with "-----BEGIN"
    if value.startswith("/") and not value.startswith("-----BEGIN"):
        # This looks like a file path
        path = Path(value)
        if not path.exists():
            raise FileNotFoundError(f"SSL file not found: {value}")
        if not path.is_file():
            raise ValueError(f"SSL path is not a file: {value}")
        async with aiofiles.open(path) as f:
            content: str = await f.read()
            return content
    # Otherwise, treat as raw PEM content
    return value


def _run_openssl_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run an openssl command synchronously.

    :param args: List of arguments for the openssl command (excluding 'openssl').
    :return: CompletedProcess result.
    """
    return subprocess.run(  # noqa: S603
        ["openssl", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


async def create_server_ssl_context(
    certificate: str,
    private_key: str,
    logger: logging.Logger | None = None,
) -> ssl.SSLContext | None:
    """Create an SSL context for a server from certificate and private key.

    :param certificate: The SSL certificate (file path or PEM content).
    :param private_key: The SSL private key (file path or PEM content).
    :param logger: Optional logger for error messages.
    :return: SSL context if successful, None otherwise.
    """
    log = logger or LOGGER
    if not certificate or not private_key:
        log.error(
            "SSL is enabled but certificate or private key is missing. "
            "Server will start without SSL."
        )
        return None

    cert_path = None
    key_path = None
    try:
        # Load certificate and key content (supports both file paths and raw content)
        cert_content = await get_ssl_content(certificate)
        key_content = await get_ssl_content(private_key)

        # Create SSL context
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)

        # Write certificate and key to temporary files
        # This is necessary because ssl.SSLContext.load_cert_chain requires file paths
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as cert_file:
            cert_file.write(cert_content)
            cert_path = cert_file.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as key_file:
            key_file.write(key_content)
            key_path = key_file.name

        # Load certificate and private key
        ssl_context.load_cert_chain(cert_path, key_path)
        log.info("SSL/TLS enabled for server")
        return ssl_context

    except Exception as e:
        log.exception("Failed to create SSL context: %s. Server will start without SSL.", e)
        return None
    finally:
        # Clean up temporary files
        if cert_path:
            with contextlib.suppress(Exception):
                Path(cert_path).unlink()
        if key_path:
            with contextlib.suppress(Exception):
                Path(key_path).unlink()


async def verify_ssl_certificate(certificate: str, private_key: str) -> SSLCertificateInfo:
    """Verify SSL certificate and private key are valid and match.

    :param certificate: The SSL certificate (file path or PEM content).
    :param private_key: The SSL private key (file path or PEM content).
    :return: SSLCertificateInfo with verification results.
    """
    if not certificate or not private_key:
        return SSLCertificateInfo(
            is_valid=False,
            key_type="Unknown",
            subject="",
            expiry="",
            is_expired=False,
            is_expiring_soon=False,
            error_message="Both certificate and private key are required.",
        )

    # Load certificate and key content
    try:
        cert_content = await get_ssl_content(certificate)
    except FileNotFoundError as e:
        return SSLCertificateInfo(
            is_valid=False,
            key_type="Unknown",
            subject="",
            expiry="",
            is_expired=False,
            is_expiring_soon=False,
            error_message=f"Certificate file not found: {e}",
        )
    except Exception as e:
        return SSLCertificateInfo(
            is_valid=False,
            key_type="Unknown",
            subject="",
            expiry="",
            is_expired=False,
            is_expiring_soon=False,
            error_message=f"Error loading certificate: {e}",
        )

    try:
        key_content = await get_ssl_content(private_key)
    except FileNotFoundError as e:
        return SSLCertificateInfo(
            is_valid=False,
            key_type="Unknown",
            subject="",
            expiry="",
            is_expired=False,
            is_expiring_soon=False,
            error_message=f"Private key file not found: {e}",
        )
    except Exception as e:
        return SSLCertificateInfo(
            is_valid=False,
            key_type="Unknown",
            subject="",
            expiry="",
            is_expired=False,
            is_expiring_soon=False,
            error_message=f"Error loading private key: {e}",
        )

    # Verify with temp files
    try:
        return await _verify_ssl_with_temp_files(cert_content, key_content)
    except ssl.SSLError as e:
        return SSLCertificateInfo(
            is_valid=False,
            key_type="Unknown",
            subject="",
            expiry="",
            is_expired=False,
            is_expiring_soon=False,
            error_message=_format_ssl_error(e),
        )
    except Exception as e:
        return SSLCertificateInfo(
            is_valid=False,
            key_type="Unknown",
            subject="",
            expiry="",
            is_expired=False,
            is_expiring_soon=False,
            error_message=f"Verification failed: {e}",
        )


async def _verify_ssl_with_temp_files(cert_content: str, key_content: str) -> SSLCertificateInfo:
    """Verify SSL using temporary files.

    :param cert_content: Certificate PEM content.
    :param key_content: Private key PEM content.
    :return: SSLCertificateInfo with verification results.
    """
    cert_path = None
    key_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as cert_file:
            cert_file.write(cert_content)
            cert_path = cert_file.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as key_file:
            key_file.write(key_content)
            key_path = key_file.name

        # Test loading into SSL context
        test_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        test_ctx.load_cert_chain(cert_path, key_path)

        # Get certificate details using openssl
        return await _get_certificate_details(cert_path)
    finally:
        # Clean up temp files
        if cert_path:
            with contextlib.suppress(Exception):
                Path(cert_path).unlink()
        if key_path:
            with contextlib.suppress(Exception):
                Path(key_path).unlink()


async def _get_certificate_details(cert_path: str) -> SSLCertificateInfo:
    """Get certificate details using openssl.

    :param cert_path: Path to the certificate file.
    :return: SSLCertificateInfo with certificate details.
    """
    # Get certificate info
    result = await asyncio.to_thread(
        _run_openssl_command,
        ["x509", "-in", cert_path, "-noout", "-subject", "-dates", "-issuer"],
    )

    if result.returncode != 0:
        return SSLCertificateInfo(
            is_valid=True,
            key_type="Unknown",
            subject="",
            expiry="",
            is_expired=False,
            is_expiring_soon=False,
        )

    # Parse certificate info
    expiry = ""
    subject = ""
    for line in result.stdout.strip().split("\n"):
        if line.startswith("notAfter="):
            expiry = line.replace("notAfter=", "")
        elif line.startswith("subject="):
            subject = line.replace("subject=", "")

    # Check expiry status
    expiry_check = await asyncio.to_thread(
        _run_openssl_command,
        ["x509", "-in", cert_path, "-noout", "-checkend", "0"],
    )
    is_expired = expiry_check.returncode != 0

    expiring_soon_check = await asyncio.to_thread(
        _run_openssl_command,
        ["x509", "-in", cert_path, "-noout", "-checkend", str(30 * 24 * 60 * 60)],
    )
    is_expiring_soon = expiring_soon_check.returncode != 0

    # Detect key type
    key_type_result = await asyncio.to_thread(
        _run_openssl_command,
        ["x509", "-in", cert_path, "-noout", "-text"],
    )
    key_type = "Unknown"
    if "rsaEncryption" in key_type_result.stdout:
        key_type = "RSA"
    elif "id-ecPublicKey" in key_type_result.stdout:
        key_type = "ECDSA"

    return SSLCertificateInfo(
        is_valid=True,
        key_type=key_type,
        subject=subject,
        expiry=expiry,
        is_expired=is_expired,
        is_expiring_soon=is_expiring_soon,
    )


def _format_ssl_error(e: ssl.SSLError) -> str:
    """Format an SSL error into a user-friendly message.

    :param e: The SSL error.
    :return: User-friendly error message.
    """
    error_msg = str(e)
    if "PEM lib" in error_msg:
        return (
            "Invalid certificate or key format. "
            "Make sure both are valid PEM format and the key is not encrypted."
        )
    if "key values mismatch" in error_msg.lower():
        return (
            "Certificate and private key do not match. "
            "Please verify you're using the correct key for this certificate."
        )
    return f"SSL Error: {error_msg}"


def format_certificate_info(info: SSLCertificateInfo) -> str:
    """Format SSLCertificateInfo into a human-readable string.

    :param info: The certificate info to format.
    :return: Human-readable string.
    """
    if not info.is_valid:
        return f"Error: {info.error_message}"

    status = "VALID"
    warning = ""
    if info.is_expired:
        status = "EXPIRED"
        warning = " (Certificate has expired!)"
    elif info.is_expiring_soon:
        status = "EXPIRING SOON"
        warning = " (Certificate expires within 30 days)"

    lines = [f"Certificate verification: {status}{warning}", f"Key type: {info.key_type}"]
    if info.subject:
        lines.append(f"Subject: {info.subject}")
    if info.expiry:
        lines.append(f"Expires: {info.expiry}")

    return "\n".join(lines)
