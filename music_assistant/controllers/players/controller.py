"""
MusicAssistant PlayerController.

Handles all logic to control supported players,
whose devices are provided by Player Providers.

Note that the PlayerController has a concept of a 'player', a 'protocol' and a 'playerstate'.
The Player is what users and clients see, one per device, and is owned by this controller.
A protocol is the object a provider attaches to a player for its device: it
incorporates the (unaltered) state the device reports (e.g. volume, state, etc)
and the functions for controlling the device (e.g. play, pause, etc).

The playerstate is the (final) state of the player, derived from what its protocols
report, including any user customizations and transformations that are applied to it.
The playerstate is the object that is exposed to the outside world (via the API).
"""

from __future__ import annotations

import asyncio
import time
import weakref
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    PlaybackState,
    PlayerType,
    ProviderType,
)
from music_assistant_models.player_control import PlayerControl  # noqa: TC002

from music_assistant.constants import (
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    CONF_ANNOUNCE_TTS_ENGINE,
    CONF_CREATED_AT,
    CONF_DEVICE_IDENTIFIERS,
    CONF_LEGACY_IDS,
    CONF_PLAYER_DSP,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_UNLINKED_FROM,
    CONF_VOLUME_STEP,
)
from music_assistant.controllers.config.constants import AUTOMATIC_VALUE_KEYS
from music_assistant.helpers.api import api_command
from music_assistant.helpers.plugin_engines import create_tts_engine_config_entries
from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider

from .announcements import AnnouncementsMixin
from .audio_sources import AudioSourceMixin, AudioSourceSession
from .config_changes import ConfigChangesMixin
from .group_commands import GroupCommandsMixin
from .lifecycle import PlayerLifecycleMixin, _Attach
from .locks import PlayerLocksMixin
from .outputs import OutputsMixin
from .playback import PlaybackMixin
from .power import PowerMixin
from .protocol_linking import ProtocolLinkingMixin
from .sleep_timer import SleepTimerMixin
from .state_updates import StateUpdatesMixin
from .volume import VolumeMixin

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from music_assistant_models.config_entries import (
        CoreConfig,
    )

    from music_assistant import MusicAssistant
    from music_assistant.helpers.json import SerializableType
    from music_assistant.models.player_protocol import PlayerProtocol


