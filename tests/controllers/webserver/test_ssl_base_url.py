"""Tests for the URL scheme the webserver advertises when SSL is enabled."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from music_assistant.constants import WILDCARD_BIND_IPS
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.helpers.datetime import utc

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant_models.config_entries import CoreConfig


@pytest.fixture(scope="module")
def self_signed_cert() -> tuple[str, str]:
    """Return a self-signed certificate and its private key, as PEM content."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = utc()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM).decode(),
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
    )


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock Music Assistant instance."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    mass.running_as_hass_addon = False
    return mass


async def test_valid_certificate_advertises_tls(
    mock_mass: MagicMock, tmp_path: Path, self_signed_cert: tuple[str, str]
) -> None:
    """Verify a usable certificate results in https URLs."""
    certificate, private_key = self_signed_cert
    webserver, server = await _setup_webserver(
        mock_mass, tmp_path, certificate=certificate, private_key=private_key
    )

    assert webserver.base_url == "https://192.168.1.5:8095"
    assert webserver.internal_base_url == "https://127.0.0.1:8095"
    assert server.setup.await_args.kwargs["ssl_context"] is not None


async def test_missing_certificate_advertises_plain_http(
    mock_mass: MagicMock, tmp_path: Path
) -> None:
    """Verify the URLs follow the plain HTTP fallback when no certificate is configured."""
    webserver, server = await _setup_webserver(mock_mass, tmp_path, certificate="", private_key="")

    assert webserver.base_url == "http://192.168.1.5:8095"
    assert webserver.internal_base_url == "http://127.0.0.1:8095"
    assert server.setup.await_args.kwargs["ssl_context"] is None


async def test_invalid_certificate_advertises_plain_http(
    mock_mass: MagicMock, tmp_path: Path
) -> None:
    """Verify the URLs follow the plain HTTP fallback when the certificate is unusable."""
    webserver, server = await _setup_webserver(
        mock_mass, tmp_path, certificate="not a certificate", private_key="not a private key"
    )

    assert webserver.base_url == "http://192.168.1.5:8095"
    assert webserver.internal_base_url == "http://127.0.0.1:8095"
    assert server.setup.await_args.kwargs["ssl_context"] is None


def _make_server_mock() -> MagicMock:
    """Create a Webserver double that adopts the address it is set up with."""
    server = MagicMock()

    async def _adopt_setup_args(**kwargs: Any) -> None:
        server.port = kwargs["bind_port"]
        bind_ip = kwargs["bind_ip"]
        server.bind_ip = None if bind_ip in WILDCARD_BIND_IPS else bind_ip

    server.setup = AsyncMock(side_effect=_adopt_setup_args)
    return server


async def _setup_webserver(
    mock_mass: MagicMock,
    tmp_path: Path,
    *,
    certificate: str,
    private_key: str,
) -> tuple[WebserverController, MagicMock]:
    """
    Run the real setup of a WebserverController with SSL enabled.

    :param mock_mass: Mocked Music Assistant instance to build the controller on.
    :param tmp_path: Directory to serve as the frontend, in place of the bundled one.
    :param certificate: Value for the ssl_certificate config option.
    :param private_key: Value for the ssl_private_key config option.
    :return: The controller and the Webserver double it was set up against.
    """
    webserver = WebserverController(mock_mass)
    server = _make_server_mock()
    webserver._server = server
    webserver.auth = MagicMock(setup=AsyncMock())
    webserver.remote_access = MagicMock(setup=AsyncMock())

    config_values: dict[str, Any] = {
        "bind_port": 8095,
        "bind_ip": None,
        "enable_ssl": True,
        "ssl_certificate": certificate,
        "ssl_private_key": private_key,
    }
    config = MagicMock()
    config.get_value.side_effect = lambda key, default=None: config_values.get(key, default)

    with (
        patch(
            "music_assistant.controllers.webserver.controller.get_ip_addresses",
            AsyncMock(return_value=("192.168.1.5",)),
        ),
        patch(
            "music_assistant.controllers.webserver.controller.locate_frontend",
            return_value=str(tmp_path),
        ),
    ):
        await webserver.setup(cast("CoreConfig", config))
    return webserver, server
