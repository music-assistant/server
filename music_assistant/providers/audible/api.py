"""API and authentication handling for Audible."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
from os import PathLike
from typing import Any
from urllib.parse import parse_qs, urlparse

import audible
import audible.register
from music_assistant_models.errors import LoginFailed

# Cache constants
CACHE_DOMAIN = "audible"
CACHE_CATEGORY_API = 0

# Cache for authenticator objects to avoid repeated file reads
_AUTH_CACHE: dict[str, audible.Authenticator] = {}


async def cached_authenticator_from_file(path: str) -> audible.Authenticator:
    """Get an authenticator from file with caching to avoid repeated file reads.

    Args:
        path: Path to the authenticator file

    Returns:
        Audible Authenticator object

    Raises:
        FileNotFoundError: If the authenticator file doesn't exist
        ValueError: If the authenticator file is invalid
    """
    logger = logging.getLogger("audible_api")

    # Return from cache if available
    if path in _AUTH_CACHE:
        logger.debug("Using cached authenticator for %s", path)
        return _AUTH_CACHE[path]

    # Check if file exists
    if not await check_file_exists(path):
        logger.error("Authenticator file not found: %s", path)
        raise FileNotFoundError(f"Authenticator file not found: {path}")

    try:
        logger.debug("Loading authenticator from file %s and caching it", path)
        auth = await asyncio.to_thread(audible.Authenticator.from_file, path)
        _AUTH_CACHE[path] = auth
        return auth
    except Exception as exc:
        logger.error("Failed to load authenticator from %s: %s", path, exc)
        raise ValueError(f"Invalid authenticator file: {exc}") from exc


async def audible_get_auth_info(locale: str) -> tuple[str, str, str]:
    """
    Generate the login URL and auth info for Audible OAuth flow asynchronously.

    Args:
        locale: The locale string (e.g., 'us', 'uk', 'de') to determine region settings

    Returns:
        A tuple containing:
        - code_verifier (str): The OAuth code verifier string
        - oauth_url (str): The complete OAuth URL for login
        - serial (str): The generated device serial number

    Raises:
        ValueError: If the locale is invalid or OAuth URL generation fails
    """
    logger = logging.getLogger("audible_api")
    logger.debug("Generating auth info for locale: %s", locale)

    try:
        # Create locale object
        locale_obj = audible.localization.Locale(locale)

        # Generate code verifier
        try:
            code_verifier = await asyncio.to_thread(audible.login.create_code_verifier)
        except Exception as exc:
            logger.error("Failed to create code verifier: %s", exc)
            raise ValueError(f"Failed to create code verifier: {exc}") from exc

        # Build OAuth URL
        try:
            oauth_url, serial = await asyncio.to_thread(
                audible.login.build_oauth_url,
                country_code=locale_obj.country_code,
                domain=locale_obj.domain,
                market_place_id=locale_obj.market_place_id,
                code_verifier=code_verifier,
                with_username=False,
            )
        except Exception as exc:
            logger.error("Failed to build OAuth URL: %s", exc)
            raise ValueError(f"Failed to build OAuth URL: {exc}") from exc

        logger.debug("Successfully generated auth info for locale: %s", locale)
        return code_verifier.decode(), oauth_url, serial

    except Exception as exc:
        if isinstance(exc, ValueError):
            # Re-raise ValueError exceptions
            raise
        # Convert other exceptions to TypeError
        logger.error("Unexpected error generating auth info: %s", exc)
        raise TypeError(f"Failed to generate auth info: {exc}") from exc


async def audible_custom_login(
    code_verifier: str, response_url: str, serial: str, locale: str
) -> audible.Authenticator:
    """
    Complete the authentication using the code_verifier, response_url, and serial asynchronously.

    Args:
        code_verifier: The code verifier string used in OAuth flow
        response_url: The response URL containing the authorization code
        serial: The device serial number
        locale: The locale string

    Returns:
        Audible Authenticator object

    Raises:
        LoginFailed: If authorization code is not found in the URL or registration fails
    """
    logger = logging.getLogger("audible_api")
    logger.debug("Starting custom login process for locale: %s", locale)

    try:
        # Create authenticator with locale
        auth = audible.Authenticator()
        auth.locale = audible.localization.Locale(locale)

        # Parse the response URL to extract authorization code
        response_url_parsed = urlparse(response_url)
        parsed_qs = parse_qs(response_url_parsed.query)

        authorization_codes = parsed_qs.get("openid.oa2.authorization_code")
        if not authorization_codes:
            logger.error("Authorization code not found in the provided URL")
            raise LoginFailed("Authorization code not found in the provided URL.")

        authorization_code = authorization_codes[0]
        logger.debug("Authorization code extracted successfully")

        # Register the device with Audible
        try:
            registration_data = await asyncio.to_thread(
                audible.register.register,
                authorization_code=authorization_code,
                code_verifier=code_verifier.encode(),
                domain=auth.locale.domain,
                serial=serial,
            )
            auth._update_attrs(**registration_data)
            logger.debug("Registration completed successfully")
            return auth

        except Exception as exc:
            logger.error("Registration failed: %s", exc)
            raise LoginFailed(f"Failed to register with Audible: {exc}") from exc

    except LoginFailed:
        # Re-raise LoginFailed exceptions
        raise
    except Exception as exc:
        # Convert other exceptions to LoginFailed
        logger.error("Unexpected error during login: %s", exc)
        raise LoginFailed(f"Login process failed: {exc}") from exc


async def check_file_exists(path: str | PathLike[str]) -> bool:
    """Check if a file exists asynchronously.

    Args:
        path: Path to the file to check

    Returns:
        True if the file exists, False otherwise
    """
    return await asyncio.to_thread(os.path.exists, path)


async def remove_file(path: str | PathLike[str]) -> None:
    """Delete a file asynchronously.

    Args:
        path: Path to the file to delete

    Raises:
        FileNotFoundError: If the file doesn't exist
        PermissionError: If the user doesn't have permission to delete the file
    """
    await asyncio.to_thread(os.remove, path)


def html_to_txt(html_text: str) -> str:
    """Convert HTML text to plain text.

    Args:
        html_text: The HTML text to convert

    Returns:
        Plain text with HTML tags removed
    """
    if not html_text:
        return ""

    try:
        # Unescape HTML entities
        txt = html.unescape(html_text)

        # Remove HTML tags
        tags = re.findall("<[^>]+>", txt)
        for tag in tags:
            txt = txt.replace(tag, "")

        # Replace multiple spaces with single space
        txt = re.sub(r"\s+", " ", txt)

        # Replace multiple newlines with single newline
        txt = re.sub(r"\n+", "\n", txt)

        # Trim leading/trailing whitespace
        return txt.strip()
    except Exception:
        # If any error occurs, return the original text
        return str(html_text)


class AudibleAPI:
    """API client for Audible."""

    def __init__(
        self,
        client: audible.AsyncClient,
        mass: Any,
        provider_domain: str,
        provider_instance: str,
        logger: logging.Logger | None = None,
    ):
        """Initialize the Audible API client."""
        self.client = client
        self.mass = mass
        self.provider_domain = provider_domain
        self.provider_instance = provider_instance
        self.logger = logger or logging.getLogger("audible_api")

    async def call_api(self, path: str, **kwargs: Any) -> Any:
        """Call the Audible API with caching.

        Args:
            path: The API endpoint path
            **kwargs: Additional parameters to pass to the API call

        Returns:
            The API response data

        Note:
            The 'use_cache' parameter (default: False) can be included in kwargs
            to control whether to use cached responses
        """
        response = None
        use_cache = kwargs.pop("use_cache", False)

        # Create a unique cache key based on the path and parameters
        params_str = json.dumps(kwargs, sort_keys=True)
        params_hash = hashlib.md5(params_str.encode()).hexdigest()
        cache_key = f"{path}:{params_hash}"

        self.logger.debug("Calling Audible API: %s (use_cache=%s)", path, use_cache)

        try:
            # Try to get from cache if enabled
            if use_cache:
                self.logger.debug("Attempting to retrieve from cache: %s", cache_key)
                response = await self.mass.cache.get(
                    key=cache_key, base_key=CACHE_DOMAIN, category=CACHE_CATEGORY_API
                )
                if response:
                    self.logger.debug("Cache hit for: %s", cache_key)

            # Make API call if not in cache
            if not response:
                self.logger.debug("Making API request to: %s", path)
                response = await self.client.get(path, **kwargs)

                # Cache the response
                await self.mass.cache.set(
                    key=cache_key, base_key=CACHE_DOMAIN, category=CACHE_CATEGORY_API, data=response
                )
                self.logger.debug("Cached response for: %s", cache_key)

            return response

        except Exception as exc:
            self.logger.error("Error calling Audible API %s: %s", path, exc)
            raise

    async def deregister(self) -> None:
        """Deregister this provider from Audible.

        This removes the device registration from Audible's servers.
        """
        self.logger.debug("Deregistering device from Audible")
        try:
            await asyncio.to_thread(self.client.auth.deregister_device)
            self.logger.debug("Device successfully deregistered from Audible")
        except Exception as exc:
            self.logger.error("Failed to deregister device from Audible: %s", exc)
            raise