class PlayerController(
    AnnouncementsMixin,
    AudioSourceMixin,
    ProtocolLinkingMixin,
    PlayerLocksMixin,
    SleepTimerMixin,
    VolumeMixin,
    PowerMixin,
    OutputsMixin,
    PlaybackMixin,
    GroupCommandsMixin,
    StateUpdatesMixin,
    ConfigChangesMixin,
    PlayerLifecycleMixin,
):
    """Controller holding all logic to control registered players."""

    domain: str = "players"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize core controller."""
        super().__init__(mass)
        self._players: dict[str, Player] = {}
        # the protocol objects the providers attached, by their id: bound to their
        # player, or held while it is not known yet which player they belong to
        self._protocols: dict[str, PlayerProtocol] = {}
        # the protocol objects the providers hold that are not attached: detached, or not
        # attached as their player is disabled or the user switched the protocol off
        self._unattached: weakref.WeakValueDictionary[str, PlayerProtocol] = (
            weakref.WeakValueDictionary()
        )
        # the attach that is setting up a protocol, by protocol id
        self._attaching: dict[str, _Attach] = {}
        # the ids of the players that are being removed, which no protocol attaches to
        self._removing: set[str] = set()
        # when each player provider was last loaded (monotonic), by instance id
        self._providers_loaded_at: dict[str, float] = {}
        # when each protocol that needs polling was last polled, by protocol id
        self._last_polls: dict[str, float] = {}
        # the player id behind every legacy id of the stored players
        self._legacy_ids: dict[str, str] = {}
        self._controls: dict[str, PlayerControl] = {}
        self.manifest.name = "Player Controller"
        self.manifest.description = (
            "Music Assistant's core controller which manages all players from all providers."
        )
        self.manifest.icon = "speaker-multiple"
        self._poll_task: asyncio.Task[None] | None = None
        self._player_command_locks: dict[str, asyncio.Lock] = {}
        # Re-entrancy tracking for get_player_lock, keyed on the task object
        # (weak ref auto-clears entries if a task is GC'd before its finally runs).
        self._task_held_locks: weakref.WeakKeyDictionary[asyncio.Task[Any], set[str]] = (
            weakref.WeakKeyDictionary()
        )
        # the task that holds each player lock, by lock key, named when a wait times out
        self._lock_holders: weakref.WeakValueDictionary[str, asyncio.Task[Any]] = (
            weakref.WeakValueDictionary()
        )
        # Lock to prevent race conditions while protocols attach
        self._register_lock = asyncio.Lock()
        # the end of the wait of every held protocol whose identifiers may still arrive
        self._pending_protocol_evaluations: dict[str, asyncio.TimerHandle] = {}
        # Live external AudioSource playing on a player, keyed on player_id
        self._source_sessions: dict[str, AudioSourceSession] = {}
        # Subscribers for player state updates (called with player + changed_values)
        self._state_update_subscribers: list[
            Callable[[Player, dict[str, tuple[Any, Any]]], None]
        ] = []

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config Entries for the Player Controller."""
        return (
            ConfigEntry(
                key=CONF_VOLUME_STEP,
                type=ConfigEntryType.INTEGER,
                default_value=0,
                range=(0, 10),
                required=False,
                category="generic",
            ),
            *await create_tts_engine_config_entries(
                self.mass, CONF_ANNOUNCE_TTS_ENGINE, category="announcements"
            ),
        )

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        self._build_legacy_id_index()
        self._poll_task = self.mass.create_task(self._poll_players())

    async def post_setup(self) -> None:
        """Bring back the stored players, before any provider attaches a protocol."""
        await self._restore_players()

    async def close(self) -> None:
        """Cleanup on exit."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        # Cancel all pending protocol evaluations
        for handle in self._pending_protocol_evaluations.values():
            handle.cancel()
        self._pending_protocol_evaluations.clear()
        for player in self._players.values():
            if player.sleep_timer_expires_at is not None:
                self.mass.cancel_timer(self._sleep_timer_task_id(player.player_id))

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this controller to include in diagnostics reports."""
        players = list(self._players.values())
        return {
            "players_synced": sum(player.state.synced_to is not None for player in players),
            "players_with_active_group": sum(
                player.state.active_group is not None for player in players
            ),
            "announcements_in_progress": sum(
                bool(player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS)) for player in players
            ),
            "pending_protocol_evaluations": len(self._pending_protocol_evaluations),
            # how the players are made up, without anything that names them
            "player_protocols": [
                {
                    "type": player.state.type.value,
                    "available": player.state.available,
                    "protocols": [
                        {
                            "domain": protocol.domain,
                            "role": protocol.role.value,
                            "available": protocol.available,
                            "active": protocol.active,
                        }
                        for protocol in player.state.protocols
                    ],
                }
                for player in players
            ],
        }

    async def on_provider_loaded(self, provider: PlayerProvider) -> None:
        """Handle logic when a provider is loaded."""
        self._providers_loaded_at[provider.instance_id] = time.monotonic()

    async def on_provider_unload(self, provider: PlayerProvider) -> None:
        """
        Handle logic when a provider is (about to get) unloaded.

        Its protocols are detached: the players stay, on what their other protocols offer.
        """
        # a discovery that was asked for must not run against the instance that goes
        self.mass.cancel_timer(f"discover_players_{provider.instance_id}")
        for protocol in list(self._protocols.values()):
            if protocol.provider.instance_id == provider.instance_id:
                await self.detach(protocol.protocol_id)

    @property
    def providers(self) -> list[PlayerProvider]:
        """Return all loaded/running MusicProviders."""
        return cast("list[PlayerProvider]", self.mass.get_providers(ProviderType.PLAYER))

    @api_command("players/merge", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def merge(self, player_id: str, target_player: str) -> None:
        """
        Merge a player into another one: the two turn out to be one device.

        The player goes and its outputs move to the target player, which answers to its
        id from then on. A name or setting only the player that goes has is kept, its
        queue is dropped.

        :param player_id: ID of the player that goes.
        :param target_player: ID of the player it is merged into, which stays.
        """
        await self.merge_players(target_player, player_id)

    async def merge_players(self, keep_player_id: str, absorb_player_id: str) -> None:
        """
        Merge a player into another one: the two turn out to be one device.

        The absorbed player goes and its protocols move to the kept player. Its id, and
        the ids it answered to, become legacy ids of the kept player, so what still names
        it reaches the kept player. A name or setting only the absorbed player has is
        carried over; where both have one, that of the kept player stays. The queue of
        the absorbed player is dropped, and stopped first when it plays.

        :param keep_player_id: Id of the player that stays, or one of its legacy ids.
        :param absorb_player_id: Id of the player that goes, or one of its legacy ids.
        :raises InvalidDataError: When a player does not exist, or both ids name one player.
        :raises InvalidCommand: When the two can not be one player: both have a protocol
            that stands for the device, or a protocol of the same domain, their identifiers
            tell them apart, or one of them is a group or only exists while connected.
        """
        keep_id = self.resolve_player_id(keep_player_id)
        absorb_id = self.resolve_player_id(absorb_player_id)
        self._check_merge(keep_id, absorb_id)
        if absorbed := self._players.get(absorb_id):
            await self._stop_player_that_is_merged(absorbed)
        async with self._register_lock:
            # either player may have changed while the playback stopped
            self._check_merge(keep_id, absorb_id)
            await self._merge_players(keep_id, absorb_id)

    @api_command("players/unlink_protocol", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def unlink_protocol(self, protocol_id: str) -> str:
        """
        Take a protocol off its player and give it a player of its own.

        For a protocol that ended up on the player of another device. It stays on its new
        player, also after a restart, and is not merged back by itself; the protocols that
        ride on it move with it. Returns the id of the new player.

        :param protocol_id: ID of the protocol.
        :raises InvalidDataError: When no protocol has the id.
        :raises InvalidCommand: When the protocol can not stand on its own: it rides on another
            protocol, it is the protocol of a group, it only exists while connected, or it is
            the only protocol of its player.
        """
        async with self._register_lock:
            player_id = self._check_unlink(protocol_id)
            return await self._unlink_protocol(protocol_id, player_id)

    def __iter__(self) -> Iterator[Player]:
        """Iterate over all players."""
        return iter(self._players.values())

    async def _stop_player_that_is_merged(self, player: Player) -> None:
        """
        Stop the queue a player plays before it is merged into another one, best effort.

        Its queue goes with it. A member of a group or of a sync leader is not stopped: its
        membership moves with its protocols.

        :param player: The player that is merged into another one.
        """
        player_id = player.player_id
        if (
            self._resolve_playback_owner(player) is not player
            or player.state.active_source != player_id
            or player.state.playback_state == PlaybackState.IDLE
        ):
            return
        try:
            await self.mass.player_queues.stop(player_id)
        except Exception:
            self.logger.exception("Error stopping %s before it is merged", player.display_name)

    async def _merge_players(self, keep_id: str, absorb_id: str) -> None:
        """
        Merge a player into another one, right after the attach rules allowed it.

        Called under the attach lock. What is stored changes before the first await and is
        saved before anything else of the absorbed player is deleted; the user filters follow
        once its removal went out, so a user limited to it still learns that it went. A
        protocol that fails to attach again, or that its provider detached meanwhile,
        attaches to the kept player when its provider finds it again.

        :param keep_id: Id of the player that stays.
        :param absorb_id: Id of the player that goes.
        """
        protocol_ids = set(self.mass.config.get_protocol_ids_of_player(absorb_id))
        if absorbed := self._players.get(absorb_id):
            protocol_ids.update(absorbed.protocol_ids)
        self.logger.info(
            "Merging player %s into %s",
            self._stored_player_name(absorb_id),
            self._stored_player_name(keep_id),
        )
        self._merge_player_config(keep_id, absorb_id, protocol_ids)
        await self.mass.config.async_save()
        absorbed = self._players.get(absorb_id)
        moving = list(absorbed.protocols.values()) if absorbed is not None else []
        # the output that plays for the absorbed player, e.g. in a group, stays the active output
        # of the kept one, so the group keeps its member
        playing_through = (
            absorbed.resolve_output_protocol()
            if absorbed is not None and absorbed.state.playback_state != PlaybackState.IDLE
            else None
        )
        # the providers of the protocols that are not attached look for their devices again
        provider_ids = {
            str(record.get("provider"))
            for protocol_id in protocol_ids - {protocol.protocol_id for protocol in moving}
            if (record := self.mass.config.get_protocol_record(protocol_id))
        }
        # a moving protocol is attaching: a detach by its provider meanwhile takes this away
        moves = {protocol.protocol_id: _Attach() for protocol in moving}
        self._attaching.update(moves)
        for protocol in moving:
            if protocol.player is absorbed:
                await self._unbind(protocol)
        if absorbed is not None:
            await self._drop_player(absorbed, permanent=True)
        else:
            self.mass.player_queues.purge_saved_queue(absorb_id)
        try:
            await self.mass.webserver.auth.replace_player_in_user_filters(
                absorb_id, keep_id, removed_player_ids=[absorb_id]
            )
        except Exception:
            self.logger.exception("Could not move the user filters of %s", absorb_id)
        for protocol in moving:
            if self._attaching.get(protocol.protocol_id) is not moves[protocol.protocol_id]:
                # its provider detached it while it moved, and keeps the object
                if not protocol.provider.unloading:
                    self._unattached[protocol.protocol_id] = protocol
                continue
            try:
                await self._run_attach(protocol)
            except Exception:
                self.logger.exception(
                    "Protocol %s did not attach to %s after the merge",
                    protocol.protocol_id,
                    self._stored_player_name(keep_id),
                )
        if keeper := self._players.get(keep_id):
            # a protocol that moved here while away may rank above one that is attached
            await self._attach_in_changed_roles(keeper)
        if (keeper := self._players.get(keep_id)) and keeper.initialized.is_set():
            # the player caches its legacy ids with its config
            keeper.set_config(await self.mass.config.get_player_object_config(keeper))
            keeper.update_state()
            if (
                playing_through is not None
                and keeper.protocols.get(playing_through.protocol_id) is playing_through
                and keeper.active_output_protocol is None
            ):
                keeper.set_active_output_protocol(playing_through.protocol_id)
        for provider_id in provider_ids:
            if isinstance(provider := self.mass.get_provider(provider_id), PlayerProvider):
                provider.schedule_discovery()
        self._schedule_held_protocols()
        self._schedule_update_all_players()

    def _merge_player_config(self, keep_id: str, absorb_id: str, protocol_ids: set[str]) -> None:
        """
        Move what is stored about a player to the player it is merged into.

        Its protocols, its group memberships and its ids follow; its name, its values and
        its DSP and queue settings where the kept player has none. Its own config is
        deleted last.

        :param keep_id: Id of the player that stays.
        :param absorb_id: Id of the player that goes.
        :param protocol_ids: Ids of the protocols of the player that goes.
        """
        config = self.mass.config
        keep_key = f"{CONF_PLAYERS}/{keep_id}"
        kept = config.get(keep_key) or {}
        absorbed = config.get(f"{CONF_PLAYERS}/{absorb_id}") or {}
        kept_values = kept.get("values") or {}
        absorbed_values = absorbed.get("values") or {}
        if absorbed.get("name") and not kept.get("name"):
            config.set(f"{keep_key}/name", absorbed["name"])
        for key, value in absorbed_values.items():
            if key not in kept_values and key not in AUTOMATIC_VALUE_KEYS:
                config.set(f"{keep_key}/values/{key}", deepcopy(value))
        stored_identifiers = absorbed_values.get(CONF_DEVICE_IDENTIFIERS)
        if isinstance(stored_identifiers, dict) and stored_identifiers:
            config.set(
                f"{keep_key}/values/{CONF_DEVICE_IDENTIFIERS}",
                {**stored_identifiers, **(kept_values.get(CONF_DEVICE_IDENTIFIERS) or {})},
            )
        legacy_ids = [
            *(kept_values.get(CONF_LEGACY_IDS) or []),
            absorb_id,
            *(absorbed_values.get(CONF_LEGACY_IDS) or []),
        ]
        config.set(
            f"{keep_key}/values/{CONF_LEGACY_IDS}",
            list(
                dict.fromkeys(
                    legacy_id
                    for legacy_id in legacy_ids
                    if isinstance(legacy_id, str) and legacy_id != keep_id
                )
            ),
        )
        merged = {keep_id, absorb_id}
        for protocol_id in protocol_ids | set(config.get_protocol_ids_of_player(keep_id)):
            if (record := config.get_protocol_record(protocol_id)) is None:
                continue
            if record.get("player_id") != keep_id:
                config.set_protocol_value(protocol_id, "player_id", keep_id)
            unlinked_from = (record.get("values") or {}).get(CONF_UNLINKED_FROM)
            if isinstance(unlinked_from, str) and self.resolve_player_id(unlinked_from) in merged:
                # the user puts the protocol back together with the player it was taken off
                config.remove(f"{CONF_PLAYER_PROTOCOLS}/{protocol_id}/values/{CONF_UNLINKED_FROM}")
        if keeper := self._players.get(keep_id):
            keeper.protocol_ids.update(protocol_ids)
        absorbed_dsp = config.get(f"{CONF_PLAYER_DSP}/{absorb_id}")
        if absorbed_dsp and not config.get(f"{CONF_PLAYER_DSP}/{keep_id}"):
            config.set(f"{CONF_PLAYER_DSP}/{keep_id}", deepcopy(absorbed_dsp))
        absorbed_queue = config.get(f"{CONF_PLAYER_QUEUES}/{absorb_id}")
        if isinstance(absorbed_queue, dict) and absorbed_queue.get("values"):
            kept_queue = deepcopy(config.get(f"{CONF_PLAYER_QUEUES}/{keep_id}") or {})
            kept_queue["queue_id"] = keep_id
            kept_queue["values"] = {
                **deepcopy(absorbed_queue["values"]),
                **(kept_queue.get("values") or {}),
            }
            config.set(f"{CONF_PLAYER_QUEUES}/{keep_id}", kept_queue)
        self._update_group_memberships(absorb_id, keep_id)
        for key in (
            f"{CONF_PLAYERS}/{absorb_id}",
            f"{CONF_PLAYER_DSP}/{absorb_id}",
            f"{CONF_PLAYER_QUEUES}/{absorb_id}",
        ):
            config.remove(key)
        self._build_legacy_id_index()

    async def _unlink_protocol(self, protocol_id: str, player_id: str) -> str:
        """
        Move a protocol, with the protocols riding on it, to a new player of its own.

        :param protocol_id: Id of the protocol, which the unlink rules allow to move.
        :param player_id: Id of the player it is taken off.
        """
        config = self.mass.config
        protocol = self._protocols.get(protocol_id)
        record = config.get_protocol_record(protocol_id) or {}
        moved = [protocol_id, *config.get_protocol_ids_derived_from(protocol_id)]
        new_id = self._new_player_id()
        config.create_player_record(
            new_id,
            provider=str(record.get("provider")),
            player_type=protocol.type if protocol else PlayerType.PLAYER,
            default_name=(protocol.name if protocol else None) or record.get("default_name"),
            values={CONF_CREATED_AT: time.time_ns()},
        )
        for moved_id in moved:
            config.set_protocol_value(moved_id, "player_id", new_id)
            self._replace_protocol_references(player_id, moved_id, None)
        config.set_protocol_value(protocol_id, CONF_UNLINKED_FROM, player_id)
        if player := self._players.get(player_id):
            player.protocol_ids.difference_update(moved)
        self.logger.info(
            "Protocol %s taken off player %s", protocol_id, self._stored_player_name(player_id)
        )
        attached = [
            attached_protocol
            for moved_id in moved
            if (attached_protocol := self._protocols.get(moved_id))
            and attached_protocol.player is not None
        ]
        for attached_protocol in attached:
            self._attaching.pop(attached_protocol.protocol_id, None)
            await self._unbind(attached_protocol)
        for attached_protocol in attached:
            try:
                await self._run_attach(attached_protocol)
            except Exception:
                self.logger.exception(
                    "Protocol %s did not attach to its new player", attached_protocol.protocol_id
                )
        if new_id not in self._players:
            # its device is away, or was not attached as its player was disabled: the
            # player waits for it like a stored one at startup, and its provider looks again
            await self._publish_player(self._create_player(new_id))
            if isinstance(
                provider := self.mass.get_provider(str(record.get("provider"))), PlayerProvider
            ):
                provider.schedule_discovery()
        if player is not None:
            # a protocol the moved one ranked above stands for the device again
            await self._attach_in_changed_roles(player)
            await self._settle_player(player)
        self._schedule_update_all_players()
        return new_id

    def _schedule_duplicate_merge(self, player_id: str) -> None:
        """
        Look for unused duplicates of a player, now that one of its protocols attached.

        :param player_id: Id of the player.
        """
        self.mass.create_task(
            self._merge_duplicates(player_id), task_id=f"merge_duplicates_{player_id}"
        )

    async def _merge_duplicates(self, player_id: str) -> None:
        """
        Merge the players that turn out to stand for the device of a player, while unused.

        Of two such players the younger one goes into the older one, and only while nothing
        about it is the user's own yet (see _is_unused); otherwise both stay, for the user to
        merge by hand.

        :param player_id: Id of the player.
        """
        async with self._register_lock:
            while not self.mass.closing and player_id in self._players:
                for other_id in self._duplicates_of(player_id):
                    keep_id, absorb_id = sorted((player_id, other_id), key=self._merge_rank)
                    if (absorbed := self._players.get(absorb_id)) and self._is_unused(absorbed):
                        break
                else:
                    return
                self.logger.info(
                    "Player %s turns out to be the device of %s",
                    self._stored_player_name(absorb_id),
                    self._stored_player_name(keep_id),
                )
                await self._merge_players(keep_id, absorb_id)
                player_id = keep_id

    # Protocol linking methods are provided by ProtocolLinkingMixin (protocol_linking.py)
