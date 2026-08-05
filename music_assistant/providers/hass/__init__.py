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
from functools import partial
from itertools import batched
from sys import intern
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict, cast

from hass_client import HomeAssistantClient
from hass_client.exceptions import BaseHassClientError
from hass_client.utils import get_websocket_url
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    MusicAssistantError,
    SetupFailedError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items.audio_format import AudioFormat
from music_assistant_models.player_control import PlayerControl
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.datetime import iso_from_utc_timestamp
from music_assistant.helpers.json import SerializableType
from music_assistant.helpers.util import lock, try_parse_int
from music_assistant.models.plugin import AIEngine, PluginProvider, TTSEngine

from .constants import OFF_STATES, MediaPlayerEntityFeature, parse_supported_features

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping

    from aiohttp import ClientSession
    from hass_client.models import (
        CompressedState,
        Context,
        Device,
        Entity,
        EntityStateEvent,
        Event,
        State,
    )
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.player import PlayerMedia
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

DOMAIN = "hass"
CONF_URL = "url"
CONF_AUTH_TOKEN = "token"
CONF_VERIFY_SSL = "verify_ssl"
CONF_POWER_CONTROLS = "power_controls"
CONF_MUTE_CONTROLS = "mute_controls"
CONF_VOLUME_CONTROLS = "volume_controls"
FEATURE_DISCOVERY_TIMEOUT = 30
STATE_FETCH_TIMEOUT = 30
STATE_FETCH_BATCH_SIZE = 500
# window to collect entity registry updates in, so an integration registering a
# batch of entities results in a single rebuild of the engine lists
ENGINE_REFRESH_DEBOUNCE = 2
# window in which repeated device lookups reuse one listing, so a burst of players
# connecting does not fetch the (unfilterable) device registry once per player
DEVICE_REGISTRY_CACHE_TTL = 60

# Home Assistant entity domains Music Assistant can offer as player controls.
CONTROL_DOMAINS = ("media_player", "switch", "input_boolean", "number", "input_number")
# Home Assistant entity domains that back the TTS and AI Task features.
FEATURE_DOMAINS = ("tts", "ai_task")
FEATURE_DOMAIN_PREFIXES = tuple(f"{domain}." for domain in FEATURE_DOMAINS)


class DeviceMediaPlayerInfo(TypedDict):
    """Home Assistant correlation info for a device that is natively connected elsewhere."""

    # user-facing device name in HA (name_by_user or name)
    name: str | None
    # first enabled media_player entity of the device that supports announcements
    announce_entity_id: str | None


class HassRegistryEntity(NamedTuple):
    """Home Assistant entity registry entry, limited to the fields Music Assistant uses."""

    platform: str
    device_id: str | None


class _ControlCapabilities(NamedTuple):
    """The player control roles a Home Assistant entity can serve."""

    power: bool = False
    volume: bool = False
    mute: bool = False


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return HomeAssistantProvider(mass, manifest, config, set())


async def _get_config_entries(hass_prov: HomeAssistantProvider) -> tuple[ConfigEntry, ...]:
    """Return the (entity based) config entries."""
    all_power_entities: list[ConfigValueOption] = []
    all_mute_entities: list[ConfigValueOption] = []
    all_volume_entities: list[ConfigValueOption] = []
    if not hass_prov.hass.connected:
        return ()
    states = await hass_prov.get_states(domains=CONTROL_DOMAINS)
    for state in states:
        capabilities = _get_control_capabilities(state, hass_prov.logger)
        option = ConfigValueOption(
            state["entity_id"], title=_get_control_name(state["entity_id"], state)
        )
        if capabilities.power:
            all_power_entities.append(option)
        if capabilities.volume:
            all_volume_entities.append(option)
        if capabilities.mute:
            all_mute_entities.append(option)
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
    ]
    return tuple(entries)


