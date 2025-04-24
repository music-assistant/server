"""Tidal music provider support for MusicAssistant."""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar, cast

from aiohttp import ClientConnectionError, ClientResponse
from aiohttp.client_exceptions import (
    ClientConnectorError,
    ClientError,
    ClientPayloadError,
    ClientResponseError,
)
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    ContentType,
    ExternalID,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    MediaItemTypeOrItemMapping,
    Playlist,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CACHE_CATEGORY_DEFAULT, CACHE_CATEGORY_RECOMMENDATIONS
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.models.music_provider import MusicProvider

from .auth_manager import ManualAuthenticationHelper, TidalAuthManager
from .tidal_page_parser import TidalPageParser

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from aiohttp import ClientResponse
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

TOKEN_TYPE = "Bearer"

# Actions
CONF_ACTION_START_PKCE_LOGIN = "start_pkce_login"
CONF_ACTION_COMPLETE_PKCE_LOGIN = "auth"
CONF_ACTION_CLEAR_AUTH = "clear_auth"

# Intermediate steps
CONF_TEMP_SESSION = "temp_session"
CONF_OOPS_URL = "oops_url"

# Config keys
CONF_AUTH_TOKEN = "auth_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_USER_ID = "user_id"
CONF_EXPIRY_TIME = "expiry_time"
CONF_COUNTRY_CODE = "country_code"
CONF_SESSION_ID = "session_id"
CONF_QUALITY = "quality"

# Labels
LABEL_START_PKCE_LOGIN = "start_pkce_login_label"
LABEL_OOPS_URL = "oops_url_label"
LABEL_COMPLETE_PKCE_LOGIN = "complete_pkce_login_label"

BROWSE_URL = "https://tidal.com/browse"
RESOURCES_URL = "https://resources.tidal.com/images"

DEFAULT_LIMIT = 50

T = TypeVar("T")


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return TidalProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    assert values is not None

    if action == CONF_ACTION_START_PKCE_LOGIN:
        async with ManualAuthenticationHelper(
            mass, cast("str", values["session_id"])
        ) as auth_helper:
            quality = str(values.get(CONF_QUALITY))
            base64_session = await TidalAuthManager.generate_auth_url(auth_helper, quality)
            values[CONF_TEMP_SESSION] = base64_session
            # Tidal is using the ManualAuthenticationHelper just to send the user to an URL
            # there is no actual oauth callback happening, instead the user is redirected
            # to a non-existent page and needs to copy the URL from the browser and paste it
            # we simply wait here to allow the user to start the auth
            await asyncio.sleep(15)

    if action == CONF_ACTION_COMPLETE_PKCE_LOGIN:
        quality = str(values.get(CONF_QUALITY))
        pkce_url = str(values.get(CONF_OOPS_URL))
        base64_session = str(values.get(CONF_TEMP_SESSION))
        auth_data = await TidalAuthManager.process_pkce_login(
            mass.http_session, base64_session, pkce_url
        )
        values[CONF_AUTH_TOKEN] = auth_data["access_token"]
        values[CONF_REFRESH_TOKEN] = auth_data["refresh_token"]
        values[CONF_EXPIRY_TIME] = auth_data["expires_at"]
        values[CONF_USER_ID] = auth_data["userId"]
        values[CONF_TEMP_SESSION] = ""

    if action == CONF_ACTION_CLEAR_AUTH:
        values[CONF_AUTH_TOKEN] = None
        values[CONF_REFRESH_TOKEN] = None
        values[CONF_EXPIRY_TIME] = None
        values[CONF_USER_ID] = None

    if values.get(CONF_AUTH_TOKEN):
        auth_entries: tuple[ConfigEntry, ...] = (
            ConfigEntry(
                key="label_ok",
                type=ConfigEntryType.LABEL,
                label="You are authenticated with Tidal",
            ),
            ConfigEntry(
                key=CONF_ACTION_CLEAR_AUTH,
                type=ConfigEntryType.ACTION,
                label="Reset authentication",
                description="Reset the authentication for Tidal",
                action=CONF_ACTION_CLEAR_AUTH,
                value=None,
            ),
            ConfigEntry(
                key=CONF_QUALITY,
                type=ConfigEntryType.STRING,
                label="Quality setting for Tidal:",
                description="High = 16bit 44.1kHz\n\nMax = Up to 24bit 192kHz",
                options=[
                    ConfigValueOption("High", "LOSSLESS"),
                    ConfigValueOption("Max", "HI_RES_LOSSLESS"),
                ],
                default_value="HI_RES_LOSSLESS",
            ),
        )
    else:
        auth_entries = (
            ConfigEntry(
                key=CONF_QUALITY,
                type=ConfigEntryType.STRING,
                label="Quality setting for Tidal:",
                required=True,
                description="High = 16bit 44.1kHz\n\nMax = Up to 24bit 192kHz",
                options=[
                    ConfigValueOption("High", "LOSSLESS"),
                    ConfigValueOption("Max", "HI_RES_LOSSLESS"),
                ],
                default_value="HI_RES_LOSSLESS",
            ),
            ConfigEntry(
                key=LABEL_START_PKCE_LOGIN,
                type=ConfigEntryType.LABEL,
                label="The button below will redirect you to Tidal.com to authenticate.\n\n"
                " After authenticating, you will be redirected to a page that prominently displays"
                " 'Oops' at the top. That is normal, you need to copy that URL from the "
                "address bar and come back here",
                hidden=action == CONF_ACTION_START_PKCE_LOGIN,
            ),
            ConfigEntry(
                key=CONF_ACTION_START_PKCE_LOGIN,
                type=ConfigEntryType.ACTION,
                label="Starts the auth process via PKCE on Tidal.com",
                description="This button will redirect you to Tidal.com to authenticate."
                " After authenticating, you will be redirected to a page that prominently displays"
                " 'Oops' at the top.",
                action=CONF_ACTION_START_PKCE_LOGIN,
                depends_on=CONF_QUALITY,
                action_label="Starts the auth process via PKCE on Tidal.com",
                value=cast("str", values.get(CONF_TEMP_SESSION)) if values else None,
                hidden=action == CONF_ACTION_START_PKCE_LOGIN,
            ),
            ConfigEntry(
                key=CONF_TEMP_SESSION,
                type=ConfigEntryType.STRING,
                label="Temporary session for Tidal",
                hidden=True,
                required=False,
                value=cast("str", values.get(CONF_TEMP_SESSION)) if values else None,
            ),
            ConfigEntry(
                key=LABEL_OOPS_URL,
                type=ConfigEntryType.LABEL,
                label="Copy the URL from the 'Oops' page that you were previously redirected to"
                " and paste it in the field below",
                hidden=action != CONF_ACTION_START_PKCE_LOGIN,
            ),
            ConfigEntry(
                key=CONF_OOPS_URL,
                type=ConfigEntryType.STRING,
                label="Oops URL from Tidal redirect",
                description="This field should be filled manually by you after authenticating on"
                " Tidal.com and being redirected to a page that prominently displays"
                " 'Oops' at the top.",
                depends_on=CONF_ACTION_START_PKCE_LOGIN,
                value=cast("str", values.get(CONF_OOPS_URL)) if values else None,
                hidden=action != CONF_ACTION_START_PKCE_LOGIN,
            ),
            ConfigEntry(
                key=LABEL_COMPLETE_PKCE_LOGIN,
                type=ConfigEntryType.LABEL,
                label="After pasting the URL in the field above, click the button below to complete"
                " the process.",
                hidden=action != CONF_ACTION_START_PKCE_LOGIN,
            ),
            ConfigEntry(
                key=CONF_ACTION_COMPLETE_PKCE_LOGIN,
                type=ConfigEntryType.ACTION,
                label="Complete the auth process via PKCE on Tidal.com",
                description="Click this after adding the 'Oops' URL above, this will complete the"
                " authentication process.",
                action=CONF_ACTION_COMPLETE_PKCE_LOGIN,
                depends_on=CONF_OOPS_URL,
                action_label="Complete the auth process via PKCE on Tidal.com",
                value=None,
                hidden=action != CONF_ACTION_START_PKCE_LOGIN,
            ),
        )

    # return the auth_data config entry
    return (
        *auth_entries,
        ConfigEntry(
            key=CONF_AUTH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Authentication token for Tidal",
            description="You need to link Music Assistant to your Tidal account.",
            hidden=True,
            value=cast("str", values.get(CONF_AUTH_TOKEN)) if values else None,
        ),
        ConfigEntry(
            key=CONF_REFRESH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Refresh token for Tidal",
            description="You need to link Music Assistant to your Tidal account.",
            hidden=True,
            value=cast("str", values.get(CONF_REFRESH_TOKEN)) if values else None,
        ),
        ConfigEntry(
            key=CONF_EXPIRY_TIME,
            type=ConfigEntryType.STRING,
            label="Expiry time of auth token for Tidal",
            hidden=True,
            value=cast("str", values.get(CONF_EXPIRY_TIME)) if values else None,
        ),
        ConfigEntry(
            key=CONF_USER_ID,
            type=ConfigEntryType.STRING,
            label="Your Tidal User ID",
            description="This is your unique Tidal user ID.",
            hidden=True,
            value=cast("str", values.get(CONF_USER_ID)) if values else None,
        ),
    )


