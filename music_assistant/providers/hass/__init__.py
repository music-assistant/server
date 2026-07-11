"""
Home Assistant Plugin for Music Assistant.

The plugin is the core of all communication to/from Home Assistant and
responsible for maintaining the WebSocket API connection to HA.
Also, the Music Assistant integration within HA will relay its own api
communication over the HA api for more flexibility as well as security.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING, cast

import shortuuid
from hass_client import HomeAssistantClient
from hass_client.exceptions import BaseHassClientError
from hass_client.utils import (
    base_url,
    get_auth_url,
    get_long_lived_token,
    get_token,
    get_websocket_url,
)
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MusicAssistantError,
    SetupFailedError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items.audio_format import AudioFormat
from music_assistant_models.player_control import PlayerControl
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import MASS_LOGO_ONLINE, VERBOSE_LOG_LEVEL
from music_assistant.helpers.auth import AuthenticationHelper
from music_assistant.helpers.json import SerializableType
from music_assistant.helpers.util import try_parse_int
from music_assistant.models.plugin import PluginProvider

from .constants import OFF_STATES, MediaPlayerEntityFeature

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from hass_client.models import CompressedState, Device, EntityStateEvent, State
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

DOMAIN = "hass"
CONF_URL = "url"
CONF_AUTH_TOKEN = "token"
CONF_ACTION_AUTH = "auth"
CONF_VERIFY_SSL = "verify_ssl"
CONF_POWER_CONTROLS = "power_controls"
CONF_MUTE_CONTROLS = "mute_controls"
CONF_VOLUME_CONTROLS = "volume_controls"
CONF_TTS_ENTITY = "tts_entity"
CONF_AI_TASK_ENTITY = "ai_task_entity"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return HomeAssistantProvider(mass, manifest, config, set())


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # config flow auth action/step (authenticate button clicked)
    if action == CONF_ACTION_AUTH and values:
        hass_url = str(values[CONF_URL]).strip()
        async with AuthenticationHelper(mass, str(values["session_id"])) as auth_helper:
            client_id = base_url(auth_helper.callback_url)
            auth_url = get_auth_url(
                hass_url,
                auth_helper.callback_url,
                client_id=client_id,
                state=values["session_id"],
            )
            result = await auth_helper.authenticate(auth_url)
        if result["state"] != values["session_id"]:
            msg = "session id mismatch"
            raise LoginFailed(msg)
        # get access token after auth was a success
        token_details = await get_token(hass_url, result["code"], client_id=client_id)
        # register for a long lived token
        long_lived_token = await get_long_lived_token(
            hass_url,
            token_details["access_token"],
            client_name=f"Music Assistant {shortuuid.random(6)}",
            client_icon=MASS_LOGO_ONLINE,
            lifespan=365 * 2,
        )
        # set the retrieved token on the values object to pass along
        values[CONF_AUTH_TOKEN] = long_lived_token

    base_entries: tuple[ConfigEntry, ...]
    if mass.running_as_hass_addon:
        # on supervisor, we use the internal url
        # token set to None for auto retrieval
        base_entries = (
            ConfigEntry(
                key=CONF_URL,
                type=ConfigEntryType.STRING,
                label=CONF_URL,
                required=True,
                default_value="http://supervisor/core/api",
                value="http://supervisor/core/api",
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_AUTH_TOKEN,
                type=ConfigEntryType.STRING,
                label=CONF_AUTH_TOKEN,
                required=False,
                default_value=None,
                value=None,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_VERIFY_SSL,
                type=ConfigEntryType.BOOLEAN,
                label=CONF_VERIFY_SSL,
                required=False,
                default_value=False,
                hidden=True,
            ),
        )
    else:
        # manual configuration
        base_entries = (
            ConfigEntry(
                key=CONF_URL,
                type=ConfigEntryType.STRING,
                required=True,
                value=cast("str", values.get(CONF_URL)) if values else None,
            ),
            ConfigEntry(
                key=CONF_ACTION_AUTH,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_AUTH,
                depends_on=CONF_URL,
                required=False,
            ),
            ConfigEntry(
                key=CONF_AUTH_TOKEN,
                type=ConfigEntryType.SECURE_STRING,
                depends_on=CONF_URL,
                value=cast("str", values.get(CONF_AUTH_TOKEN)) if values else None,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_VERIFY_SSL,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                advanced=True,
                default_value=True,
            ),
        )

    # append player controls entries (if we have an active instance)
    if instance_id and (hass_prov := mass.get_provider(instance_id)) and hass_prov.available:
        hass_prov = cast("HomeAssistantProvider", hass_prov)
        return (
            *base_entries,
            *(await _get_config_entries(hass_prov.hass)),
        )

    return (
        *base_entries,
        ConfigEntry(
            key=CONF_POWER_CONTROLS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            label=CONF_POWER_CONTROLS,
            default_value=[],
        ),
        ConfigEntry(
            key=CONF_VOLUME_CONTROLS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            label=CONF_VOLUME_CONTROLS,
            default_value=[],
        ),
        ConfigEntry(
            key=CONF_MUTE_CONTROLS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            label=CONF_MUTE_CONTROLS,
            default_value=[],
        ),
        ConfigEntry(
            key=CONF_TTS_ENTITY,
            type=ConfigEntryType.STRING,
            label=CONF_TTS_ENTITY,
            required=False,
        ),
        ConfigEntry(
            key=CONF_AI_TASK_ENTITY,
            type=ConfigEntryType.STRING,
            label=CONF_AI_TASK_ENTITY,
            required=False,
        ),
    )


async def _get_config_entries(hass: HomeAssistantClient) -> tuple[ConfigEntry, ...]:
    """Return the (entity based) config entries."""
    all_power_entities: list[ConfigValueOption] = []
    all_mute_entities: list[ConfigValueOption] = []
    all_volume_entities: list[ConfigValueOption] = []
    if not hass.connected:
        return ()
    states = await hass.get_states()
    tts_entities, ai_task_entities = _get_feature_entity_options(states)
    for state in states:
        entity_platform = state["entity_id"].split(".")[0]
        if entity_platform in ("tts", "ai_task"):
            continue
        if "friendly_name" not in state["attributes"]:
            name = state["entity_id"]
        else:
            name = f"{state['attributes']['friendly_name']} ({state['entity_id']})"

        if entity_platform in ("switch", "input_boolean"):
            # simple on/off controls are suitable as power and mute controls
            all_power_entities.append(ConfigValueOption(state["entity_id"], title=name))
            all_mute_entities.append(ConfigValueOption(state["entity_id"], title=name))
            continue
        if entity_platform in ("number", "input_number"):
            # number and input_number are very similar, both are suitable for volume control
            all_volume_entities.append(ConfigValueOption(state["entity_id"], title=name))
            continue
        # media player can be used as control, depending on features
        if entity_platform != "media_player":
            continue
        if "mass_player_type" in state["attributes"]:
            # filter out mass players
            continue
        supported_features = MediaPlayerEntityFeature(state["attributes"]["supported_features"])
        if MediaPlayerEntityFeature.VOLUME_MUTE in supported_features:
            all_mute_entities.append(ConfigValueOption(state["entity_id"], title=name))
        if MediaPlayerEntityFeature.VOLUME_SET in supported_features:
            all_volume_entities.append(ConfigValueOption(state["entity_id"], title=name))
        if (
            MediaPlayerEntityFeature.TURN_ON in supported_features
            and MediaPlayerEntityFeature.TURN_OFF in supported_features
        ):
            all_power_entities.append(ConfigValueOption(state["entity_id"], title=name))
    all_power_entities.sort(key=lambda x: x.title or "")
    all_mute_entities.sort(key=lambda x: x.title or "")
    all_volume_entities.sort(key=lambda x: x.title or "")
    entries: list[ConfigEntry] = [
        ConfigEntry(
            key=CONF_POWER_CONTROLS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            required=True,
            options=all_power_entities,
            default_value=[],
            category="player_controls",
        ),
        ConfigEntry(
            key=CONF_VOLUME_CONTROLS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            required=True,
            options=all_volume_entities,
            default_value=[],
            category="player_controls",
        ),
        ConfigEntry(
            key=CONF_MUTE_CONTROLS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            required=True,
            options=all_mute_entities,
            default_value=[],
            category="player_controls",
        ),
        ConfigEntry(
            key=CONF_TTS_ENTITY,
            type=ConfigEntryType.STRING,
            required=False,
            options=tts_entities,
            default_value=tts_entities[0].value if tts_entities else None,
            category="features",
        ),
        ConfigEntry(
            key=CONF_AI_TASK_ENTITY,
            type=ConfigEntryType.STRING,
            required=False,
            options=ai_task_entities,
            default_value=ai_task_entities[0].value if ai_task_entities else None,
            category="features",
        ),
    ]
    return tuple(entries)


class HomeAssistantProvider(PluginProvider):
    """Home Assistant Plugin for Music Assistant."""

    hass: HomeAssistantClient
    _listen_task: asyncio.Task[None] | None = None
    _player_controls: dict[str, PlayerControl] | None = None
    _tts_entity_id: str | None = None
    _ai_task_entity_id: str | None = None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the plugin."""
        self._player_controls = {}
        url = get_websocket_url(self.config.get_value(CONF_URL))
        token = self.config.get_value(CONF_AUTH_TOKEN)
        logging.getLogger("hass_client").setLevel(self.logger.level + 10)
        ssl = bool(self.config.get_value(CONF_VERIFY_SSL))
        http_session = self.mass.http_session if ssl else self.mass.http_session_no_ssl
        self.hass = HomeAssistantClient(url, token, http_session)
        try:
            await self.hass.connect()
        except BaseHassClientError as err:
            err_msg = str(err) or err.__class__.__name__
            raise SetupFailedError(err_msg) from err
        self._listen_task = self.mass.create_task(self._hass_listener())
        try:
            await self._resolve_feature_entities()
        except asyncio.CancelledError:
            await self._cleanup_failed_init()
            raise
        except BaseHassClientError as err:
            await self._cleanup_failed_init()
            err_msg = str(err) or err.__class__.__name__
            raise SetupFailedError(err_msg) from err
        except Exception:
            await self._cleanup_failed_init()
            raise

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await self._register_player_controls()

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        # unregister all player controls
        if self._player_controls:
            for entity_id in self._player_controls:
                self.mass.players.remove_player_control(entity_id)
        await self._disconnect_hass()

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this provider to include in diagnostics reports."""
        return {
            "connected": self.hass.connected,
            "ha_version": self.hass.version,
            "listener_active": self._listen_task is not None and not self._listen_task.done(),
            "player_controls": len(self._player_controls) if self._player_controls else 0,
        }

    async def get_device_by_connection(
        self,
        connection_value: str,
        connection_type: str = "mac",
    ) -> Device | None:
        """
        Get device details from Home Assistant by connection type and value.

        :param connection_value: The connection value (e.g. MAC address).
        :param connection_type: The connection type (default: 'mac').
        """
        devices = await self.hass.get_device_registry()
        for device in devices:
            for connection in device.get("connections", []):
                if (
                    len(connection) == 2
                    and connection[0] == connection_type
                    and connection[1].lower() == connection_value.lower()
                ):
                    return device
        return None

    async def get_user_details(self, ha_user_id: str) -> tuple[str | None, str | None, str | None]:
        """
        Get user username, display name and avatar URL from Home Assistant.

        Looks up the user in config/auth/list for username, and the person entity
        for display name and picture URL.

        :param ha_user_id: Home Assistant user ID.
        :return: Tuple of (username, display_name, avatar_url) or all None if not found.
        """
        try:
            username: str | None = None
            display_name: str | None = None
            avatar_url: str | None = None

            # Get username from config/auth/list (admin endpoint, we have admin access)
            try:
                users = await self.hass.send_command("config/auth/list")
                for user in users or []:
                    if user.get("id") == ha_user_id:
                        username = user.get("username")
                        # Also get name as fallback display name
                        if not display_name:
                            display_name = user.get("name")
                        break
            except Exception as err:
                self.logger.log(VERBOSE_LOG_LEVEL, "Failed to get HA user list: %s", err)

            # Get external URL for building avatar URL
            ha_url: str | None = None
            try:
                network_urls = await self.hass.send_command("network/url")
                if network_urls:
                    ha_url = network_urls.get("external") or network_urls.get("internal")
            except Exception as err:
                self.logger.log(VERBOSE_LOG_LEVEL, "Failed to get HA network URLs: %s", err)

            # Find person linked to this HA user ID for display name and avatar
            try:
                persons = await self.hass.send_command("person/list")
                # person/list returns {storage: [...], config: [...]}
                all_persons = (persons.get("storage") or []) + (persons.get("config") or [])
                for person in all_persons:
                    if person.get("user_id") == ha_user_id:
                        # Person name takes priority for display name
                        if person_name := person.get("name"):
                            display_name = person_name
                        if (person_picture := person.get("picture")) and ha_url:
                            avatar_url = f"{ha_url.rstrip('/')}{person_picture}"
                        break
            except Exception as err:
                self.logger.log(VERBOSE_LOG_LEVEL, "Failed to get HA person details: %s", err)

            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "get_user_details for %s: username=%s, display_name=%s, avatar_url=%s",
                ha_user_id,
                username,
                display_name,
                avatar_url,
            )
            return username, display_name, avatar_url
        except Exception as err:
            self.logger.warning("Failed to get HA user details: %s", err)
            return None, None, None

    async def resolve_image(self, path: str) -> bytes:
        """Resolve an image from an image path."""
        ha_url, headers, http_session = self._get_ha_http()
        async with http_session.get(f"{ha_url}{path}", headers=headers) as response:
            response.raise_for_status()
            return await response.read()

    async def ai_query(self, query: str) -> str:
        """Handle an AI query via Home Assistant's ai_task service."""
        if self._ai_task_entity_id is None:
            raise UnsupportedFeaturedException("AI Task entity is not configured")
        result = await self.hass.send_command(
            "call_service",
            domain="ai_task",
            service="generate_data",
            service_data={
                "task_name": "music_assistant",
                "instructions": query,
                "entity_id": self._ai_task_entity_id,
            },
            return_response=True,
        )
        response = result.get("response", {}) if isinstance(result, dict) else {}
        data = response.get("data") if isinstance(response, dict) else None
        if not data:
            msg = f"AI Task returned no data in response: {result}"
            raise MusicAssistantError(msg)
        return str(data)

    async def get_tts_message(self, message: str, language: str | None = None) -> StreamDetails:
        """Handle text-to-speech via Home Assistant's REST API."""
        if self._tts_entity_id is None:
            raise UnsupportedFeaturedException("TTS entity is not configured")
        ha_url, headers, http_session = self._get_ha_http()
        payload: dict[str, str] = {"engine_id": self._tts_entity_id, "message": message}
        if language:
            payload["language"] = language
        async with http_session.post(
            f"{ha_url}/api/tts_get_url", headers=headers, json=payload
        ) as response:
            response.raise_for_status()
            data = await response.json()
        url = str(data["url"])
        return StreamDetails(
            provider=self.instance_id,
            item_id=url,
            audio_format=AudioFormat(content_type=ContentType.MP3),
            media_type=MediaType.SOUND_EFFECT,
            stream_type=StreamType.HTTP,
            path=url,
        )

    async def _hass_listener(self) -> None:
        """Start listening on the HA websockets."""
        try:
            # start listening will block until the connection is lost/closed
            await self.hass.start_listening()
        except BaseHassClientError as err:
            self.logger.warning("Connection to HA lost due to error: %s", err)
        self.logger.info("Connection to HA lost. Connection will be automatically retried later.")
        # schedule a reload of the provider
        self.available = False
        self.mass.call_later(5, self.mass.load_provider, self.instance_id, allow_retry=True)

    def _on_entity_state_update(self, event: EntityStateEvent) -> None:
        """Handle Entity State event."""
        if entity_additions := event.get("a"):
            for entity_id, state in entity_additions.items():
                self._update_control_from_state_msg(entity_id, state)
        if entity_changes := event.get("c"):
            for entity_id, state_diff in entity_changes.items():
                if "+" not in state_diff:
                    continue
                self._update_control_from_state_msg(entity_id, state_diff["+"])

    async def _register_player_controls(self) -> None:
        """Register all player controls."""
        power_controls = cast("list[str]", self.config.get_value(CONF_POWER_CONTROLS))
        mute_controls = cast("list[str]", self.config.get_value(CONF_MUTE_CONTROLS))
        volume_controls = cast("list[str]", self.config.get_value(CONF_VOLUME_CONTROLS))
        control_entity_ids: set[str] = {
            *power_controls,
            *mute_controls,
            *volume_controls,
        }
        hass_states = {
            state["entity_id"]: state
            for state in await self.hass.get_states()
            if state["entity_id"] in control_entity_ids
        }
        assert self._player_controls is not None  # for type checking
        for entity_id in control_entity_ids:
            entity_platform = entity_id.split(".")[0]
            hass_state = hass_states.get(entity_id)
            if hass_state and (friendly_name := hass_state["attributes"].get("friendly_name")):
                name = f"{friendly_name} ({entity_id})"
            else:
                name = entity_id
            control = PlayerControl(
                id=entity_id,
                provider=self.instance_id,
                name=name,
            )
            if entity_id in power_controls:
                control.supports_power = True
                control.power_state = hass_state["state"] not in OFF_STATES if hass_state else False
                control.power_on = partial(self._handle_player_control_power_on, entity_id)
                control.power_off = partial(self._handle_player_control_power_off, entity_id)
            if entity_id in volume_controls:
                control.supports_volume = True
                if not hass_state:
                    control.volume_level = 0
                elif entity_platform == "media_player":
                    control.volume_level = int(
                        hass_state["attributes"].get("volume_level", 0) * 100
                    )
                else:
                    control.volume_level = try_parse_int(hass_state["state"]) or 0
                control.volume_set = partial(self._handle_player_control_volume_set, entity_id)
            if entity_id in mute_controls:
                control.supports_mute = True
                if not hass_state:
                    control.volume_muted = False
                elif entity_platform == "media_player":
                    control.volume_muted = hass_state["attributes"].get("volume_muted")
                elif hass_state:
                    control.volume_muted = hass_state["state"] not in OFF_STATES
                else:
                    control.volume_muted = False
                control.mute_set = partial(self._handle_player_control_mute_set, entity_id)
            self._player_controls[entity_id] = control
            await self.mass.players.register_player_control(control)
        # register for entity state updates
        await self.hass.subscribe_entities(self._on_entity_state_update, list(control_entity_ids))

    async def _handle_player_control_power_on(self, entity_id: str) -> None:
        """Handle powering on the playercontrol."""
        await self.hass.call_service(
            domain="homeassistant",
            service="turn_on",
            target={"entity_id": entity_id},
        )

    async def _handle_player_control_power_off(self, entity_id: str) -> None:
        """Handle powering off the playercontrol."""
        await self.hass.call_service(
            domain="homeassistant",
            service="turn_off",
            target={"entity_id": entity_id},
        )

    async def _handle_player_control_mute_set(self, entity_id: str, muted: bool) -> None:
        """Handle muting the playercontrol."""
        if entity_id.startswith("media_player."):
            await self.hass.call_service(
                domain="media_player",
                service="volume_mute",
                service_data={"is_volume_muted": muted},
                target={"entity_id": entity_id},
            )
        else:
            await self.hass.call_service(
                domain="homeassistant",
                service="turn_off" if muted else "turn_on",
                target={"entity_id": entity_id},
            )

    async def _handle_player_control_volume_set(self, entity_id: str, volume_level: int) -> None:
        """Handle setting volume on the playercontrol."""
        domain = entity_id.split(".", 1)[0]

        if domain == "media_player":
            await self.hass.call_service(
                domain=domain,
                service="volume_set",
                service_data={"volume_level": volume_level / 100},
                target={"entity_id": entity_id},
            )
            return

        # At this point, `set_value` will work for both `number` or `input_number`
        await self.hass.call_service(
            domain=domain,
            service="set_value",
            target={"entity_id": entity_id},
            service_data={"value": volume_level},
        )

    def _update_control_from_state_msg(self, entity_id: str, state: CompressedState) -> None:
        """Update PlayerControl from state(update) message."""
        if self._player_controls is None:
            return
        if not (player_control := self._player_controls.get(entity_id)):
            return
        entity_platform = entity_id.split(".", maxsplit=1)[0]
        if "s" in state:
            # state changed
            if player_control.supports_power:
                player_control.power_state = state["s"] not in OFF_STATES
            if player_control.supports_mute and entity_platform != "media_player":
                player_control.volume_muted = state["s"] not in OFF_STATES
            if player_control.supports_volume and entity_platform != "media_player":
                player_control.volume_level = try_parse_int(state["s"]) or 0
        if "a" in state and (attributes := state["a"]):
            if player_control.supports_volume and "volume_level" in attributes:
                player_control.volume_level = int(attributes.get("volume_level", 0) * 100)
            if player_control.supports_mute and "is_volume_muted" in attributes:
                player_control.volume_muted = attributes.get("is_volume_muted")
        self.mass.players.update_player_control(entity_id)

    def _get_ha_http(self) -> tuple[str, dict[str, str], ClientSession]:
        """Return HA base URL (without trailing /api), auth headers, and the HTTP session."""
        ha_url = cast("str", self.config.get_value(CONF_URL)).rstrip("/")
        ha_url = ha_url.removesuffix("/api")
        token = self.config.get_value(CONF_AUTH_TOKEN) or os.environ.get("HASSIO_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        ssl = bool(self.config.get_value(CONF_VERIFY_SSL))
        http_session = self.mass.http_session if ssl else self.mass.http_session_no_ssl
        return ha_url, headers, http_session

    async def _disconnect_hass(self) -> None:
        """Stop listening for Home Assistant events and disconnect the client."""
        if listen_task := self._listen_task:
            self._listen_task = None
            if not listen_task.done():
                listen_task.cancel()
            with suppress(asyncio.CancelledError):
                await listen_task
        await self.hass.disconnect()

    async def _cleanup_failed_init(self) -> None:
        """Clean up the Home Assistant connection after initialization fails."""
        try:
            await self._disconnect_hass()
        except BaseHassClientError as err:
            self.logger.warning("Failed to disconnect from Home Assistant: %s", err)

    async def _resolve_feature_entities(self) -> None:
        """Resolve configured or default Home Assistant feature entities."""
        tts_entities, ai_task_entities = _get_feature_entity_options(await self.hass.get_states())
        self._tts_entity_id = _select_feature_entity(
            self.config.get_value(CONF_TTS_ENTITY), tts_entities
        )
        self._ai_task_entity_id = _select_feature_entity(
            self.config.get_value(CONF_AI_TASK_ENTITY), ai_task_entities
        )
        self._supported_features.discard(ProviderFeature.TTS)
        self._supported_features.discard(ProviderFeature.AI_QUERY)
        if self._tts_entity_id:
            self._supported_features.add(ProviderFeature.TTS)
        if self._ai_task_entity_id:
            self._supported_features.add(ProviderFeature.AI_QUERY)


def _get_feature_entity_options(
    states: list[State],
) -> tuple[list[ConfigValueOption], list[ConfigValueOption]]:
    """Return sorted TTS and AI Task entity options."""
    feature_entities: dict[str, list[ConfigValueOption]] = {"tts": [], "ai_task": []}
    for state in states:
        entity_platform = state["entity_id"].split(".", 1)[0]
        if entity_platform not in feature_entities:
            continue
        friendly_name = state["attributes"].get("friendly_name")
        name = f"{friendly_name} ({state['entity_id']})" if friendly_name else state["entity_id"]
        feature_entities[entity_platform].append(ConfigValueOption(state["entity_id"], title=name))
    for entities in feature_entities.values():
        entities.sort(key=lambda option: option.title or "")
    return feature_entities["tts"], feature_entities["ai_task"]


def _select_feature_entity(
    configured_entity: ConfigValueType, options: list[ConfigValueOption]
) -> str | None:
    """Return the configured available entity or the first available entity."""
    available_entity_ids = {str(option.value) for option in options}
    if configured_entity:
        if not isinstance(configured_entity, str):
            return None
        return configured_entity if configured_entity in available_entity_ids else None
    return str(options[0].value) if options else None
