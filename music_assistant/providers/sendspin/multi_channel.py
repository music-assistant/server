"""
Multi-channel players of the Sendspin provider.

A multi-channel player combines Sendspin speakers into one player, each speaker playing its own
channel. It is a silent client of the server that leads the Sendspin group its speakers play in,
or follows another leader together with them. Its protocol record holds its speakers, which have
no protocol of their own while they belong to it.
"""

from __future__ import annotations

import asyncio
import base64
import re
import secrets
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from aiosendspin.server import VolumeChangedEvent
from aiosendspin.server.events import ClientGroupChangedEvent
from aiosendspin.server.roles.player.events import OutputDelayChangedEvent
from aiosendspin.server.roles.player.types import PlayerRoleProtocol
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ChannelLayout,
    ConfigEntryType,
    PlaybackState,
    PlayerType,
    SpeakerChannel,
)
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import (
    CONF_CROSSOVER_FREQUENCY,
    CONF_HIGH_PASS_MAIN_SPEAKERS,
    CONF_OUTPUT_CHANNELS,
    CONF_PLAYER_PROTOCOLS,
    CONF_SUBWOOFER_LEVEL,
    CONF_SUBWOOFER_OWN_CROSSOVER,
    DEFAULT_CROSSOVER_FREQUENCY,
    DEFAULT_HIGH_PASS_MAIN_SPEAKERS,
    DEFAULT_SUBWOOFER_LEVEL,
    DEFAULT_SUBWOOFER_OWN_CROSSOVER,
)

from .constants import (
    CONF_SENDSPIN_STATIC_DELAY,
    CONF_SPEAKER_CHANNEL,
    CONF_SPEAKER_CHANNEL_SETTING,
    CONF_SPEAKER_CLIENT_ID,
    CONF_SPEAKER_DISPLAY_NAME,
    CONF_SPEAKER_NAME,
    CONF_SPEAKER_OUTPUT_DELAY,
    CONF_SPEAKER_STATE,
    DEFAULT_SENDSPIN_STATIC_DELAY,
)
from .player import SendspinPlayer

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiosendspin.models import AudioCodec
    from aiosendspin.models.core import ClientHelloPayload
    from aiosendspin.server import ClientEvent, SendspinGroup
    from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat
    from aiosendspin.server.client import SendspinClient
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.models.player import Player

    from .provider import SendspinProvider

# The channels of each layout a multi-channel player can have.
LAYOUT_CHANNELS: dict[ChannelLayout, tuple[SpeakerChannel, ...]] = {
    ChannelLayout.STEREO: (SpeakerChannel.FRONT_LEFT, SpeakerChannel.FRONT_RIGHT),
    ChannelLayout.LAYOUT_2_1: (
        SpeakerChannel.FRONT_LEFT,
        SpeakerChannel.FRONT_RIGHT,
        SpeakerChannel.LFE,
    ),
    ChannelLayout.LAYOUT_1_1: (SpeakerChannel.FRONT_CENTER, SpeakerChannel.LFE),
}
# The crossover frequencies (Hz) and the subwoofer levels (dB) a player with a subwoofer offers.
CROSSOVER_FREQUENCIES = range(40, 201, 10)
SUBWOOFER_LEVEL_RANGE = (-12, 0)
# A stored value of one speaker of a multi-channel player, by the number of the speaker.
SPEAKER_KEY = re.compile(r"^speaker_(?P<number>\d+)_(?P<field>.+)$")
# Seconds a speaker that connected again has to report the state it kept, before it takes the
# volume of its player.
SPEAKER_JOIN_GRACE = 2.0
# Delay to let the changes of several speakers end in one restart of the playback.
CHANNEL_CHANGE_DEBOUNCE = 1.0
# Seconds a speaker that changed group has to settle, as a move passes through a solo group.
SPEAKER_GROUP_SETTLE = 0.5
# Seconds within which a speaker reports a level or delay it got, if it reports it at all.
UNREPORTED_EXPIRY = 5.0


@dataclass(frozen=True, slots=True)
class SetSpeaker:
    """A speaker of a multi-channel player, as the record of the player holds it."""

    number: int
    client_id: str
    name: str
    channel: SpeakerChannel