class TidalProvider(MusicProvider):
    """Implementation of a Tidal MusicProvider."""

    BASE_URL: str = "https://api.tidal.com/v1"
    BASE_URL_V2: str = "https://api.tidal.com/v2"
    OPEN_API_URL: str = "https://openapi.tidal.com/v2"

    throttler = ThrottlerManager(rate_limit=1, period=2)

    #
    # INITIALIZATION & SETUP
    #

    def __init__(self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig):
        """Initialize Tidal provider."""
        super().__init__(mass, manifest, config)
        self.auth = TidalAuthManager(
            http_session=mass.http_session,
            config_updater=self._update_auth_config,
            logger=self.logger,
        )
        self.page_cache_ttl = 3 * 3600

    def _update_auth_config(self, auth_info: dict[str, Any]) -> None:
        """Update auth config with new auth info."""
        self.update_config_value(CONF_AUTH_TOKEN, auth_info["access_token"], encrypted=True)
        self.update_config_value(CONF_REFRESH_TOKEN, auth_info["refresh_token"], encrypted=True)
        self.update_config_value(CONF_EXPIRY_TIME, auth_info["expires_at"])
        self.update_config_value(CONF_USER_ID, auth_info["userId"])

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # Load auth info from individual config values
        access_token = self.config.get_value(CONF_AUTH_TOKEN)
        refresh_token = self.config.get_value(CONF_REFRESH_TOKEN)
        expires_at = self.config.get_value(CONF_EXPIRY_TIME)
        user_id = self.config.get_value(CONF_USER_ID)

        if not access_token or not refresh_token:
            raise LoginFailed("Missing authentication data")

        # Handle conversion from ISO format to timestamp if needed
        if isinstance(expires_at, str) and "T" in expires_at:
            # This looks like an ISO format date
            import datetime

            try:
                dt = datetime.datetime.fromisoformat(expires_at)
                # Convert to timestamp
                expires_at = dt.timestamp()
                # Update the config with the numeric value
                self.update_config_value(CONF_EXPIRY_TIME, expires_at)
            except ValueError:
                self.logger.warning(
                    "Could not parse expiry time %s, setting to expired", expires_at
                )
                expires_at = 0

        # Create auth data dictionary from individual config values
        auth_data = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "userId": user_id,
        }

        # Initialize auth manager
        if not await self.auth.initialize(json.dumps(auth_data)):
            raise LoginFailed("Failed to authenticate with Tidal")

        # Get user information from sessions API
        api_result = await self._get_data("sessions")
        user_info = self._extract_data(api_result)
        logged_in_user = await self.get_user(str(user_info.get("userId")))
        await self.auth.update_user_info(logged_in_user, str(user_info.get("sessionId")))

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
            ProviderFeature.ARTIST_ALBUMS,
            ProviderFeature.ARTIST_TOPTRACKS,
            ProviderFeature.SEARCH,
            ProviderFeature.LIBRARY_ARTISTS_EDIT,
            ProviderFeature.LIBRARY_ALBUMS_EDIT,
            ProviderFeature.LIBRARY_TRACKS_EDIT,
            ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
            ProviderFeature.PLAYLIST_CREATE,
            ProviderFeature.SIMILAR_TRACKS,
            ProviderFeature.BROWSE,
            ProviderFeature.PLAYLIST_TRACKS_EDIT,
            ProviderFeature.RECOMMENDATIONS,
        }

    #
    # API REQUEST HELPERS & DECORATORS
    #

    @staticmethod
    def prepare_api_request(method: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        """Prepare API requests with authentication and common parameters."""

        @functools.wraps(method)
        async def wrapper(self: TidalProvider, endpoint: str, **kwargs: Any) -> T:
            # Ensure we have a valid token through auth manager
            if not await self.auth.ensure_valid_token():
                raise LoginFailed("Failed to authenticate with Tidal")

            # Add required parameters to every request
            params = kwargs.pop("params", {}) or {}

            # Add session ID and country code if available
            if self.auth.session_id:
                params["sessionId"] = self.auth.session_id

            if self.auth.country_code:
                params["countryCode"] = self.auth.country_code

            kwargs["params"] = params

            # Prepare headers
            headers = kwargs.pop("headers", {}) or {}
            headers["Authorization"] = f"Bearer {self.auth.access_token}"

            # Add locale headers
            locale = self.mass.metadata.locale.replace("_", "-")
            language = locale.split("-")[0]
            headers["Accept-Language"] = f"{locale}, {language};q=0.9, *;q=0.5"
            kwargs["headers"] = headers

            return await method(self, endpoint, **kwargs)

        return wrapper

    #
    # CORE API METHODS
    #

    @throttle_with_retries
    @prepare_api_request
    async def _get_data(
        self, endpoint: str, **kwargs: Any
    ) -> dict[str, Any] | tuple[dict[str, Any], str]:
        """Get data from Tidal API using mass.http_session."""
        # Check if we want to return the ETag
        return_etag = kwargs.pop("return_etag", False)

        base_url = kwargs.pop("base_url", self.BASE_URL)
        url = f"{base_url}/{endpoint}"

        self.logger.debug("Making request to Tidal API: %s", endpoint)

        async with self.mass.http_session.get(url, **kwargs) as response:
            return await self._handle_response(response, return_etag)

    @prepare_api_request
    async def _post_data(
        self,
        endpoint: str,
        data: dict[str, Any] | None = None,
        as_form: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send POST data to Tidal API."""
        base_url = kwargs.pop("base_url", self.BASE_URL)
        url = f"{base_url}/{endpoint}"

        if as_form:
            # Set content type for form data
            headers = kwargs.get("headers", {})
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            kwargs["headers"] = headers
            # Use data parameter for form-encoded data
            async with self.mass.http_session.post(url, data=data, **kwargs) as response:
                return cast(
                    "dict[str, Any]",
                    await self._handle_response(response, return_etag=False),
                )
        # Use json parameter for JSON data (default)
        async with self.mass.http_session.post(url, json=data, **kwargs) as response:
            return cast(
                "dict[str, Any]",
                await self._handle_response(response, return_etag=False),
            )

    @prepare_api_request
    async def _put_data(
        self,
        endpoint: str,
        data: dict[str, Any] | None = None,
        as_form: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send PUT data to Tidal API."""
        # Use BASE_URL_V2 for PUT requests to mixes endpoints
        base_url = kwargs.pop(
            "base_url", self.BASE_URL_V2 if "mixes" in endpoint else self.BASE_URL
        )
        url = f"{base_url}/{endpoint}"

        if as_form:
            # Set content type for form data
            headers = kwargs.get("headers", {})
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            kwargs["headers"] = headers
            # Use data parameter for form-encoded data
            async with self.mass.http_session.put(url, data=data, **kwargs) as response:
                return cast(
                    "dict[str, Any]",
                    await self._handle_response(response, return_etag=False),
                )
        # Use json parameter for JSON data (default)
        async with self.mass.http_session.put(url, json=data, **kwargs) as response:
            return cast(
                "dict[str, Any]",
                await self._handle_response(response, return_etag=False),
            )

    @prepare_api_request
    async def _delete_data(
        self, endpoint: str, data: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Delete data from Tidal API using mass.http_session."""
        url = f"{self.BASE_URL}/{endpoint}"
        self.logger.debug("Making DELETE request to Tidal API: %s", endpoint)

        # For DELETE requests with a body, we need to use json parameter
        async with self.mass.http_session.delete(url, json=data, **kwargs) as response:
            return cast("dict[str, Any]", await self._handle_response(response, return_etag=False))

    async def _handle_response(
        self, response: ClientResponse, return_etag: bool = False
    ) -> dict[str, Any] | tuple[dict[str, Any], str]:
        """Handle API response and common error conditions."""
        # Handle error responses
        if response.status == 401:
            # Authentication error is handled by the calling method (which will retry)
            raise LoginFailed("Authentication failed")

        if response.status == 404:
            raise MediaNotFoundError(f"Item not found: {response.url}")

        if response.status == 429:
            retry_after = int(response.headers.get("Retry-After", 30))
            raise ResourceTemporarilyUnavailable(
                "Tidal Rate limit reached", backoff_time=retry_after
            )

        if response.status == 412:
            text = await response.text()
            self.logger.error("Precondition failed: %s", text)
            raise ResourceTemporarilyUnavailable(
                "Resource changed while updating, please try again"
            )

        if response.status >= 400:
            text = await response.text()
            self.logger.error("API error: %s - %s", response.status, text)
            raise ResourceTemporarilyUnavailable("API error")

        # Parse successful response
        try:
            # Check if there's content to parse
            if (
                response.content_length == 0
                or not response.content_type
                or response.content_type == ""
            ):
                # Empty response, return success indicator
                data = {"success": True}
            else:
                data = await response.json()

            # Return with etag if requested
            if return_etag:
                etag = response.headers.get("ETag", "")
                return data, etag
            return data
        except json.JSONDecodeError as err:
            self.logger.error("Failed to parse JSON response: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to parse response") from err
        except (TypeError, ValueError, KeyError) as err:
            self.logger.error("Invalid response format: %s", err)
            raise ResourceTemporarilyUnavailable("Invalid response format") from err

    async def _paginate_api(
        self,
        endpoint: str,
        item_key: str = "items",
        nested_key: str | None = None,
        limit: int = DEFAULT_LIMIT,
        **kwargs: Any,
    ) -> AsyncGenerator[Any, None]:
        """Paginate through all items from a Tidal API endpoint."""
        offset = 0
        while True:
            # Get a batch of items
            params = {"limit": limit, "offset": offset}
            if "params" in kwargs:
                params.update(kwargs.pop("params"))

            api_result = await self._get_data(endpoint, params=params, **kwargs)
            response = self._extract_data(api_result)

            # Extract items from response
            items = response.get(item_key, [])
            if not items:
                break

            # Process each item in the batch
            for item in items:
                if nested_key and nested_key in item and item[nested_key]:
                    yield item[nested_key]
                else:
                    yield item

            # Update offset for next batch
            offset += len(items)

            # Stop if we've received fewer items than the limit
            if len(items) < limit:
                break

    def _extract_data(
        self, api_result: dict[str, Any] | tuple[dict[str, Any], str]
    ) -> dict[str, Any]:
        """Extract data from API result that might be tuple of (data, etag)."""
        return api_result[0] if isinstance(api_result, tuple) else api_result

    def _extract_data_and_etag(
        self, api_result: dict[str, Any] | tuple[dict[str, Any], str]
    ) -> tuple[dict[str, Any], str | None]:
        """Extract both data and etag from API result."""
        if isinstance(api_result, tuple):
            return api_result
        return api_result, None

    #
    # SEARCH & DISCOVERY
    #

    async def get_user(self, prov_user_id: str) -> dict[str, Any]:
        """Get user information."""
        api_result = await self._get_data(f"users/{prov_user_id}")
        return self._extract_data(api_result)

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        parsed_results = SearchResults()

        # Filter supported media types and convert to strings for the API
        media_type_strings = []
        for media_type in media_types:
            if media_type == MediaType.ARTIST:
                media_type_strings.append("artists")
            elif media_type == MediaType.ALBUM:
                media_type_strings.append("albums")
            elif media_type == MediaType.TRACK:
                media_type_strings.append("tracks")
            elif media_type == MediaType.PLAYLIST:
                media_type_strings.append("playlists")

        if not media_type_strings:
            return parsed_results

        # Add debug logging
        self.logger.debug(
            "Searching Tidal for %s, types: %s, limit: %d",
            search_query,
            media_type_strings,
            limit,
        )

        api_result = await self._get_data(
            "search",
            params={
                "query": search_query.replace("'", ""),
                "limit": limit,
                "types": ",".join(media_type_strings),  # Use strings, not enum values
            },
        )

        # Handle potential tuple return (data, etag)
        results = self._extract_data(api_result)

        self.logger.debug("Tidal search response keys: %s", list(results.keys()))

        # Check if keys exist and are not None before processing
        if "artists" in results and results["artists"] and "items" in results["artists"]:
            parsed_results.artists = [
                self._parse_artist(artist) for artist in results["artists"]["items"]
            ]

        if "albums" in results and results["albums"] and "items" in results["albums"]:
            parsed_results.albums = [
                self._parse_album(album) for album in results["albums"]["items"]
            ]

        if "playlists" in results and results["playlists"] and "items" in results["playlists"]:
            parsed_results.playlists = [
                self._parse_playlist(playlist) for playlist in results["playlists"]["items"]
            ]

        if "tracks" in results and results["tracks"] and "items" in results["tracks"]:
            parsed_results.tracks = [
                self._parse_track(track) for track in results["tracks"]["items"]
            ]

        self.logger.debug(
            "Search results - artists: %d, albums: %d, tracks: %d, playlists: %d",
            len(parsed_results.artists),
            len(parsed_results.albums),
            len(parsed_results.tracks),
            len(parsed_results.playlists),
        )

        return parsed_results

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Get similar tracks for given track id."""
        try:
            api_result = await self._get_data(
                f"tracks/{prov_track_id}/radio", params={"limit": limit}
            )
            similar_tracks = self._extract_data(api_result)
            return [self._parse_track(track_obj) for track_obj in similar_tracks.get("items", [])]
        except ResourceTemporarilyUnavailable:
            raise
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Track {prov_track_id} not found") from err

    #
    # ITEM RETRIEVAL METHODS
    #

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details for given artist id."""
        try:
            api_result = await self._get_data(f"artists/{prov_artist_id}")
            artist_obj = self._extract_data(api_result)
            return self._parse_artist(artist_obj)
        except ResourceTemporarilyUnavailable:
            raise
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found") from err

    async def get_album(self, prov_album_id: str) -> Album:
        """Get album details for given album id."""
        try:
            api_result = await self._get_data(f"albums/{prov_album_id}")
            album_obj = self._extract_data(api_result)
            return self._parse_album(album_obj)
        except ResourceTemporarilyUnavailable:
            raise
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Album {prov_album_id} not found") from err

    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details for given track id."""
        try:
            api_result = await self._get_data(f"tracks/{prov_track_id}")
            track_obj = self._extract_data(api_result)
            track = self._parse_track(track_obj)
            # Get additional details like lyrics if needed
            with suppress(MediaNotFoundError):
                api_result = await self._get_data(f"tracks/{prov_track_id}/lyrics")
                lyrics_data = self._extract_data(api_result)

                if lyrics_data and "text" in lyrics_data:
                    track.metadata.lyrics = lyrics_data["text"]

            return track
        except ResourceTemporarilyUnavailable:
            raise
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Track {prov_track_id} not found") from err

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details for given playlist id."""
        # Check if this is a mix by ID prefix
        is_mix = prov_playlist_id.startswith("mix_")

        if is_mix:
            # Strip prefix and use mix API
            actual_id = prov_playlist_id[4:]  # Remove "mix_" prefix
            try:
                return await self._get_mix_details(actual_id)
            except ResourceTemporarilyUnavailable:
                raise
            except (ClientError, KeyError, ValueError) as err:
                raise MediaNotFoundError(f"Mix {prov_playlist_id} not found") from err

        # Try regular playlist endpoint
        try:
            api_result = await self._get_data(f"playlists/{prov_playlist_id}")
            playlist_obj = self._extract_data(api_result)
            return self._parse_playlist(playlist_obj)
        except MediaNotFoundError:
            # If not found, try as a Tidal mix (might be unidentified mix)
            self.logger.debug("Playlist %s not found, trying as Tidal Mix", prov_playlist_id)
            try:
                return await self._get_mix_details(prov_playlist_id)
            except ResourceTemporarilyUnavailable:
                raise
            except (ClientError, KeyError, ValueError) as err:
                # Re-raise the original error with the requested ID
                raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found") from err
        except ResourceTemporarilyUnavailable:
            raise
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found") from err

    async def _get_mix_details(self, prov_mix_id: str) -> Playlist:
        """Get details for a Tidal Mix."""
        try:
            params = {"mixId": prov_mix_id, "deviceType": "BROWSER"}
            api_result = await self._get_data("pages/mix", params=params)
            tidal_mix = self._extract_data(api_result)

            # Extract mix details from page data
            if "title" not in tidal_mix:
                raise MediaNotFoundError(f"Mix {prov_mix_id} not found")

            # Create basic mix object with required fields
            mix_obj = {
                "id": prov_mix_id,
                "title": tidal_mix.get("title", "Unknown Mix"),
                "updated": tidal_mix.get("lastUpdated", ""),
                "images": {},  # Initialize empty images dict
            }

            # Safely extract the mix object and its images from the header module
            rows = tidal_mix.get("rows", [])
            if rows and isinstance(rows, list) and len(rows) > 0:
                first_row = rows[0]
                if isinstance(first_row, dict):
                    modules = first_row.get("modules", [])
                    if modules and isinstance(modules, list) and len(modules) > 0:
                        header_module = modules[0]
                        if isinstance(header_module, dict):
                            mix_data = header_module.get("mix", {})
                            if isinstance(mix_data, dict):
                                # Get images if they exist
                                if "images" in mix_data and isinstance(mix_data["images"], dict):
                                    mix_obj["images"] = mix_data["images"]
                                    self.logger.debug(
                                        "Successfully extracted mix images from header module"
                                    )

                                # Get subtitle if it exists
                                subtitle = mix_data.get("subTitle")
                                if subtitle:
                                    mix_obj["subTitle"] = subtitle

            # Safely check if we have useful images
            images = mix_obj.get("images", {})
            if images and any(key in images for key in ["MEDIUM", "LARGE", "SMALL"]):
                self.logger.debug("Found images for mix %s: %s", prov_mix_id, list(images.keys()))
            else:
                self.logger.debug("No images found for mix %s", prov_mix_id)

            return self._parse_playlist(mix_obj, is_mix=True)
        except ResourceTemporarilyUnavailable:
            raise
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Mix {prov_mix_id} not found") from err

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        try:
            api_result = await self._get_data(
                f"albums/{prov_album_id}/tracks", params={"limit": 250}
            )
            album_tracks = self._extract_data(api_result)
            return [self._parse_track(track_obj) for track_obj in album_tracks.get("items", [])]
        except ResourceTemporarilyUnavailable:
            raise
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Album {prov_album_id} not found") from err

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        try:
            api_result = await self._get_data(
                f"artists/{prov_artist_id}/albums", params={"limit": 250}
            )
            artist_albums = self._extract_data(api_result)
            return [self._parse_album(album_obj) for album_obj in artist_albums.get("items", [])]
        except ResourceTemporarilyUnavailable:
            raise
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found") from err

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        try:
            api_result = await self._get_data(
                f"artists/{prov_artist_id}/toptracks", params={"limit": 10, "offset": 0}
            )
            artist_top_tracks = self._extract_data(api_result)
            return [
                self._parse_track(track_obj) for track_obj in artist_top_tracks.get("items", [])
            ]
        except ResourceTemporarilyUnavailable:
            raise
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found") from err

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks for either regular playlists or Tidal mixes."""
        page_size = 200
        offset = page * page_size

        # Check if this is a mix by ID prefix
        is_mix = prov_playlist_id.startswith("mix_")

        if is_mix:
            # Strip prefix and use mix API
            actual_id = prov_playlist_id[4:]  # Remove "mix_" prefix
            try:
                return await self._get_mix_playlist_tracks(actual_id, page_size, offset)
            except ResourceTemporarilyUnavailable:
                raise
            except (ClientError, KeyError, ValueError) as err:
                raise MediaNotFoundError(f"Mix playlist {prov_playlist_id} not found") from err

        # Otherwise try regular endpoint first, fall back only if needed
        try:
            return await self._get_regular_playlist_tracks(prov_playlist_id, page_size, offset)
        except MediaNotFoundError:
            self.logger.debug("Playlist not found, trying as Tidal Mix")
            try:
                return await self._get_mix_playlist_tracks(prov_playlist_id, page_size, offset)
            except ResourceTemporarilyUnavailable:
                raise
            except (ClientError, KeyError, ValueError) as err:
                # Re-raise the original error with the requested ID
                raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found") from err

    async def _get_regular_playlist_tracks(
        self, prov_playlist_id: str, page_size: int, offset: int
    ) -> list[Track]:
        """Get tracks from a regular Tidal playlist."""
        api_result = await self._get_data(
            f"playlists/{prov_playlist_id}/tracks",
            params={"limit": page_size, "offset": offset},
        )
        tidal_tracks = self._extract_data(api_result)

        return self._process_track_results(tidal_tracks.get("items", []), offset)

    async def _get_mix_playlist_tracks(
        self, prov_playlist_id: str, page_size: int, offset: int
    ) -> list[Track]:
        """Get tracks from a Tidal Mix playlist."""
        try:
            params = {"mixId": prov_playlist_id, "deviceType": "BROWSER"}
            api_result = await self._get_data("pages/mix", params=params)
            tidal_mix = self._extract_data(api_result)

            # Verify we have the expected structure
            if "rows" not in tidal_mix or len(tidal_mix["rows"]) < 2:
                raise MediaNotFoundError(f"Invalid mix structure for {prov_playlist_id}")

            module = tidal_mix["rows"][1]["modules"][0] if len(tidal_mix["rows"]) > 1 else None
            if not module or "pagedList" not in module:
                raise MediaNotFoundError(f"Invalid mix module for {prov_playlist_id}")

            all_tracks = module["pagedList"].get("items", [])

            # Manually paginate the results
            start_idx = min(offset, len(all_tracks))
            end_idx = min(offset + page_size, len(all_tracks))
            paginated_tracks = all_tracks[start_idx:end_idx]

            self.logger.debug(
                "Mix tracks - total: %d, page: %d, returning: %d tracks",
                len(all_tracks),
                offset // page_size,
                len(paginated_tracks),
            )

            return self._process_track_results(paginated_tracks, offset)
        except ResourceTemporarilyUnavailable:
            raise
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found") from err

    async def recommendations(self) -> list[RecommendationFolder]:
        """Get this provider's recommendations organized into folders."""
        # Check cache first
        cache_key = f"tidal_recommendations_{self.lookup_key}"
        cached_recommendations: list[RecommendationFolder] = await self.mass.cache.get(
            cache_key, category=CACHE_CATEGORY_RECOMMENDATIONS, base_key=self.lookup_key
        )

        if cached_recommendations:
            self.logger.debug("Returning cached recommendations (TTL: 1 hour)")
            return cached_recommendations

        results: list[RecommendationFolder] = []

        # Pages to fetch
        pages = ["pages/home", "pages/for_you"]

        # Dictionary to track items by module title to combine duplicates
        combined_modules: dict[str, list[Playlist | Album | Track | Artist]] = {}
        module_content_types: dict[str, MediaType] = {}
        module_page_names: dict[str, str] = {}

        try:
            # Process pages and collect modules
            await self._process_recommendation_pages(
                pages, combined_modules, module_content_types, module_page_names
            )

            # Create recommendation folders from combined modules
            results = self._create_recommendation_folders(
                combined_modules, module_content_types, module_page_names
            )

            self.logger.debug("Created %d recommendation folders from Tidal", len(results))

            # Cache the results for 1 hour (3600 seconds)
            await self.mass.cache.set(
                cache_key,
                results,
                category=CACHE_CATEGORY_RECOMMENDATIONS,
                base_key=self.lookup_key,
                expiration=3600,
            )

        except (ClientError, ResourceTemporarilyUnavailable) as err:
            # Network-related errors
            self.logger.warning("Network error fetching Tidal recommendations: %s", err)
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as err:
            # Data parsing errors
            self.logger.warning("Error parsing Tidal recommendations data: %s", err)
        except (
            ClientConnectionError,
            ClientConnectorError,
            ClientResponseError,
            ClientPayloadError,
        ) as err:
            # More specific network errors
            self.logger.warning("Network error in Tidal recommendations: %s", err)

        return results

    async def _process_recommendation_pages(
        self,
        pages: list[str],
        combined_modules: dict[str, list[Playlist | Album | Track | Artist]],
        module_content_types: dict[str, MediaType],
        module_page_names: dict[str, str],
    ) -> None:
        """Process recommendation pages and collect modules."""
        for page_path in pages:
            # Get page content
            page_parser = await self.get_page_content(page_path)
            page_name = page_path.split("/")[-1].replace("_", " ").title()

            # Process all modules in a single pass
            await self._process_page_modules(
                page_parser, page_name, combined_modules, module_content_types, module_page_names
            )

    async def _process_page_modules(
        self,
        page_parser: TidalPageParser,
        page_name: str,
        combined_modules: dict[str, list[Playlist | Album | Track | Artist]],
        module_content_types: dict[str, MediaType],
        module_page_names: dict[str, str],
    ) -> None:
        """Process all modules from a single page."""
        for module_info in page_parser._module_map:
            try:
                module_title = module_info.get("title", "Unknown")

                # Skip modules without proper titles
                if not module_title or module_title == "Unknown":
                    continue

                # Get module items
                module_items, content_type = page_parser.get_module_items(module_info)

                # Skip empty modules
                if not module_items:
                    continue

                # For all modules, collect items based on title
                if module_title not in combined_modules:
                    combined_modules[module_title] = []
                    module_content_types[module_title] = content_type
                    module_page_names[module_title] = page_name
                else:
                    # If we already have this module title, update the content type
                    # if this module has more items than we already collected
                    current_items_count = len(combined_modules[module_title])
                    if len(module_items) > current_items_count:
                        module_content_types[module_title] = content_type

                # Add items to the combined collection
                combined_modules[module_title].extend(module_items)

            except (KeyError, ValueError, TypeError, AttributeError) as err:
                self.logger.warning(
                    "Error processing module %s from %s: %s",
                    module_info.get("title", "Unknown"),
                    page_name,
                    err,
                )

    def _create_recommendation_folders(
        self,
        combined_modules: dict[str, list[Playlist | Album | Track | Artist]],
        module_content_types: dict[str, MediaType],
        module_page_names: dict[str, str],
    ) -> list[RecommendationFolder]:
        """Create recommendation folders from combined modules."""
        results: list[RecommendationFolder] = []

        # Helper function to determine icon based on content type
        def get_icon_for_type(media_type: MediaType) -> str:
            if media_type == MediaType.PLAYLIST:
                return "mdi-playlist-music"
            elif media_type == MediaType.ALBUM:
                return "mdi-album"
            elif media_type == MediaType.TRACK:
                return "mdi-file-music"
            elif media_type == MediaType.ARTIST:
                return "mdi-account-music"
            return "mdi-motion-play"  # Default for mixed content

        for module_title, items in combined_modules.items():
            # Use unique items list to prevent duplicates
            unique_items = UniqueList(items)

            # Create a sanitized unique ID
            item_id = "".join(
                c
                for c in module_title.lower().replace(" ", "_").replace("-", "_")
                if c.isalnum() or c == "_"
            )

            # Get content type and page source
            content_type = module_content_types.get(module_title, MediaType.PLAYLIST)
            page_name = module_page_names.get(module_title, "Tidal")

            # Create folder with combined items
            folder = RecommendationFolder(
                item_id=item_id,
                name=module_title,
                provider=self.lookup_key,
                items=UniqueList[MediaItemTypeOrItemMapping](unique_items),
                subtitle=f"From {page_name} • {len(unique_items)} items",
                translation_key=item_id,
                icon=get_icon_for_type(content_type),
            )
            results.append(folder)

            # Log a message if we combined multiple sources
            if len(unique_items) < len(items):
                self.logger.debug(
                    "Combined %d items into %d unique items for '%s'",
                    len(items),
                    len(unique_items),
                    module_title,
                )

        return results

    def _process_track_results(
        self, track_objects: list[dict[str, Any]], offset: int
    ) -> list[Track]:
        """Process track objects into Track objects with positions."""
        result: list[Track] = []
        for index, track_obj in enumerate(track_objects, 1):
            try:
                track = self._parse_track(track_obj)
                track.position = offset + index
                result.append(track)
            except (KeyError, TypeError) as err:
                self.logger.warning("Error parsing track: %s", err)
                continue
        return result

    async def get_stream_details(
        self, item_id: str, media_type: MediaType = MediaType.TRACK
    ) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        # Try direct track lookup first with exception handling
        try:
            track = await self.get_track(item_id)
        except MediaNotFoundError:
            self.logger.info(
                "Track %s not found, attempting fallback by ISRC lookup",
                item_id,
            )
            track_result = await self._get_track_by_isrc(item_id)
            if not track_result:
                raise MediaNotFoundError(f"Track {item_id} not found")
            track = track_result

        quality = self.config.get_value(CONF_QUALITY)

        # Request stream manifest
        async with self.throttler.bypass():
            api_result = await self._get_data(
                f"tracks/{item_id}/playbackinfopostpaywall",
                params={
                    "playbackmode": "STREAM",
                    "audioquality": quality,
                    "assetpresentation": "FULL",
                },
            )
        stream_data = self._extract_data(api_result)

        # Extract streaming information
        manifest_type = stream_data.get("manifestMimeType", "")
        is_mpd = "dash+xml" in manifest_type

        if is_mpd and "manifest" in stream_data:
            url = f"data:application/dash+xml;base64,{stream_data['manifest']}"
        else:
            # For non-MPD streams, use the direct URL
            urls = stream_data.get("urls", [])
            if not urls:
                raise MediaNotFoundError(f"No stream URL for track {item_id}")
            url = urls[0]

        # Determine audio format info
        bit_depth = stream_data.get("bitDepth", 16)
        sample_rate = stream_data.get("sampleRate", 44100)
        audio_quality: str | None = stream_data.get("audioQuality")
        if audio_quality in ("HIRES_LOSSLESS", "HI_RES_LOSSLESS", "LOSSLESS"):
            content_type = ContentType.FLAC
        elif codec := stream_data.get("codec"):
            content_type = ContentType.try_parse(codec)
        else:
            content_type = ContentType.MP4

        return StreamDetails(
            item_id=track.item_id,
            provider=self.lookup_key,
            audio_format=AudioFormat(
                content_type=content_type,
                sample_rate=sample_rate,
                bit_depth=bit_depth,
                channels=2,
            ),
            stream_type=StreamType.HTTP,
            duration=track.duration,
            path=url,
            can_seek=True,
            allow_seek=True,
        )

    async def _get_track_by_isrc(self, item_id: str) -> Track | None:
        """Get track by ISRC from library item, with caching."""
        # Try to get from cache first
        cache_key = f"isrc_map_{item_id}"
        cached_track_id = await self.mass.cache.get(
            cache_key, category=CACHE_CATEGORY_DEFAULT, base_key=self.lookup_key
        )

        if cached_track_id:
            self.logger.debug("Using cached track id")
            try:
                api_result = await self._get_data(f"tracks/{cached_track_id}")
                track_data = self._extract_data(api_result)
                return self._parse_track(track_data)
            except MediaNotFoundError:
                # Track no longer exists, invalidate cache
                await self.mass.cache.delete(
                    cache_key, category=CACHE_CATEGORY_DEFAULT, base_key=self.lookup_key
                )

        # Lookup by ISRC if no cache or cached track not found
        library_track = await self.mass.music.tracks.get_library_item_by_prov_id(
            item_id, self.instance_id
        )
        if not library_track:
            return None

        isrc = next(
            (
                id_value
                for id_type, id_value in library_track.external_ids
                if id_type == ExternalID.ISRC
            ),
            None,
        )
        if not isrc:
            return None

        self.logger.debug("Attempting track lookup by ISRC: %s", isrc)

        # Get tracks by ISRC using direct API
        api_result = await self._get_data(
            "/tracks",
            params={
                "filter[isrc]": isrc,
            },
            base_url=self.OPEN_API_URL,
        )
        tracks_data = self._extract_data(api_result)

        if not tracks_data and not tracks_data.get("data"):
            return None

        track_data = tracks_data["data"][0]
        track_id = str(track_data["id"])

        # Cache the mapping for future use
        await self.mass.cache.set(
            cache_key,
            track_id,
            category=CACHE_CATEGORY_DEFAULT,
            base_key=self.lookup_key,
        )

        return await self.get_track(track_id)

    def get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        """Create a generic item mapping."""
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.lookup_key,
            name=name,
        )

    #
    # LIBRARY MANAGEMENT
    #

    async def get_page_content(self, page_path: str = "pages/home") -> TidalPageParser:
        """Get a lazy page parser for a Tidal page."""
        # Try to get from cache first
        cached_parser = await TidalPageParser.from_cache(self, page_path)
        if cached_parser:
            self.logger.debug(
                "Using cached page content for '%s' (age: %.1f minutes)",
                page_path,
                cached_parser.content_stats.get("cache_age_minutes", 0),
            )
            return cached_parser

        # Not in cache or expired, fetch fresh content
        try:
            # Get the page structure
            self.logger.debug("Fetching fresh page content for '%s'", page_path)
            locale = self.mass.metadata.locale.replace("_", "-")
            api_result = await self._get_data(
                page_path,
                base_url="https://listen.tidal.com/v1",
                params={
                    "locale": locale,
                    "deviceType": "BROWSER",
                    "countryCode": self.auth.country_code or "US",
                },
            )

            # Extract and build lazy parser
            page_data = self._extract_data(api_result) or {}
            parser = TidalPageParser(self)
            parser.parse_page_structure(page_data, page_path)

            self.logger.debug("Page '%s' indexed with: %s", page_path, parser.content_stats)

            # Cache the parser data
            cache_key = f"tidal_page_{page_path}"
            cache_data = {
                "module_map": parser._module_map,
                "content_map": parser._content_map,
                "parsed_at": parser._parsed_at,
            }
            await self.mass.cache.set(
                cache_key,
                cache_data,
                category=CACHE_CATEGORY_RECOMMENDATIONS,
                base_key=self.lookup_key,
                expiration=self.page_cache_ttl,
            )

            return parser
        except ResourceTemporarilyUnavailable:
            # Network-related errors - propagate
            raise
        except (ClientError, ClientConnectorError, ClientPayloadError) as err:
            # Network-related errors
            self.logger.error("Network error fetching Tidal page: %s", err)
            return TidalPageParser(self)  # Return empty parser
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as err:
            # Data parsing errors
            self.logger.error("Error parsing Tidal page data: %s", err)
            return TidalPageParser(self)  # Return empty parser

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Tidal."""
        user_id = self.auth.user_id
        path = f"users/{user_id}/favorites/artists"

        async for artist_item in self._paginate_api(path, nested_key="item"):
            if artist_item and artist_item.get("id"):
                yield self._parse_artist(artist_item)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Tidal."""
        user_id = self.auth.user_id
        path = f"users/{user_id}/favorites/albums"

        async for album_item in self._paginate_api(path, nested_key="item"):
            if album_item and album_item.get("id"):
                yield self._parse_album(album_item)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Tidal."""
        user_id = self.auth.user_id
        path = f"users/{user_id}/favorites/tracks"

        async for track_item in self._paginate_api(path, nested_key="item"):
            if track_item and track_item.get("id"):
                yield self._parse_track(track_item)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        user_id = self.auth.user_id
        mix_path = "favorites/mixes"

        async for mix_item in self._paginate_api(
            mix_path, item_key="items", base_url=self.BASE_URL_V2
        ):
            if mix_item and mix_item.get("id"):
                yield self._parse_playlist(mix_item, is_mix=True)

        playlist_path = f"users/{user_id}/playlistsAndFavoritePlaylists"

        async for playlist_item in self._paginate_api(playlist_path, nested_key="playlist"):
            if playlist_item and playlist_item.get("uuid"):
                yield self._parse_playlist(playlist_item)

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to library."""
        endpoint, data, is_mix = self._get_library_endpoint_data(
            item.item_id, item.media_type, "add"
        )

        if not endpoint:
            return False

        try:
            if is_mix:
                await self._put_data(endpoint, data=data, as_form=True)
            else:
                endpoint = f"users/{self.auth.user_id}/{endpoint}"
                await self._post_data(endpoint, data=data, as_form=True)
            return True
        except (ClientError, MediaNotFoundError, ResourceTemporarilyUnavailable) as err:
            self.logger.warning(
                "Failed to add %s:%s to library: %s", item.media_type, item.item_id, err
            )
            return False

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from library."""
        endpoint, data, is_mix = self._get_library_endpoint_data(prov_item_id, media_type, "remove")

        if not endpoint:
            return False

        try:
            if is_mix:
                await self._put_data(endpoint, data=data, as_form=True)
            else:
                endpoint = f"users/{self.auth.user_id}/{endpoint}"
                await self._delete_data(endpoint)
            return True
        except (ClientError, MediaNotFoundError, ResourceTemporarilyUnavailable) as err:
            self.logger.warning(
                "Failed to remove %s:%s from library: %s", media_type, prov_item_id, err
            )
            return False

    def _get_library_endpoint_data(
        self, item_id: str, media_type: MediaType, operation: str
    ) -> tuple[str | None, dict[str, Any], bool]:
        """Get the endpoint, data, and mix flag for library operations."""
        is_mix = False
        data = {}

        # Check if this is a mix by ID prefix
        if media_type == MediaType.PLAYLIST and item_id.startswith("mix_"):
            is_mix = True
            # Strip prefix for API calls
            mix_id = item_id[4:]  # Remove "mix_" prefix

            if operation == "add":
                endpoint = "favorites/mixes/add"
                data = {"mixIds": mix_id, "onArtifactNotFound": "FAIL", "deviceType": "BROWSER"}
            else:  # remove
                endpoint = "favorites/mixes/remove"
                data = {"mixIds": mix_id, "deviceType": "BROWSER"}
            return endpoint, data, is_mix

        # Regular items
        if media_type == MediaType.ARTIST:
            if operation == "add":
                endpoint = "favorites/artists"
                data = {"artistId": item_id}
            else:
                endpoint = f"favorites/artists/{item_id}"
        elif media_type == MediaType.ALBUM:
            if operation == "add":
                endpoint = "favorites/albums"
                data = {"albumId": item_id}
            else:
                endpoint = f"favorites/albums/{item_id}"
        elif media_type == MediaType.TRACK:
            if operation == "add":
                endpoint = "favorites/tracks"
                data = {"trackId": item_id}
            else:
                endpoint = f"favorites/tracks/{item_id}"
        elif media_type == MediaType.PLAYLIST:
            if operation == "add":
                endpoint = "favorites/playlists"
                data = {"uuids": item_id}
            else:
                endpoint = f"favorites/playlists/{item_id}"
        else:
            return None, {}, False

        return endpoint, data, is_mix

    #
    # PLAYLIST MANAGEMENT
    #

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        # Create playlist using form-encoded data
        data = {"title": name, "description": ""}

        try:
            playlist_obj = await self._post_data(
                f"users/{self.auth.user_id}/playlists", data=data, as_form=True
            )

            return self._parse_playlist(playlist_obj)
        except (ClientResponseError, MediaNotFoundError, LoginFailed) as err:
            self.logger.error("API error creating playlist: %s", err)
            raise
        except (ClientConnectorError, ClientPayloadError) as err:
            # Network or payload errors
            self.logger.error("Network error creating playlist: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to create playlist") from err
        except (KeyError, ValueError, TypeError) as err:
            # Data parsing errors
            self.logger.error("Data error creating playlist: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to create playlist") from err

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        try:
            # Get playlist details first with ETag
            api_result = await self._get_data(f"playlists/{prov_playlist_id}", return_etag=True)
            playlist_obj, etag = self._extract_data_and_etag(api_result)

            # Send using form-encoded data like the synchronous library
            data = {
                "onArtifactNotFound": "SKIP",
                "trackIds": ",".join(map(str, prov_track_ids)),
                "toIndex": playlist_obj["numberOfTracks"],
                "onDupes": "SKIP",
            }

            # Force using form data instead of JSON and include ETag
            headers = {"If-None-Match": etag} if etag else {}
            await self._post_data(
                f"playlists/{prov_playlist_id}/items",
                data=data,
                as_form=True,
                headers=headers,
            )

        except (MediaNotFoundError, ClientResponseError) as err:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found") from err
        except (ClientConnectorError, ClientPayloadError) as err:
            # Network errors
            self.logger.error("Network error adding tracks to playlist: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to add tracks to playlist") from err
        except (KeyError, ValueError) as err:
            # Data errors
            self.logger.error("Data error adding tracks to playlist: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to add tracks to playlist") from err

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        # Get playlist with ETag first
        api_result = await self._get_data(f"playlists/{prov_playlist_id}", return_etag=True)
        _, etag = self._extract_data_and_etag(api_result)

        # Format positions as string in URL path
        # Tidal can use directly indices in path, not track IDs in the body
        position_string = ",".join([str(pos - 1) for pos in positions_to_remove])

        # Use DELETE with If-None-Match header
        # Tidal uses this incorrectly, but it's required
        headers = {"If-None-Match": etag} if etag else {}

        # Make a direct DELETE request to the endpoint with positions in the URL path
        await self._delete_data(
            f"playlists/{prov_playlist_id}/items/{position_string}", headers=headers
        )

    #
    # ITEM PARSERS
    #

    def _parse_artist(self, artist_obj: dict[str, Any]) -> Artist:
        """Parse tidal artist object to generic layout."""
        artist_id = str(artist_obj["id"])
        artist = Artist(
            item_id=artist_id,
            provider=self.lookup_key,
            name=artist_obj["name"],
            provider_mappings={
                ProviderMapping(
                    item_id=artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    # NOTE: don't use the /browse endpoint as it's
                    # not working for musicbrainz lookups
                    url=f"https://tidal.com/artist/{artist_id}",
                )
            },
        )
        # metadata
        if artist_obj["picture"]:
            picture_id = artist_obj["picture"].replace("-", "/")
            image_url = f"{RESOURCES_URL}/{picture_id}/750x750.jpg"
            artist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.lookup_key,
                        remotely_accessible=True,
                    )
                ]
            )

        return artist

    def _parse_album(self, album_obj: dict[str, Any]) -> Album:
        """Parse tidal album object to generic layout."""
        name = album_obj.get("title", "Unknown Album")
        version = album_obj.get("version", "") or ""
        album_id = str(album_obj.get("id", ""))

        album = Album(
            item_id=album_id,
            provider=self.lookup_key,
            name=name,
            version=version,
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.FLAC,
                    ),
                    url=f"https://tidal.com/album/{album_id}",
                    available=album_obj.get("streamReady", True),  # Default to available
                )
            },
        )

        # Safely handle artists array
        various_artist_album: bool = False
        for artist_obj in album_obj.get("artists", []):
            try:
                if artist_obj.get("name") == "Various Artists":
                    various_artist_album = True
                album.artists.append(self._parse_artist(artist_obj))
            except (KeyError, TypeError) as err:
                self.logger.warning("Error parsing artist in album %s: %s", name, err)

        # Safely determine album type
        album_type = album_obj.get("type", "ALBUM")
        if album_type == "COMPILATION" or various_artist_album:
            album.album_type = AlbumType.COMPILATION
        elif album_type == "ALBUM":
            album.album_type = AlbumType.ALBUM
        elif album_type == "EP":
            album.album_type = AlbumType.EP
        elif album_type == "SINGLE":
            album.album_type = AlbumType.SINGLE

        # Safely parse year
        if release_date := album_obj.get("releaseDate", ""):
            try:
                album.year = int(release_date.split("-")[0])
            except (ValueError, IndexError):
                self.logger.debug("Invalid release date format: %s", release_date)
            with suppress(ValueError):
                album.metadata.release_date = datetime.fromisoformat(release_date)

        # Safely set metadata
        upc = album_obj.get("upc")
        if upc:
            album.external_ids.add((ExternalID.BARCODE, upc))

        album.metadata.copyright = album_obj.get("copyright", "")
        album.metadata.explicit = album_obj.get("explicit", False)
        album.metadata.popularity = album_obj.get("popularity", 0)

        # Safely handle cover image
        cover = album_obj.get("cover")
        if cover:
            picture_id = cover.replace("-", "/")
            image_url = f"{RESOURCES_URL}/{picture_id}/750x750.jpg"
            album.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.lookup_key,
                        remotely_accessible=True,
                    )
                ]
            )

        return album

    def _parse_track(
        self,
        track_obj: dict[str, Any],
    ) -> Track:
        """Parse tidal track object to generic layout."""
        version = track_obj.get("version", "") or ""
        track_id = str(track_obj.get("id", 0))
        media_metadata = track_obj.get("mediaMetadata", {})
        tags = media_metadata.get("tags", [])
        hi_res_lossless = any(tag in tags for tag in ["HIRES_LOSSLESS", "HI_RES_LOSSLESS"])
        track = Track(
            item_id=track_id,
            provider=self.lookup_key,
            name=track_obj.get("title", "Unknown"),
            version=version,
            duration=track_obj.get("duration", 0),
            provider_mappings={
                ProviderMapping(
                    item_id=str(track_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.FLAC,
                        bit_depth=24 if hi_res_lossless else 16,
                    ),
                    url=f"https://tidal.com/track/{track_id}",
                    available=track_obj["streamReady"],
                )
            },
            disc_number=track_obj.get("volumeNumber", 0) or 0,
            track_number=track_obj.get("trackNumber", 0) or 0,
        )
        if "isrc" in track_obj:
            track.external_ids.add((ExternalID.ISRC, track_obj["isrc"]))
        track.artists = UniqueList()
        for track_artist in track_obj["artists"]:
            artist = self._parse_artist(track_artist)
            track.artists.append(artist)
        # metadata
        track.metadata.explicit = track_obj["explicit"]
        track.metadata.popularity = track_obj["popularity"]
        if "copyright" in track_obj:
            track.metadata.copyright = track_obj["copyright"]
        if track_obj["album"]:
            # Here we use an ItemMapping as Tidal returns
            # minimal data when getting an Album from a Track
            track.album = self.get_item_mapping(
                media_type=MediaType.ALBUM,
                key=str(track_obj["album"]["id"]),
                name=track_obj["album"]["title"],
            )
            if track_obj["album"]["cover"]:
                picture_id = track_obj["album"]["cover"].replace("-", "/")
                image_url = f"{RESOURCES_URL}/{picture_id}/750x750.jpg"
                track.metadata.images = UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=image_url,
                            provider=self.lookup_key,
                            remotely_accessible=True,
                        )
                    ]
                )
        return track

    def _parse_playlist(self, playlist_obj: dict[str, Any], is_mix: bool = False) -> Playlist:
        """Parse tidal playlist object to generic layout."""
        # Get ID based on playlist type
        raw_id = str(playlist_obj.get("id" if is_mix else "uuid", ""))

        # Add prefix for mixes to distinguish them
        playlist_id = f"mix_{raw_id}" if is_mix else raw_id

        # Owner logic differs between types
        if is_mix:
            owner_name = "Created by Tidal"
            is_editable = False
        else:
            creator_id = None
            creator = playlist_obj.get("creator", {})
            if creator:
                creator_id = creator.get("id")
            is_editable = bool(creator_id and str(creator_id) == str(self.auth.user_id))

            owner_name = "Tidal"
            if is_editable:
                if self.auth.user.profile_name:
                    owner_name = self.auth.user.profile_name
                elif self.auth.user.user_name:
                    owner_name = self.auth.user.user_name
                elif self.auth.user_id:
                    owner_name = str(self.auth.user_id)

        # URL path differs by type - use raw_id for URLs
        url_path = "mix" if is_mix else "playlist"

        playlist = Playlist(
            item_id=playlist_id,
            provider=self.instance_id if is_editable else self.lookup_key,
            name=playlist_obj.get("title", "Unknown"),
            owner=owner_name,
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,  # Use raw ID for provider mapping
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{BROWSE_URL}/{url_path}/{raw_id}",
                )
            },
            is_editable=is_editable,
        )

        # Metadata - different fields based on type
        if is_mix:
            playlist.cache_checksum = str(playlist_obj.get("updated", ""))
        else:
            playlist.cache_checksum = str(playlist_obj.get("lastUpdated", ""))
            if "popularity" in playlist_obj:
                playlist.metadata.popularity = playlist_obj.get("popularity", 0)

        # Add the description from the subtitle for mixes
        if is_mix:
            subtitle = playlist_obj.get("subTitle")
            if subtitle:
                playlist.metadata.description = subtitle

        # Handle images differently based on type
        if is_mix:
            if pictures := playlist_obj.get("images", {}).get("LARGE"):
                image_url = pictures.get("url", "")
                if image_url:
                    playlist.metadata.images = UniqueList(
                        [
                            MediaItemImage(
                                type=ImageType.THUMB,
                                path=image_url,
                                provider=self.lookup_key,
                                remotely_accessible=True,
                            )
                        ]
                    )
        elif picture := (playlist_obj.get("squareImage") or playlist_obj.get("image")):
            picture_id = picture.replace("-", "/")
            image_url = f"{RESOURCES_URL}/{picture_id}/750x750.jpg"
            playlist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.lookup_key,
                        remotely_accessible=True,
                    )
                ]
            )

        return playlist
