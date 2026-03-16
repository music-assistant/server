"""Helper utilities for the Pandora provider."""

from __future__ import annotations

import secrets
from typing import Any

import aiohttp
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)

from .constants import AUTH_ERRORS, NOT_FOUND_ERRORS, UNAVAILABLE_ERRORS


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
        InvalidDataError: For other API errors
    """
    if (error_code := response_data.get("errorCode")) is None:
        return

    message = response_data.get("message", response_data.get("errorString", "Unknown error"))

    # Use the categorized sets for cleaner logic
    if error_code in AUTH_ERRORS:
        raise LoginFailed(f"Authentication failed: {message}")

    if error_code in NOT_FOUND_ERRORS:
        raise MediaNotFoundError(f"The requested resource was not found: {message}")

    if error_code in UNAVAILABLE_ERRORS:
        raise ResourceTemporarilyUnavailable(f"Pandora service issue: {message}")

    # Fallback for any other API error
    raise InvalidDataError(f"Pandora API Error [{error_code}]: {message}")


async def get_csrf_token(session: aiohttp.ClientSession) -> str:
    """Get CSRF token from Pandora website.

    Attempts to retrieve CSRF token from Pandora cookies.

    Args:
        session: aiohttp client session

    Returns:
        CSRF token string

    Raises:
        ProviderUnavailableError: If network request fails
        ResourceTemporarilyUnavailable: If no token available
    """
    try:
        # Use a more specific timeout for this initial handshake
        async with session.head(
            "https://www.pandora.com/",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if "csrftoken" in response.cookies:
                return str(response.cookies["csrftoken"].value)
    except aiohttp.ClientError as err:
        # Catch network issues at the source and wrap in MA error
        raise ProviderUnavailableError(f"Network error while reaching Pandora: {err}") from err

    raise ResourceTemporarilyUnavailable("Pandora web session failed to provide a CSRF token.")


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
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.pandora.com",
        "Referer": "https://www.pandora.com/",
    }

    if auth_token:
        headers["X-AuthToken"] = auth_token

    return headers
