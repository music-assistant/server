"""State synchronization mapper."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerSource

from music_assistant.providers.samsung_wam.features.playback.models import (
    WamPlaybackState,
    WamSource,
)

from .models import WamSpeakerAttributes

if TYPE_CHECKING:
    from pywam.speaker import Speaker

    from music_assistant.providers.samsung_wam.player import WamPlayer

PLAYBACK_STATE_MAP: dict[WamPlaybackState, PlaybackState] = {
    WamPlaybackState.PLAY: PlaybackState.PLAYING,
    WamPlaybackState.PAUSE: PlaybackState.PAUSED,
    WamPlaybackState.STOP: PlaybackState.IDLE,
}

PLAYER_SOURCE_MAP = {
    WamSource.WIFI: PlayerSource(id=WamSource.WIFI, name="Wi-Fi", passive=True),
    WamSource.BLUETOOTH: PlayerSource(id=WamSource.BLUETOOTH, name="Bluetooth", passive=False),
    WamSource.AUX: PlayerSource(id=WamSource.AUX, name="AUX", passive=True),
    WamSource.OPTICAL: PlayerSource(id=WamSource.OPTICAL, name="Optical", passive=True),
    WamSource.TV_SOUNDCONNECT: PlayerSource(
        id=WamSource.TV_SOUNDCONNECT, name="TV SoundConnect", passive=False
    ),
}


class StateSyncMapper:
    """Maps pywam speaker attributes to player state."""

    @staticmethod
    def create_speaker_attributes(wam_speaker: Speaker) -> WamSpeakerAttributes:
        """
        Create a type-safe attribute snapshot from the raw pywam object.

        :param wam_speaker: The underlying pywam speaker instance.
        :return: A WamSpeakerAttributes instance.
        """
        raw = wam_speaker.attribute
        return WamSpeakerAttributes(
            mac=str(raw.mac or ""),
            name=str(raw.name or "Unknown"),
            model=raw.model if isinstance(raw.model, str) else None,
            software_version=raw.software_version
            if isinstance(raw.software_version, str)
            else None,
            source=raw.source if isinstance(raw.source, str) else None,
            source_list=list(raw.source_list) if isinstance(raw.source_list, list) else [],
            group_leader_mac=raw.master_mac if isinstance(raw.master_mac, str) else None,
            is_group_child=bool(raw.is_slave),
            volume=int(raw.volume) if isinstance(raw.volume, (int, float)) else None,
            muted=bool(raw.muted) if isinstance(raw.muted, bool) else None,
            play_status=raw._playstatus if isinstance(raw._playstatus, str) else None,
        )

    @staticmethod
    def apply_attributes_to_player(
        player: WamPlayer,
        speaker_attrs: WamSpeakerAttributes,
        group_children: set[str],
        stream_active: bool,
    ) -> None:
        """
        Apply a full attribute snapshot to the player state.

        :param player: The WamPlayer to update.
        :param speaker_attrs: The current speaker attributes.
        :param group_children: Known group children for this player.
        :param stream_active: Whether the audio stream is currently active.
        """
        player._attr_available = True
        player._attr_name = speaker_attrs.name

        if speaker_attrs.volume is not None:
            player._attr_volume_level = speaker_attrs.volume
        if speaker_attrs.muted is not None:
            player._attr_volume_muted = speaker_attrs.muted

        play_status_str = speaker_attrs.play_status or "stop"
        try:
            play_status = WamPlaybackState(play_status_str)
            player._attr_playback_state = PLAYBACK_STATE_MAP.get(play_status, PlaybackState.IDLE)
        except ValueError:
            player._attr_playback_state = PlaybackState.IDLE

        if stream_active:
            player._attr_active_source = player.player_id
        else:
            player._attr_active_source = speaker_attrs.source

        new_sources: list[PlayerSource] = []
        for source_name in speaker_attrs.source_list:
            try:
                wam_source = WamSource(source_name)
                if source := PLAYER_SOURCE_MAP.get(wam_source):
                    new_sources.append(source)
            except ValueError:
                continue
        player._attr_source_list = new_sources

        if speaker_attrs.is_group_child and speaker_attrs.group_leader_mac:
            player.synced_to_internal = speaker_attrs.group_leader_mac
            player._attr_group_members = []
        else:
            player.synced_to_internal = None
            if group_children:
                player._attr_group_members = [player.player_id, *sorted(group_children)]
            else:
                player._attr_group_members = []
