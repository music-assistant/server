"""
Playback redirect into Spotify Connect sessions for the Spotify provider.

The counterpart of ``streaming.py``: where librespot mode streams every track
itself, the Connect redirect hands a play request to a Spotify Connect session
hosted by the ``spotify_connect`` provider (see
``controllers/player_queues/delegation.py`` for the queue-side seam). This module
owns finding the account-matched session, the passive account verification, the
Web API assist (play targeting the session's device, with start offset) and its
session-control fallback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import aiohttp
from music_assistant_models.enums import MediaType, QueueOption
from music_assistant_models.errors import (
    AudioError,
    MediaNotFoundError,
    ProviderUnavailableError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import AudioSource

from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import throttle_with_retries
from music_assistant.providers.spotify_connect import SpotifyConnectProvider

from .constants import LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX

if TYPE_CHECKING:
    import logging
    from collections.abc import Iterator

    from music_assistant_models.media_items import (
        MediaItem,
        MediaItemType,
        PlayableMediaItemType,
    )

    from music_assistant.helpers.throttle_retry import ThrottlerManager

    from .provider import SpotifyProvider

# The Web API's play request accepts at most this many uris; larger context-less
# lists are truncated (with a log) rather than rejected wholesale.
MAX_DELEGATED_TRACKS = 100


class SpotifyConnectRedirect:
    """Routes the provider's play requests into an account-matched Spotify Connect session."""

    def __init__(self, provider: SpotifyProvider) -> None:
        """Initialize the redirect for the given provider instance."""
        self.provider = provider

    @property
    def logger(self) -> logging.Logger:
        """Return the owning provider's logger (also read by the throttle decorator)."""
        return self.provider.logger

    @property
    def throttler(self) -> ThrottlerManager:
        """Return the owning provider's (live) throttler for the throttle decorator."""
        return self.provider.throttler

    async def find_connect_delegate(self, target_player_id: str) -> AudioSource | None:
        """
        Return this account's Connect session to redirect playback into (Connect mode).

        The instance explicitly bound to the target player is preferred (the Spotify
        app then shows the session under the player's own name); otherwise the
        system-wide instance is used. Instances bound to another player belong to
        that player and are never used.

        :param target_player_id: The player (queue) the play request targets.
        """
        if (account_id := self.provider.account_id) is None:
            return None
        candidates = sorted(
            (
                plugin
                for plugin in self._iter_connect_plugins()
                if plugin.configured_player_id in (None, target_player_id)
            ),
            # a target-player-bound instance sorts before the system-wide one
            key=lambda plugin: plugin.configured_player_id is None,
        )
        for plugin in candidates:
            if await self._verify_connect_account(plugin) == account_id:
                return plugin.audio_source
        return None

    async def find_active_connect_delegate(self, target_player_id: str) -> AudioSource | None:
        """
        Return the same-account session already active on the target player, if any.

        The opportunistic redirect for librespot mode: only when the target player's
        current queue item is a live same-account Connect session does playing
        Spotify content drive that session; anything else streams via librespot.

        :param target_player_id: The player (queue) the play request targets.
        """
        if (account_id := self.provider.account_id) is None:
            return None
        queue = self.provider.mass.player_queues.get(target_player_id)
        if queue is None or queue.current_item is None or queue.current_item.media_item is None:
            return None
        media_item = queue.current_item.media_item
        if not isinstance(media_item, AudioSource):
            return None
        plugin = self.provider.mass.get_provider(media_item.provider)
        if not isinstance(plugin, SpotifyConnectProvider) or not self._is_redirect_target(plugin):
            return None
        if not plugin.session_active or plugin.active_player_id != target_player_id:
            # a leftover source of an ended session, or one whose live session has
            # since moved to another player: librespot streams normally (redirecting
            # would steal the session from wherever it plays now)
            return None
        if await self._verify_connect_account(plugin) != account_id:
            return None
        return plugin.audio_source

    async def play_on_delegate(
        self,
        delegate: AudioSource,
        media_items: list[PlayableMediaItemType],
        option: QueueOption,
        target_player_id: str,
        context: MediaItemType | None = None,
        start_item: PlayableMediaItemType | str | None = None,
    ) -> None:
        """
        Play or enqueue the given items on the Connect session.

        PLAY/REPLACE start the content through the Web API targeting the session's
        device (which supports a start offset), falling back to the session's own
        play command. ADD, NEXT and REPLACE_NEXT all enqueue onto the session's
        active queue: Spotify's queue section plays before the context remainder,
        so add-to-queue is the faithful mapping for NEXT and ADD is coerced along.

        :param delegate: The session AudioSource returned by the delegate lookup.
        :param media_items: The resolved playable items of the play request.
        :param option: The enqueue option of the play request.
        :param target_player_id: The player (queue) the play request targets.
        :param context: The single original container the request expanded from, if any.
        :param start_item: Optional item (or uri) within the context to start at.
        """
        plugin = self.provider.mass.get_provider(delegate.provider)
        if not isinstance(plugin, SpotifyConnectProvider):
            raise ProviderUnavailableError("The Spotify Connect session is no longer available")
        if option in (QueueOption.ADD, QueueOption.NEXT, QueueOption.REPLACE_NEXT):
            if plugin.active_player_id != target_player_id or not plugin.session_active:
                raise AudioError(
                    "There is no active Spotify Connect session on this player to add "
                    "to - start Spotify playback on it first.",
                    translation_key="connect_no_session_for_enqueue",
                    translation_owner=self.provider.translation_owner,
                )
            await plugin.enqueue_on_source(self._delegate_item_uris(media_items))
            return
        context_uri, start_uri = self._delegate_context(context, start_item, media_items)
        uris = [] if context_uri else self._delegate_item_uris(media_items)
        if not context_uri and not uris:
            raise MediaNotFoundError("No playable items found", translation_key="no_playable_items")
        await plugin.prepare_redirect(target_player_id)
        try:
            if await self._web_api_play(
                plugin, context_uri=context_uri, start_uri=start_uri, uris=uris
            ):
                return
            # Web API assist unavailable or rejected: the session's own play command
            # starts the context from the beginning where it has no start offset and
            # plays track lists as first-track + enqueue
            await plugin.play_media_on_source(uris, context_uri=context_uri, start_uri=start_uri)
        except BaseException:
            # nothing is going to start: roll the prepared redirect back so an
            # app-initiated session start is not mistaken for this redirect and
            # no stale player pre-target lingers
            plugin.cancel_redirect()
            raise

    def _is_redirect_target(self, plugin: SpotifyConnectProvider) -> bool:
        """Return whether the plugin instance is usable as a redirect target at all."""
        if not plugin.available:
            return False
        caps = plugin.audio_source.queue_capabilities
        return caps is not None and caps.provider_domain == self.provider.domain

    def _iter_connect_plugins(self) -> Iterator[SpotifyConnectProvider]:
        """Yield the loaded Spotify Connect plugin instances usable as redirect targets."""
        for prov in self.provider.mass.providers:
            if isinstance(prov, SpotifyConnectProvider) and self._is_redirect_target(prov):
                yield prov

    async def _verify_connect_account(self, plugin: SpotifyConnectProvider) -> str | None:
        """
        Return the Spotify account the plugin's session belongs to, verifying when unknown.

        Verification is passive and side-effect-free (it also runs for play requests
        that end up not redirecting): the backend's own account report when it has
        one, else the silent device-list check — a paired running daemon appears in
        its own account's Connect device list, so a name match on this account's
        list establishes the account without any playback. Colliding device names
        are disambiguated to a concrete device id at play time (after the redirect
        activated the session), not here. The result is cached on the plugin
        instance; None means the account could not be established and the instance
        is not used as a redirect target.

        :param plugin: The Spotify Connect plugin instance to verify.
        """
        if (account_id := plugin.verified_account_id) is not None:
            return account_id
        if (account_id := await plugin.get_backend_account_id()) is not None:
            plugin.set_verified_account_id(account_id)
            return account_id
        if (account_id := self.provider.account_id) is None:
            return None
        try:
            matches = await self._get_connect_devices(plugin.publish_name)
        except Exception as err:
            self.logger.debug("Connect device lookup failed: %s", err)
            return None
        if matches:
            plugin.set_verified_account_id(account_id)
            return plugin.verified_account_id
        return None

    async def _get_connect_devices(self, device_name: str) -> list[dict[str, Any]]:
        """
        Return this account's Spotify Connect devices carrying the given name.

        :param device_name: The published device name to filter on.
        """
        devices = await self._get_player_data("me/player/devices")
        return [
            device
            for device in (devices or {}).get("devices", [])
            if device.get("name") == device_name
        ]

    async def _web_api_play(
        self,
        plugin: SpotifyConnectProvider,
        *,
        context_uri: str | None,
        start_uri: str | None,
        uris: list[str],
    ) -> bool:
        """
        Start playback through the Web API targeting the session's Connect device.

        Preferred over the session's own play command because it supports starting a
        context at a specific track and playing ad-hoc track lists. Returns True on
        success and False when the assist is unavailable or rejected (the caller
        then falls back to the session control plane). The device lookup doubles as
        a freshness check: a device that vanished from this account's list clears
        the cached account verification so the next request re-verifies.

        :param plugin: The plugin instance owning the target session.
        :param context_uri: The Spotify context to play, when the request was one container.
        :param start_uri: Track within the context to start at.
        :param uris: Track uris to play when no context is given.
        """
        try:
            matches = await self._get_connect_devices(plugin.publish_name)
        except Exception as err:
            self.logger.debug("Connect device lookup failed: %s", err)
            return False
        if not matches:
            plugin.set_verified_account_id(None)
            self.logger.warning(
                "Spotify Connect device '%s' is not in this account's device list",
                plugin.publish_name,
            )
            return False
        device_id: str | None = None
        if len(matches) == 1:
            device_id = matches[0].get("id")
        else:
            # colliding names: prepare_redirect just activated our session, so the
            # account's active device is the one we are after
            try:
                state = await self._get_player_data("me/player")
            except Exception:
                state = None
            if state and state.get("device", {}).get("name") == plugin.publish_name:
                device_id = state["device"].get("id")
        if not device_id:
            return False
        body: dict[str, Any] = {}
        if context_uri:
            body["context_uri"] = context_uri
            # a start offset is only valid for album/playlist contexts
            if start_uri and context_uri.split(":")[1] in ("album", "playlist"):
                body["offset"] = {"uri": start_uri}
        else:
            body["uris"] = uris
        try:
            await self.provider._put_data("me/player/play", data=body, device_id=device_id)
        except Exception as err:
            self.logger.info(
                "Web API play on '%s' failed (%s); falling back to the session control",
                plugin.publish_name,
                err,
            )
            return False
        return True

    def _delegate_item_uris(self, media_items: list[PlayableMediaItemType]) -> list[str]:
        """
        Return the Spotify uris for the given resolved playable items.

        Capped at the Web API's play-request limit: a context-less list larger than
        that (e.g. Liked Songs) plays its first chunk only, until queue mirroring
        brings proper continuation.
        """
        uris: list[str] = []
        for item in media_items:
            if (uri := self._delegate_item_uri(item)) is not None:
                uris.append(uri)
        if len(uris) > MAX_DELEGATED_TRACKS:
            self.logger.warning(
                "Sending only the first %d of %d items to the Spotify Connect session",
                MAX_DELEGATED_TRACKS,
                len(uris),
            )
            return uris[:MAX_DELEGATED_TRACKS]
        return uris

    def _delegate_item_uri(self, item: MediaItem) -> str | None:
        """Return the Spotify uri for a single resolved playable item, if it has one."""
        # only item-level types: an audiobook is a container of chapters and is
        # rejected by the play request as an item (it plays as a context instead)
        uri_types = {
            MediaType.TRACK: "track",
            MediaType.PODCAST_EPISODE: "episode",
        }
        if (uri_type := uri_types.get(item.media_type)) is None:
            return None
        if (item_id := self._item_id_for_this_provider(item)) is None:
            return None
        return f"spotify:{uri_type}:{item_id}"

    def _delegate_context(
        self,
        context: MediaItemType | None,
        start_item: PlayableMediaItemType | str | None,
        media_items: list[PlayableMediaItemType],
    ) -> tuple[str | None, str | None]:
        """
        Return the Spotify context uri and start-track uri for a single-container request.

        Returns (None, None) when the request has no usable Spotify context (the
        caller then plays the expanded track list instead).

        :param context: The single original container the request expanded from, if any.
        :param start_item: Optional item (or uri) within the context to start at.
        :param media_items: The resolved items of the request, to resolve a start
            item given as a foreign (e.g. library) uri to its Spotify mapping.
        """
        context_types = {
            MediaType.ALBUM: "album",
            MediaType.PLAYLIST: "playlist",
            MediaType.ARTIST: "artist",
            MediaType.PODCAST: "show",
            MediaType.AUDIOBOOK: "audiobook",
        }
        if context is None or (uri_type := context_types.get(context.media_type)) is None:
            return None, None
        item_id = self._item_id_for_this_provider(context)
        if item_id is None or item_id.startswith(LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX):
            # not a real Spotify context (e.g. the Liked Songs pseudo-playlist)
            return None, None
        start_uri: str | None = None
        if isinstance(start_item, str):
            # only an MA uri of this provider's domain carries a Spotify item id in its
            # tail (a library:// uri ends in a database id instead); anything else is
            # resolved through the already-resolved batch, which media resolution built
            # honoring the very same start item
            domain = self.provider.domain
            prefix, _, rest = start_item.partition("://")
            media_type_str, _, start_id = rest.partition("/")
            if (
                (prefix == domain or prefix.startswith(f"{domain}--"))
                and media_type_str in ("track", "podcast_episode")
                and start_id
            ):
                uri_kind = "episode" if media_type_str == "podcast_episode" else "track"
                start_uri = f"spotify:{uri_kind}:{start_id}"
            else:
                start_match = next(
                    (item for item in media_items if item.uri == start_item),
                    None,
                )
                if start_match is not None:
                    start_uri = self._delegate_item_uri(start_match)
        elif start_item is not None:
            start_uri = self._delegate_item_uri(start_item)
        if uri_type == "show" and start_uri is not None:
            # show contexts have no reliable start offset; the expanded episode list
            # (which media resolution already starts at the chosen episode) plays instead
            return None, None
        if uri_type == "audiobook":
            # an audiobook only plays as a context (its chapters are not item uris);
            # Spotify resumes it at the account's own position
            return f"spotify:{uri_type}:{item_id}", None
        return f"spotify:{uri_type}:{item_id}", start_uri

    def _item_id_for_this_provider(self, item: MediaItem) -> str | None:
        """
        Return the item's Spotify item id, preferring this instance's own mapping.

        Any other Spotify instance's mapping carries the same global Spotify id, so
        it serves as fallback.

        :param item: The media item to read the Spotify id from.
        """
        fallback: str | None = None
        for mapping in item.provider_mappings:
            if not mapping.available or mapping.provider_domain != self.provider.domain:
                continue
            if mapping.provider_instance == self.provider.instance_id:
                return mapping.item_id
            fallback = mapping.item_id
        return fallback

    @throttle_with_retries
    async def _get_player_data(self, endpoint: str, **kwargs: Any) -> dict[str, Any] | None:
        """
        Get data from a Web API player endpoint.

        The player endpoints reject the market/country parameters the generic
        ``_get_data`` helper injects, hence this dedicated call path. They answer
        204 (no content) when there is no active playback, returned as None.

        :param endpoint: API endpoint to call (e.g. ``me/player/devices``).
        """
        provider = self.provider
        url = f"https://api.spotify.com/v1/{endpoint}"
        auth_info = await provider._get_auth_info()
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        async with provider.mass.http_session.get(
            url,
            headers=headers,
            params=kwargs,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise RateLimited("Spotify Rate Limiter", backoff_time=backoff_time)
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                if not provider.dev_session_active:
                    provider._auth_info_global = None
                else:
                    provider._auth_info_dev = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=1)
            if response.status == 204:
                return None
            response.raise_for_status()
            return cast("dict[str, Any]", await response.json(loads=json_loads))
