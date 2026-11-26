"""
Sendspin player role of the bridge of a slimproto player.

The role plays one of the PCM formats the hello of its client lists, and takes the one
Sendspin prefers. That makes the Sendspin provider pick its format for every playback
session, as it does for a Sendspin player that follows the sample rate of what it plays.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from aiosendspin.server.roles.registry import register_role

from music_assistant.providers.sendspin.bridge_role import BridgePlayerRole

if TYPE_CHECKING:
    from aiosendspin.models.player import SupportedAudioFormat
    from aiosendspin.models.types import AudioCodec, PlayerCommand
    from aiosendspin.server.audio import AudioFormat

SLIMPROTO_BRIDGE_ROLE_ID = "player@_slimproto_bridge"


class SlimprotoBridgeRole(BridgePlayerRole):
    """
    Bridge player role that plays the format Sendspin prefers for its player.

    Next to what a bridge player role does, it is a player role as aiosendspin defines one
    (``PlayerRoleProtocol``): that is what the Sendspin provider selects formats on.
    """

    # returns the most bits a sample of the next stream has, None when that is not known
    _bit_depth_limit: Callable[[], int | None] | None = None

    @property
    def role_id(self) -> str:
        """Return role identifier."""
        return SLIMPROTO_BRIDGE_ROLE_ID

    def limit_bit_depth(self, bit_depth_limit: Callable[[], int | None]) -> None:
        """
        Play a format in no more bits than the audio it is asked for has.

        :param bit_depth_limit: Returns the most bits a sample of the next stream has, None
            when that is not known.
        """
        self._bit_depth_limit = bit_depth_limit

    @property
    def state_supported_commands(self) -> list[PlayerCommand]:
        """Return the commands the client declares in its state: none, its hello lists them."""
        return []

    def get_supported_formats(self) -> list[SupportedAudioFormat] | None:
        """Return the formats of the hello of the client, the one it plays by default first."""
        if (support := self._client.info.player_support) is None:
            return None
        return list(support.supported_formats)

    def set_preferred_format(
        self,
        audio_format: AudioFormat | None,
        codec: AudioCodec | None = None,
    ) -> bool:
        """
        Play another of the formats of the hello from the next stream on.

        Where the bit depth of the audio is known, the format is played in no more bits
        than that. A stream that plays is started again by Sendspin, in the format asked
        for.

        :param audio_format: The format to play, None for the default format.
        :param codec: The codec of that format.
        :return: Whether the role plays that format now, False for one the hello does not list.
        """
        formats = self.get_supported_formats() or []
        chosen = next(
            (
                fmt
                for fmt in formats
                if audio_format is None
                or (
                    fmt.codec == codec
                    and fmt.sample_rate == audio_format.sample_rate
                    and fmt.bit_depth == audio_format.bit_depth
                    and fmt.channels == audio_format.channels
                )
            ),
            None,
        )
        if chosen is None:
            return False
        limit = self._bit_depth_limit() if self._bit_depth_limit else None
        if limit is not None and limit < chosen.bit_depth:
            # the highest depth within the limit, else the lowest the player plays
            chosen = min(
                (
                    fmt
                    for fmt in formats
                    if (fmt.codec, fmt.sample_rate, fmt.channels)
                    == (chosen.codec, chosen.sample_rate, chosen.channels)
                ),
                key=lambda fmt: (
                    (0, -fmt.bit_depth) if fmt.bit_depth <= limit else (1, fmt.bit_depth)
                ),
            )
        previous = self.preferred_format
        self.setup_audio_requirements(chosen.sample_rate, chosen.bit_depth, chosen.channels)
        if previous is not None and (
            previous.sample_rate,
            previous.bit_depth,
            previous.channels,
        ) != (chosen.sample_rate, chosen.bit_depth, chosen.channels):
            self._client.group.on_role_format_changed(self)
        return True

    def get_output_delay_ms(self) -> int:
        """Return the output delay Sendspin applies for the player: its bridge applies its own."""
        return 0

    def set_output_delay(self, delay_ms: int) -> None:
        """
        Take an output delay for the player, which goes unused.

        The delay of a slimproto player is the sync delay setting of that player, which
        its bridge applies.

        :param delay_ms: The output delay in milliseconds.
        """


register_role(SLIMPROTO_BRIDGE_ROLE_ID, lambda client: SlimprotoBridgeRole(client=client))
