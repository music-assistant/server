"""Home Assistant Player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from hass_client.exceptions import FailedCommand
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType

from music_assistant.constants import (
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_ENABLE_ICY_METADATA_HIDDEN,
    CONF_ENTRY_FLOW_MODE_DEFAULT_ENABLED,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_HTTP_PROFILE,
    CONF_ENTRY_HTTP_PROFILE_FORCED_2,
    CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3,
    HIDDEN_ANNOUNCE_VOLUME_CONFIG_ENTRIES,
    create_output_codec_config_entry,
    create_sample_rates_config_entry,
)
from music_assistant.helpers.datetime import from_iso_string
from music_assistant.helpers.tags import async_parse_tags
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.hass.constants import (
    OFF_STATES,
    UNAVAILABLE_STATES,
    MediaPlayerEntityFeature,
    StateMap,
)

from .constants import CONF_ENTRY_WARN_HASS_INTEGRATION, WARN_HASS_INTEGRATIONS
from .helpers import ESPHomeSupportedAudioFormat

if TYPE_CHECKING:
    from hass_client import HomeAssistantClient
    from hass_client.models import CompressedState
    from hass_client.models import Entity as HassEntity
    from hass_client.models import State as HassState
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType


DEFAULT_PLAYER_CONFIG_ENTRIES = (
    CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3,
    CONF_ENTRY_HTTP_PROFILE,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
)


class HomeAssistantPlayer(Player):
    """Home Assistant Player implementation."""

    _attr_type = PlayerType.PLAYER

    def __init__(
        self,
        provider: PlayerProvider,
        hass: HomeAssistantClient,
        player_id: str,
        hass_state: HassState,
        dev_info: dict[str, Any],
        extra_player_data: dict[str, Any],
        entity_registry: dict[str, HassEntity],
    ) -> None:
        """Initialize the Home Assistant Player."""
        super().__init__(provider, player_id)
        self.hass = hass
        self.hass_state = hass_state
        self._extra_data = extra_player_data
        # Set base attributes from Home Assistant state
        self._attr_available = hass_state["state"] not in UNAVAILABLE_STATES
        self._attr_device_info = DeviceInfo.from_dict(dev_info)
        self._attr_playback_state = StateMap.get(hass_state["state"], PlaybackState.IDLE)
        # Work out supported features
        self._attr_supported_features = set()
        hass_supported_features = MediaPlayerEntityFeature(
            hass_state["attributes"]["supported_features"]
        )
        if MediaPlayerEntityFeature.PAUSE in hass_supported_features:
            self._attr_supported_features.add(PlayerFeature.PAUSE)
        if MediaPlayerEntityFeature.VOLUME_SET in hass_supported_features:
            self._attr_supported_features.add(PlayerFeature.VOLUME_SET)
        if MediaPlayerEntityFeature.VOLUME_MUTE in hass_supported_features:
            self._attr_supported_features.add(PlayerFeature.VOLUME_MUTE)
        if MediaPlayerEntityFeature.MEDIA_ANNOUNCE in hass_supported_features:
            self._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        hass_domain = extra_player_data.get("hass_domain")
        if hass_domain and MediaPlayerEntityFeature.GROUPING in hass_supported_features:
            self._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
            self._attr_can_group_with = {
                x["entity_id"]
                for x in entity_registry.values()
                if x["entity_id"].startswith("media_player") and x["platform"] == hass_domain
            }
        if (
            MediaPlayerEntityFeature.TURN_ON in hass_supported_features
            and MediaPlayerEntityFeature.TURN_OFF in hass_supported_features
        ):
            self._attr_supported_features.add(PlayerFeature.POWER)
            self._attr_powered = hass_state["state"] not in OFF_STATES

        self.extra_data["hass_supported_features"] = hass_supported_features
        self._update_attributes(hass_state["attributes"])

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        base_entries = await super().get_config_entries(action=action, values=values)
        base_entries = [*base_entries, *DEFAULT_PLAYER_CONFIG_ENTRIES]
        if self.extra_data.get("esphome_supported_audio_formats"):
            # optimized config for new ESPHome mediaplayer
            supported_sample_rates: list[int] = []
            supported_bit_depths: list[int] = []
            codec: str | None = None
            supported_formats: list[ESPHomeSupportedAudioFormat] = self.extra_data[
                "esphome_supported_audio_formats"
            ]
            # sort on purpose field, so we prefer the media pipeline
            # but allows fallback to announcements pipeline if no media pipeline is available
            supported_formats.sort(key=lambda x: x["purpose"])
            for supported_format in supported_formats:
                codec = supported_format["format"]
                if supported_format["sample_rate"] not in supported_sample_rates:
                    supported_sample_rates.append(supported_format["sample_rate"])
                bit_depth = (supported_format["sample_bytes"] or 2) * 8
                if bit_depth not in supported_bit_depths:
                    supported_bit_depths.append(bit_depth)
            if not supported_sample_rates or not supported_bit_depths:
                # esphome device with no media pipeline configured
                # simply use the default config of the media pipeline
                supported_sample_rates = [48000]
                supported_bit_depths = [16]

            config_entries = [
                *base_entries,
                # New ESPHome mediaplayer (used in Voice PE) uses FLAC 48khz/16 bits
                CONF_ENTRY_FLOW_MODE_ENFORCED,
                CONF_ENTRY_HTTP_PROFILE_FORCED_2,
            ]

            if codec is not None:
                config_entries.append(create_output_codec_config_entry(True, codec))

            config_entries.extend(
                [
                    CONF_ENTRY_ENABLE_ICY_METADATA_HIDDEN,
                    create_sample_rates_config_entry(
                        supported_sample_rates=supported_sample_rates,
                        supported_bit_depths=supported_bit_depths,
                        hidden=True,
                    ),
                    # although the Voice PE supports announcements,
                    # it does not support volume for announcements
                    *HIDDEN_ANNOUNCE_VOLUME_CONFIG_ENTRIES,
                ]
            )

            return config_entries

        # add alert if player is a known player type that has a native provider in MA
        if self.extra_data.get("hass_domain") in WARN_HASS_INTEGRATIONS:
            base_entries = [CONF_ENTRY_WARN_HASS_INTEGRATION, *base_entries]

        # enable flow mode by default if player does not report enqueue support
        if MediaPlayerEntityFeature.MEDIA_ENQUEUE not in self.extra_data["hass_supported_features"]:
            base_entries = [*base_entries, CONF_ENTRY_FLOW_MODE_DEFAULT_ENABLED]

        return base_entries

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        await self.hass.call_service(
            domain="media_player",
            service="media_play",
            target={"entity_id": self.player_id},
        )

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        await self.hass.call_service(
            domain="media_player",
            service="media_pause",
            target={"entity_id": self.player_id},
        )

    async def stop(self) -> None:
        """Send STOP command to player."""
        try:
            await self.hass.call_service(
                domain="media_player",
                service="media_stop",
                target={"entity_id": self.player_id},
            )
        except FailedCommand as exc:
            # some HA players do not support STOP
            if "does not support" not in str(exc):
                raise
            if PlayerFeature.PAUSE in self.supported_features:
                await self.pause()
        finally:
            self._attr_current_media = None
            self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self.hass.call_service(
            domain="media_player",
            service="volume_set",
            target={"entity_id": self.player_id},
            service_data={"volume_level": volume_level / 100},
        )

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        await self.hass.call_service(
            domain="media_player",
            service="volume_mute",
            target={"entity_id": self.player_id},
            service_data={"is_volume_muted": muted},
        )

    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        await self.hass.call_service(
            domain="media_player",
            service="turn_on" if powered else "turn_off",
            target={"entity_id": self.player_id},
        )

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        extra_data: dict[str, Any] = {
            # passing metadata to the player
            # so far only supported by google cast, but maybe others can follow
            "metadata": {
                "title": media.title,
                "artist": media.artist,
                "metadataType": 3,
                "album": media.album,
                "albumName": media.album,
                "images": [{"url": media.image_url}] if media.image_url else None,
                "imageUrl": media.image_url,
                "duration": media.duration,
            },
        }
        if self.extra_data.get("hass_domain") == "esphome":
            # tell esphome mediaproxy to bypass the proxy,
            # as MA already delivers an optimized stream
            extra_data["bypass_proxy"] = True

        # stop the player if it is already playing
        if self.playback_state == PlaybackState.PLAYING:
            await self.stop()

        await self.hass.call_service(
            domain="media_player",
            service="play_media",
            target={"entity_id": self.player_id},
            service_data={
                "media_content_id": media.uri,
                "media_content_type": "music",
                "enqueue": "replace",
                "extra": extra_data,
            },
        )

        # Optimistically update state
        self._attr_current_media = media
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Handle (provider native) playback of an announcement on given player."""
        self.logger.info(
            "Playing announcement %s on %s",
            announcement.uri,
            self.display_name,
        )
        if volume_level is not None:
            self.logger.warning(
                "Announcement volume level is not supported for player %s",
                self.display_name,
            )
        await self.hass.call_service(
            domain="media_player",
            service="play_media",
            service_data={
                "media_content_id": announcement.uri,
                "media_content_type": "music",
                "announce": True,
            },
            target={"entity_id": self.player_id},
        )
        # Wait until the announcement is finished playing
        # This is helpful for people who want to play announcements in a sequence
        media_info = await async_parse_tags(announcement.uri, require_duration=True)
        duration = media_info.duration or 5
        await asyncio.sleep(duration)
        self.logger.debug(
            "Playing announcement on %s completed",
            self.display_name,
        )

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """
        Handle SET_MEMBERS command on the player.

        Group or ungroup the given child player(s) to/from this player.
        Will only be called if the PlayerFeature.SET_MEMBERS is supported.

        :param player_ids_to_add: List of player_id's to add to the group.
        :param player_ids_to_remove: List of player_id's to remove from the group.
        """
        for player_id_to_remove in player_ids_to_remove or []:
            await self.hass.call_service(
                domain="media_player",
                service="unjoin",
                target={"entity_id": player_id_to_remove},
            )
        if player_ids_to_add:
            await self.hass.call_service(
                domain="media_player",
                service="join",
                service_data={"group_members": player_ids_to_add},
                target={"entity_id": self.player_id},
            )

    def update_from_compressed_state(self, state: CompressedState) -> None:
        """Handle updating the player with updated info in a HA CompressedState."""
        if "s" in state:
            self._attr_playback_state = StateMap.get(state["s"], PlaybackState.IDLE)
            self._attr_available = state["s"] not in UNAVAILABLE_STATES
            if PlayerFeature.POWER in self.supported_features:
                self._attr_powered = state["s"] not in OFF_STATES
        if "a" in state:
            self._update_attributes(state["a"])
        self.update_state()

    def _update_attributes(self, attributes: dict[str, Any]) -> None:
        """Update Player attributes from HA state attributes."""
        # process optional attributes - these may not be present in all states
        for key, value in attributes.items():
            if key == "friendly_name":
                self._attr_name = value
            elif key == "media_position":
                self._attr_elapsed_time = value
            elif key == "media_position_updated_at":
                self._attr_elapsed_time_last_updated = from_iso_string(value).timestamp()
            elif key == "volume_level":
                self._attr_volume_level = int(value * 100)
            elif key == "is_volume_muted":
                self._attr_volume_muted = value
            elif key == "group_members":
                group_members: list[str] = (
                    [
                        # ignore integrations that incorrectly set the group members attribute
                        # (e.g. linkplay)
                        x
                        for x in value
                        if x.startswith("media_player.")
                    ]
                    if value
                    else []
                )
                if group_members and group_members[0] == self.player_id:
                    # first in the list is the group leader
                    self._attr_group_members = group_members
                elif group_members and group_members[0] != self.player_id:
                    # this player is not the group leader
                    self._attr_group_members.clear()
                else:
                    self._attr_group_members.clear()
