"""Authentication for Jellyfin provider."""

from __future__ import annotations

import hashlib
import logging
import socket
from typing import TYPE_CHECKING, Any

from aiojellyfin import authenticate_by_name
from aiojellyfin.session import SessionConfiguration
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.jellyfin.const import USER_APP_NAME

if TYPE_CHECKING:
    from aiojellyfin import Connection


async def authenticate(
    server_id: str,
    username: str,
    password: str,
    url: str,
    verify_ssl: bool,
    http_session: Any,
    app_version: str,
    logger: logging.Logger,
) -> Connection:
    """Authenticate with Jellyfin server and return authenticated connection.

    :param server_id: Unique server identifier.
    :param username: Username for authentication.
    :param password: Password for authentication.
    :param url: Jellyfin server URL.
    :param verify_ssl: Whether to verify SSL certificates.
    :param http_session: aiohttp session to use.
    :param app_version: Application version for device identification.
    :param logger: Logger instance.
    :return: Authenticated Jellyfin connection.
    :raises LoginFailed: If authentication fails.
    """
    device_id = _generate_device_id(server_id, username)
    session_config = _create_session_config(device_id, url, verify_ssl, http_session, app_version)

    try:
        client = await authenticate_by_name(session_config, username, password)
        logger.debug("Successfully authenticated with Jellyfin server")
        return client
    except Exception as err:
        logger.error(f"Jellyfin authentication failed: {err}")
        raise LoginFailed(f"Authentication failed: {err}") from err


def _generate_device_id(server_id: str, username: str) -> str:
    """Generate a stable device ID for this Jellyfin connection.

    Device ID should be stable between reboots. Otherwise every time the
    provider starts we "leak" a new device entry in the Jellyfin backend,
    which creates devices and entities in HA if they also use the Jellyfin
    integration there.

    We follow a suggestion a Jellyfin dev gave to HA and use an ID that is
    stable even if provider is removed and re-added. They said mix in username
    in case the same device/app has 2 connections to the same servers.

    Neither of these are secrets (username is handed over to mint a token and
    server_id is used in zeroconf) but hash them anyway as it's meant to be an
    opaque identifier.

    :param server_id: Unique server identifier.
    :param username: Username for this connection.
    :return: Stable device identifier.
    """
    return hashlib.sha256(f"{server_id}+{username}".encode()).hexdigest()


def _create_session_config(
    device_id: str, url: str, verify_ssl: bool, http_session: Any, app_version: str
) -> SessionConfiguration:
    """Create SessionConfiguration for Jellyfin connection.

    :param device_id: Device identifier for this session.
    :param url: Jellyfin server URL.
    :param verify_ssl: Whether to verify SSL certificates.
    :param http_session: aiohttp session to use.
    :param app_version: Application version.
    :return: Configured SessionConfiguration instance.
    """
    return SessionConfiguration(
        session=http_session,
        url=url,
        verify_ssl=verify_ssl,
        app_name=USER_APP_NAME,
        app_version=app_version,
        device_name=socket.gethostname(),
        device_id=device_id,
    )