def new_group_id() -> str:
    """Return a new group id: 32 random bytes in base64url without padding."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def layout_of(channels: Iterable[SpeakerChannel]) -> ChannelLayout | None:
    """
    Return the layout that speakers on these channels make up, None for no known layout.

    :param channels: The channel of each speaker.
    """
    channels = sorted(channels)
    for layout, layout_channels in LAYOUT_CHANNELS.items():
        if channels == sorted(layout_channels):
            return layout
    return None


def stored_speakers(values: Mapping[str, Any]) -> list[SetSpeaker]:
    """
    Return the speakers in the stored values of a multi-channel player.

    :param values: The stored values of the protocol of the multi-channel player.
    """
    speakers: list[SetSpeaker] = []
    number = 1
    while isinstance(client_id := values.get(CONF_SPEAKER_CLIENT_ID.format(n=number)), str):
        name = values.get(CONF_SPEAKER_NAME.format(n=number))
        speakers.append(
            SetSpeaker(
                number=number,
                client_id=client_id,
                name=name if isinstance(name, str) else client_id,
                channel=SpeakerChannel(str(values.get(CONF_SPEAKER_CHANNEL.format(n=number)))),
            )
        )
        number += 1
    return speakers


class SendspinMultiChannelPlayer(SendspinPlayer):
    """
    A player made of Sendspin speakers, each of which plays its own channel.

    One volume and one DSP apply to the whole player. While a main speaker is away, the other
    main speakers play every channel, and while the subwoofer is away the main speakers play the
    full range.
    """

    _speakers: tuple[SetSpeaker, ...] = ()

    def __init__(
        self,
        provider: SendspinProvider,
        protocol_id: str,
        initial_hello: ClientHelloPayload | None = None,
    ) -> None:
        """
        Initialize the multi-channel player.

        :param provider: The Sendspin provider instance.
        :param protocol_id: The client id of the silent client of the player.
        :param initial_hello: Optional hello payload of that client.
        """
        super().__init__(provider, protocol_id, initial_hello)
        self._attr_type = PlayerType.MULTI_CHANNEL
        self._speaker_listeners: dict[str, tuple[SendspinClient, Callable[[], None]]] = {}
        # levels and delays sent to a speaker that it has not reported back yet
        self._sent_volumes: dict[str, list[tuple[int, float]]] = {}
        self._sent_mutes: dict[str, list[tuple[bool, float]]] = {}
        self._sent_delays: dict[str, list[tuple[int, float]]] = {}
        # speakers that connected again, which still report the state they kept
        self._joining: set[str] = set()
        # speakers that left the group on their own, back at the next playback start
        self._left: set[str] = set()
        # subwoofers kept out of the group while no main speaker plays in it
        self._set_aside: set[str] = set()
        self._take_lock = asyncio.Lock()
        self._load_speakers()
        self._played_channels: dict[str, SpeakerChannel | None] = {
            speaker.client_id: speaker.channel for speaker in self._speakers
        }
        self._crossover = self._crossover_settings()
        self._refresh_speaker_state()

    @property
    def supported_sample_rates(self) -> list[tuple[int, int]] | None:
        """Return the (sample_rate, bit_depth) tuples the connected speakers play."""
        rates = {
            (fmt.sample_rate, fmt.bit_depth)
            for _, role in self._speaker_roles()
            for fmt in self._top_codec_formats(role)
        }
        return sorted(rates) if rates else [(44100, 16)]

    def speaker_channel(self, client_id: str) -> SpeakerChannel | None:
        """
        Return the channel a speaker of this player plays.

        :param client_id: The client id of the speaker.
        :return: Its channel, or None for a main speaker while another main speaker of this
            player is away, when it plays every channel.
        """
        return self._current_channels().get(client_id)

    @property
    def subwoofer_plays(self) -> bool:
        """Return whether the subwoofer of this player plays, taking the bass of the others."""
        return SpeakerChannel.LFE in self._current_channels().values()

    def plays_with(self, client_id: str) -> bool:
        """
        Return whether a speaker of this player is in the group this player plays in.

        :param client_id: The client id of the speaker.
        """
        client = self._sendspin_provider.server_api.get_client(client_id)
        # a group lists a client that joins before the client names its group, and the
        # session plans the client in between
        return client is not None and client in self.api.group.clients

    async def take_speakers(self, *, rejoin: bool = False) -> None:
        """
        Take the speakers of this player into the group it plays in.

        A speaker that still has a protocol of its own loses it first: the speaker belongs
        to this player, and a player that has no other protocol left goes with it. A speaker
        that is unavailable stays out, and so does one that left the group on its own.

        :param rejoin: Take back the speakers that left the group on their own, as a new
            playback session does.
        """
        async with self._take_lock:
            for speaker in self._speakers:
                record = self.mass.config.get_protocol_record(speaker.client_id)
                if record is not None and record.get("provider") == self.provider.instance_id:
                    await self.mass.players.remove_protocol(speaker.client_id)
            for speaker in self._speakers:
                await self._take_speaker(speaker.client_id, rejoin=rejoin)
        self._on_speakers_changed(restart=not rejoin)

    def add_speaker(self, member: SendspinPlayer, channel: SpeakerChannel) -> None:
        """
        Store a speaker as the next speaker of this player, which take_speakers then takes.

        :param member: The Sendspin protocol of the speaker.
        :param channel: The channel the speaker plays.
        :raises InvalidDataError: When the channels make up no layout a player can have.
        """
        if layout_of([*(speaker.channel for speaker in self._speakers), channel]) is None:
            raise InvalidDataError(
                f"{member.display_name} can not play {channel.value} in {self.display_name}",
                translation_key="multi_channel_channels",
                translation_owner=self._sendspin_provider.translation_owner,
            )
        number = len(self._speakers) + 1
        for key, value in (
            (CONF_SPEAKER_CLIENT_ID, member.protocol_id),
            (CONF_SPEAKER_NAME, member.display_name),
            (CONF_SPEAKER_CHANNEL, channel.value),
            (
                CONF_SPEAKER_OUTPUT_DELAY,
                member.config.get_value(CONF_SENDSPIN_STATIC_DELAY, member.static_delay_default_ms),
            ),
        ):
            self.mass.config.set_protocol_value(self.protocol_id, key.format(n=number), value)
        self._load_speakers()

    def remove_speaker(self, number: int) -> str:
        """
        Let a speaker of this player go, which plays on with its other speakers.

        The speakers after it move up a number.

        :param number: The number of the speaker.
        :return: The client id of the speaker.
        :raises InvalidDataError: When this player has no such speaker, or its other speakers
            make up no layout a player can have.
        """
        speaker = next((s for s in self._speakers if s.number == number), None)
        if speaker is None:
            raise InvalidDataError(
                f"{self.display_name} has no speaker {number}",
                translation_key="multi_channel_unknown_speaker",
                translation_owner=self._sendspin_provider.translation_owner,
            )
        if layout_of(s.channel for s in self._speakers if s is not speaker) is None:
            raise InvalidDataError(
                f"Without {speaker.name} the speakers of {self.display_name} make up no layout",
                translation_key="multi_channel_last_speakers",
                translation_owner=self._sendspin_provider.translation_owner,
                translation_args=[speaker.name],
            )
        client_id = speaker.client_id
        self._stop_listening(client_id)
        self.mass.cancel_timer(f"sendspin_mc_join_{client_id}")
        self.mass.cancel_timer(f"sendspin_mc_group_{client_id}")
        self._joining.discard(client_id)
        self._left.discard(client_id)
        self._set_aside.discard(client_id)
        for sent in (self._sent_volumes, self._sent_mutes, self._sent_delays):
            sent.pop(client_id, None)
        values = (self.mass.config.get_protocol_record(self.protocol_id) or {}).get("values") or {}
        self.mass.config.set(
            f"{CONF_PLAYER_PROTOCOLS}/{self.protocol_id}/values",
            _without_speaker(values, number),
        )
        self._load_speakers()
        self._on_speakers_changed()
        return client_id

    def on_speaker_changed(self, client_id: str) -> None:
        """
        Follow a speaker of this player that connected, went away or was removed.

        :param client_id: The client id of the speaker.
        """
        client = self._sendspin_provider.server_api.get_client(client_id)
        if client is not None and client.connection is not None:
            self._joining.add(client_id)
            self.mass.call_later(
                SPEAKER_JOIN_GRACE,
                self._join_speaker,
                client_id,
                task_id=f"sendspin_mc_join_{client_id}",
            )
        else:
            self._joining.discard(client_id)
            self.mass.cancel_timer(f"sendspin_mc_join_{client_id}")
            # what was sent over the connection that ended may never have arrived
            self._sent_volumes.pop(client_id, None)
            self._sent_mutes.pop(client_id, None)
            self._sent_delays.pop(client_id, None)
            if client is None:
                self._stop_listening(client_id)
                self._left.discard(client_id)
                self._set_aside.discard(client_id)
        self._on_speakers_changed()

    async def volume_set(self, volume_level: int) -> None:
        """Set the volume of every speaker of this player."""
        self._attr_volume_level = volume_level
        for client_id, role in self._speaker_roles():
            self._send_level(client_id, role)
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Mute or unmute every speaker of this player."""
        self._attr_volume_muted = muted
        for client_id, role in self._speaker_roles():
            self._send_level(client_id, role)
        self.update_state()

    async def follow_session_sample_rate(
        self,
        item_streamdetails: StreamDetails | None,
        *,
        crossfade_enabled: bool = False,
        overlay_active: bool = False,
        later_item: bool = False,
    ) -> None:
        """
        Prepare the speakers for a new playback session, or for a later item of one.

        The speakers that left the group on their own come back for a new session, and the
        format of every speaker follows the sample rate of the item the session sends.

        :param item_streamdetails: Stream details of the item the session sends, if any.
        :param crossfade_enabled: Whether the session blends the audio of its items.
        :param overlay_active: Whether an audio overlay is mixed into the session.
        :param later_item: The item follows another one this player played in the session.
        """
        if not later_item:
            await self.take_speakers(rejoin=True)
        await super().follow_session_sample_rate(
            item_streamdetails,
            crossfade_enabled=crossfade_enabled,
            overlay_active=overlay_active,
            later_item=later_item,
        )
        for _, role in self._speaker_roles():
            role.set_preferred_format(*self._session_format(role, self._session_sample_rate))

    async def get_speaker_session_format(
        self, client_id: str, start_streamdetails: StreamDetails
    ) -> tuple[SendspinAudioFormat, AudioCodec] | None:
        """
        Return the Sendspin format and codec a speaker plays in a session starting with an item.

        The speakers follow the sample rate this player picks for the session.

        :param client_id: The client id of the speaker.
        :param start_streamdetails: Stream details of the first item of the session.
        :return: The format and codec, or None for a speaker that does not play with this player.
        """
        role = next((r for speaker_id, r in self._speaker_roles() if speaker_id == client_id), None)
        if role is None:
            return None
        pcm_format = await self.mass.streams.audio.select_flow_pcm_format(
            self, start_streamdetails=start_streamdetails
        )
        return self._played_session_format(role, pcm_format.sample_rate)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return the config entries of this player: one set per speaker."""
        entries = [
            # each speaker plays its channel, the player itself every channel
            ConfigEntry(
                key=CONF_OUTPUT_CHANNELS,
                type=ConfigEntryType.STRING,
                default_value="stereo",
                required=False,
                hidden=True,
            ),
        ]
        if self._has_subwoofer():
            entries.extend(self._crossover_entries())
        channel_options = [ConfigValueOption(channel.value) for channel in self._layout_channels()]
        for speaker in self._speakers:
            number = speaker.number
            online = self._is_online(speaker.client_id)
            entries.extend(
                (
                    ConfigEntry(
                        key=CONF_SPEAKER_STATE.format(n=number),
                        type=ConfigEntryType.LABEL,
                        default_value="online" if online else "offline",
                        translation_key="speaker_online"
                        if online
                        else self._offline_translation_key(speaker),
                        translation_params=[speaker.name],
                    ),
                    # the record has no entries, so a config save never changes it: the name
                    # shows under a key of its own, and a channel comes in through a setting
                    ConfigEntry(
                        key=CONF_SPEAKER_DISPLAY_NAME.format(n=number),
                        type=ConfigEntryType.LABEL,
                        default_value=speaker.name,
                        translation_key="speaker_display_name",
                    ),
                    ConfigEntry(
                        key=CONF_SPEAKER_CHANNEL_SETTING.format(n=number),
                        type=ConfigEntryType.STRING,
                        default_value=speaker.channel.value,
                        options=channel_options,
                        translation_key="speaker_channel",
                        translation_params=[speaker.name],
                    ),
                    ConfigEntry(
                        key=CONF_SPEAKER_OUTPUT_DELAY.format(n=number),
                        type=ConfigEntryType.INTEGER,
                        default_value=DEFAULT_SENDSPIN_STATIC_DELAY,
                        range=(0, 5000),
                        required=False,
                        immediate_apply=True,
                        translation_key="speaker_output_delay",
                        translation_params=[speaker.name],
                    ),
                )
            )
        return entries

    async def on_config_updated(self) -> None:
        """Apply a changed channel or output delay of a speaker, or a changed crossover."""
        channels_changed = self._take_channel_settings()
        self._load_speakers()
        for client_id, role in self._speaker_roles():
            self._send_output_delay(client_id, role)
        crossover, self._crossover = self._crossover, self._crossover_settings()
        self._on_speakers_changed(channels_changed=channels_changed or crossover != self._crossover)

    async def on_attach(self, player: Player) -> None:
        """Take the speakers into the group of this player once it is attached."""
        await super().on_attach(player)
        self.mass.create_task(self.take_speakers())

    async def on_detach(self) -> None:
        """Stop following the speakers when this player is detached."""
        for client_id in list(self._speaker_listeners):
            self._stop_listening(client_id)
        for speaker in self._speakers:
            self.mass.cancel_timer(f"sendspin_mc_join_{speaker.client_id}")
            self.mass.cancel_timer(f"sendspin_mc_group_{speaker.client_id}")
        self._joining.clear()
        self.mass.cancel_timer(f"sendspin_mc_channels_{self.protocol_id}")
        await super().on_detach()

    async def on_remove(self) -> None:
        """Let the speakers go, which come back as players of their own."""
        await self._sendspin_provider.release_multi_channel_player(self.protocol_id)

    @property
    def _sendspin_provider(self) -> SendspinProvider:
        """Return the Sendspin provider of this player."""
        return cast("SendspinProvider", self.provider)

    def _refresh_client_info(
        self,
        sendspin_client: SendspinClient,
        hello_payload: ClientHelloPayload | None = None,
    ) -> None:
        """Refresh the player from its silent client, as the player of a device."""
        super()._refresh_client_info(sendspin_client, hello_payload=hello_payload)
        self.is_web_player = False
        self._attr_expose_to_ha_by_default = True
        self._attr_hidden_by_default = False
        self._attr_private = False
        self._attr_session_only = False
        self._attr_available = self._main_speaker_online()

    def _on_group_changed(self, new_group: SendspinGroup) -> None:
        """Bring the speakers along to the group this player joined."""
        super()._on_group_changed(new_group)
        if self.player is not None:
            self.mass.create_task(self.take_speakers())

    def _load_speakers(self) -> None:
        """Read the speakers from the stored record of this player."""
        record = self.mass.config.get_protocol_record(self.protocol_id) or {}
        self._speakers = tuple(stored_speakers(record.get("values") or {}))
        self._attr_channel_layout = layout_of(speaker.channel for speaker in self._speakers)

    def _layout_channels(self) -> tuple[SpeakerChannel, ...]:
        """Return the channels the speakers of this player can play."""
        return LAYOUT_CHANNELS.get(
            self._attr_channel_layout or ChannelLayout.UNKNOWN,
            tuple(speaker.channel for speaker in self._speakers),
        )

    def _take_channel_settings(self) -> bool:
        """
        Store the channels the user set for the speakers in the record of this player.

        A channel taken from a speaker that kept its own swaps the two. Channels that make up
        no layout change nothing.

        :return: Whether the channel of a speaker changed.
        """
        values = (self.mass.config.get_protocol_record(self.protocol_id) or {}).get("values") or {}
        layout_channels = {channel.value for channel in self._layout_channels()}
        previous = {speaker.number: speaker.channel for speaker in self._speakers}
        channels = dict(previous)
        for speaker in self._speakers:
            key = CONF_SPEAKER_CHANNEL_SETTING.format(n=speaker.number)
            if key not in values:
                continue
            channel = values[key]
            # a setting that stayed would shadow the channel the record gets from a swap
            self.mass.config.remove(f"{CONF_PLAYER_PROTOCOLS}/{self.protocol_id}/values/{key}")
            if channel in layout_channels:
                channels[speaker.number] = SpeakerChannel(channel)
        set_numbers = [number for number in channels if channels[number] != previous[number]]
        for number in set_numbers:
            # a channel taken from a speaker that kept its own swaps the two
            other = next(
                (
                    other
                    for other, channel in channels.items()
                    if other not in set_numbers and channel == channels[number]
                ),
                None,
            )
            if other is not None:
                channels[other] = previous[number]
        if layout_of(channels.values()) is None:
            return False
        changed = False
        for number, channel in channels.items():
            if channel != previous[number]:
                self.mass.config.set_protocol_value(
                    self.protocol_id, CONF_SPEAKER_CHANNEL.format(n=number), channel.value
                )
                changed = True
        return changed

    def _speaker_number(self, client_id: str) -> int | None:
        """Return the number of a speaker of this player in its record."""
        return next((s.number for s in self._speakers if s.client_id == client_id), None)

    def _is_online(self, client_id: str) -> bool:
        """Return whether a speaker of this player is connected."""
        client = self._sendspin_provider.server_api.get_client(client_id)
        return client is not None and client.connection is not None

    def _speaker_roles(self) -> list[tuple[str, PlayerRoleProtocol]]:
        """Return the player role of every connected speaker that plays with this player."""
        roles: list[tuple[str, PlayerRoleProtocol]] = []
        for client_id, (client, _) in self._speaker_listeners.items():
            if (
                client.connection is None
                or client_id in self._joining
                or client.group is not self.api.group
            ):
                continue
            role = next(
                (r for r in client.roles_by_family("player") if isinstance(r, PlayerRoleProtocol)),
                None,
            )
            if role is not None:
                roles.append((client_id, role))
        return roles

    def _current_channels(self) -> dict[str, SpeakerChannel | None]:
        """
        Return the channel each speaker plays now.

        A main speaker plays every channel (None) while another main speaker is away, and a
        subwoofer that is away plays none (None as well).
        """
        present = {
            s.client_id
            for s in self._speakers
            if self._is_online(s.client_id) and self.plays_with(s.client_id)
        }
        complete = all(
            s.client_id in present for s in self._speakers if s.channel != SpeakerChannel.LFE
        )
        channels: dict[str, SpeakerChannel | None] = {}
        for speaker in self._speakers:
            if speaker.channel == SpeakerChannel.LFE:
                plays = speaker.client_id in present
            else:
                plays = complete
            channels[speaker.client_id] = speaker.channel if plays else None
        return channels

    def _has_subwoofer(self) -> bool:
        """Return whether one of the speakers of this player is a subwoofer."""
        return any(speaker.channel == SpeakerChannel.LFE for speaker in self._speakers)

    def _is_subwoofer(self, client_id: str) -> bool:
        """Return whether a speaker of this player is a subwoofer."""
        return any(
            s.client_id == client_id and s.channel == SpeakerChannel.LFE for s in self._speakers
        )

    def _main_speaker_online(self) -> bool:
        """Return whether a speaker of this player that plays the music is connected."""
        return any(
            self._is_online(s.client_id) for s in self._speakers if s.channel != SpeakerChannel.LFE
        )

    def _main_speaker_plays(self) -> bool:
        """Return whether a main speaker of this player is connected and in the group it plays in."""
        return any(
            self._is_online(s.client_id) and self.plays_with(s.client_id)
            for s in self._speakers
            if s.channel != SpeakerChannel.LFE
        )

    async def _set_subwoofer_aside(self) -> None:
        """Take the subwoofer out of the group while no main speaker plays in it."""
        async with self._take_lock:
            if self._main_speaker_plays():
                return
            for speaker in self._speakers:
                client = self._sendspin_provider.server_api.get_client(speaker.client_id)
                if (
                    client is None
                    or speaker.channel != SpeakerChannel.LFE
                    or not self.plays_with(speaker.client_id)
                ):
                    continue
                # a subwoofer can not play the music alone
                self._set_aside.add(speaker.client_id)
                await self.api.group.remove_client(client)

    def _offline_translation_key(self, speaker: SetSpeaker) -> str:
        """Return the translation key that says what plays while a speaker is offline."""
        if speaker.channel == SpeakerChannel.LFE:
            return "subwoofer_offline"
        if any(
            s.number != speaker.number and s.channel != SpeakerChannel.LFE for s in self._speakers
        ):
            return "speaker_offline"
        return "only_speaker_offline"

    def _crossover_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the entries of how this player splits the bass with its subwoofer."""
        return (
            ConfigEntry(
                key=CONF_SUBWOOFER_OWN_CROSSOVER,
                type=ConfigEntryType.BOOLEAN,
                default_value=DEFAULT_SUBWOOFER_OWN_CROSSOVER,
                required=False,
            ),
            ConfigEntry(
                key=CONF_HIGH_PASS_MAIN_SPEAKERS,
                type=ConfigEntryType.BOOLEAN,
                default_value=DEFAULT_HIGH_PASS_MAIN_SPEAKERS,
                required=False,
            ),
            ConfigEntry(
                key=CONF_CROSSOVER_FREQUENCY,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_CROSSOVER_FREQUENCY,
                options=[
                    ConfigValueOption(frequency, title=f"{frequency} Hz")
                    for frequency in CROSSOVER_FREQUENCIES
                ],
                required=False,
            ),
            ConfigEntry(
                key=CONF_SUBWOOFER_LEVEL,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_SUBWOOFER_LEVEL,
                range=SUBWOOFER_LEVEL_RANGE,
                required=False,
            ),
        )

    def _crossover_settings(self) -> tuple[Any, ...]:
        """Return the stored settings of how this player splits the bass with its subwoofer."""
        return tuple(
            self.mass.config.get_protocol_value(self.protocol_id, entry.key, entry.default_value)
            for entry in self._crossover_entries()
        )

    async def _join_speaker(self, client_id: str) -> None:
        """Take a speaker that connected again, once it reported the state it kept."""
        self._joining.discard(client_id)
        async with self._take_lock:
            await self._take_speaker(client_id)
        self._on_speakers_changed()

    async def _take_speaker(self, client_id: str, *, rejoin: bool = False) -> None:
        """
        Take a speaker into the group of this player and give it the state of the player.

        :param client_id: The client id of the speaker.
        :param rejoin: Also take it when it left the group on its own.
        """
        provider = self._sendspin_provider
        client = provider.server_api.get_client(client_id)
        # a speaker that still has a protocol of its own is taken once that is removed
        if client is None or self.player is None or provider.get_protocol(client_id) is not None:
            return
        self._listen_to(client)
        if not client.available or (client_id in self._left and not rejoin):
            return
        self._left.discard(client_id)
        is_subwoofer = self._is_subwoofer(client_id)
        if is_subwoofer and not self._main_speaker_plays():
            # a subwoofer can not play the music alone
            self._set_aside.add(client_id)
            return
        self._set_aside.discard(client_id)
        await self.api.group.add_client(client)
        if not is_subwoofer:
            # a main speaker brings back the subwoofer that was set aside
            for subwoofer_id in list(self._set_aside):
                await self._take_speaker(subwoofer_id)
        if client.connection is None:
            return
        role = next(
            (r for r in client.roles_by_family("player") if isinstance(r, PlayerRoleProtocol)),
            None,
        )
        if role is None:
            return
        if self._attr_volume_level is None:
            # the first speaker brings the volume of this player
            self._attr_volume_level = role.get_player_volume()
            self._attr_volume_muted = role.get_player_muted()
        self._send_level(client_id, role)
        self._send_output_delay(client_id, role)
        role.set_preferred_format(*self._session_format(role, self._session_sample_rate))

    def _listen_to(self, client: SendspinClient) -> None:
        """Follow the events of the client of a speaker."""
        client_id = client.client_id
        if (listener := self._speaker_listeners.get(client_id)) and listener[0] is client:
            return
        self._stop_listening(client_id)
        self._speaker_listeners[client_id] = (
            client,
            client.add_event_listener(self._on_speaker_event),
        )

    def _stop_listening(self, client_id: str) -> None:
        """Stop following the events of the client of a speaker."""
        if (listener := self._speaker_listeners.pop(client_id, None)) is not None:
            listener[1]()

    def _on_speaker_event(self, client: SendspinClient, event: ClientEvent) -> None:
        """Handle an event of the client of a speaker."""
        match event:
            case VolumeChangedEvent(volume=volume, muted=muted):
                self._take_speaker_level(client.client_id, volume, muted)
            case OutputDelayChangedEvent(output_delay_ms=delay_ms):
                client_id = client.client_id
                if (
                    (number := self._speaker_number(client_id)) is None
                    or client_id in self._joining
                    or _take_echo(self._sent_delays.setdefault(client_id, []), delay_ms)
                ):
                    return
                key = CONF_SPEAKER_OUTPUT_DELAY.format(n=number)
                if self.mass.config.get_protocol_value(self.protocol_id, key) != delay_ms:
                    self.mass.config.set_protocol_value(self.protocol_id, key, delay_ms)
            case ClientGroupChangedEvent(new_group=group) if (
                group is not self.api.group
                or client.client_id in self._left
                or client.client_id in self._set_aside
            ):
                self.mass.call_later(
                    SPEAKER_GROUP_SETTLE,
                    self._follow_speaker_group,
                    client.client_id,
                    task_id=f"sendspin_mc_group_{client.client_id}",
                )

    async def _follow_speaker_group(self, client_id: str) -> None:
        """Follow a speaker that changed group: another group, one of its own, or back."""
        client = self._sendspin_provider.server_api.get_client(client_id)
        # a speaker that is gone or away is followed through its connection
        if client is None or client.connection is None:
            return
        if client_id in self._set_aside:
            if self.plays_with(client_id):
                # it came back by itself, such as through its switch
                await self._set_subwoofer_aside()
            return
        if self.plays_with(client_id):
            if client_id not in self._left:
                return
            # it came back by itself, such as through its switch
            async with self._take_lock:
                await self._take_speaker(client_id, rejoin=True)
        elif client.available and len(client.group.clients) > 1:
            # another group took it, such as through its switch: it belongs to this player
            async with self._take_lock:
                await self._take_speaker(client_id)
        else:
            # it left, as unavailable or on its own: it comes back at the next playback start
            self._left.add(client_id)
        self._on_speakers_changed()

    def _take_speaker_level(self, client_id: str, volume: int, muted: bool) -> None:
        """
        Take a volume or mute a speaker reports, and give it to the other speakers.

        A report of a level this player sent changes nothing, so the speakers never
        send each other's levels back and forth.
        """
        if client_id in self._joining or not self.plays_with(client_id):
            return
        changed = False
        if (
            not _take_echo(self._sent_volumes.setdefault(client_id, []), volume)
            and volume != self._attr_volume_level
        ):
            self._attr_volume_level = volume
            changed = True
        if (
            not _take_echo(self._sent_mutes.setdefault(client_id, []), muted)
            and muted != self._attr_volume_muted
        ):
            self._attr_volume_muted = muted
            changed = True
        if not changed:
            return
        for other_id, role in self._speaker_roles():
            if other_id != client_id:
                self._send_level(other_id, role)
        self.update_state()

    def _send_level(self, client_id: str, role: PlayerRoleProtocol) -> None:
        """Send the volume and mute of this player to a speaker that has another."""
        volume = self._attr_volume_level
        sent_volumes = self._sent_volumes.setdefault(client_id, [])
        if (
            volume is not None
            and (reported_volume := role.get_player_volume()) is not None
            and _last_known(sent_volumes, reported_volume) != volume
        ):
            _remember(sent_volumes, volume)
            role.set_player_volume(volume)
        muted = self._attr_volume_muted
        sent_mutes = self._sent_mutes.setdefault(client_id, [])
        if (
            muted is not None
            and (reported_muted := role.get_player_muted()) is not None
            and _last_known(sent_mutes, reported_muted) != muted
        ):
            _remember(sent_mutes, muted)
            role.set_player_mute(muted)

    def _send_output_delay(self, client_id: str, role: PlayerRoleProtocol) -> None:
        """Send the stored output delay of a speaker to it, when it has another."""
        if (number := self._speaker_number(client_id)) is None:
            return
        delay_ms = self.mass.config.get_protocol_value(
            self.protocol_id,
            CONF_SPEAKER_OUTPUT_DELAY.format(n=number),
            DEFAULT_SENDSPIN_STATIC_DELAY,
        )
        sent_delays = self._sent_delays.setdefault(client_id, [])
        if isinstance(delay_ms, int) and _last_known(sent_delays, role.get_output_delay_ms()) != (
            delay_ms
        ):
            _remember(sent_delays, delay_ms)
            role.set_output_delay(delay_ms)

    def _refresh_speaker_state(self) -> None:
        """Refresh what this player reports from its speakers."""
        self._attr_available = self._main_speaker_online()
        if roles := [role for _, role in self._speaker_roles()]:
            # the volume commands every speaker accepts
            self._sync_control_features(
                set.intersection(*(set(role.state_supported_commands) for role in roles))
            )

    def _on_speakers_changed(self, *, channels_changed: bool = False, restart: bool = True) -> None:
        """
        Publish a change of the speakers, and play the channels they play now.

        The subwoofer of this player leaves the group while none of its main speakers plays
        in it, and the playback this player leads stops once none of them is connected.

        :param channels_changed: Whether the channel of a speaker changed.
        :param restart: Start a running stream again for other channels; False for a session
            that starts, which plays the channels of now already.
        """
        self._refresh_speaker_state()
        if not self._main_speaker_plays():
            self.mass.create_task(
                self._set_subwoofer_aside(), task_id=f"sendspin_mc_aside_{self.protocol_id}"
            )
        if (
            not self._attr_available
            and self._attr_playback_state == PlaybackState.PLAYING
            and self.player is not None
            and self.player.synced_to is None
        ):
            # an unavailable player takes no stop command; the playback of a group it follows
            # goes on for the other players
            self.mass.create_task(self.stop(), task_id=f"sendspin_mc_stop_{self.protocol_id}")
        played_channels = self._current_channels()
        if not restart:
            self.mass.cancel_timer(f"sendspin_mc_channels_{self.protocol_id}")
        elif (channels_changed or played_channels != self._played_channels) and (
            self.player is not None
        ):
            # the speakers of a running stream play other channels from a new stream
            self.mass.call_later(
                CHANNEL_CHANGE_DEBOUNCE,
                self.mass.players.on_player_dsp_change,
                self.player.player_id,
                task_id=f"sendspin_mc_channels_{self.protocol_id}",
            )
        self._played_channels = played_channels
        self.update_state()


