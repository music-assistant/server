"""Twitch Audio music provider for Music Assistant."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    EventType,
    ImageType,
    MediaType,
    PlaybackState,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    Radio,
    RecommendationFolder,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.auth import AuthenticationHelper
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.twitch.eventsub import EventSubClient

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_RADIOS,
    ProviderFeature.RECOMMENDATIONS,
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
CONF_AUTO_RAID = "auto_raid"
CONF_ACTION_AUTH = "auth"
CONF_ACTION_REVOKE = "revoke"

# Browse paths
BROWSE_LIVE = "live"
BROWSE_FOLLOWING = "following"

TWITCH_AUTH_URL = "https://id.twitch.tv/oauth2/authorize"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_REVOKE_URL = "https://id.twitch.tv/oauth2/revoke"
TWITCH_SCOPES = ("user:read:follows",)
CALLBACK_REDIRECT_URL = "https://music-assistant.io/callback"


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
            "redirect_uri": CALLBACK_REDIRECT_URL,
            "response_type": "code",
            "scope": " ".join(TWITCH_SCOPES),
            "state": auth_helper.callback_url,
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
        "redirect_uri": CALLBACK_REDIRECT_URL,
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
        except Exception:
            logger.debug("Failed to revoke Twitch token", exc_info=True)

    values[CONF_ACCESS_TOKEN] = ""
    values[CONF_REFRESH_TOKEN] = ""


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    if not config.get_value(CONF_ACCESS_TOKEN):
        msg = "Not authenticated. Please configure and authenticate the Twitch provider."
        raise LoginFailed(msg)
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
        # Setup instructions
        ConfigEntry(
            key="setup_info",
            type=ConfigEntryType.LABEL,
            label="Register a Twitch application at dev.twitch.tv/console/apps. "
            f"Use {CALLBACK_REDIRECT_URL} as the OAuth Redirect URL.",
            hidden=is_authenticated,
        ),
        # Credentials
        ConfigEntry(
            key=CONF_CLIENT_ID,
            type=ConfigEntryType.SECURE_STRING,
            label="Twitch Client ID",
            description="From your Twitch application at dev.twitch.tv/console/apps.",
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
        # Optional Twitch website token
        ConfigEntry(
            key=CONF_STREAMLINK_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Twitch Website Token (optional)",
            description="Your Twitch website auth token. If you have Twitch Turbo "
            "or are subscribed to a channel, this reduces ad frequency. "
            "See the Streamlink Twitch plugin docs for how to extract this token: "
            "https://streamlink.github.io/cli/plugins/twitch.html#authentication",
            required=False,
            value=values.get(CONF_STREAMLINK_TOKEN),
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
    _streamlink_session: Any | None = None

    # Live status cache
    _cached_channels: list[dict[str, Any]] | None = None
    _cached_live: dict[str, dict[str, Any]] | None = None
    _cached_profiles: dict[str, dict[str, Any]] | None = None
    _cache_time: float = 0.0

    # Raid state
    _eventsub: EventSubClient | None = None
    _unsub_queue_updated: Any | None = None
    _current_channel_login: str | None = None
    _current_queue_id: str | None = None
    _auto_raid: bool = True
    _grace_timer: asyncio.Task[None] | None = None
    _idle_timer: asyncio.Task[None] | None = None

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
        val = self.config.get_value(CONF_AUTO_RAID)
        self._auto_raid = bool(val) if val is not None else True
        self.logger.info(
            "Twitch provider initialized: auto_raid=%s, authenticated=%s",
            self._auto_raid,
            self.is_authenticated,
        )

        # Resolve user ID if authenticated
        if self._access_token:
            try:
                data = await self._api_get("/helix/users", params={})
                if data.get("data"):
                    self._user_id = data["data"][0]["id"]
                    self.logger.info("Resolved Twitch user ID: %s", self._user_id)
            except LoginFailed:
                raise  # Propagate auth failures — user needs to re-authenticate
            except Exception:
                self.logger.warning("Failed to resolve user ID during init")

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        self._unsub_queue_updated = self.mass.subscribe(
            self._on_queue_updated,
            EventType.QUEUE_UPDATED,
        )
        self.logger.debug("Subscribed to QUEUE_UPDATED events")

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        # Unsubscribe from event bus
        if self._unsub_queue_updated is not None:
            with contextlib.suppress(Exception):
                self._unsub_queue_updated()
            self._unsub_queue_updated = None

        # Cancel timers
        self._cancel_timers()

        # Stop EventSub
        if self._eventsub is not None:
            await self._eventsub.stop()
            self._eventsub = None

        # Clear cache
        self._clear_cache()

    # --- Raid State Machine ---

    def _extract_twitch_login(self, queue_item: Any) -> str | None:
        """Extract Twitch channel login from a QueueItem, handling both URI schemes.

        When played from Browse, URI is 'twitch://radio/channel_login'.
        When played from Library, URI is 'library://radio/N' and the channel
        login must be extracted from the media_item's provider mapping.
        """
        uri = getattr(queue_item, "uri", "") or ""

        # Direct Twitch URI (from browse or play_media)
        if uri.startswith("twitch://"):
            parts = uri.split("/")
            return parts[-1] if len(parts) >= 3 and parts[-1] else None

        # Library URI — check media_item for Twitch provider mapping
        media_item = getattr(queue_item, "media_item", None)
        if media_item is not None:
            for pm in getattr(media_item, "provider_mappings", []):
                if getattr(pm, "provider_domain", "") == self.domain:
                    item_id = getattr(pm, "item_id", "")
                    if item_id:
                        return item_id

        return None

    async def _on_queue_updated(self, event: Any = None) -> None:
        """Handle queue update events for raid following."""
        if event is None or not hasattr(event, "data"):
            return

        queue = event.data
        state = getattr(queue, "state", None)
        current_item = getattr(queue, "current_item", None)
        queue_id = getattr(queue, "queue_id", "")

        self.logger.debug(
            "Queue update: state=%s, queue_id=%s, current_item=%s",
            state,
            queue_id,
            getattr(current_item, "uri", None) if current_item else None,
        )

        if state == PlaybackState.PLAYING and current_item:
            channel_login = self._extract_twitch_login(current_item)
            if channel_login:
                self._current_queue_id = queue_id
                await self._handle_queue_playing(f"twitch://radio/{channel_login}", channel_login)
                return
            # Non-Twitch content playing — stop tracking
            self.logger.debug(
                "Non-Twitch content playing: %s — stopping raid tracking",
                getattr(current_item, "uri", "?"),
            )
            await self._handle_queue_stopped()
        elif state == PlaybackState.PAUSED:
            await self._handle_queue_paused()
        elif state == PlaybackState.IDLE:
            await self._handle_queue_idle()

    async def _handle_queue_playing(self, uri: str, channel_login: str) -> None:
        """Handle playback of a Twitch channel — subscribe to raids."""
        self.logger.debug(
            "Handle queue playing: channel=%s, current=%s, auto_raid=%s, authenticated=%s",
            channel_login,
            self._current_channel_login,
            self._auto_raid,
            self.is_authenticated,
        )

        if channel_login == self._current_channel_login:
            self.logger.debug("Already tracking %s — skipping", channel_login)
            return

        self._cancel_timers()
        self._current_channel_login = channel_login

        if not self._auto_raid:
            self.logger.debug("Auto-raid disabled — not subscribing to EventSub")
            return
        if not self.is_authenticated:
            self.logger.debug("Not authenticated — not subscribing to EventSub")
            return

        # Ensure EventSub client exists
        if self._eventsub is None:
            self.logger.debug("Creating EventSub client and starting WebSocket")
            self._eventsub = EventSubClient(
                http_session=self.mass.http_session,
                api_headers_fn=self._api_headers,
            )
            await self._eventsub.start(
                on_raid=lambda from_l, to_l: asyncio.create_task(self._on_raid(from_l, to_l))
            )

        # Resolve user ID for the channel and subscribe
        users = await self._get_users(logins=[channel_login])
        if users:
            self.logger.debug(
                "Subscribing to raids for %s (user_id=%s)", channel_login, users[0]["id"]
            )
            await self._eventsub.subscribe_raids(users[0]["id"])
        else:
            self.logger.warning(
                "Could not resolve user ID for %s — no raid subscription", channel_login
            )

    async def _handle_queue_paused(self) -> None:
        """Handle pause — unsubscribe EventSub, keep WebSocket warm."""
        self.logger.debug("Handle queue paused — unsubscribing EventSub, keeping WS warm")
        self._cancel_timers()
        if self._eventsub is not None:
            await self._eventsub.unsubscribe_all()

    async def _handle_queue_idle(self) -> None:
        """Handle stop/idle — start grace period before disconnecting."""
        self.logger.debug("Handle queue idle — starting grace period")
        self._cancel_timers()
        self._grace_timer = asyncio.create_task(self._grace_period())

    async def _handle_queue_stopped(self) -> None:
        """Handle non-Twitch content — immediate cleanup."""
        self.logger.debug("Handle queue stopped — cleaning up raid tracking")
        self._cancel_timers()
        self._current_channel_login = None
        if self._eventsub is not None:
            await self._eventsub.unsubscribe_all()

    async def _grace_period(self) -> None:
        """Wait 15s grace period, then unsubscribe and start idle timer."""
        await asyncio.sleep(15)
        if self._eventsub is not None:
            await self._eventsub.unsubscribe_all()
        self._idle_timer = asyncio.create_task(self._idle_disconnect())

    async def _idle_disconnect(self) -> None:
        """Wait 5 minutes, then disconnect EventSub WebSocket."""
        await asyncio.sleep(300)
        if self._eventsub is not None:
            self.logger.debug("Idle timeout reached — disconnecting EventSub WebSocket")
            await self._eventsub.stop()
            self._eventsub = None

    async def _on_raid(self, from_login: str, to_login: str) -> None:
        """Handle a raid event — switch playback to raid target."""
        self.logger.debug(
            "Raid event received: %s → %s (auto_raid=%s, current=%s)",
            from_login,
            to_login,
            self._auto_raid,
            self._current_channel_login,
        )
        if not self._auto_raid:
            self.logger.debug("Auto-raid disabled — ignoring raid")
            return

        if from_login != self._current_channel_login:
            self.logger.debug(
                "Ignoring stale raid from %s (current=%s)",
                from_login,
                self._current_channel_login,
            )
            return

        self.logger.info("Raid received: %s → %s", from_login, to_login)
        try:
            await self.mass.player_queues.play_media(
                queue_id=self._current_queue_id or "",
                media=f"twitch://radio/{to_login}",
            )
        except Exception:
            self.logger.warning("Failed to follow raid to %s", to_login, exc_info=True)

    def _cancel_timers(self) -> None:
        """Cancel any pending grace/idle timers."""
        if self._grace_timer is not None:
            self._grace_timer.cancel()
            self._grace_timer = None
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

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

    @staticmethod
    def _raise_for_status(status: int) -> None:
        """Raise an appropriate MA exception for non-success HTTP status codes."""
        if 200 <= status < 300:
            return
        if status == 404:
            msg = f"Twitch API: resource not found ({status})"
            raise MediaNotFoundError(msg)
        if status == 429:
            msg = f"Twitch API: rate limited ({status})"
            raise ResourceTemporarilyUnavailable(msg)
        if status >= 500:
            msg = f"Twitch API: server error ({status})"
            raise ProviderUnavailableError(msg)
        msg = f"Twitch API error ({status})"
        raise MusicAssistantError(msg)

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

        # Persist tokens to config storage so they survive restarts
        self._update_config_value(CONF_ACCESS_TOKEN, self._access_token, encrypted=True)
        self._update_config_value(CONF_REFRESH_TOKEN, self._refresh_token, encrypted=True)

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
                    self._raise_for_status(retry_response.status)
                    return await retry_response.json()  # type: ignore[no-any-return]
            self._raise_for_status(response.status)
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

    async def _get_user_profiles(self, user_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Get user profiles by ID (batched, max 100 per request)."""
        if not user_ids:
            return {}
        profiles: dict[str, dict[str, Any]] = {}
        for i in range(0, len(user_ids), 100):
            batch = user_ids[i : i + 100]
            params = [("id", uid) for uid in batch]
            data = await self._api_get("/helix/users", params=params)
            for user in data.get("data", []):
                profiles[user["login"]] = user
        return profiles

    async def _get_followed_live_status(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Get followed channels, live status, and profiles (cached 5 min)."""
        if (
            self._cached_channels is not None
            and self._cached_live is not None
            and self._cached_profiles is not None
            and (time.monotonic() - self._cache_time) < LIVE_STATUS_TTL
        ):
            return self._cached_channels, self._cached_live, self._cached_profiles

        channels = await self._get_followed_channels()
        user_ids = [ch["broadcaster_id"] for ch in channels]
        # Fetch streams and profiles concurrently — both only need user_ids
        streams, profiles = await asyncio.gather(
            self._get_live_streams(user_ids),
            self._get_user_profiles(user_ids),
        )
        live_by_login = {s["user_login"]: s for s in streams}

        self._cached_channels = channels
        self._cached_live = live_by_login
        self._cached_profiles = profiles
        self._cache_time = time.monotonic()

        return channels, live_by_login, profiles

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
        self._cached_profiles = None
        self._cache_time = 0.0

    # --- Radio Model Helpers ---

    def _channel_to_radio(
        self,
        channel: dict[str, Any],
        stream: dict[str, Any] | None = None,
        profile: dict[str, Any] | None = None,
    ) -> Radio:
        """Convert a Twitch channel + optional stream/profile data to a Radio model."""
        login = channel.get("broadcaster_login", channel.get("user_login", ""))
        display_name = channel.get("broadcaster_name", channel.get("display_name", login))
        name = display_name
        if stream:
            viewer_count = stream.get("viewer_count", 0)
            name = f"{display_name} ({viewer_count} viewers)"

        # Prefer stream thumbnail (live preview), fall back to profile image
        thumbnail = ""
        if stream and stream.get("thumbnail_url"):
            thumbnail = stream["thumbnail_url"].replace("{width}", "320").replace("{height}", "180")
        elif profile and profile.get("profile_image_url"):
            thumbnail = profile["profile_image_url"]

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

            import music_assistant.providers.twitch.ad_handling as _ah  # noqa: PLC0415

            fd = await asyncio.to_thread(stream.open)
            prev_ad_state = False
            try:
                while True:
                    chunk = await asyncio.to_thread(fd.read, STREAM_CHUNK_SIZE)
                    if chunk:
                        reconnects = 0
                        if _ah.ad_break_active != prev_ad_state:
                            prev_ad_state = _ah.ad_break_active
                            if _ah.ad_break_active:
                                streamdetails.stream_title = f"{item_id} - Ad Break"
                            else:
                                streamdetails.stream_metadata = None
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
        from streamlink import Streamlink  # type: ignore[attr-defined]  # noqa: PLC0415

        try:
            if self._streamlink_session is None:
                self._streamlink_session = Streamlink()
                streamlink_token = str(self.config.get_value(CONF_STREAMLINK_TOKEN) or "")
                if streamlink_token:
                    self._streamlink_session.set_option(
                        "http-headers", {"Authorization": f"OAuth {streamlink_token}"}
                    )
            session = self._streamlink_session
            streams = session.streams(f"https://twitch.tv/{channel}")
            if not streams:
                return None

            # Apply ad handling monkey-patch to the ACTUAL reader class from
            # Streamlink's plugin system. Must be done after streams() because
            # Streamlink loads plugins into a fresh module namespace — patching
            # the imported class at startup patches a different class object.
            result = dict(streams)
            any_stream = next(iter(result.values()), None)
            if any_stream is not None:
                reader_cls = getattr(type(any_stream), "__reader__", None)
                if reader_cls is not None:
                    from music_assistant.providers.twitch.ad_handling import (  # noqa: PLC0415
                        patch_ad_handling,
                    )

                    patch_ad_handling(reader_cls=reader_cls)

            return result
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
                    item_id=BROWSE_LIVE,
                    provider=self.domain,
                    path=f"{self.instance_id}://{BROWSE_LIVE}",
                    name="Live",
                ),
                BrowseFolder(
                    item_id=BROWSE_FOLLOWING,
                    provider=self.domain,
                    path=f"{self.instance_id}://{BROWSE_FOLLOWING}",
                    name="Following",
                ),
            ]

        if subpath not in (BROWSE_LIVE, BROWSE_FOLLOWING):
            return []

        if not self.is_authenticated or not self._user_id:
            return []

        channels, live_by_login, profiles = await self._get_followed_live_status()

        if subpath == BROWSE_LIVE:
            return [
                self._channel_to_radio(
                    ch,
                    live_by_login.get(ch["broadcaster_login"]),
                    profiles.get(ch["broadcaster_login"]),
                )
                for ch in channels
                if ch["broadcaster_login"] in live_by_login
            ]

        if subpath == BROWSE_FOLLOWING:
            result: list[MediaItemType | BrowseFolder] = []
            for ch in sorted(channels, key=lambda c: c["broadcaster_name"].lower()):
                login = ch["broadcaster_login"]
                stream = live_by_login.get(login)
                radio = self._channel_to_radio(ch, stream, profiles.get(login))
                if not stream:
                    radio.name = f"{ch['broadcaster_name']} (offline)"
                result.append(radio)
            return result

        return []  # pragma: no cover

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve live followed channels as radio stations."""
        if not self.is_authenticated or not self._user_id:
            return

        channels, live_by_login, profiles = await self._get_followed_live_status()
        for ch in channels:
            login = ch["broadcaster_login"]
            if login in live_by_login:
                yield self._channel_to_radio(ch, live_by_login[login], profiles.get(login))

    async def recommendations(self) -> list[RecommendationFolder]:
        """Get this provider's recommendations."""
        if not self.is_authenticated or not self._user_id:
            return []

        channels, live_by_login, profiles = await self._get_followed_live_status()
        live_radios = [
            self._channel_to_radio(
                ch, live_by_login[ch["broadcaster_login"]], profiles.get(ch["broadcaster_login"])
            )
            for ch in channels
            if ch["broadcaster_login"] in live_by_login
        ]
        if not live_radios:
            return []

        folder = RecommendationFolder(
            name="Twitch Live Channels",
            item_id=f"{self.instance_id}_live_channels",
            provider=self.instance_id,
            icon="mdi-broadcast",
        )
        folder.items.extend(live_radios)
        return [folder]

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id (channel login)."""
        if not self.is_authenticated:
            msg = f"Not authenticated — cannot look up channel {prov_radio_id}"
            raise MediaNotFoundError(msg)

        users = await self._get_users(logins=[prov_radio_id])
        if not users:
            msg = f"Twitch channel not found: {prov_radio_id}"
            raise MediaNotFoundError(msg)

        user = users[0]
        # Check if live
        streams = await self._get_live_streams([user["id"]])
        stream = streams[0] if streams else None

        return self._channel_to_radio(
            {"broadcaster_login": user["login"], "broadcaster_name": user["display_name"]},
            stream,
            user,
        )

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
