"""Home Assistant Player implementation."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

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
from music_assistant.helpers.tags import async_parse_tags
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.providers.hass.constants import (
    OFF_STATES,
    UNAVAILABLE_STATES,
    MediaPlayerEntityFeature,
    StateMap,
)

from .constants import CONF_ENTRY_WARN_HASS_INTEGRATION, WARN_HASS_INTEGRATIONS
from .helpers import ESPHomeSupportedAudioFormat

if TYPE_CHECKING:
    from hass_client.models import CompressedState
    from music_assistant_models.config_entries import ConfigEntry

    from .provider import HomeAssistantPlayerProvider


DEFAULT_PLAYER_CONFIG_ENTRIES = (
    CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3,
    CONF_ENTRY_HTTP_PROFILE,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
)


class HassPlayer(Player):
    """Home Assistant Player implementation."""

    provider: HomeAssistantPlayerProvider

    def __init__(
        self,
        provider: HomeAssistantPlayerProvider,
        player_id: str,
        hass_state: CompressedState,
        dev_info: dict,
        extra_player_data: dict,
    ) -> None:
        """Initialize the Home Assistant Player."""
        super().__init__(provider, player_id)

        self.hass_state = hass_state
        self.extra_data = extra_player_data

        # Set player attributes from Home Assistant state
        self._attr_type = PlayerType.PLAYER
        self._attr_name = hass_state["attributes"]["friendly_name"]
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

        # Set grouping support if applicable
        if (
            hass_domain := self.extra_data.get("hass_domain")
        ) and MediaPlayerEntityFeature.GROUPING in MediaPlayerEntityFeature(
            self.hass_state["attributes"]["supported_features"]
        ):
            self._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
            self._attr_can_group_with = {
                x["entity_id"]
                for x in self.provider.hass.get_entities("media_player")
                if x.get("platform") == hass_domain
            }

        # Set initial state
        if hass_state["state"] in OFF_STATES:
            self._attr_powered = False
        elif hass_state["state"] not in UNAVAILABLE_STATES:
            self._attr_powered = True

        if "volume_level" in hass_state["attributes"]:
            self._attr_volume_level = int(hass_state["attributes"]["volume_level"] * 100)
        if "is_volume_muted" in hass_state["attributes"]:
            self._attr_volume_muted = hass_state["attributes"]["is_volume_muted"]

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        entries = await super().get_config_entries()
        entries = (*entries, *DEFAULT_PLAYER_CONFIG_ENTRIES)
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
            return (
                *entries,
                # New ESPHome mediaplayer (used in Voice PE) uses FLAC 48khz/16 bits
                CONF_ENTRY_FLOW_MODE_ENFORCED,
                CONF_ENTRY_HTTP_PROFILE_FORCED_2,
                create_output_codec_config_entry(True, codec),
                CONF_ENTRY_ENABLE_ICY_METADATA_HIDDEN,
                create_sample_rates_config_entry(
                    supported_sample_rates=supported_sample_rates,
                    supported_bit_depths=supported_bit_depths,
                    hidden=True,
                ),
                # although the Voice PE supports announcements,
                # it does not support volume for announcements
                *HIDDEN_ANNOUNCE_VOLUME_CONFIG_ENTRIES,
            )

        # add alert if player is a known player type that has a native provider in MA
        if self.extra_data.get("hass_domain") in WARN_HASS_INTEGRATIONS:
            base_entries = (CONF_ENTRY_WARN_HASS_INTEGRATION, *entries)

        # enable flow mode by default if player does not report enqueue support
        if MediaPlayerEntityFeature.MEDIA_ENQUEUE not in self.extra_data["hass_supported_features"]:
            base_entries = (*base_entries, CONF_ENTRY_FLOW_MODE_DEFAULT_ENABLED)

        return base_entries

    async def stop(self, player_id: str) -> None:
        """Send STOP command to player."""
        try:
            await self.provider.hass.call_service(
                domain="media_player",
                service="media_stop",
                target={"entity_id": player_id},
            )
        except FailedCommand as exc:
            # some HA players do not support STOP
            if "does not support this service" not in str(exc):
                raise
            if player := self.mass.players.get(player_id):
                if PlayerFeature.PAUSE in player.supported_features:
                    await self.cmd_pause(player_id)

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        extra_data = {
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
            },
        }

        if media.uri.endswith((".flac", ".wav")):
            # Parse tags for lossless files to provide metadata to the player
            try:
                parsed_meta = await async_parse_tags(media.uri)
                if parsed_meta and parsed_meta.duration:
                    extra_data["metadata"]["duration"] = parsed_meta.duration
            except Exception:
                # Ignore errors
                pass

        await self.provider.hass.call_service(
            domain="media_player",
            service="play_media",
            target={"entity_id": self.player_id},
            service_data={
                "media_content_id": media.uri,
                "media_content_type": "music",
                "extra": extra_data,
            },
        )

        # Optimistically update state
        self._attr_current_media = media
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        await self.provider.hass.call_service(
            domain="media_player",
            service="media_play",
            target={"entity_id": self.player_id},
        )

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        await self.provider.hass.call_service(
            domain="media_player",
            service="media_pause",
            target={"entity_id": self.player_id},
        )

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self.provider.hass.call_service(
            domain="media_player",
            service="volume_set",
            target={"entity_id": self.player_id},
            service_data={"volume_level": volume_level / 100},
        )

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        await self.provider.hass.call_service(
            domain="media_player",
            service="volume_mute",
            target={"entity_id": self.player_id},
            service_data={"is_volume_muted": muted},
        )

    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        if powered:
            await self.provider.hass.call_service(
                domain="media_player",
                service="turn_on",
                target={"entity_id": self.player_id},
            )
        else:
            await self.provider.hass.call_service(
                domain="media_player",
                service="turn_off",
                target={"entity_id": self.player_id},
            )

    def update_from_hass_state(self, state: CompressedState) -> None:
        """Update player state from Home Assistant state."""
        self.state_data = state

        # Update basic attributes
        self._attr_name = state["attributes"]["friendly_name"]
        self._attr_available = state["state"] not in UNAVAILABLE_STATES
        self._attr_playback_state = StateMap.get(state["state"], PlaybackState.IDLE)

        # Update power state
        if state["state"] in OFF_STATES:
            self._attr_powered = False
        elif state["state"] not in UNAVAILABLE_STATES:
            self._attr_powered = True

        # Update volume
        if "volume_level" in state["attributes"]:
            self._attr_volume_level = int(state["attributes"]["volume_level"] * 100)
        if "is_volume_muted" in state["attributes"]:
            self._attr_volume_muted = state["attributes"]["is_volume_muted"]

        # Update media info if available
        if state["attributes"].get("media_title"):
            self._attr_current_media = PlayerMedia(
                uri=state["attributes"].get("media_content_id", ""),
                title=state["attributes"].get("media_title"),
                artist=state["attributes"].get("media_artist"),
                album=state["attributes"].get("media_album_name"),
                image_url=state["attributes"].get("entity_picture_local"),
            )

        self.update_state()
