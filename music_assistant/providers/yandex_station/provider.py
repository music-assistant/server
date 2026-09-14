"""Yandex Station Player Provider — device discovery and lifecycle."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import TYPE_CHECKING, Any, cast

from aiohttp import ClientSession, CookieJar
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from ya_passport_auth import PassportClient, SecretStr
from ya_passport_auth.ma import (
    BORROW_SOURCE_OWN,
    BorrowedCredentialSource,
    CascadeHooks,
    CredentialCascade,
    KeySpec,
)

from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    CONF_INTERCEPT_FEATURE_ENABLED,
    CONF_MUSIC_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_REMEMBER_SESSION,
    CONF_X_TOKEN,
    CONF_YM_INSTANCE,
    MDNS_TYPE,
)
from .glagol import YandexGlagol
from .player import YandexStationPlayer
from .quasar import YandexQuasar
from .session import YandexSession

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest
    from ya_passport_auth import Credentials
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.mass import MusicAssistant

_LOGGER = logging.getLogger(__name__)


class YandexStationProvider(PlayerProvider):
    """Player provider for Yandex Station smart speakers."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._session: YandexSession | None = None
        self._quasar: YandexQuasar | None = None
        self._http_session: ClientSession | None = None
        self._passport_client: PassportClient | None = None
        self._pending_discoveries: set[str] = set()
        self._mdns_players: dict[str, str] = {}  # mDNS service name → player_id
        self._discovery_done = False
        self._init_lock: asyncio.Lock = asyncio.Lock()
        self._cascade = self._build_cascade()
        self._borrow_source = self._build_borrow_source()

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return Config entries to configure this provider.

        Authentication and the Yandex account source are collected by the interactive
        setup flow (see setup_flow.py); only the genuine options live on this surface.
        """
        return (
            # Experimental intercept feature — provider-level master switch.
            # When OFF, no per-player intercept setting takes effect.
            ConfigEntry(
                key=CONF_INTERCEPT_FEATURE_ENABLED,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
            ),
        )

    # ── Discovery ─────────────────────────────────────────────────────

    async def discover_players(self) -> None:
        """
        Discover Yandex Station players.

        Two-phase discovery:
        1. Cloud: Quasar API (requires session cookies from x_token)
        2. Local: mDNS (handled by MA core via manifest.json mdns_discovery)
        """
        if self._discovery_done:
            return

        if not await self._init_session():
            return

        # Load device list from Quasar cloud API.  Two endpoints with
        # *different* auth — ``get_speakers`` uses cookie auth and
        # carries cloud-side metadata (name, model, room, etc.), while
        # ``get_local_speakers`` uses Glagol/music_token auth and carries
        # the IP/port needed for the local WebSocket.  When cookies are
        # stale or x_token broke (but music_token is still valid), the
        # cloud call fails — we fall back to registering devices from
        # the Glagol list only, so users in that state still get working
        # players (with placeholder names) instead of an empty integration.
        assert self._session is not None  # guaranteed by _init_session()
        self._quasar = YandexQuasar(self._session)
        speakers: list[dict[str, Any]] = []
        quasar_ok = False
        try:
            speakers = await self._get_speakers_with_reauth()
            self.logger.info("Found %d speakers via Quasar API", len(speakers))
            # ``YandexQuasar.get_speakers()`` filters the bulk device list
            # to only entries that already carry ``quasar_info``, so we
            # don't need a per-device ``load_device_config`` enrichment
            # loop here — every returned speaker is guaranteed to have
            # the field set.  ``load_device_config`` remains available
            # for callers that bypass the filter and want to enrich a
            # raw device entry on demand.
            quasar_ok = True
        except Exception:
            self.logger.warning(
                "Failed to load speakers from Quasar (cloud auth or API issue) — "
                "falling back to Glagol device_list",
                exc_info=True,
            )

        # Local connection info from glagol API (host/port + glagol-side info).
        # Used both as enrichment for Quasar-listed speakers AND as the primary
        # device source when Quasar fails.
        local_speakers: dict[str, dict[str, Any]] = {}
        try:
            local_list = await self._quasar.get_local_speakers()
            for ls in local_list:
                local_speakers[ls["device_id"]] = ls
            self.logger.info("Found %d local speakers via Glagol API", len(local_speakers))
        except Exception:
            self.logger.debug("Failed to get local speakers from Glagol API")

        # If Quasar failed, build a synthetic speakers list from the local
        # device_list response.  Local entries already carry device_id, name,
        # platform, host, port — enough to register a working player.
        if not quasar_ok:
            if not local_speakers:
                # Both auth paths failed — leave _discovery_done=False so MA
                # retries when cookies/x_token/music_token become valid again.
                self.logger.warning(
                    "Both Quasar and Glagol device_list discovery failed — will retry later"
                )
                return
            for device_id, ls in local_speakers.items():
                speakers.append(
                    {
                        "name": ls.get("name") or "Yandex Station",
                        "host": ls["host"],
                        "port": ls["port"],
                        "glagol": ls.get("glagol", {}),
                        "quasar_info": {
                            "device_id": device_id,
                            "platform": ls.get("platform", ""),
                        },
                    }
                )
            self.logger.info(
                "Registering %d speakers from Glagol device_list only "
                "(cloud-side metadata unavailable)",
                len(speakers),
            )

        for speaker in speakers:
            qi = speaker.get("quasar_info", {})
            device_id = qi.get("device_id", "")
            if not device_id:
                continue
            player_id = f"ys_{device_id}"
            # Merge local connection info (IP/port from glagol API)
            if device_id in local_speakers:
                ls = local_speakers[device_id]
                speaker.setdefault("host", ls["host"])
                speaker.setdefault("port", ls["port"])
                speaker.setdefault("glagol", ls.get("glagol", {}))
            self.logger.info(
                "Registering speaker: %s [%s]", speaker.get("name"), qi.get("platform")
            )
            await self._create_player(player_id, speaker)

        self._discovery_done = True

    async def on_mdns_service_state_change(
        self,
        name: str,
        state_change: ServiceStateChange,
        info: AsyncServiceInfo | None,
    ) -> None:
        """
        Handle mDNS discovery callback (called by MA core).

        Note: MA passes info=None for Removed events, so we use a cached
        name→player_id mapping to mark players unavailable.
        """
        from zeroconf import ServiceStateChange  # noqa: PLC0415, RUF100

        if state_change == ServiceStateChange.Removed:
            player_id = self._mdns_players.get(name)
            if player_id:
                existing = self.mass.players.get_player(player_id)
                if existing and isinstance(existing, YandexStationPlayer):
                    existing._attr_available = False
                    existing.update_state()
                    _LOGGER.debug("Marked player %s unavailable (mDNS removed)", player_id)
            return

        if not info or not info.addresses:
            return

        try:
            properties: dict[str, Any] = {
                k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
                for k, v in info.properties.items()
            }

            device_id = properties.get("deviceId", "")
            platform = properties.get("platform", "")
            host = str(ipaddress.ip_address(info.addresses[0]))
            port = info.port or 0

            if not device_id or not port:
                return

            player_id = f"ys_{device_id}"

            # Cache mDNS name → player_id for Removed events (info=None)
            self._mdns_players[name] = player_id

            if player_id in self._pending_discoveries:
                return

            # Check if player already registered (cloud-discovered) — connect Glagol
            existing = self.mass.players.get_player(player_id)
            if existing and isinstance(existing, YandexStationPlayer):
                if not existing.glagol.connected:
                    existing.update_connection(host, port)
                    await existing.async_setup()
                else:
                    existing.update_connection(host, port)
                return

            self._pending_discoveries.add(player_id)

            device_info: dict[str, Any] = {
                "quasar_info": {
                    "device_id": device_id,
                    "platform": platform,
                },
                "name": name.replace(f".{MDNS_TYPE}", ""),
                "host": host,
                "port": port,
            }

            # Enrich with Quasar cloud data if available
            if self._quasar and self._quasar.devices:
                for cloud_device in self._quasar.devices:
                    qi = cloud_device.get("quasar_info", {})
                    if qi.get("device_id") == device_id:
                        device_info.update(
                            {k: v for k, v in cloud_device.items() if k not in ("host", "port")}
                        )
                        break

            await self._create_player(player_id, device_info)

        except Exception:
            _LOGGER.exception("Error processing mDNS discovery for %s", name)

    async def unload(self, is_removed: bool = False) -> None:
        """Clean up on provider unload."""
        await self._cleanup_session()

    # ── Credential cascade ────────────────────────────────────────────

    def _build_borrow_source(self) -> BorrowedCredentialSource | None:
        """
        Build the borrowed-credentials source when an account source is set.

        Borrow mode reads (never writes) the linked yandex_music instance's
        credentials — the linked instance stays the single writer/rotator.
        Returns None for the provider's own login.
        """
        ym_instance = cast("str | None", self.get_setup_value(CONF_YM_INSTANCE))
        if not ym_instance or ym_instance == BORROW_SOURCE_OWN:
            return None
        return BorrowedCredentialSource(self.mass, ym_instance)

    def _build_cascade(self) -> CredentialCascade:
        """
        Build the shared token engine wired to this provider.

        Fast-path validation, x_token → music_token refresh, refresh-token
        rotation and credential clearing live in ``ya_passport_auth.ma``;
        this provider contributes its config keys and the session-side
        hooks. One instance per provider — its internal lock serializes
        rotation across concurrent 401 storms.
        """
        return CredentialCascade(
            keys=KeySpec(
                x_token=CONF_X_TOKEN,
                music_token=CONF_MUSIC_TOKEN,
                refresh_token=CONF_REFRESH_TOKEN,
                remember_session=CONF_REMEMBER_SESSION,
            ),
            get_value=self.get_setup_value,
            set_value=lambda key, value: self._update_setup_data(key, value),
            hooks=CascadeHooks(
                fast_path=self._fast_path,
                apply_music_token=self._apply_music_token,
                apply_credentials=self._apply_credentials,
                post_refresh=self._refresh_session_cookies,
                on_failure=self._cleanup_session,
            ),
            logger=self.logger,
        )

    async def _init_session(self) -> bool:
        """
        Initialize the Yandex HTTP session and run the credential cascade.

        Session objects are created here; the token cascade itself
        (fast-path validation → x_token → music_token refresh →
        refresh-token rotation → clearing) is the shared
        ``ya_passport_auth.ma`` engine, wired to this provider's config
        keys, the live :class:`YandexSession` and the Quasar cookie
        refresh. ``CONF_REMEMBER_SESSION`` semantics are handled by the
        engine (no silent refresh for throw-away sessions).

        :returns: ``True`` when a working session is established, ``False``
            otherwise.
        :raises ResourceTemporarilyUnavailable: Transient failure (network,
            rate limit) while talking to Yandex Passport during silent
            refresh. Stored credentials are preserved so retrying later
            can succeed.
        """
        # Serialize init: concurrent callers (discover_players + mDNS-triggered
        # _create_player) must not race on self._http_session/self._session. The
        # first to arrive runs the cascade; others await and reuse the result.
        async with self._init_lock:
            if self._borrow_source is not None:
                return await self._init_session_borrowed()

            music_token_val = self.get_setup_value(CONF_MUSIC_TOKEN)
            x_token_val = self.get_setup_value(CONF_X_TOKEN)
            refresh_token_val = self.get_setup_value(CONF_REFRESH_TOKEN)

            if not music_token_val and not x_token_val:
                self.logger.warning("No credentials configured, cannot discover devices")
                return False

            # Idempotent: if a previous init already produced a healthy session,
            # reuse it. mDNS-triggered ``_create_player`` may have initialised the
            # session already and started Glagol on top of it — tearing it down
            # here would break those connections.
            if (
                self._session is not None
                and self._http_session is not None
                and not self._http_session.closed
            ):
                return True

            # Close any orphaned HTTP session from a failed init to avoid leaking
            # aiohttp sockets.
            if self._http_session is not None:
                await self._cleanup_session()

            # Dedicated session: Yandex Passport rejects percent-encoded
            # cookies, so we need ``CookieJar(quote_cookie=False)`` — a
            # constructor-only kwarg that can't be applied to ``mass.http_session``.
            # Bare ``aiohttp.ClientSession`` rather than MA's
            # ``create_clientsession`` helper: the helper's connector +
            # ``_default_headers`` override broke Yandex Passport's session
            # refresh redirect chain (HTTP 400) on production stations.
            self._http_session = ClientSession(cookie_jar=CookieJar(quote_cookie=False))
            self._passport_client = PassportClient(session=self._http_session)

            self._session = YandexSession(
                self._http_session,
                self._passport_client,
                x_token=SecretStr(str(x_token_val)) if x_token_val else None,
                music_token=SecretStr(str(music_token_val)) if music_token_val else None,
                refresh_token=SecretStr(str(refresh_token_val)) if refresh_token_val else None,
            )

            return await self._cascade.initialize()

    async def _silent_reauth(self) -> bool:
        """
        Attempt a silent re-auth after a runtime 401/403 from Quasar.

        Returns ``True`` if credentials were rotated and the session was
        refreshed so the caller can retry its operation; ``False`` if silent
        refresh isn't possible with the currently available credentials (no
        x_token, or x_token+refresh_token both rejected).

        One-retry semantics are enforced by the caller (see
        :meth:`_get_speakers_with_reauth`), not here. Concurrent 401s are
        serialized by the shared cascade engine so a storm triggers only
        one rotation.
        """
        if self._borrow_source is not None:
            return await self._silent_reauth_borrowed()
        return await self._cascade.silent_reauth()

    async def _init_session_borrowed(self) -> bool:
        """
        Bootstrap the session from the linked yandex_music instance.

        Read-only: nothing is persisted on either side; the linked instance
        owns rotation. Caller holds ``_init_lock``.

        :raises ResourceTemporarilyUnavailable: The linked instance is not
            loaded yet (start-up ordering) or Passport is unavailable.
        :raises LoginFailed: The linked instance holds no usable credentials.
        """
        assert self._borrow_source is not None
        if (
            self._session is not None
            and self._http_session is not None
            and not self._http_session.closed
        ):
            return True
        if self._http_session is not None:
            await self._cleanup_session()

        music_token = await self._borrow_source.resolve_music_token()
        _, x_token = self._borrow_source.read_tokens()

        self._http_session = ClientSession(cookie_jar=CookieJar(quote_cookie=False))
        self._passport_client = PassportClient(session=self._http_session)
        self._session = YandexSession(
            self._http_session,
            self._passport_client,
            x_token=x_token,
            music_token=music_token,
            refresh_token=None,
        )
        if await self._refresh_session_cookies():
            return True
        await self._cleanup_session()
        return False

    async def _silent_reauth_borrowed(self) -> bool:
        """
        Re-derive Quasar cookies from the linked instance after a 401.

        Never rotates: re-reads the owner's current tokens, re-resolves the
        music token (the source skips values it has seen rejected) and
        refreshes session cookies. A terminal failure means the user must
        re-authenticate the linked Yandex Music provider.
        """
        assert self._borrow_source is not None
        if self._session is None:
            return False
        music_token = await self._borrow_source.resolve_music_token()
        _, x_token = self._borrow_source.read_tokens()
        if x_token is not None:
            self._session.x_token = x_token
        self._session.music_token = music_token
        return await self._refresh_session_cookies()

    async def _fast_path(self) -> bool:
        """Validate the stored music_token + x_token pair as-is."""
        if self._session is None:
            return False
        if await self._session.login_token():
            await self._session.ensure_music_token()
            return True
        return False

    async def _apply_music_token(self, music_token: SecretStr) -> None:
        """Apply a freshly refreshed music token to the live session."""
        if self._session is not None:
            self._session.music_token = music_token

    async def _apply_credentials(self, creds: Credentials) -> None:
        """Apply a rotated credential triple to the live session."""
        if self._session is not None:
            self._session.x_token = creds.x_token
            self._session.music_token = creds.music_token
            self._session.refresh_token = creds.refresh_token

    async def _refresh_session_cookies(self) -> bool:
        """
        Refresh Quasar session cookies after a token refresh/rotation.

        Without fresh cookies the next Quasar request would 401 even though
        the stored tokens are fine — the cascade treats a ``False`` return
        as a failed step. No live session (mDNS not up yet) is a no-op.
        """
        if self._session is None:
            return True
        return bool(await self._session.login_token())

    async def _cleanup_session(self) -> None:
        """Close HTTP session and PassportClient."""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        self._http_session = None
        self._passport_client = None
        self._session = None

    async def _get_speakers_with_reauth(self) -> list[dict[str, Any]]:
        """Fetch Quasar speakers with one silent-reauth retry on 401/403."""
        assert self._quasar is not None
        try:
            return await self._quasar.get_speakers()
        except RuntimeError as err:
            msg = str(err)
            if ("401" in msg or "403" in msg) and await self._silent_reauth():
                self.logger.info("Retrying Quasar get_speakers after silent reauth")
                return await self._quasar.get_speakers()
            raise

    async def _create_player(self, player_id: str, device_info: dict[str, Any]) -> None:
        """Create and register a new YandexStationPlayer."""
        try:
            if not self._session:
                # Lazily initialize session for mDNS events arriving before discover_players
                if not await self._init_session():
                    self.logger.warning("Session not initialized, skipping player creation")
                    return

            assert self._session is not None  # guaranteed by _init_session()
            assert self._passport_client is not None  # guaranteed by _init_session()

            # Skip if already registered
            existing = self.mass.players.get_player(player_id)
            if existing is not None:
                return

            glagol = YandexGlagol(self._session, self._passport_client, device_info)

            player = YandexStationPlayer(
                provider=self,
                player_id=player_id,
                device_info=device_info,
                glagol=glagol,
            )
            # Register BEFORE starting Glagol.  The WS connect callback can
            # fire `update_handler` very quickly, and the resulting
            # `player.update_state()` would otherwise run for an unknown
            # player and trigger queue/state side-effects on something the
            # players controller doesn't know about yet.
            await self.mass.players.register_or_update(player)
            # Only start Glagol if host/port are available (mDNS or glagol API)
            host = device_info.get("host")
            port = device_info.get("port")
            if host and port:
                self.logger.info("Starting Glagol for %s at %s:%s", player_id, host, port)
                await player.async_setup()
            else:
                self.logger.info("No host/port for %s — cloud-only", player_id)

        except Exception:
            self.logger.exception("Failed to create player %s", player_id)
        finally:
            self._pending_discoveries.discard(player_id)
