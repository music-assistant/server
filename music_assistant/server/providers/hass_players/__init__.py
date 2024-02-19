"""
Home Assistant PlayerProvider for Music Assistant.

Allows using media_player entities in HA to be used as players in MA.
Requires the Home Assistant Plugin.
"""

from __future__ import annotations

import time
from enum import IntFlag
from typing import TYPE_CHECKING, Any

from music_assistant.common.helpers.datetime import from_iso_string
from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_FLOW_MODE,
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    PlayerFeature,
    PlayerState,
    PlayerType,
)
from music_assistant.common.models.errors import SetupFailedError
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.constants import CONF_CROSSFADE, CONF_FLOW_MODE
from music_assistant.server.models.player_provider import PlayerProvider
from music_assistant.server.providers.hass import DOMAIN as HASS_DOMAIN

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from hass_client.models import CompressedState, EntityStateEvent
    from hass_client.models import Device as HassDevice
    from hass_client.models import Entity as HassEntity
    from hass_client.models import State as HassState

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.common.models.queue_item import QueueItem
    from music_assistant.server import MusicAssistant
    from music_assistant.server.controllers.streams import MultiClientStreamJob
    from music_assistant.server.models import ProviderInstanceType
    from music_assistant.server.providers.hass import HomeAssistant as HomeAssistantProvider

CONF_PLAYERS = "players"

StateMap = {
    "playing": PlayerState.PLAYING,
    "paused": PlayerState.PLAYING,
    "buffering": PlayerState.PLAYING,
    "idle": PlayerState.IDLE,
    "off": PlayerState.IDLE,
    "standby": PlayerState.IDLE,
    "unknown": PlayerState.IDLE,
    "unavailable": PlayerState.IDLE,
}


class MediaPlayerEntityFeature(IntFlag):
    """Supported features of the media player entity."""

    PAUSE = 1
    SEEK = 2
    VOLUME_SET = 4
    VOLUME_MUTE = 8
    PREVIOUS_TRACK = 16
    NEXT_TRACK = 32

    TURN_ON = 128
    TURN_OFF = 256
    PLAY_MEDIA = 512
    VOLUME_STEP = 1024
    SELECT_SOURCE = 2048
    STOP = 4096
    CLEAR_PLAYLIST = 8192
    PLAY = 16384
    SHUFFLE_SET = 32768
    SELECT_SOUND_MODE = 65536
    BROWSE_MEDIA = 131072
    REPEAT_SET = 262144
    GROUPING = 524288
    MEDIA_ANNOUNCE = 1048576
    MEDIA_ENQUEUE = 2097152


CONF_ENFORCE_MP3 = "enforce_mp3"

PLAYER_CONFIG_ENTRIES = (
    ConfigEntry(
        key=CONF_CROSSFADE,
        type=ConfigEntryType.BOOLEAN,
        label="Enable crossfade",
        default_value=False,
        description="Enable a crossfade transition between (queue) tracks. \n\n"
        "Note that you need to enable the 'flow mode' workaround to use "
        "crossfading with Home Assistant players.",
        advanced=False,
        depends_on=CONF_FLOW_MODE,
    ),
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_CROSSFADE_DURATION,
    ConfigEntry(
        key=CONF_ENFORCE_MP3,
        type=ConfigEntryType.BOOLEAN,
        label="Enforce (lossy) mp3 stream",
        default_value=False,
        description="By default, Music Assistant sends lossless, high quality audio "
        "to all players. Some players can not deal with that and require the stream to be packed "
        "into a lossy mp3 codec. \n\n "
        "Only enable when needed. Saves some bandwidth at the cost of audio quality.",
        advanced=True,
    ),
)


async def _get_hass_media_players(
    hass_prov: HomeAssistantProvider,
) -> AsyncGenerator[HassState, None]:
    """Return all HA state objects for (valid) media_player entities."""
    for state in await hass_prov.hass.get_states():
        if not state["entity_id"].startswith("media_player"):
            continue
        if "mass_player_id" in state["attributes"]:
            # filter out mass players
            continue
        if "friendly_name" not in state["attributes"]:
            # filter out invalid/unavailable players
            continue
        supported_features = MediaPlayerEntityFeature(state["attributes"]["supported_features"])
        if MediaPlayerEntityFeature.PLAY_MEDIA not in supported_features:
            continue
        yield state


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    hass_prov: HomeAssistantProvider = mass.get_provider(HASS_DOMAIN)
    if not hass_prov:
        msg = "The Home Assistant Plugin needs to be set-up first"
        raise SetupFailedError(msg)
    prov = HomeAssistantPlayers(mass, manifest, config)
    prov.hass_prov = hass_prov
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    hass_prov: HomeAssistantProvider = mass.get_provider(HASS_DOMAIN)
    player_entities: list[ConfigValueOption] = []
    if hass_prov and hass_prov.hass.connected:
        async for state in _get_hass_media_players(hass_prov):
            name = f'{state["attributes"]["friendly_name"]} ({state["entity_id"]})'
            player_entities.append(ConfigValueOption(name, state["entity_id"]))
    return (
        ConfigEntry(
            key=CONF_PLAYERS,
            type=ConfigEntryType.STRING,
            label="Player entities",
            required=True,
            options=tuple(player_entities),
            multi_value=True,
            description="Specify which HA media_player entity id's you "
            "like to import as players in Music Assistant.",
        ),
    )


