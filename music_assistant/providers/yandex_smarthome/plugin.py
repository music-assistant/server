"""
Yandex Smart Home Plugin Provider.

Bridges Music Assistant players to the Yandex Smart Home ecosystem,
allowing Alice voice control of playback, volume, and transport.

The plugin:
1. Listens for MA player events (added, removed, updated)
2. Exposes them as Yandex Smart Home media_device devices
3. Handles capability actions (on_off, volume, pause) from Alice
4. Reports state changes back to Yandex

Connection modes:
- Cloud: WebSocket relay through yaha-cloud.ru (no public URL needed)
- Cloud Plus: Private skill via yaha-cloud.ru relay (custom Yandex.Dialogs skill)
- Direct: HTTP endpoints on MA webserver that Yandex calls directly (requires public URL)
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ya_dialogs_api import SecretStr

from music_assistant.models.plugin import PluginProvider

from .cloud import CloudManager
from .constants import (
    CLOUD_CALLBACK_URL,
    CONF_CLOUD_CONNECTION_TOKEN,
    CONF_CLOUD_INSTANCE_ID,
    CONF_CLOUD_INSTANCE_PASSWORD,
    CONF_CONNECTION_TYPE,
    CONF_DIRECT_ACCESS_TOKEN,
    CONF_DIRECT_CLIENT_SECRET,
    CONF_EXPOSED_PLAYERS,
    CONF_EXPOSED_PLAYLISTS,
    CONF_INSTANCE_NAME,
    CONF_SKILL_ID,
    CONF_SKILL_TOKEN,
    CONNECTION_TYPE_CLOUD,
    CONNECTION_TYPE_CLOUD_PLUS,
    CONNECTION_TYPE_DIRECT,
    MAX_INPUT_SOURCES,
    YANDEX_DIALOGS_CALLBACK_BASE,
)
from .direct import DirectConnectionHandler
from .handlers import (
    build_response,
    handle_device_list,
    handle_devices_action,
    handle_devices_query,
    handle_user_unlink,
    parse_action_payload,
)
from .notifier import StateNotifier
from .schema import CloudRequest


class YandexSmartHomePlugin(PluginProvider):
    """
    Plugin provider that exposes MA players to Yandex Alice via Smart Home API.

    Follows the same pattern as the HASS plugin provider: subscribes to MA events,
    maintains a mapping of MA players to Yandex Smart Home devices, and handles
    capability actions from Alice by translating them to MA player commands.
    """

    _cloud_manager: CloudManager | None = None
    _state_notifier: StateNotifier | None = None
    _direct_handler: DirectConnectionHandler | None = None
    _cloud_task: Any = None
    _user_id: str = ""

    async def handle_async_init(self) -> None:
        """Handle async initialization of the plugin."""
        self._connection_type = str(
            self.config.get_value(CONF_CONNECTION_TYPE) or CONNECTION_TYPE_CLOUD
        )
        self._instance_name = str(self.config.get_value(CONF_INSTANCE_NAME) or "Music Assistant")
        cloud_token_raw = str(self.config.get_value(CONF_CLOUD_INSTANCE_PASSWORD) or "")
        self._cloud_token: SecretStr | None = (
            SecretStr(cloud_token_raw) if cloud_token_raw else None
        )
        conn_token_raw = str(self.config.get_value(CONF_CLOUD_CONNECTION_TOKEN) or "")
        self._connection_token: SecretStr | None = (
            SecretStr(conn_token_raw) if conn_token_raw else None
        )
        self._cloud_instance_id = str(self.config.get_value(CONF_CLOUD_INSTANCE_ID) or "")
        self._skill_id = str(self.config.get_value(CONF_SKILL_ID) or "")
        skill_token_raw = str(self.config.get_value(CONF_SKILL_TOKEN) or "")
        self._skill_token: SecretStr | None = (
            SecretStr(skill_token_raw) if skill_token_raw else None
        )
        self._direct_access_token = str(self.config.get_value(CONF_DIRECT_ACCESS_TOKEN) or "")
        self._direct_client_secret = str(self.config.get_value(CONF_DIRECT_CLIENT_SECRET) or "")

        # Parse exposed players filter
        exposed_raw = self.config.get_value(CONF_EXPOSED_PLAYERS) or []
        if isinstance(exposed_raw, str):
            exposed_raw = [x.strip() for x in exposed_raw.split(",") if x.strip()]
        elif isinstance(exposed_raw, list):
            exposed_raw = [str(x) for x in exposed_raw if x]
        else:
            exposed_raw = []
        self._exposed_ids: set[str] | None = set(exposed_raw) if exposed_raw else None

        # Parse exposed playlists (URIs) — capped at MAX_INPUT_SOURCES.
        playlists_raw = self.config.get_value(CONF_EXPOSED_PLAYLISTS) or []
        if isinstance(playlists_raw, str):
            playlists_raw = [x.strip() for x in playlists_raw.split(",") if x.strip()]
        elif isinstance(playlists_raw, list):
            playlists_raw = [str(x) for x in playlists_raw if x]
        else:
            playlists_raw = []
        if len(playlists_raw) > MAX_INPUT_SOURCES:
            self.logger.warning(
                "Exposed playlists count (%d) exceeds cap %d; truncating",
                len(playlists_raw),
                MAX_INPUT_SOURCES,
            )
            playlists_raw = playlists_raw[:MAX_INPUT_SOURCES]
        self._exposed_playlists: tuple[str, ...] = tuple(playlists_raw)

        self.logger.info(
            "Yandex Smart Home plugin init (mode=%s, name=%s)",
            self._connection_type,
            self._instance_name,
        )

    async def loaded_in_mass(self) -> None:
        """
        Call after the provider has been loaded.

        Starts cloud WebSocket connection and state notifier.
        """
        self.logger.info("Yandex Smart Home plugin loaded")

        if self._connection_type in (CONNECTION_TYPE_CLOUD, CONNECTION_TYPE_CLOUD_PLUS):
            await self._start_cloud_mode()
        elif self._connection_type == CONNECTION_TYPE_DIRECT:
            await self._start_direct_mode()
        else:
            self.logger.error("Unknown connection type: %s", self._connection_type)

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        self.logger.info("Yandex Smart Home plugin unloading (removed=%s)", is_removed)

        if self._state_notifier:
            await self._state_notifier.stop()
            self._state_notifier = None

        if self._direct_handler:
            self._direct_handler.unregister_routes()
            self._direct_handler = None

        if self._cloud_manager:
            await self._cloud_manager.disconnect()
            self._cloud_manager = None

        if self._cloud_task:
            cloud_task = self._cloud_task
            self._cloud_task = None
            if not cloud_task.done():
                cloud_task.cancel()

    async def _start_cloud_mode(self) -> None:
        """Initialize and start cloud relay connection + state notifier."""
        if not self._connection_token or not self._connection_token.get_secret():
            self.logger.error(
                "Cloud connection token not configured — "
                "register an instance at yaha-cloud.ru and set the connection token"
            )
            return

        # Validate Cloud Plus credentials before starting any tasks
        if self._connection_type == CONNECTION_TYPE_CLOUD_PLUS:
            if not self._skill_id or not self._skill_token or not self._skill_token.get_secret():
                self.logger.error("Cloud Plus mode requires skill_id and skill_token")
                return

        # Validate cloud password (used for callback auth in basic cloud mode)
        if self._connection_type == CONNECTION_TYPE_CLOUD and (
            not self._cloud_token or not self._cloud_token.get_secret()
        ):
            self.logger.error(
                "Cloud instance password not configured — "
                "set the password from yaha-cloud.ru instance settings"
            )
            return

        # Determine user_id once — used in both API responses and state callbacks
        self._user_id = self._cloud_instance_id or self._instance_name

        session = self.mass.http_session

        # Cloud WebSocket manager
        self._cloud_manager = CloudManager(
            session=session,
            connection_token=self._connection_token,
            on_request=self._handle_cloud_request,
            logger=self.logger,
        )
        self._cloud_task = self.mass.create_task(
            self._cloud_manager.connect(),
            task_id="yandex_smarthome_cloud",
        )

        # State notifier — different callback URL/auth for cloud_plus
        if self._connection_type == CONNECTION_TYPE_CLOUD_PLUS:
            assert self._skill_token is not None  # validated above
            callback_url = f"{YANDEX_DIALOGS_CALLBACK_BASE}/{self._skill_id}/callback/state"
            auth_header = {"Authorization": f"OAuth {self._skill_token.get_secret()}"}
        else:
            assert self._cloud_token is not None  # validated above
            callback_url = f"{CLOUD_CALLBACK_URL}/state"
            auth_header = {"Authorization": f"Bearer {self._cloud_token.get_secret()}"}

        self._state_notifier = StateNotifier(
            mass=self.mass,
            session=session,
            user_id=self._user_id,
            callback_url=callback_url,
            auth_header=auth_header,
            logger=self.logger,
            exposed_ids=self._exposed_ids,
            playlist_uris=self._exposed_playlists,
        )
        await self._state_notifier.start()

    async def _start_direct_mode(self) -> None:
        """
        Initialize direct connection mode — HTTP endpoints + state notifier.

        Two-stage: HTTP routes are registered as soon as ``direct_client_secret``
        is available (auto-generated when the user opens the config form), so
        Yandex's backend-validation step during ``request_deploy`` can reach
        them before the skill is created. The state notifier (outgoing
        callbacks to Yandex) only starts once ``skill_id``/``skill_token``
        are populated by a successful auto-create — there is nothing to
        report state to before that point.
        """
        if not self._direct_client_secret:
            self.logger.error("Direct mode requires a client secret for OAuth account linking")
            return

        self._user_id = self._instance_name

        def _on_token_created(token: str) -> None:
            """Persist new access token generated during OAuth flow."""
            self._direct_access_token = token
            self._update_config_value(CONF_DIRECT_ACCESS_TOKEN, token, encrypted=True)

        self._direct_handler = DirectConnectionHandler(
            mass=self.mass,
            user_id=self._user_id,
            access_token=self._direct_access_token,
            client_secret=self._direct_client_secret,
            exposed_ids=self._exposed_ids,
            logger=self.logger,
            on_token_created=_on_token_created,
            playlist_uris=self._exposed_playlists,
        )
        self._direct_handler.register_routes()

        # State notifier needs skill_id + skill_token to push state callbacks
        # to Yandex — these only exist after a successful auto-create AND the
        # user pasting the OAuth token. Skip silently if either is missing;
        # this is the normal "first run" / "skill created but token not yet
        # pasted" state.
        has_skill_id = bool(self._skill_id)
        skill_token = self._skill_token
        has_skill_token = skill_token is not None and bool(skill_token.get_secret())
        if has_skill_id and has_skill_token and skill_token is not None:
            session = self.mass.http_session
            callback_url = f"{YANDEX_DIALOGS_CALLBACK_BASE}/{self._skill_id}/callback/state"
            auth_header = {"Authorization": f"OAuth {skill_token.get_secret()}"}

            self._state_notifier = StateNotifier(
                mass=self.mass,
                session=session,
                user_id=self._user_id,
                callback_url=callback_url,
                auth_header=auth_header,
                logger=self.logger,
                exposed_ids=self._exposed_ids,
                playlist_uris=self._exposed_playlists,
            )
            await self._state_notifier.start()
        else:
            missing = []
            if not has_skill_id:
                missing.append("Skill ID")
            if not has_skill_token:
                missing.append("Skill OAuth Token")
            self.logger.info(
                "Direct mode: HTTP routes registered, but state notifier is "
                "idle (missing: %s). Open the plugin settings: 'Auto-create "
                "Smart Home skill' fills the Skill ID for you, then open the "
                "OAuth-token URL shown in the form, approve access, and paste "
                "the resulting access_token into 'Skill OAuth Token'.",
                " + ".join(missing),
            )

        self.logger.info("Direct connection mode started")

    async def _handle_cloud_request(self, request: CloudRequest) -> dict[str, Any]:
        """Route incoming cloud WS request to the appropriate handler."""
        action = request.action
        request_id = request.request_id
        message = request.message or {}

        # Normalize action path — relay may send with or without /v1.0 prefix
        normalized = action.removeprefix("/v1.0")

        self.logger.debug(
            "Cloud request: action=%s, request_id=%s",
            action,
            request_id,
        )

        try:
            if normalized == "/user/devices":
                device_list = await handle_device_list(
                    self.mass,
                    self._user_id,
                    exposed_ids=self._exposed_ids,
                    playlist_uris=self._exposed_playlists,
                )
                return build_response(request_id, asdict(device_list))

            if normalized == "/user/devices/query":
                device_ids = [
                    device_id
                    for d in message.get("devices", [])
                    if isinstance(d, dict) and (device_id := d.get("id"))
                ]
                states = await handle_devices_query(
                    self.mass,
                    device_ids,
                    exposed_ids=self._exposed_ids,
                    playlist_uris=self._exposed_playlists,
                )
                return build_response(request_id, asdict(states))

            if normalized == "/user/devices/action":
                action_payload = parse_action_payload(message)
                action_result = await handle_devices_action(
                    self.mass,
                    action_payload,
                    exposed_ids=self._exposed_ids,
                    playlist_uris=self._exposed_playlists,
                )
                return build_response(request_id, asdict(action_result))

            if normalized == "/user/unlink":
                unlink_result = await handle_user_unlink()
                return build_response(request_id, unlink_result)

            self.logger.warning("Unknown cloud action: %s", action)
            return build_response(request_id, {})

        except Exception:
            self.logger.exception("Error handling cloud request: %s", action)
            return build_response(request_id, {})