class HomeAssistantProvider(PluginProvider):
    """Home Assistant Plugin for Music Assistant."""

    hass: HomeAssistantClient
    _listen_task: asyncio.Task[None] | None = None
    _player_controls: dict[str, PlayerControl] | None = None
    _unsubscribe_controls: Callable[[], None] | None = None
    _unsubscribe_entity_registry: Callable[[], None] | None = None
    _engine_refresh_task: asyncio.Task[None] | None = None
    _ai_engines: list[AIEngine]
    _tts_engines: list[TTSEngine]
    _startup_complete: bool = False
    _entity_registry: Mapping[str, HassRegistryEntity] | None = None
    _entity_registry_generation: int = 0
    _entity_registry_lock: asyncio.Lock
    _wanted_controls: dict[str, _ControlCapabilities] | None = None
    _control_reconcile_lock: asyncio.Lock

    @property
    def url(self) -> str | None:
        """Return the configured Home Assistant URL, or None if not configured."""
        url = self.get_setup_value(CONF_URL)
        if isinstance(url, str) and url:
            return url
        return None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the (options) config entries for the Home Assistant provider.

        The connection URL and authentication token are collected by the setup flow (see
        setup_flow.py) unless running as a Home Assistant add-on, where they are fixed; only
        the player-control and feature options are configurable here.
        """
        base_entries: tuple[ConfigEntry, ...]
        if self.mass.running_as_hass_addon:
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
            # url/token/verify_ssl are collected by the setup flow instead (see setup_flow.py)
            base_entries = ()

        # append player controls entries (if we have an active instance)
        if self.available:
            try:
                return (
                    *base_entries,
                    *(await _get_config_entries(self)),
                )
            except TimeoutError:
                # listing the selectable entities needs a live sweep of Home Assistant, so a
                # slow or busy instance must not fail config resolution for the whole server.
                # The entries below carry no options but do keep any stored selection.
                self.logger.warning(
                    "Timeout fetching entities from Home Assistant, "
                    "player control options are unavailable until the next refresh"
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
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the plugin."""
        if self._listen_task and not self._listen_task.done():
            msg = "Home Assistant listener is already running"
            raise SetupFailedError(msg)
        self._startup_complete = False
        self._player_controls = {}
        self._wanted_controls = None
        self._control_reconcile_lock = asyncio.Lock()
        self._ai_engines = []
        self._tts_engines = []
        url = get_websocket_url(cast("str", self.get_setup_value(CONF_URL)))
        token = self.get_setup_value(CONF_AUTH_TOKEN)
        logging.getLogger("hass_client").setLevel(self.logger.level + 10)
        ssl = bool(self.get_setup_value(CONF_VERIFY_SSL, True))
        http_session = self.mass.http_session if ssl else self.mass.http_session_no_ssl
        self.hass = HomeAssistantClient(url, token, http_session)
        self._entity_registry = None
        self._entity_registry_lock = asyncio.Lock()
        try:
            await self.hass.connect()
        except BaseHassClientError as err:
            await self._cleanup_failed_init()
            err_msg = str(err) or err.__class__.__name__
            raise SetupFailedError(err_msg) from err
        self._listen_task = self.mass.create_task(self._hass_listener())
        try:
            # the registry subscription must be live before the first registry read, so no
            # registry change can slip through unnoticed; _disconnect_hass tears the
            # subscription down again on the failure paths below
            await self._subscribe_entity_registry()
            await self._resolve_startup_features()
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
        self._startup_complete = False
        await self._disconnect_hass()

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """
        Handle logic when the config is updated.

        A change limited to the player control selection is applied in place, so adding or
        removing a control does not drop and re-establish the Home Assistant connection.
        Any other change reloads the provider as usual.

        Raises when the in place update fails (because Home Assistant is unreachable, for
        example); the controls are then left as they were and a later update retries.

        :param config: The updated provider config.
        :param changed_keys: The keys that changed in the given config.
        """
        control_keys = {
            f"values/{conf_key}"
            for conf_key in (CONF_POWER_CONTROLS, CONF_VOLUME_CONTROLS, CONF_MUTE_CONTROLS)
        }
        if not changed_keys or not changed_keys <= control_keys:
            await super().update_config(config, changed_keys)
            return
        # store the new config before reconciling: the control lists are read back from it
        self.config = config
        await self._register_player_controls()

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this provider to include in diagnostics reports."""
        return {
            "connected": self.hass.connected,
            "ha_version": self.hass.version,
            "listener_active": self._listen_task is not None and not self._listen_task.done(),
            "player_controls": len(self._player_controls) if self._player_controls else 0,
        }

    async def get_entity_registry(self) -> Mapping[str, HassRegistryEntity]:
        """
        Return the Home Assistant entity registry, keyed by entity ID.

        Entities that are disabled in Home Assistant are absent from the result, and so are
        entities without a unique ID: those are not part of Home Assistant's registry at all.

        The result is shared between all callers and is read-only: both the mapping and
        its entries reject writes.
        """
        if (registry := self._entity_registry) is not None:
            return registry
        async with self._entity_registry_lock:
            if (registry := self._entity_registry) is None:
                generation = self._entity_registry_generation
                registry = await self._fetch_entity_registry()
                # a registry change while the fetch was in flight leaves the listing stale
                # on arrival, so serve it to this caller but keep it out of the cache
                if generation == self._entity_registry_generation:
                    self._entity_registry = registry
            return registry

    async def get_entity_registry_entries(self, entity_ids: Collection[str]) -> dict[str, Entity]:
        """
        Return the full Home Assistant entity registry entries of the given entities.

        :param entity_ids: The entity IDs to look up.
        :return: The registry entries keyed by entity ID; entities unknown to
            Home Assistant are absent from the result.
        """
        if not entity_ids:
            return {}
        result = cast(
            "dict[str, Entity | None]",
            await self.hass.send_command(
                "config/entity_registry/get_entries", entity_ids=list(entity_ids)
            ),
        )
        return {entity_id: entry for entity_id, entry in result.items() if entry is not None}

    async def get_device_registry(self) -> dict[str, Device]:
        """
        Return the Home Assistant device registry, keyed by device ID.

        Home Assistant offers no abbreviated variant of the device registry listing, so the
        entries carry all of their fields. The listing is reused for a short while, so a
        device change may take up to DEVICE_REGISTRY_CACHE_TTL seconds to be reflected.
        """
        return await self._fetch_device_registry()

    async def get_media_player_device_infos(
        self,
        mac_addresses: Collection[str],
        platform: str,
    ) -> dict[str, DeviceMediaPlayerInfo]:
        """
        Correlate devices (by MAC address) to their HA name and media_player entity.

        Used for devices that are natively connected to Music Assistant but also
        present in Home Assistant, to pick up their HA device name and their
        (announcement-capable) media_player entity.

        :param mac_addresses: Device MAC addresses to look up (case-insensitive).
        :param platform: The HA integration domain the media_player entities must belong to.
        :return: Correlation info keyed by lowercased MAC address; devices unknown
            to Home Assistant are absent from the result.
        """
        wanted_macs = {mac.lower() for mac in mac_addresses}
        if not wanted_macs:
            return {}
        device_registry = await self.get_device_registry()
        device_by_mac: dict[str, Device] = {
            connection[1].lower(): device
            for device in device_registry.values()
            for connection in device.get("connections", [])
            if len(connection) == 2
            and connection[0] == "mac"
            and connection[1].lower() in wanted_macs
        }
        if not device_by_mac:
            return {}
        media_players_by_device: dict[str, list[str]] = {}
        for entity_id, entry in (await self.get_entity_registry()).items():
            if (
                entry.platform == platform
                and entity_id.startswith("media_player.")
                and (device_id := entry.device_id)
            ):
                media_players_by_device.setdefault(device_id, []).append(entity_id)
        candidates_by_mac = {
            mac: media_players_by_device.get(device["id"], [])
            for mac, device in device_by_mac.items()
        }
        states = {
            state["entity_id"]: state
            for state in await self.get_states(
                entity_ids=[
                    entity_id
                    for entity_ids in candidates_by_mac.values()
                    for entity_id in entity_ids
                ]
            )
        }

        def _supports_announce(entity_id: str) -> bool:
            if (state := states.get(entity_id)) is None:
                return False
            supported_features = parse_supported_features(
                state["attributes"].get("supported_features"), entity_id, self.logger
            )
            return MediaPlayerEntityFeature.MEDIA_ANNOUNCE in supported_features

        return {
            mac: DeviceMediaPlayerInfo(
                name=device["name_by_user"] or device["name"],
                announce_entity_id=next(
                    (
                        entity_id
                        for entity_id in candidates_by_mac[mac]
                        if _supports_announce(entity_id)
                    ),
                    None,
                ),
            )
            for mac, device in device_by_mac.items()
        }

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

    async def get_states(
        self,
        *,
        entity_ids: list[str] | None = None,
        domains: Collection[str] | None = None,
    ) -> list[State]:
        """
        Return the current Home Assistant state for the requested entities.

        Provide explicit entity IDs and/or a set of domains; only those entities
        are fetched.

        :param entity_ids: Explicit entity IDs to fetch the current state for.
        :param domains: Entity domains whose entities should be fetched.
        """
        ids: set[str] = set(entity_ids or ())
        if domains:
            # resolve domains to entity_ids via the registry, which is far smaller
            # than a full state dump (it carries no attributes)
            registry = await self.get_entity_registry()
            ids.update(entity_id for entity_id in registry if entity_id.split(".", 1)[0] in domains)
        if not ids:
            return []
        states: list[State] = []
        async with asyncio.timeout(STATE_FETCH_TIMEOUT):
            # exceeding hass_client's 16MB websocket message limit drops the entire
            # connection, so bound the state dump by construction and fetch in batches
            for batch in batched(sorted(ids), STATE_FETCH_BATCH_SIZE, strict=False):
                states.extend(await self._fetch_states(list(batch)))
        return states

    async def resolve_image(self, path: str) -> bytes:
        """Resolve an image from an image path."""
        ha_url, headers, http_session = self._get_ha_http()
        async with http_session.get(f"{ha_url}{path}", headers=headers) as response:
            response.raise_for_status()
            return await response.read()

    async def get_ai_engines(self) -> list[AIEngine]:
        """Return the Home Assistant AI Task entities as AI engines."""
        return self._ai_engines

    async def get_tts_engines(self) -> list[TTSEngine]:
        """Return the Home Assistant TTS entities as TTS engines."""
        return self._tts_engines

    async def ai_query(self, query: str, engine_id: str | None = None) -> str:
        """Handle an AI query via Home Assistant's ai_task service."""
        entity_id = engine_id or next((engine.id for engine in self._ai_engines), None)
        if entity_id is None:
            raise UnsupportedFeaturedException("AI Task entity is not available")
        result = await self.hass.send_command(
            "call_service",
            domain="ai_task",
            service="generate_data",
            service_data={
                "task_name": "music_assistant",
                "instructions": query,
                "entity_id": entity_id,
            },
            return_response=True,
        )
        response = result.get("response", {}) if isinstance(result, dict) else {}
        data = response.get("data") if isinstance(response, dict) else None
        if not data:
            msg = f"AI Task returned no data in response: {result}"
            raise MusicAssistantError(msg)
        return str(data)

    async def play_announcement_on_entity(self, entity_id: str, announcement: PlayerMedia) -> None:
        """
        Play an announcement on a Home Assistant media_player entity.

        Uses Home Assistant's announce feature, so the entity's integration ducks
        or pauses any running playback and resumes it afterwards. Returns once the
        announcement has finished playing (approximated by its duration).

        :param entity_id: The media_player entity to play the announcement on.
        :param announcement: The announcement to play.
        """
        await self.hass.call_service(
            domain="media_player",
            service="play_media",
            service_data={
                "media_content_id": announcement.uri,
                "media_content_type": "music",
                "announce": True,
            },
            target={"entity_id": entity_id},
        )
        # Wait until the announcement is finished playing so callers can play
        # announcements in a sequence; HA gives no completion signal for announcements.
        duration = await self.mass.streams.get_announcement_duration(announcement)
        await asyncio.sleep(duration or 5)

    async def get_tts_message(
        self, message: str, language: str | None = None, engine_id: str | None = None
    ) -> StreamDetails:
        """Handle text-to-speech via Home Assistant's REST API."""
        entity_id = engine_id or next((engine.id for engine in self._tts_engines), None)
        if entity_id is None:
            raise UnsupportedFeaturedException("TTS entity is not available")
        ha_url, headers, http_session = self._get_ha_http()
        # the tts_get_url payload field is called engine_id but takes a tts entity_id
        payload: dict[str, str] = {"engine_id": entity_id, "message": message}
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
        if not self._startup_complete:
            return
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
        """Bring the registered player controls in line with the current configuration."""
        assert self._player_controls is not None  # for type checking
        # the wanted selection is determined inside the lock, so a reconcile that had to
        # wait for another one cannot apply a selection that was already superseded
        async with self._control_reconcile_lock:
            power_controls = cast("list[str]", self.config.get_value(CONF_POWER_CONTROLS))
            mute_controls = cast("list[str]", self.config.get_value(CONF_MUTE_CONTROLS))
            volume_controls = cast("list[str]", self.config.get_value(CONF_VOLUME_CONTROLS))
            wanted_controls: dict[str, _ControlCapabilities] = {
                entity_id: _ControlCapabilities(
                    power=entity_id in power_controls,
                    volume=entity_id in volume_controls,
                    mute=entity_id in mute_controls,
                )
                for entity_id in (*power_controls, *mute_controls, *volume_controls)
            }
            if wanted_controls == self._wanted_controls:
                # the selection is unchanged, so there is no need to consult Home Assistant
                return
            hass_states = {
                state["entity_id"]: state
                for state in await self.get_states(entity_ids=list(wanted_controls))
            }
            for entity_id in set(self._player_controls) - set(wanted_controls):
                del self._player_controls[entity_id]
                self.mass.players.remove_player_control(entity_id)
            for entity_id, capabilities in wanted_controls.items():
                control = self._create_player_control(
                    entity_id, hass_states.get(entity_id), capabilities
                )
                self._player_controls[entity_id] = control
                await self.mass.players.register_or_update_player_control(control)
            await self._subscribe_control_states()
            self._wanted_controls = wanted_controls

    def _create_player_control(
        self,
        entity_id: str,
        hass_state: State | None,
        capabilities: _ControlCapabilities,
    ) -> PlayerControl:
        """
        Return a ready to use PlayerControl for a Home Assistant entity.

        :param entity_id: The entity to base the control on.
        :param hass_state: The entity's current state, if known.
        :param capabilities: The control roles the entity should serve.
        """
        entity_platform = entity_id.split(".", maxsplit=1)[0]
        control = PlayerControl(
            id=entity_id,
            provider=self.instance_id,
            name=_get_control_name(entity_id, hass_state),
        )
        if capabilities.power:
            control.supports_power = True
            control.power_state = hass_state["state"] not in OFF_STATES if hass_state else False
            control.power_on = partial(self._handle_player_control_power_on, entity_id)
            control.power_off = partial(self._handle_player_control_power_off, entity_id)
        if capabilities.volume:
            control.supports_volume = True
            if not hass_state:
                control.volume_level = 0
            elif entity_platform == "media_player":
                control.volume_level = int(hass_state["attributes"].get("volume_level", 0) * 100)
            else:
                control.volume_level = try_parse_int(hass_state["state"]) or 0
            control.volume_set = partial(self._handle_player_control_volume_set, entity_id)
        if capabilities.mute:
            control.supports_mute = True
            if not hass_state:
                control.volume_muted = False
            elif entity_platform == "media_player":
                control.volume_muted = hass_state["attributes"].get("volume_muted")
            else:
                control.volume_muted = hass_state["state"] not in OFF_STATES
            control.mute_set = partial(self._handle_player_control_mute_set, entity_id)
        return control

    async def _subscribe_control_states(self) -> None:
        """Subscribe to the Home Assistant state of all currently tracked controls."""
        assert self._player_controls is not None  # for type checking
        # the earlier subscription is only released once the new one is live, so a failure
        # to subscribe leaves the controls watched by the subscription they already had
        previous_unsubscribe = self._unsubscribe_controls
        self._unsubscribe_controls = await self.hass.subscribe_entities(
            self._on_entity_state_update, list(self._player_controls)
        )
        if previous_unsubscribe:
            previous_unsubscribe()

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

    async def _fetch_states(self, entity_ids: list[str]) -> list[State]:
        """
        Return the current Home Assistant state of the given entities.

        :param entity_ids: The entity IDs to fetch the current state for.
        :return: The states of the requested entities; entities that currently have
            no state are absent from the result.
        """
        initial_states: asyncio.Future[dict[str, CompressedState]]
        initial_states = asyncio.get_running_loop().create_future()

        def _on_initial_states(event: EntityStateEvent) -> None:
            # only the first message of a subscription carries the full state under "a";
            # a state change racing in ahead of it must not resolve the fetch
            if (added := event.get("a")) is not None and not initial_states.done():
                initial_states.set_result(added)

        unsubscribe = await self.hass.subscribe_entities(_on_initial_states, entity_ids)
        try:
            compressed_states = await initial_states
        finally:
            unsubscribe()
        return [
            _decompress_state(entity_id, compressed_state)
            for entity_id, compressed_state in compressed_states.items()
        ]

    def _get_ha_http(self) -> tuple[str, dict[str, str], ClientSession]:
        """Return HA base URL (without trailing /api), auth headers, and the HTTP session."""
        ha_url = cast("str", self.get_setup_value(CONF_URL)).rstrip("/")
        ha_url = ha_url.removesuffix("/api")
        token = self.get_setup_value(CONF_AUTH_TOKEN) or os.environ.get("HASSIO_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        ssl = bool(self.get_setup_value(CONF_VERIFY_SSL, True))
        http_session = self.mass.http_session if ssl else self.mass.http_session_no_ssl
        return ha_url, headers, http_session

    async def _disconnect_hass(self) -> None:
        """Stop listening for Home Assistant events and disconnect the client."""
        if unsubscribe := self._unsubscribe_controls:
            self._unsubscribe_controls = None
            unsubscribe()
        if unsubscribe := self._unsubscribe_entity_registry:
            self._unsubscribe_entity_registry = None
            unsubscribe()
        if refresh_task := self._engine_refresh_task:
            self._engine_refresh_task = None
            refresh_task.cancel()
        if listen_task := self._listen_task:
            self._listen_task = None
            if not listen_task.done():
                listen_task.cancel()
            try:
                await listen_task
            except asyncio.CancelledError:
                pass
            except Exception as err:
                self.logger.warning("Home Assistant listener stopped with error: %s", err)
        await self.hass.disconnect()

    async def _cleanup_failed_init(self) -> None:
        """Clean up the Home Assistant connection after initialization fails."""
        try:
            await self._disconnect_hass()
        except Exception as err:
            self.logger.warning("Failed to disconnect from Home Assistant: %s", err)

    async def _resolve_startup_features(self) -> None:
        """Resolve Home Assistant features while the listener remains active."""
        assert self._listen_task is not None
        feature_task = asyncio.create_task(self._refresh_engines())
        try:
            try:
                async with asyncio.timeout(FEATURE_DISCOVERY_TIMEOUT):
                    await asyncio.wait(
                        {feature_task, self._listen_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
            except TimeoutError as err:
                msg = "Timed out while resolving Home Assistant feature entities"
                raise SetupFailedError(msg) from err
            if not feature_task.done():
                msg = "Home Assistant listener stopped during startup"
                raise SetupFailedError(msg)
            if feature_task.cancelled():
                if self._listen_task.done():
                    msg = "Home Assistant listener stopped during startup"
                else:
                    msg = "Home Assistant feature resolution was cancelled"
                raise SetupFailedError(msg)
            await feature_task
            if self._listen_task.done():
                msg = "Home Assistant listener stopped during startup"
                raise SetupFailedError(msg)
            self._startup_complete = True
        finally:
            if not feature_task.done():
                feature_task.cancel()
                await asyncio.gather(feature_task, return_exceptions=True)

    async def _refresh_engines(self) -> None:
        """Rebuild the TTS/AI engine lists from the Home Assistant feature entities."""
        tts_engines: list[TTSEngine] = []
        ai_engines: list[AIEngine] = []
        for state in await self.get_states(domains=FEATURE_DOMAINS):
            entity_id = state["entity_id"]
            entity_platform = entity_id.split(".", 1)[0]
            if friendly_name := state["attributes"].get("friendly_name"):
                name = f"{friendly_name} ({entity_id})"
            else:
                name = entity_id
            if entity_platform == "tts":
                tts_engines.append(TTSEngine(id=entity_id, name=name, provider=self))
            elif entity_platform == "ai_task":
                ai_engines.append(AIEngine(id=entity_id, name=name, provider=self))
        tts_engines.sort(key=lambda engine: engine.name)
        ai_engines.sort(key=lambda engine: engine.name)
        self._tts_engines = tts_engines
        self._ai_engines = ai_engines
        self._supported_features.discard(ProviderFeature.TTS)
        self._supported_features.discard(ProviderFeature.AI_QUERY)
        if tts_engines:
            self._supported_features.add(ProviderFeature.TTS)
        if ai_engines:
            self._supported_features.add(ProviderFeature.AI_QUERY)

    async def _subscribe_entity_registry(self) -> None:
        """Watch the Home Assistant entity registry to keep the engine lists up to date."""
        # register for entity registry updates, replacing any earlier subscription
        if unsubscribe := self._unsubscribe_entity_registry:
            self._unsubscribe_entity_registry = None
            unsubscribe()
        self._unsubscribe_entity_registry = await self.hass.subscribe_events(
            self._on_entity_registry_update, "entity_registry_updated"
        )

    def _on_entity_registry_update(self, event: Event) -> None:
        """Handle an entity registry update event."""
        # any registry change invalidates the cached registry, whatever domain it concerns
        self._entity_registry = None
        self._entity_registry_generation += 1
        entity_id = event["data"].get("entity_id", "")
        if not entity_id.startswith(FEATURE_DOMAIN_PREFIXES):
            return
        self._schedule_engine_refresh()

    def _schedule_engine_refresh(self) -> None:
        """(Re)schedule the debounced rebuild of the engine lists."""
        if refresh_task := self._engine_refresh_task:
            self._engine_refresh_task = None
            refresh_task.cancel()
        self._engine_refresh_task = self.mass.create_task(self._delayed_engine_refresh())

    async def _delayed_engine_refresh(self) -> None:
        """Rebuild the engine lists once the debounce window has passed."""
        await asyncio.sleep(ENGINE_REFRESH_DEBOUNCE)
        try:
            await self._refresh_engines()
        except Exception as err:
            self.logger.warning("Failed to refresh Home Assistant engines: %s", err)

    # unlike _fetch_device_registry, this listing is mirrored for the lifetime of the
    # connection rather than kept behind a TTL: it runs to several megabytes on a large
    # setup, and Home Assistant announces every change, so the mirror is both the cheaper
    # and the more accurate option
    async def _fetch_entity_registry(self) -> Mapping[str, HassRegistryEntity]:
        """Fetch the entity registry from Home Assistant, keyed by entity ID."""
        # the display variant of the registry listing carries abbreviated keys and only the
        # fields the Home Assistant frontend needs, making it several times smaller
        result = cast(
            "dict[str, Any]",
            await self.hass.send_command("config/entity_registry/list_for_display"),
        )
        # the listing repeats a handful of platform names and one device id per device over
        # all of its entities, so hold on to a single string object per distinct value
        device_ids: dict[str, str] = {}
        registry: dict[str, HassRegistryEntity] = {}
        for entry in result["entities"]:
            if (device_id := entry.get("di")) is not None:
                device_id = device_ids.setdefault(device_id, device_id)
            registry[entry["ei"]] = HassRegistryEntity(
                platform=intern(entry["pl"]),
                device_id=device_id,
            )
        return MappingProxyType(registry)

    # the lock sits outside the cache to keep a burst of lookups from fetching once per
    # caller. use_cache stores in the background, so the callers that reach a still-cold
    # cache can overlap: the burst costs a couple of fetches instead of one per player
    @lock
    @use_cache(expiration=DEVICE_REGISTRY_CACHE_TTL)
    async def _fetch_device_registry(self) -> dict[str, Any]:
        """Fetch the device registry from Home Assistant, keyed by device ID."""
        # use_cache rebuilds the cached value from this return annotation, which rules out
        # the Device TypedDict; get_device_registry restores the type for callers
        return {device["id"]: device for device in await self.hass.get_device_registry()}


def _get_control_capabilities(state: State, logger: logging.Logger) -> _ControlCapabilities:
    """
    Return the player control roles the given Home Assistant entity can serve.

    :param state: The current state of the entity to inspect.
    :param logger: Logger to report an unparsable supported_features attribute on.
    :return: The supported roles; all False when the entity is unusable as a player control.
    """
    entity_platform = state["entity_id"].split(".")[0]
    if entity_platform in ("switch", "input_boolean"):
        # simple on/off controls are suitable as power and mute controls
        return _ControlCapabilities(power=True, mute=True)
    if entity_platform in ("number", "input_number"):
        # number and input_number are very similar, both are suitable for volume control
        return _ControlCapabilities(volume=True)
    # media player can be used as control, depending on features
    if entity_platform != "media_player":
        return _ControlCapabilities()
    if "mass_player_type" in state["attributes"]:
        # filter out mass players
        return _ControlCapabilities()
    supported_features = parse_supported_features(
        state["attributes"].get("supported_features"),
        state["entity_id"],
        logger,
    )
    return _ControlCapabilities(
        power=(
            MediaPlayerEntityFeature.TURN_ON in supported_features
            and MediaPlayerEntityFeature.TURN_OFF in supported_features
        ),
        volume=MediaPlayerEntityFeature.VOLUME_SET in supported_features,
        mute=MediaPlayerEntityFeature.VOLUME_MUTE in supported_features,
    )


def _get_control_name(entity_id: str, state: State | None) -> str:
    """
    Return the human readable name to present a Home Assistant entity control under.

    :param entity_id: The entity the control is based on.
    :param state: The entity's current state, if known.
    """
    if state and (friendly_name := state["attributes"].get("friendly_name")):
        return f"{friendly_name} ({entity_id})"
    return entity_id


def _decompress_state(entity_id: str, compressed_state: CompressedState) -> State:
    """
    Return the full state representation of a compressed state message.

    :param entity_id: The entity the compressed state belongs to.
    :param compressed_state: The compressed state as received over the websocket.
    """
    raw_context = compressed_state.get("c")
    context: Context = (
        raw_context
        if isinstance(raw_context, dict)
        else {"id": raw_context or "", "parent_id": None, "user_id": None}
    )
    last_changed = compressed_state.get("lc")
    # Home Assistant omits last_updated when it is identical to last_changed
    last_updated = compressed_state.get("lu", last_changed)
    return {
        "entity_id": entity_id,
        "state": compressed_state.get("s", ""),
        "attributes": compressed_state.get("a", {}),
        "last_changed": iso_from_utc_timestamp(last_changed) if last_changed else "",
        "last_updated": iso_from_utc_timestamp(last_updated) if last_updated else "",
        "context": context,
    }