def _without_speaker(values: Mapping[str, Any], number: int) -> dict[str, Any]:
    """Return the stored values of a multi-channel player without a speaker, renumbering the rest."""
    kept: dict[str, Any] = {}
    for key, value in values.items():
        if (match := SPEAKER_KEY.match(key)) is None or int(match["number"]) < number:
            kept[key] = value
        elif (speaker_number := int(match["number"])) > number:
            kept[f"speaker_{speaker_number - 1}_{match['field']}"] = value
    return kept


def _last_known[T](sent: list[tuple[T, float]], reported: T) -> T:
    """Return the level a speaker has, counting the last one sent that it did not report yet."""
    return unreported[-1][0] if (unreported := _unreported(sent)) else reported


def _remember[T](sent: list[tuple[T, float]], value: T) -> None:
    """Keep a level sent to a speaker until it reports it back, or no longer will."""
    _unreported(sent).append((value, time.monotonic()))


def _take_echo[T](sent: list[tuple[T, float]], value: T) -> bool:
    """Return whether a reported level was sent, forgetting it and the levels sent before it."""
    unreported = _unreported(sent)
    index = next((i for i, (sent_value, _) in enumerate(unreported) if sent_value == value), None)
    if index is None:
        return False
    del unreported[: index + 1]
    return True


def _unreported[T](sent: list[tuple[T, float]]) -> list[tuple[T, float]]:
    """Forget the levels sent too long ago for a speaker to report them, and return the rest."""
    expired_before = time.monotonic() - UNREPORTED_EXPIRY
    while sent and sent[0][1] < expired_before:
        del sent[0]
    return sent
