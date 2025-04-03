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
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed, SetupFailedError
from music_assistant_models.player_control import PlayerControl

from music_assistant.constants import MASS_LOGO_ONLINE
from music_assistant.helpers.auth import AuthenticationHelper
from music_assistant.helpers.util import try_parse_int
from music_assistant.models.plugin import PluginProvider

from .constants import OFF_STATES, MediaPlayerEntityFeature

if TYPE_CHECKING:
    from hass_client.models import CompressedState, EntityStateEvent
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


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return HomeAssistantProvider(mass, manifest, config)


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
        hass_url = values[CONF_URL]
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
                label="URL",
                required=True,
                description="URL to your Home Assistant instance (e.g. http://192.168.1.1:8123)",
                value=cast("str", values.get(CONF_URL)) if values else None,
            ),
            ConfigEntry(
                key=CONF_ACTION_AUTH,
                type=ConfigEntryType.ACTION,
                label="(re)Authenticate Home Assistant",
                description="Authenticate to your home assistant "
                "instance and generate the long lived token.",
                action=CONF_ACTION_AUTH,
                depends_on=CONF_URL,
                required=False,
            ),
            ConfigEntry(
                key=CONF_AUTH_TOKEN,
                type=ConfigEntryType.SECURE_STRING,
                label="Authentication token for HomeAssistant",
                description="You can either paste a Long Lived Token here manually or use the "
                "'authenticate' button to generate a token for you with logging in.",
                depends_on=CONF_URL,
                value=cast("str", values.get(CONF_AUTH_TOKEN)) if values else None,
                category="advanced",
            ),
            ConfigEntry(
                key=CONF_VERIFY_SSL,
                type=ConfigEntryType.BOOLEAN,
                label="Verify SSL",
                required=False,
                description="Whether or not to verify the certificate of SSL/TLS connections.",
                category="advanced",
                default_value=True,
            ),
        )

    # append player controls entries (if we have an active instance)
    if instance_id and (hass_prov := mass.get_provider(instance_id)) and hass_prov.available:
        hass_prov = cast("HomeAssistantProvider", hass_prov)
        return (*base_entries, *(await _get_player_control_config_entries(hass_prov.hass)))

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
    )


async def _get_player_control_config_entries(hass: HomeAssistantClient) -> tuple[ConfigEntry, ...]:
    """Return all HA state objects for (valid) media_player entities."""
    all_power_entities: list[ConfigValueOption] = []
    all_mute_entities: list[ConfigValueOption] = []
    all_volume_entities: list[ConfigValueOption] = []
    # collect all entities that are usable for player controls
    if not hass.connected:
        return ()
    for state in await hass.get_states():
        if "friendly_name" not in state["attributes"]:
            # filter out invalid/unavailable players
            continue
        entity_platform = state["entity_id"].split(".")[0]
        name = f"{state['attributes']['friendly_name']} ({state['entity_id']})"

        if entity_platform in ("switch", "input_boolean"):
            # simple on/off controls are suitable as power and mute controls
            all_power_entities.append(ConfigValueOption(name, state["entity_id"]))
            all_mute_entities.append(ConfigValueOption(name, state["entity_id"]))
            continue
        if entity_platform in ("number", "input_number"):
            # number and input_number are very similar, both are suitable for volume control
            all_volume_entities.append(ConfigValueOption(name, state["entity_id"]))
            continue

        # media player can be used as control, depending on features
        if entity_platform != "media_player":
            continue
        if "mass_player_type" in state["attributes"]:
            # filter out mass players
            continue
        supported_features = MediaPlayerEntityFeature(state["attributes"]["supported_features"])
        if MediaPlayerEntityFeature.VOLUME_MUTE in supported_features:
            all_mute_entities.append(ConfigValueOption(name, state["entity_id"]))
        if MediaPlayerEntityFeature.VOLUME_SET in supported_features:
            all_volume_entities.append(ConfigValueOption(name, state["entity_id"]))
        if (
            MediaPlayerEntityFeature.TURN_ON in supported_features
            and MediaPlayerEntityFeature.TURN_OFF in supported_features
        ):
            all_power_entities.append(ConfigValueOption(name, state["entity_id"]))
    all_power_entities.sort(key=lambda x: x.title)
    all_mute_entities.sort(key=lambda x: x.title)
    all_volume_entities.sort(key=lambda x: x.title)
    return (
        ConfigEntry(
            key=CONF_POWER_CONTROLS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            label="Player Power Control entities",
            required=True,
            options=all_power_entities,
            default_value=[],
            description="Specify which Home Assistant entities you "
            "like to import as player Power controls in Music Assistant.",
            category="player_controls",
        ),
        ConfigEntry(
            key=CONF_VOLUME_CONTROLS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            label="Player Volume Control entities",
            required=True,
            options=all_volume_entities,
            default_value=[],
            description="Specify which Home Assistant entities you "
            "like to import as player Volume controls in Music Assistant.",
            category="player_controls",
        ),
        ConfigEntry(
            key=CONF_MUTE_CONTROLS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            label="Player Mute Control entities",
            required=True,
            options=all_mute_entities,
            default_value=[],
            description="Specify which Home Assistant entities you "
            "like to import as player Mute controls in Music Assistant.",
            category="player_controls",
        ),
    )


class HomeAssistantProvider(PluginProvider):
    """Home Assistant Plugin for Music Assistant."""

    hass: HomeAssistantClient
    _listen_task: asyncio.Task[None] | None = None
    _player_controls: dict[str, PlayerControl] | None = None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the plugin."""
        self._player_controls = {}
        url = get_websocket_url(self.config.get_value(CONF_URL))
        token = self.config.get_value(CONF_AUTH_TOKEN)
        logging.getLogger("hass_client").setLevel(self.logger.level + 10)
        self.hass = HomeAssistantClient(url, token, self.mass.http_session)
        try:
            await self.hass.connect(ssl=bool(self.config.get_value(CONF_VERIFY_SSL)))
        except BaseHassClientError as err:
            err_msg = str(err) or err.__class__.__name__
            raise SetupFailedError(err_msg) from err
        self._listen_task = self.mass.create_task(self._hass_listener())

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
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        await self.hass.disconnect()

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
            if hass_state:
                name = f"{hass_state['attributes']['friendly_name']} ({entity_id})"
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
        entity_platform = entity_id.split(".")[0]
        if "s" in state:
            # state changed
            if player_control.supports_power:
                player_control.power_state = state["s"] not in OFF_STATES
            if player_control.supports_mute and entity_platform != "media_player":
                player_control.volume_muted = state["s"] not in OFF_STATES
            if player_control.supports_volume and entity_platform != "media_player":
                player_control.volume_level = try_parse_int(state["s"]) or 0
        if "a" in state and (attributes := state["a"]):
            if player_control.supports_volume:
                if entity_platform == "media_player":
                    player_control.volume_level = int(attributes.get("volume_level", 0) * 100)
                else:
                    player_control.volume_level = try_parse_int(attributes.get("value")) or 0
            if player_control.supports_mute and entity_platform == "media_player":
                player_control.volume_muted = attributes.get("volume_muted")
        self.mass.players.update_player_control(entity_id)
