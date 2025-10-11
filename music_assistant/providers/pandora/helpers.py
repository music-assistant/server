"""Helper utilities for the Pandora provider."""

from __future__ import annotations

import secrets
from typing import Any

import aiohttp
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)

from .constants import PANDORA_ERROR_CODES


def generate_csrf_token() -> str:
    """Generate a random CSRF token."""
    return secrets.token_hex(16)


def handle_pandora_error(response_data: dict[str, Any]) -> None:
    """Handle Pandora API error responses.

    Maps Pandora API error codes to appropriate Music Assistant exceptions.

    Raises:
        LoginFailed: For authentication errors
        MediaNotFoundError: For missing stations/tracks
        ResourceTemporarilyUnavailable: For service availability issues
        ProviderUnavailableError: For other API errors
    """
    if response_data.get("errorCode") is not None:
        error_code = response_data["errorCode"]
        error_string = response_data.get("errorString", "UNKNOWN_ERROR")
        message = response_data.get("message", "An unknown error occurred")

        # Map specific error codes to Music Assistant exceptions
        if error_code in (12, 13, 1002):  # Invalid username/password/login
            raise LoginFailed(f"Login failed: {message}")

        if error_code in (4, 5):  # Station/track not found
            raise MediaNotFoundError(f"Media not found: {message}")

        if error_code in (9, 10):  # Service unavailable
            raise ResourceTemporarilyUnavailable(f"Service unavailable: {message}")

        if error_code in (1001, 1003):  # Auth token issues
            raise LoginFailed(f"Authentication error: {message}")

        # Get error description from our mapping
        error_desc = PANDORA_ERROR_CODES.get(error_code, error_string)
        raise ProviderUnavailableError(f"Pandora API error {error_code} ({error_desc}): {message}")


async def get_csrf_token(session: aiohttp.ClientSession) -> str:
    """Get CSRF token from Pandora website.

    Attempts to retrieve CSRF token from Pandora cookies.

    Args:
        session: aiohttp client session

    Returns:
        CSRF token string

    Raises:
        ResourceTemporarilyUnavailable: If network request fails or no token available
    """
    try:
        async with session.head("https://www.pandora.com/") as response:
            if "csrftoken" in response.cookies:
                return str(response.cookies["csrftoken"].value)
    except aiohttp.ClientError as e:
        raise ResourceTemporarilyUnavailable(f"Failed to get CSRF token from Pandora: {e}") from e

    # No token found - service may be unavailable
    raise ResourceTemporarilyUnavailable(
        "Pandora did not provide a CSRF token. Service may be unavailable."
    )


def create_auth_headers(csrf_token: str, auth_token: str | None = None) -> dict[str, str]:
    """Create authentication headers for Pandora API requests.

    Args:
        csrf_token: CSRF token for request validation
        auth_token: Optional authentication token for authenticated requests

    Returns:
        Dictionary of HTTP headers
    """
    headers = {
        "Content-Type": "application/json;charset=utf-8",
        "X-CsrfToken": csrf_token,
        "Cookie": f"csrftoken={csrf_token}",
        "User-Agent": "Music Assistant Pandora Provider/1.0",
    }

    if auth_token:
        headers["X-AuthToken"] = auth_token

    return headers
