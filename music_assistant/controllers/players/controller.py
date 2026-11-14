"""
MusicAssistant PlayerController.

Handles all logic to control supported players,
whose devices are provided by Player Providers.

Note that the PlayerController has a concept of a 'player', a 'protocol' and a 'playerstate'.
The Player is what users and clients see, one per device, and is owned by this controller.
A protocol is the object a provider registers for its device, which this controller
attaches to a player: it incorporates the (unaltered) state the device reports
(e.g. volume, state, etc) and the functions for controlling the device (e.g. play, pause, etc).

The playerstate is the (final) state of the player, derived from what its protocols
report, including any user customizations and transformations that are applied to it.
The playerstate is the object that is exposed to the outside world (via the API).

The controller is composed of one mixin per concern, over the registry in base.py:
- lifecycle.py: attaching and detaching protocols, creating, removing, enabling and
  disabling players
- protocol_linking.py: finding the player a protocol belongs to
- merging.py: merging two players, taking a protocol off its player
- state_updates.py: publishing the state of a player and acting on its changes
- config_changes.py: what a changed player, protocol or DSP config does
- locks.py: the player command locks
- playback.py, outputs.py, audio_sources.py, power.py, volume.py, group_commands.py,
  sleep_timer.py and announcements.py: the player commands
"""

from __future__ import annotations

import asyncio
import time
import weakref
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ProviderType
from music_assistant_models.player_control import PlayerControl  # noqa: TC002

from music_assistant.constants import (
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    CONF_ANNOUNCE_TTS_ENGINE,
    CONF_VOLUME_STEP,
)
from music_assistant.helpers.plugin_engines import create_tts_engine_config_entries
from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider

from .announcements import AnnouncementsMixin
from .audio_sources import AudioSourceMixin, AudioSourceSession
from .config_changes import ConfigChangesMixin
from .group_commands import GroupCommandsMixin
from .lifecycle import PlayerLifecycleMixin, _Attach
from .locks import PlayerLocksMixin
from .merging import PlayerMergingMixin
from .outputs import OutputsMixin
from .playback import PlaybackMixin
from .power import PowerMixin
from .protocol_linking import ProtocolLinkingMixin
from .sleep_timer import SleepTimerMixin
from .state_updates import StateUpdatesMixin
from .volume import VolumeMixin

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from music_assistant_models.config_entries import CoreConfig

    from music_assistant import MusicAssistant
    from music_assistant.helpers.json import SerializableType
    from music_assistant.models.player_protocol import PlayerProtocol


class PlayerController(
    PlayerLifecycleMixin,
    ProtocolLinkingMixin,
    PlayerMergingMixin,
    StateUpdatesMixin,
    ConfigChangesMixin,
    PlayerLocksMixin,
    PlaybackMixin,
    OutputsMixin,
    AudioSourceMixin,
    PowerMixin,
    VolumeMixin,
    GroupCommandsMixin,
    SleepTimerMixin,
    AnnouncementsMixin,
):
    """Controller holding all logic to control the players it owns."""

    domain: str = "players"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize core controller."""
        super().__init__(mass)
        self._players: dict[str, Player] = {}
        # the registered protocol objects that are attached to their player, or held while
        # it is not known yet which player they belong to, by their id
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
        """Bring back the stored players, before any provider registers a protocol."""
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
            "players_synced": sum(player.synced_to is not None for player in players),
            "players_with_active_group": sum(player.active_group is not None for player in players),
            "announcements_in_progress": sum(
                bool(player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS)) for player in players
            ),
            "pending_protocol_evaluations": len(self._pending_protocol_evaluations),
            # how the players are made up, without anything that names them
            "player_protocols": [
                {
                    "type": player.type.value,
                    "available": player.available,
                    "protocols": [
                        {
                            "domain": protocol.domain,
                            "role": protocol.role.value,
                            "available": protocol.available,
                            "active": protocol.active,
                        }
                        for protocol in player.protocols
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
                await self.unregister(protocol.protocol_id)

    def on_provider_manifests_changed(self) -> None:
        """
        Handle a provider manifest that was registered or removed while the server runs.

        A player without protocols says in its state whether its provider is installed.
        """
        for player in self._players.values():
            if not player.attached_protocols:
                self.trigger_player_update(player.player_id)

    @property
    def providers(self) -> list[PlayerProvider]:
        """Return all loaded/running MusicProviders."""
        return cast("list[PlayerProvider]", self.mass.get_providers(ProviderType.PLAYER))

    def __iter__(self) -> Iterator[Player]:
        """Iterate over all players."""
        return iter(self._players.values())

    # Protocol linking methods are provided by ProtocolLinkingMixin (protocol_linking.py)