class HomeAssistantPlayers(PlayerProvider):
    """Home Assistant PlayerProvider for Music Assistant."""

    hass_prov: HomeAssistantProvider

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        player_ids: list[str] = self.config.get_value(CONF_PLAYERS)
        # prefetch the device- and entity registry
        device_registry = {x["id"]: x for x in await self.hass_prov.hass.get_device_registry()}
        entity_registry = {
            x["entity_id"]: x for x in await self.hass_prov.hass.get_entity_registry()
        }
        # setup players from hass entities
        async for state in _get_hass_media_players(self.hass_prov):
            if state["entity_id"] not in player_ids:
                continue
            await self._setup_player(state, entity_registry, device_registry)
        # register for entity state updates
        await self.hass_prov.hass.subscribe_entities(self._on_entity_state_update, player_ids)
        # remove any leftover players (after reconfigure of players)
        for player in self.players:
            if player.player_id not in player_ids:
                self.mass.players.remove(player.player_id)

    async def get_player_config_entries(
        self,
        player_id: str,
    ) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        return base_entries + PLAYER_CONFIG_ENTRIES

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        await self.hass_prov.hass.call_service(
            domain="media_player", service="media_stop", target={"entity_id": player_id}
        )

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY (unpause) command to given player.

        - player_id: player_id of the player to handle the command.
        """
        await self.hass_prov.hass.call_service(
            domain="media_player", service="media_play", target={"entity_id": player_id}
        )

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player.

        - player_id: player_id of the player to handle the command.
        """
        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="media_pause",
            target={"entity_id": player_id},
        )

    async def play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int,
        fade_in: bool,
    ) -> None:
        """Handle PLAY MEDIA on given player.

        This is called by the Queue controller to start playing a queue item on the given player.
        The provider's own implementation should work out how to handle this request.

            - player_id: player_id of the player to handle the command.
            - queue_item: The QueueItem that needs to be played on the player.
            - seek_position: Optional seek to this position.
            - fade_in: Optionally fade in the item at playback start.
        """
        use_flow_mode = await self.mass.config.get_player_config_value(player_id, CONF_FLOW_MODE)
        enforce_mp3 = await self.mass.config.get_player_config_value(player_id, CONF_ENFORCE_MP3)
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            output_codec=ContentType.MP3 if enforce_mp3 else ContentType.FLAC,
            seek_position=seek_position,
            fade_in=fade_in,
            flow_mode=use_flow_mode,
        )
        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="play_media",
            service_data={
                "media_content_id": url,
                "media_content_type": "music",
                "enqueue": "replace",
            },
            target={"entity_id": player_id},
        )
        # optimistically set the elapsed_time as some HA players do not report this
        if player := self.mass.players.get(player_id):
            player.elapsed_time = 0
            player.elapsed_time_last_updated = time.time()

    async def play_stream(self, player_id: str, stream_job: MultiClientStreamJob) -> None:
        """Handle PLAY STREAM on given player.

        This is a special feature from the Universal Group provider.
        """
        enforce_mp3 = await self.mass.config.get_player_config_value(player_id, CONF_ENFORCE_MP3)
        output_codec = ContentType.MP3 if enforce_mp3 else ContentType.FLAC
        url = stream_job.resolve_stream_url(player_id, output_codec)
        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="play_media",
            service_data={
                "media_content_id": url,
                "media_content_type": "music",
                "enqueue": "replace",
            },
            target={"entity_id": player_id},
        )

    async def enqueue_next_queue_item(self, player_id: str, queue_item: QueueItem) -> None:
        """
        Handle enqueuing of the next queue item on the player.

        Only called if the player supports PlayerFeature.ENQUE_NEXT.
        Called about 1 second after a new track started playing.
        Called about 15 seconds before the end of the current track.

        A PlayerProvider implementation is in itself responsible for handling this
        so that the queue items keep playing until its empty or the player stopped.

        This will NOT be called if the end of the queue is reached (and repeat disabled).
        This will NOT be called if the player is using flow mode to playback the queue.
        """
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            output_codec=ContentType.FLAC,
        )
        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="play_media",
            service_data={
                "media_content_id": url,
                "media_content_type": "music",
                "enqueue": "next",
            },
            target={"entity_id": player_id},
        )

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player.

        - player_id: player_id of the player to handle the command.
        - powered: bool if player should be powered on or off.
        """
        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="turn_on" if powered else "turn_off",
            target={"entity_id": player_id},
        )

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player.

        - player_id: player_id of the player to handle the command.
        - volume_level: volume level (0..100) to set on the player.
        """
        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="volume_set",
            service_data={"volume_level": volume_level / 100},
            target={"entity_id": player_id},
        )

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player.

        - player_id: player_id of the player to handle the command.
        - muted: bool if player should be muted.
        """
        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="volume_mute",
            service_data={"is_volume_muted": muted},
            target={"entity_id": player_id},
        )

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Handle SYNC command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup master or group player.
        """
        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="join",
            service_data={"group_members": [player_id]},
            target={"entity_id": target_player},
        )

    async def cmd_unsync(self, player_id: str) -> None:
        """Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.

            - player_id: player_id of the player to handle the command.
        """
        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="unjoin",
            target={"entity_id": player_id},
        )

    async def _setup_player(
        self,
        state: HassState,
        entity_registry: dict[str, HassEntity],
        device_registry: dict[str, HassDevice],
    ) -> None:
        """Handle setup of a Player from an hass entity."""
        # fetch the entity registry entry for this entity to obtain more details
        hass_device: HassDevice | None = None
        platform_players: list[str] = []
        if entity_registry_entry := entity_registry.get(state["entity_id"]):
            # collect all players from same platform
            platform_players = [
                entity_id
                for entity_id, entity in entity_registry.items()
                if entity["platform"] == entity_registry_entry["platform"]
                and entity_id != state["entity_id"]
            ]
            hass_device = device_registry.get(entity_registry_entry["device_id"])
        hass_supported_features = MediaPlayerEntityFeature(
            state["attributes"]["supported_features"]
        )
        supported_features: list[PlayerFeature] = []
        if MediaPlayerEntityFeature.GROUPING in hass_supported_features:
            supported_features.append(PlayerFeature.SYNC)
        if MediaPlayerEntityFeature.MEDIA_ENQUEUE in hass_supported_features:
            supported_features.append(PlayerFeature.ENQUEUE_NEXT)
        if MediaPlayerEntityFeature.VOLUME_SET in hass_supported_features:
            supported_features.append(PlayerFeature.VOLUME_SET)
        if MediaPlayerEntityFeature.VOLUME_MUTE in hass_supported_features:
            supported_features.append(PlayerFeature.VOLUME_MUTE)
        if (
            MediaPlayerEntityFeature.TURN_ON in hass_supported_features
            and MediaPlayerEntityFeature.TURN_OFF in hass_supported_features
        ):
            supported_features.append(PlayerFeature.POWER)
        player = Player(
            player_id=state["entity_id"],
            provider=self.instance_id,
            type=PlayerType.PLAYER,
            name=state["attributes"]["friendly_name"],
            available=state["state"] not in ("unavailable", "unknown"),
            powered=state["state"] not in ("unavailable", "unknown", "standby", "off"),
            device_info=DeviceInfo(
                model=hass_device["model"] if hass_device else "Unknown model",
                manufacturer=(
                    hass_device["manufacturer"] if hass_device else "Unknown Manufacturer"
                ),
            ),
            supported_features=tuple(supported_features),
            state=StateMap.get(state["state"], PlayerState.IDLE),
        )
        if MediaPlayerEntityFeature.GROUPING in hass_supported_features:
            player.can_sync_with = platform_players
        self._update_player_attributes(player, state["attributes"])
        self.mass.players.register_or_update(player)

    def _on_entity_state_update(self, event: EntityStateEvent) -> None:
        """Handle Entity State event."""

        def update_player_from_state_msg(entity_id: str, state: CompressedState) -> None:
            """Handle updating MA player with updated info in a HA CompressedState."""
            player = self.mass.players.get(entity_id)
            if player is None:
                return  # should not happen, but guard just in case
            if "s" in state:
                player.state = StateMap.get(state["s"], PlayerState.IDLE)
                player.powered = state["s"] not in (
                    "unavailable",
                    "unknown",
                    "standby",
                    "off",
                )
            if "a" in state:
                self._update_player_attributes(player, state["a"])
            self.mass.players.update(entity_id)

        if entity_additions := event.get("a"):
            for entity_id, state in entity_additions.items():
                update_player_from_state_msg(entity_id, state)
        if entity_changes := event.get("c"):
            for entity_id, state_diff in entity_changes.items():
                if "+" not in state_diff:
                    continue
                update_player_from_state_msg(entity_id, state_diff["+"])

    def _update_player_attributes(self, player: Player, attributes: dict[str, Any]) -> None:
        """Update Player attributes from HA state attributes."""
        for key, value in attributes.items():
            if key == "media_position":
                player.elapsed_time = value
            if key == "media_position_updated_at":
                player.elapsed_time_last_updated = from_iso_string(value).timestamp()
            if key == "volume_level":
                player.volume_level = int(value * 100)
            if key == "volume_muted":
                player.volume_muted = value
            if key == "media_content_id":
                player.current_item_id = value
            if key == "group_members":
                if value and value[0] == player.player_id:
                    player.group_childs = value
                    player.synced_to = None
                elif value and value[0] != player.player_id:
                    player.group_childs = set()
                    player.synced_to = value[0]
                else:
                    player.group_childs = set()
                    player.synced_to = None
