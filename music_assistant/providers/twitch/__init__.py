"""Twitch Audio music provider for Music Assistant."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import LoginFailed
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    Radio,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.auth import AuthenticationHelper
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_RADIOS,
}

# Streamlink constants
STREAM_CHUNK_SIZE = 64 * 1024  # 64KB
MAX_CONSECUTIVE_RECONNECTS = 5
RECONNECT_DELAY = 0.5  # seconds
PREFERRED_QUALITIES = ("audio_only", "worst")

# Cache TTL
LIVE_STATUS_TTL = 300.0  # 5 minutes

# OAuth / Config constants
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_STREAMLINK_TOKEN = "streamlink_token"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_AD_HANDLING = "ad_handling"
CONF_AUTO_RAID = "auto_raid"
CONF_ACTION_AUTH = "auth"
CONF_ACTION_REVOKE = "revoke"

TWITCH_AUTH_URL = "https://id.twitch.tv/oauth2/authorize"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_REVOKE_URL = "https://id.twitch.tv/oauth2/revoke"
TWITCH_SCOPES = ("user:read:follows",)


async def _handle_auth_action(
    mass: MusicAssistant,
    values: dict[str, ConfigValueType],
) -> None:
    """Handle OAuth authentication action."""
    client_id = str(values.get(CONF_CLIENT_ID, "")).strip()
    client_secret = str(values.get(CONF_CLIENT_SECRET, "")).strip()
    if not client_id or not client_secret:
        msg = "Client ID and Client Secret are required to authenticate."
        raise LoginFailed(msg)

    session_id = str(values.get("session_id", ""))

    async with AuthenticationHelper(mass, session_id) as auth_helper:
        params = {
            "client_id": client_id,
            "redirect_uri": auth_helper.callback_url,
            "response_type": "code",
            "scope": " ".join(TWITCH_SCOPES),
        }
        auth_url = f"{TWITCH_AUTH_URL}?{urlencode(params)}"
        result = await auth_helper.authenticate(auth_url)
        code = result.get("code", "")

    if not code:
        msg = "No authorization code received from Twitch."
        raise LoginFailed(msg)

    # Exchange code for tokens
    token_params = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": auth_helper.callback_url,
    }
    async with mass.http_session.post(TWITCH_TOKEN_URL, data=token_params) as response:
        if response.status != 200:
            error_text = await response.text()
            msg = f"Failed to exchange authorization code: {error_text}"
            raise LoginFailed(msg)
        token_data = await response.json()

    values[CONF_ACCESS_TOKEN] = token_data["access_token"]
    values[CONF_REFRESH_TOKEN] = token_data.get("refresh_token", "")


async def _handle_revoke_action(
    mass: MusicAssistant,
    values: dict[str, ConfigValueType],
) -> None:
    """Handle credential revocation action."""
    access_token = str(values.get(CONF_ACCESS_TOKEN, ""))
    client_id = str(values.get(CONF_CLIENT_ID, ""))

    # Best-effort revoke — clear local state even if revoke fails
    if access_token:
        try:
            async with mass.http_session.post(
                TWITCH_REVOKE_URL,
                data={"client_id": client_id, "token": access_token},
            ):
                pass
        except Exception:  # noqa: S110
            pass

    values[CONF_ACCESS_TOKEN] = ""
    values[CONF_REFRESH_TOKEN] = ""


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return TwitchProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    if values is None:
        values = {}

    # Handle actions
    if action == CONF_ACTION_AUTH:
        await _handle_auth_action(mass, values)
    elif action == CONF_ACTION_REVOKE:
        await _handle_revoke_action(mass, values)

    # Determine auth state
    is_authenticated = bool(values.get(CONF_ACCESS_TOKEN))

    return (
        # Credentials
        ConfigEntry(
            key=CONF_CLIENT_ID,
            type=ConfigEntryType.SECURE_STRING,
            label="Twitch Client ID",
            required=True,
            value=values.get(CONF_CLIENT_ID),
        ),
        ConfigEntry(
            key=CONF_CLIENT_SECRET,
            type=ConfigEntryType.SECURE_STRING,
            label="Twitch Client Secret",
            required=True,
            value=values.get(CONF_CLIENT_SECRET),
        ),
        # Auth status
        ConfigEntry(
            key="auth_status",
            type=ConfigEntryType.LABEL,
            label="Authenticated" if is_authenticated else "Not authenticated",
        ),
        # Auth action (hidden when authenticated)
        ConfigEntry(
            key=CONF_ACTION_AUTH,
            type=ConfigEntryType.ACTION,
            label="Authenticate with Twitch",
            action=CONF_ACTION_AUTH,
            action_label="Authenticate",
            hidden=is_authenticated,
        ),
        # Revoke action (hidden when not authenticated)
        ConfigEntry(
            key=CONF_ACTION_REVOKE,
            type=ConfigEntryType.ACTION,
            label="Revoke credentials",
            action=CONF_ACTION_REVOKE,
            action_label="Revoke",
            hidden=not is_authenticated,
        ),
        # Token storage (hidden)
        ConfigEntry(
            key=CONF_ACCESS_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Access Token",
            hidden=True,
            required=False,
            value=values.get(CONF_ACCESS_TOKEN, ""),
        ),
        ConfigEntry(
            key=CONF_REFRESH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Refresh Token",
            hidden=True,
            required=False,
            value=values.get(CONF_REFRESH_TOKEN, ""),
        ),
        # Optional streamlink token
        ConfigEntry(
            key=CONF_STREAMLINK_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Streamlink Auth Token",
            description="Optional: Twitch Turbo or subscriber token to reduce ads.",
            required=False,
            value=values.get(CONF_STREAMLINK_TOKEN),
        ),
        # Ad handling mode
        ConfigEntry(
            key=CONF_AD_HANDLING,
            type=ConfigEntryType.STRING,
            label="Ad Handling",
            options=[
                ConfigValueOption("Silence (replace ads with silence)", "silence"),
                ConfigValueOption("Passthrough (play ad audio)", "passthrough"),
            ],
            default_value="silence",
            value=values.get(CONF_AD_HANDLING),
        ),
        # Auto-raid toggle
        ConfigEntry(
            key=CONF_AUTO_RAID,
            type=ConfigEntryType.BOOLEAN,
            label="Auto-follow raids",
            description="Automatically switch to raid target when a streamer raids.",
            default_value=True,
            value=values.get(CONF_AUTO_RAID),
        ),
    )


class TwitchProvider(MusicProvider):
    """Provider implementation for Twitch audio streaming."""

    _access_token: str | None = None
    _refresh_token: str | None = None
    _client_id: str | None = None
    _client_secret: str | None = None
    _user_id: str | None = None

    # Live status cache
    _cached_channels: list[dict[str, Any]] | None = None
    _cached_live: dict[str, dict[str, Any]] | None = None
    _cache_time: float = 0.0

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._client_id = str(self.config.get_value(CONF_CLIENT_ID) or "")
        self._client_secret = str(self.config.get_value(CONF_CLIENT_SECRET) or "")
        self._access_token = str(self.config.get_value(CONF_ACCESS_TOKEN) or "") or None
        self._refresh_token = str(self.config.get_value(CONF_REFRESH_TOKEN) or "") or None
        # Resolve user ID if authenticated
        if self._access_token:
            try:
                data = await self._api_get("/helix/users", params={})
                if data.get("data"):
                    self._user_id = data["data"][0]["id"]
            except Exception:
                self.logger.warning("Failed to resolve user ID during init")

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # Step 6 will subscribe to QUEUE_UPDATED events here

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        # Step 6 will clean up event subscriptions, timers, WebSocket here

    @property
    def is_authenticated(self) -> bool:
        """Return whether the provider has valid credentials."""
        return bool(self._access_token)

    def _api_headers(self) -> dict[str, str]:
        """Return headers for Twitch API calls."""
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Client-Id": self._client_id or "",
        }

    async def _refresh_access_token(self) -> None:
        """Refresh the Twitch access token using the refresh token."""
        if not self._refresh_token:
            self._access_token = None
            msg = "No refresh token available. Re-authenticate."
            raise LoginFailed(msg)

        params = {
            "client_id": self._client_id or "",
            "client_secret": self._client_secret or "",
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }
        async with self.mass.http_session.post(TWITCH_TOKEN_URL, data=params) as response:
            if response.status != 200:
                self._access_token = None
                self._refresh_token = None
                error_text = await response.text()
                msg = f"Token refresh failed: {error_text}"
                raise LoginFailed(msg)
            data = await response.json()

        self._access_token = data["access_token"]
        # Twitch may rotate the refresh token
        self._refresh_token = data.get("refresh_token", self._refresh_token)

    async def _api_get(
        self,
        url: str,
        params: dict[str, Any] | list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Make authenticated GET request to Twitch API, with auto-refresh on 401."""
        full_url = url if url.startswith("http") else f"https://api.twitch.tv{url}"
        async with self.mass.http_session.get(
            full_url, headers=self._api_headers(), params=params
        ) as response:
            if response.status == 401:
                await self._refresh_access_token()
                async with self.mass.http_session.get(
                    full_url, headers=self._api_headers(), params=params
                ) as retry_response:
                    if retry_response.status != 200:
                        msg = f"Twitch API error {retry_response.status}"
                        raise Exception(msg)
                    return await retry_response.json()  # type: ignore[no-any-return]
            if response.status != 200:
                msg = f"Twitch API error {response.status}"
                raise Exception(msg)
            return await response.json()  # type: ignore[no-any-return]

    # --- Twitch API Methods ---

    async def _get_followed_channels(self) -> list[dict[str, Any]]:
        """Get all followed channels (paginated)."""
        all_channels: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, str] = {"user_id": self._user_id or "", "first": "100"}
            if cursor:
                params["after"] = cursor
            data = await self._api_get("/helix/channels/followed", params=params)
            all_channels.extend(data.get("data", []))
            cursor = data.get("pagination", {}).get("cursor")
            if not cursor:
                break
        return all_channels

    async def _get_live_streams(self, user_ids: list[str]) -> list[dict[str, Any]]:
        """Get live streams for user IDs (batched, max 100 per request)."""
        if not user_ids:
            return []
        all_streams: list[dict[str, Any]] = []
        for i in range(0, len(user_ids), 100):
            batch = user_ids[i : i + 100]
            params = [("user_id", uid) for uid in batch]
            data = await self._api_get("/helix/streams", params=params)
            all_streams.extend(data.get("data", []))
        return all_streams

    async def _get_followed_live_status(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """Get followed channels and their live status (cached 5 min)."""
        if (
            self._cached_channels is not None
            and self._cached_live is not None
            and (time.monotonic() - self._cache_time) < LIVE_STATUS_TTL
        ):
            return self._cached_channels, self._cached_live

        channels = await self._get_followed_channels()
        user_ids = [ch["broadcaster_id"] for ch in channels]
        streams = await self._get_live_streams(user_ids)
        live_by_login = {s["user_login"]: s for s in streams}

        self._cached_channels = channels
        self._cached_live = live_by_login
        self._cache_time = time.monotonic()

        return channels, live_by_login

    async def _get_users(self, logins: list[str] | None = None) -> list[dict[str, Any]]:
        """Get user info by login names."""
        if not logins:
            return []
        params = [("login", login) for login in logins]
        data = await self._api_get("/helix/users", params=params)
        return data.get("data", [])  # type: ignore[no-any-return]

    def _clear_cache(self) -> None:
        """Clear the live status cache."""
        self._cached_channels = None
        self._cached_live = None
        self._cache_time = 0.0

    # --- Radio Model Helpers ---

    def _channel_to_radio(
        self,
        channel: dict[str, Any],
        stream: dict[str, Any] | None = None,
    ) -> Radio:
        """Convert a Twitch channel + optional stream data to a Radio model."""
        login = channel.get("broadcaster_login", channel.get("user_login", ""))
        display_name = channel.get("broadcaster_name", channel.get("display_name", login))
        name = display_name
        if stream:
            viewer_count = stream.get("viewer_count", 0)
            name = f"{display_name} ({viewer_count} viewers)"

        thumbnail = ""
        if stream and stream.get("thumbnail_url"):
            thumbnail = stream["thumbnail_url"].replace("{width}", "320").replace("{height}", "180")

        radio = Radio(
            item_id=login,
            provider=self.domain,
            name=name,
            provider_mappings={
                ProviderMapping(
                    item_id=login,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if thumbnail:
            radio.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumbnail,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        return radio

    # --- MusicProvider Interface ---

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a Twitch channel."""
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
            media_type=MediaType.RADIO,
            stream_type=StreamType.CUSTOM,
            allow_seek=False,
            can_seek=False,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for a Twitch channel."""
        item_id = streamdetails.item_id
        reconnects = 0

        while True:
            streams = await asyncio.to_thread(self._resolve_streams, item_id)
            if not streams:
                return

            stream = self._select_quality(streams)
            if not stream:
                return

            fd = await asyncio.to_thread(stream.open)
            try:
                while True:
                    chunk = await asyncio.to_thread(fd.read, STREAM_CHUNK_SIZE)
                    if chunk:
                        reconnects = 0
                        yield chunk
                        continue
                    break
            finally:
                await asyncio.to_thread(fd.close)

            reconnects += 1
            if reconnects > MAX_CONSECUTIVE_RECONNECTS:
                return

            await asyncio.sleep(RECONNECT_DELAY)

    def _resolve_streams(self, channel: str) -> dict[str, Any] | None:
        """Resolve Streamlink streams for a channel. Blocking — call via to_thread."""
        from streamlink import Streamlink  # noqa: PLC0415

        from music_assistant.providers.twitch.ad_handling import patch_ad_handling  # noqa: PLC0415

        try:
            ad_mode = str(self.config.get_value(CONF_AD_HANDLING) or "silence")
            patch_ad_handling(ad_mode)

            session = Streamlink()
            streamlink_token = str(self.config.get_value(CONF_STREAMLINK_TOKEN) or "")
            if streamlink_token:
                session.set_option("http-headers", {"Authorization": f"OAuth {streamlink_token}"})
            streams = session.streams(f"https://twitch.tv/{channel}")
            return dict(streams) if streams else None
        except Exception:
            self.logger.exception("Failed to resolve streams for %s", channel)
            return None

    @staticmethod
    def _select_quality(streams: dict[str, Any]) -> Any | None:
        """Select preferred audio quality from available streams."""
        return next((streams[q] for q in PREFERRED_QUALITIES if q in streams), None)

    async def browse(self, path: str) -> Sequence[MediaItemType | BrowseFolder]:
        """Browse this provider's items."""
        # Parse path: "" for root, "instance://live" or "instance://following"
        subpath = ""
        if "://" in path:
            subpath = path.split("://")[1].split("/")[0]

        if not subpath:
            return [
                BrowseFolder(
                    item_id="live",
                    provider=self.domain,
                    path=f"{self.instance_id}://live",
                    name="Live",
                ),
                BrowseFolder(
                    item_id="following",
                    provider=self.domain,
                    path=f"{self.instance_id}://following",
                    name="Following",
                ),
            ]

        if subpath not in ("live", "following"):
            return []

        if not self.is_authenticated or not self._user_id:
            return []

        channels, live_by_login = await self._get_followed_live_status()

        if subpath == "live":
            return [
                self._channel_to_radio(ch, live_by_login.get(ch["broadcaster_login"]))
                for ch in channels
                if ch["broadcaster_login"] in live_by_login
            ]

        if subpath == "following":
            result: list[MediaItemType | BrowseFolder] = []
            for ch in sorted(channels, key=lambda c: c["broadcaster_name"].lower()):
                login = ch["broadcaster_login"]
                stream = live_by_login.get(login)
                radio = self._channel_to_radio(ch, stream)
                if not stream:
                    radio.name = f"{ch['broadcaster_name']} (offline)"
                result.append(radio)
            return result

        return []  # pragma: no cover

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve live followed channels as radio stations."""
        if not self.is_authenticated or not self._user_id:
            return

        channels, live_by_login = await self._get_followed_live_status()
        for ch in channels:
            login = ch["broadcaster_login"]
            if login in live_by_login:
                yield self._channel_to_radio(ch, live_by_login[login])

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on Twitch."""
        result = SearchResults()
        if MediaType.RADIO not in media_types:
            return result
        if not search_query or not self.is_authenticated:
            return result

        try:
            data = await self._api_get(
                "/helix/search/channels",
                params={"query": search_query, "first": str(limit)},
            )
            result.radio = [self._channel_to_radio(ch) for ch in data.get("data", [])]
        except Exception:
            self.logger.warning("Twitch search failed for query '%s'", search_query)

        return result
