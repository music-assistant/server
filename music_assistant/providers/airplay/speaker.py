"""A speaker of a set of AirPlay devices that plays as one player."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import PlaybackState, ProtocolRole

from .player import AirPlayPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, PlayerConfig

    from .provider import AirPlayProvider
    from .stream import AirPlayStream


class AirPlaySpeaker(AirPlayPlayer):
    """
    A HomePod of the home theater set of an Apple TV, or the second HomePod of a stereo pair.

    It renders the audio of its set and is never registered as a protocol of its own: it
    plays with the settings and the volume of its set, or the defaults of its device, and
    stores nothing itself.
    """

    _attr_role = ProtocolRole.AUDIO

    @property
    def owner(self) -> AirPlayPlayer | None:
        """Return the protocol of the set this speaker belongs to, None while it is not there."""
        if (membership := self.set_membership) is None:
            return None
        return cast("AirPlayProvider", self.provider).get_set_carrier(membership.set_id)

    @property
    def settings(self) -> PlayerConfig:
        """Return the config of the set, whose settings apply to the speaker."""
        return owner.config if (owner := self.owner) else self.config

    @property
    def output_plan_protocol_id(self) -> str:
        """Return the protocol of the set, whose output settings apply to the speaker."""
        return owner.output_plan_protocol_id if (owner := self.owner) else self.protocol_id

    @property
    def password_invalid(self) -> bool:
        """Return whether the speaker or another device of the set rejected its password."""
        return owner.password_invalid if (owner := self.owner) else False

    @property
    def owns_volume(self) -> bool:
        """Return whether the set owns its volume."""
        return owner.owns_volume if (owner := self.owner) else True

    @property
    def volume_level(self) -> int | None:
        """Return the volume of the set."""
        return owner.volume_level if (owner := self.owner) else None

    @property
    def volume_muted(self) -> bool | None:
        """Return whether the set is muted."""
        return owner.volume_muted if (owner := self.owner) else None

    @property
    def hires_playback_enabled(self) -> bool:
        """Return the 24-bit default of the device."""
        return self._hires_route and self._hires_default_enabled

    def get_setup_value(self, key: str, default: ConfigValueType = None) -> ConfigValueType:
        """Return the default: a speaker of a set takes no pairing."""
        return default

    def set_available(self, available: bool) -> None:
        """
        Set whether the speaker has a current _airplay._tcp record that names its set.

        :param available: Whether such a record is current.
        """
        self._attr_available = available

    def set_password_invalid(self, invalid: bool) -> None:
        """
        Mark the password of the set as rejected, or as working again.

        :param invalid: True when the speaker rejected the password.
        """
        if owner := self.owner:
            owner.set_password_invalid(invalid)

    def set_state_from_stream(
        self,
        state: PlaybackState | None = None,
        elapsed_time: float | None = None,
        stream: AirPlayStream | None = None,
    ) -> None:
        """
        Set the playback state of the speaker's stream, which is also that of its set.

        :param state: New playback state (or None to keep current).
        :param elapsed_time: New elapsed time (or None to keep current).
        :param stream: The stream instance sending this update (for validation).
        """
        if stream is not None and stream != self.stream:
            return
        super().set_state_from_stream(state, elapsed_time, stream)
        if (owner := self.owner) is None:
            return
        if state == PlaybackState.IDLE and any(
            endpoint is not self and endpoint.stream and endpoint.stream.running
            for endpoint in owner.endpoints
        ):
            # the set still plays on another of its speakers
            return
        owner.set_state_from_stream(state, elapsed_time)

    def update_volume_from_device(self, volume: int) -> None:
        """
        Hand a volume the device reported to its set.

        :param volume: The level the device reported.
        """
        if owner := self.owner:
            owner.update_volume_from_device(volume)

    def release_foreign_mute_latch(self) -> None:
        """Clear the mute latch of the set when another control owns its mute."""
        if owner := self.owner:
            owner.release_foreign_mute_latch()

    def cancel_group_rejoin(self) -> None:
        """Cancel a pending automatic group re-join of the set."""
        if owner := self.owner:
            owner.cancel_group_rejoin()
