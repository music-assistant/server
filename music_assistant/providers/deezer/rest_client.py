"""Resolve external identifiers through Deezer's public catalogue API."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.enums import ExternalID
from music_assistant_models.errors import (
    InvalidDataError,
    ProviderUnavailableError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)
from yarl import URL

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.external_ids import normalize_external_id
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

REST_API_URL = "https://api.deezer.com"


class DeezerRESTClient:
    """Look up Deezer catalogue IDs without account credentials."""

    domain = "deezer"
    throttler = ThrottlerManager(rate_limit=1, period=1, retry_attempts=3)

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the catalogue client."""
        self.mass = mass
        self.logger = logging.getLogger(__name__)

    @use_cache(3600 * 24 * 7)
    @throttle_with_retries
    async def get_item_id(self, external_id: str, external_id_type: ExternalID) -> str | None:
        """Resolve a normalized ISRC or UPC to a Deezer ID, or None if absent."""
        media_type, field = {
            ExternalID.ISRC: ("track", "isrc"),
            ExternalID.BARCODE: ("album", "upc"),
        }[external_id_type]
        url = URL(f"{REST_API_URL}/{media_type}/{field}:{external_id}")
        session = self.mass.http_session
        # The shared jar may contain another Deezer account's domain-wide cookies.
        cookies = dict.fromkeys(session.cookie_jar.filter_cookies(url), "")
        try:
            async with session.get(
                url, cookies=cookies, timeout=ClientTimeout(total=15), allow_redirects=False
            ) as response:
                if response.status == 429:
                    raise RateLimited(
                        "Deezer catalogue rate limit",
                        backoff_time=parse_retry_after(response.headers.get("Retry-After")),
                    )
                if response.status >= 500:
                    raise ResourceTemporarilyUnavailable("Deezer catalogue unavailable")
                if response.status == 404:
                    return None
                if response.status != 200:
                    raise ProviderUnavailableError(
                        f"Deezer catalogue returned HTTP {response.status}"
                    )
                data = await response.json(loads=json_loads)
        except (ClientError, TimeoutError) as err:
            raise ResourceTemporarilyUnavailable("Deezer catalogue request failed") from err
        except ValueError as err:
            raise InvalidDataError("Invalid Deezer catalogue response") from err

        if not isinstance(data, dict):
            raise InvalidDataError("Invalid Deezer catalogue response")
        if error := data.get("error"):
            if not isinstance(error, dict):
                raise InvalidDataError("Invalid Deezer catalogue error")
            if error.get("code") == 800:
                return None
            if error.get("code") == 4:
                raise RateLimited("Deezer catalogue quota exceeded")
            raise ProviderUnavailableError(f"Deezer catalogue error {error.get('code')}")

        item_id = data.get("id")
        identifier = data.get(field)
        if (
            not isinstance(item_id, (int, str))
            or not str(item_id).isascii()
            or not str(item_id).isdigit()
            or int(item_id) <= 0
            or not isinstance(identifier, str)
        ):
            raise InvalidDataError("Incomplete Deezer catalogue result")
        if normalize_external_id(external_id_type, identifier) != normalize_external_id(
            external_id_type, external_id
        ):
            raise InvalidDataError("Deezer catalogue returned a different external ID")
        return str(item_id)
