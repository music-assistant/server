"""Helper utilities for the Pandora provider."""

from __future__ import annotations

import secrets
from typing import Any

import aiohttp
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)

from .constants import PANDORA_ERROR_CODES


def generate_csrf_token() -> str:
    """Generate a random CSRF token."""
    return secrets.token_hex(16)


def handle_pandora_error(response_data: dict[str, Any]) -> None:
    """Handle Pandora API error responses."""
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
        raise RuntimeError(f"Pandora API error {error_code} ({error_desc}): {message}")


async def get_csrf_token(session: aiohttp.ClientSession) -> str:
    """Get CSRF token from Pandora website."""
    try:
        async with session.head("https://www.pandora.com/") as response:
            # Try to extract from cookies first
            if "csrftoken" in response.cookies:
                return str(response.cookies["csrftoken"].value)
    except aiohttp.ClientError as e:
        # Network issues - this is temporarily unavailable
        raise ResourceTemporarilyUnavailable(f"Failed to get CSRF token from Pandora: {e}")
    except Exception as e:
        # Unexpected errors should also be treated as temporary issues
        raise ResourceTemporarilyUnavailable(f"Unexpected error getting CSRF token: {e}")

    # If we get here, no CSRF token was found in cookies
    return generate_csrf_token()


def create_auth_headers(csrf_token: str, auth_token: str | None = None) -> dict[str, str]:
    """Create authentication headers for Pandora API requests."""
    headers = {
        "Content-Type": "application/json;charset=utf-8",
        "X-CsrfToken": csrf_token,
        "Cookie": f"csrftoken={csrf_token}",
        "User-Agent": "Music Assistant Pandora Provider/1.0",
    }

    if auth_token:
        headers["X-AuthToken"] = auth_token

    return headers


def format_duration(duration_ms: int | None) -> float:
    """Convert duration from milliseconds to seconds."""
    if duration_ms is None:
        return 0.0
    return duration_ms / 1000.0


def safe_get(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Safely get nested dictionary values."""
    for key in keys:
        if isinstance(data, dict) and key in data:
            data = data[key]
        else:
            return default
    return data
