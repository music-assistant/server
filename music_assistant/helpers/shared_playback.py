"""
Helpers for hosting a shared listening experience.

Provides the SharedPlaybackSession abstraction that plugin providers (e.g. the
party plugin) build on to let a group of guests listen to the same queue.
Two modes are supported:

- VENUE: an existing real player owns the queue and plays out loud;
  guests may optionally listen in on their own device when the venue
  player supports grouping with it.
- REMOTE: a hidden Sendspin virtual player owns the queue and leads the
  group; every guest's web player can be attached, so all playback
  happens on the guests' own devices (silent-disco style).

NOTE: the virtual player backing a REMOTE session lives in memory of the
Sendspin provider. When that provider reloads, the session is gone and the
owning plugin is responsible for re-creating it; passing the same session_id
to :meth:`SharedPlaybackSession.create_remote` yields the same player_id.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import PlayerFeature
from music_assistant_models.errors import SetupFailedError, UnsupportedFeaturedException

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player
    from music_assistant.providers.sendspin.provider import SendspinProvider

SENDSPIN_DOMAIN = "sendspin"


class SharedPlaybackMode(StrEnum):
    """Mode of a shared playback session."""

    VENUE = "venue"
    REMOTE = "remote"


class SharedPlaybackSession:
    """
    A player/queue that hosts a shared listening experience.

    Use the :meth:`create_venue` or :meth:`create_remote` factory to create a
    session; the owning plugin drives playback on :attr:`queue_id` and calls
    :meth:`close` when the session ends.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        mode: SharedPlaybackMode,
        player_id: str,
        audio_only: bool = False,
    ) -> None:
        """Initialize the session. Use the create_venue/create_remote factories instead."""
        self.mass = mass
        self._mode = mode
        self._player_id = player_id
        self._audio_only = audio_only
        self._guest_listeners: set[str] = set()

    @classmethod
    async def create_venue(
        cls,
        mass: MusicAssistant,
        venue_player_id: str,
        *,
        audio_only: bool = False,
    ) -> SharedPlaybackSession:
        """
        Create a session hosted by an existing (real) player.

        :param mass: MusicAssistant instance.
        :param venue_player_id: The player_id of the player that owns the queue
            and plays out loud.
        :param audio_only: Redact media metadata from attached guest players.
        :raises SetupFailedError: If the venue player is unknown.
        :return: The created session.
        """
        if mass.players.get_player(venue_player_id) is None:
            raise SetupFailedError(f"Venue player {venue_player_id} is not available")
        return cls(mass, SharedPlaybackMode.VENUE, venue_player_id, audio_only)

    @classmethod
    async def create_remote(
        cls,
        mass: MusicAssistant,
        owner_instance_id: str,
        display_name: str,
        session_id: str | None = None,
        *,
        audio_only: bool = False,
    ) -> SharedPlaybackSession:
        """
        Create a session hosted by a hidden Sendspin virtual player.

        :param mass: MusicAssistant instance.
        :param owner_instance_id: Instance id of the plugin provider that owns
            the session (the virtual player is removed when it unloads).
        :param display_name: Human readable name for the virtual player.
        :param session_id: Optional stable id for the virtual player so the
            owner can re-create the session with the same player_id.
        :param audio_only: Redact media metadata from attached guest players.
        :raises SetupFailedError: If the Sendspin provider is not loaded.
        :return: The created session.
        """
        sendspin = cast("SendspinProvider | None", mass.get_provider(SENDSPIN_DOMAIN))
        if sendspin is None:
            raise SetupFailedError("The Sendspin provider is required for a remote session")
        player_id = await sendspin.create_virtual_player(
            owner_instance_id=owner_instance_id,
            display_name=display_name,
            player_id=session_id,
        )
        return cls(mass, SharedPlaybackMode.REMOTE, player_id, audio_only)

    @property
    def mode(self) -> SharedPlaybackMode:
        """Return the mode of this session."""
        return self._mode

    @property
    def player_id(self) -> str:
        """Return the player_id of the player that hosts this session."""
        return self._player_id

    @property
    def queue_id(self) -> str:
        """Return the queue_id of the queue that hosts this session."""
        # a player-owned queue always has the same id as the player
        return self._player_id

    def can_listen_in(self, web_player_id: str) -> bool:
        """
        Return whether the given guest web player can listen in on this session.

        :param web_player_id: The player_id of the guest's web player.
        """
        if (host_player := self._get_host_player()) is None:
            return False
        if PlayerFeature.SET_MEMBERS not in host_player.state.supported_features:
            return False
        # state.can_group_with handles all protocol expansion and translation,
        # for both a real venue player and a (virtual) Sendspin host player
        return (
            web_player_id in host_player.state.can_group_with
            or web_player_id in host_player.state.group_members
        )

    async def add_guest_listener(self, web_player_id: str) -> None:
        """
        Attach a guest's web player to this session so it plays the same audio.

        :param web_player_id: The player_id of the guest's web player.
        :raises UnsupportedFeaturedException: If the session host does not
            support grouping with the given player.
        """
        if web_player_id in self._guest_listeners:
            return
        if not self.can_listen_in(web_player_id):
            raise UnsupportedFeaturedException(
                f"Player {web_player_id} can not listen in on this session"
            )
        grouping_succeeded = False
        if self._audio_only:
            self.mass.players.register_audio_only_player(web_player_id)
        try:
            await self.mass.players.cmd_set_members(
                self._player_id, player_ids_to_add=[web_player_id]
            )
            grouping_succeeded = True
        finally:
            if self._audio_only and not grouping_succeeded:
                self.mass.players.unregister_audio_only_player(web_player_id)
        self._guest_listeners.add(web_player_id)

    async def remove_guest_listener(self, web_player_id: str) -> None:
        """
        Detach a guest's web player from this session.

        :param web_player_id: The player_id of the guest's web player.
        """
        if web_player_id not in self._guest_listeners:
            return
        if self._get_host_player() is not None:
            await self.mass.players.cmd_set_members(
                self._player_id, player_ids_to_remove=[web_player_id]
            )
        self._guest_listeners.remove(web_player_id)
        if self._audio_only:
            self.mass.players.unregister_audio_only_player(web_player_id)

    async def close(self) -> None:
        """
        Tear down the session.

        In REMOTE mode the virtual player (and its queue) is removed entirely.
        In VENUE mode only the guest listeners added through this session are
        detached; the venue player itself is left untouched.
        """
        if self._mode == SharedPlaybackMode.REMOTE:
            sendspin = cast("SendspinProvider | None", self.mass.get_provider(SENDSPIN_DOMAIN))
            if sendspin is not None and sendspin.is_virtual_player(self._player_id):
                await sendspin.remove_virtual_player(self._player_id)
        elif self._guest_listeners and self._get_host_player() is not None:
            await self.mass.players.cmd_set_members(
                self._player_id, player_ids_to_remove=list(self._guest_listeners)
            )
        if self._audio_only:
            for web_player_id in self._guest_listeners:
                self.mass.players.unregister_audio_only_player(web_player_id)
        self._guest_listeners.clear()

    def _get_host_player(self) -> Player | None:
        """Return the (available) player hosting this session, if any."""
        player = self.mass.players.get_player(self._player_id)
        if player is None or not player.state.available:
            return None
        return player
