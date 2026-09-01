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

import asyncio
import logging
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import PlayerFeature
from music_assistant_models.errors import (
    MusicAssistantError,
    SetupFailedError,
    UnsupportedFeaturedException,
)

from music_assistant.helpers.util import join_task

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player
    from music_assistant.providers.sendspin.provider import SendspinProvider

SENDSPIN_DOMAIN = "sendspin"
REMOTE_CREATION_CLEANUP_TIMEOUT = 15.0
REMOTE_REMOVAL_CLEANUP_DELAYS = (0.0, 1.0, 5.0)

LOGGER = logging.getLogger(__name__)


class SharedPlaybackMode(StrEnum):
    """Mode of a shared playback session."""

    VENUE = "venue"
    REMOTE = "remote"


def is_remote_session_host(mass: MusicAssistant, player_id: str) -> bool:
    """
    Return whether the given player is the virtual host of a REMOTE session.

    :param mass: MusicAssistant instance.
    :param player_id: Player to inspect.
    """
    sendspin = cast("SendspinProvider | None", mass.get_provider(SENDSPIN_DOMAIN))
    return sendspin is not None and sendspin.is_virtual_player(player_id)


class SharedPlaybackSession:
    """
    A player/queue that hosts a shared listening experience.

    Use the :meth:`create_venue` or :meth:`create_remote` factory to create a
    session; the owning plugin drives playback on :attr:`queue_id` and calls
    :meth:`close` when the session ends.
    """

    def __init__(self, mass: MusicAssistant, mode: SharedPlaybackMode, player_id: str) -> None:
        """Initialize the session. Use the create_venue/create_remote factories instead."""
        self.mass = mass
        self._mode = mode
        self._player_id = player_id
        self._guest_listeners: set[str] = set()

    @classmethod
    async def create_venue(
        cls, mass: MusicAssistant, venue_player_id: str
    ) -> SharedPlaybackSession:
        """
        Create a session hosted by an existing (real) player.

        :param mass: MusicAssistant instance.
        :param venue_player_id: The player_id of the player that owns the queue
            and plays out loud.
        :raises SetupFailedError: If the venue player is unknown.
        :return: The created session.
        """
        if mass.players.get_player(venue_player_id) is None:
            raise SetupFailedError(f"Venue player {venue_player_id} is not available")
        return cls(mass, SharedPlaybackMode.VENUE, venue_player_id)

    @classmethod
    async def create_remote(
        cls,
        mass: MusicAssistant,
        owner_instance_id: str,
        display_name: str,
        session_id: str | None = None,
    ) -> SharedPlaybackSession:
        """
        Create a session hosted by a hidden Sendspin virtual player.

        :param mass: MusicAssistant instance.
        :param owner_instance_id: Instance id of the plugin provider that owns
            the session (the virtual player is removed when it unloads).
        :param display_name: Human readable name for the virtual player.
        :param session_id: Optional stable id for the virtual player so the
            owner can re-create the session with the same player_id.
        :raises SetupFailedError: If the Sendspin provider is not loaded.
        :return: The created session.
        """
        sendspin = cast("SendspinProvider | None", mass.get_provider(SENDSPIN_DOMAIN))
        if sendspin is None:
            raise SetupFailedError("The Sendspin provider is required for a remote session")
        creation = sendspin.create_virtual_player(
            owner_instance_id=owner_instance_id,
            display_name=display_name,
            player_id=session_id,
        )
        try:
            creation_task = mass.create_task(creation, eager_start=False)
        except Exception:
            creation.close()
            raise
        cleanup_required = asyncio.get_running_loop().create_future()
        cleanup = cls._cleanup_cancelled_remote_creation(
            mass,
            sendspin,
            creation_task,
            cleanup_required,
        )
        try:
            mass.create_task(cleanup, eager_start=False)
        except Exception:
            cleanup.close()
            cls._cancel_and_observe_creation(mass, sendspin, creation_task)
            raise
        try:
            # join: cancelling the caller must not abort the creation halfway, or the
            # cleanup task can no longer remove the player it left behind
            player_id = await join_task(creation_task)
        except asyncio.CancelledError:
            cleanup_required.set_result(True)
            raise
        except Exception:
            cleanup_required.set_result(False)
            raise
        cleanup_required.set_result(False)
        return cls(mass, SharedPlaybackMode.REMOTE, player_id)

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
        if not self.can_listen_in(web_player_id):
            raise UnsupportedFeaturedException(
                f"Player {web_player_id} can not listen in on this session"
            )
        await self.mass.players.cmd_set_members(self._player_id, player_ids_to_add=[web_player_id])
        self._guest_listeners.add(web_player_id)

    async def restore_guest_listeners(self) -> None:
        """
        Restore tracked guest listeners missing from the host player's group.

        Missing or temporarily incompatible guest players remain tracked so a
        later playback transition can restore them after they reconnect.
        """
        host_player = self._get_host_player()
        if host_player is None or not self._guest_listeners:
            return
        group_members = set(host_player.state.group_members)
        for web_player_id in sorted(self._guest_listeners):
            if web_player_id in group_members:
                continue
            guest_player = self.mass.players.get_player(web_player_id)
            if (
                guest_player is None
                or not guest_player.state.available
                or not self.can_listen_in(web_player_id)
            ):
                continue
            try:
                await self.mass.players.cmd_set_members(
                    self._player_id,
                    player_ids_to_add=[web_player_id],
                )
            except MusicAssistantError as err:
                LOGGER.warning(
                    "Could not restore guest listener %s to shared playback session %s: %s",
                    web_player_id,
                    self._player_id,
                    err,
                )
                continue
            group_members.add(web_player_id)

    async def remove_guest_listener(self, web_player_id: str) -> None:
        """
        Detach a guest's web player from this session.

        :param web_player_id: The player_id of the guest's web player.
        """
        self._guest_listeners.discard(web_player_id)
        if self._get_host_player() is None:
            return
        await self.mass.players.cmd_set_members(
            self._player_id, player_ids_to_remove=[web_player_id]
        )

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
            self._guest_listeners.clear()
            return
        if self._guest_listeners and self._get_host_player() is not None:
            await self.mass.players.cmd_set_members(
                self._player_id, player_ids_to_remove=list(self._guest_listeners)
            )
        self._guest_listeners.clear()

    @classmethod
    async def _cleanup_cancelled_remote_creation(
        cls,
        mass: MusicAssistant,
        sendspin: SendspinProvider,
        creation_task: asyncio.Task[str],
        cleanup_required: asyncio.Future[bool],
    ) -> None:
        """
        Clean up a remote virtual player when its session creation is cancelled.

        :param mass: MusicAssistant instance.
        :param sendspin: Sendspin provider that owns the virtual player.
        :param creation_task: In-flight virtual-player creation task.
        :param cleanup_required: Signal indicating whether cleanup is needed.
        """
        if not await asyncio.shield(cleanup_required):
            return
        try:
            done, _ = await asyncio.wait(
                (creation_task,),
                timeout=REMOTE_CREATION_CLEANUP_TIMEOUT,
            )
            if not done:
                LOGGER.warning("Timed out waiting for cancelled remote session creation")
                cls._cancel_and_observe_creation(mass, sendspin, creation_task)
                return
        except asyncio.CancelledError:
            cls._cancel_and_observe_creation(mass, sendspin, creation_task)
            raise

        try:
            player_id = creation_task.result()
        except asyncio.CancelledError:
            return
        except Exception as err:
            LOGGER.debug("Cancelled remote session creation failed: %s", err)
            return

        await cls._cleanup_cancelled_remote_player(sendspin, player_id)

    @staticmethod
    async def _cleanup_cancelled_remote_player(
        sendspin: SendspinProvider,
        player_id: str,
    ) -> None:
        """
        Remove a virtual player left by cancelled remote session creation.

        :param sendspin: Sendspin provider that owns the virtual player.
        :param player_id: Virtual player to remove.
        """
        last_error: Exception | None = None
        for delay in REMOTE_REMOVAL_CLEANUP_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                if not sendspin.is_virtual_player(player_id):
                    return
                # awaited to completion on purpose: a timeout is no reliable bound on
                # the teardown - parts of it swallow the cancellation (see
                # AsyncProcess.close), and one that does land leaves the player
                # half torn down for the next attempt to trip over
                await sendspin.remove_virtual_player(player_id)
                return
            except Exception as err:
                last_error = err
        LOGGER.warning(
            "Could not clean up cancelled remote session %s: %s",
            player_id,
            last_error,
        )

    @classmethod
    def _cancel_and_observe_creation(
        cls,
        mass: MusicAssistant,
        sendspin: SendspinProvider,
        task: asyncio.Task[str],
    ) -> None:
        """Cancel virtual-player creation and observe its eventual result."""
        task.cancel()
        cls._observe_late_remote_creation(mass, sendspin, task)

    @classmethod
    def _observe_late_remote_creation(
        cls,
        mass: MusicAssistant,
        sendspin: SendspinProvider,
        task: asyncio.Task[str],
    ) -> None:
        """Observe creation after bounded cleanup stops waiting for it."""
        task.add_done_callback(partial(cls._handle_late_remote_creation, mass, sendspin))

    @classmethod
    def _handle_late_remote_creation(
        cls,
        mass: MusicAssistant,
        sendspin: SendspinProvider,
        task: asyncio.Task[str],
    ) -> None:
        """Schedule cleanup when cancelled creation eventually returns a player."""
        if task.cancelled():
            return
        try:
            player_id = task.result()
        except Exception as err:
            LOGGER.debug("Cancelled remote session creation failed: %s", err)
            return
        cleanup = cls._cleanup_cancelled_remote_player(sendspin, player_id)
        try:
            mass.create_task(cleanup, eager_start=False)
        except Exception as err:
            cleanup.close()
            LOGGER.warning(
                "Could not schedule cancelled remote session cleanup for %s: %s",
                player_id,
                err,
            )

    def _get_host_player(self) -> Player | None:
        """Return the (available) player hosting this session, if any."""
        player = self.mass.players.get_player(self._player_id)
        if player is None or not player.state.available:
            return None
        return player
