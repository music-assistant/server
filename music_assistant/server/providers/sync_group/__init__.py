"""
Sync Group Player provider.

This is more like a "virtual" player provider,
allowing the user to create 'presets' of players to sync together (of the same type).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import shortuuid

from music_assistant.common.models.config_entries import (
    BASE_PLAYER_CONFIG_ENTRIES,
    CONF_ENTRY_PLAYER_ICON_GROUP,
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    PlayerConfig,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant.common.models.errors import (
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant.common.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.constants import (
    CONF_CROSSFADE,
    CONF_CROSSFADE_DURATION,
    CONF_ENABLE_ICY_METADATA,
    CONF_ENFORCE_MP3,
    CONF_FLOW_MODE,
    CONF_GROUP_MEMBERS,
    CONF_HTTP_PROFILE,
    CONF_SAMPLE_RATES,
    SYNCGROUP_PREFIX,
)
from music_assistant.server.helpers.util import TaskManager
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from collections.abc import Iterable

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


# ruff: noqa: ARG002


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SyncGroupProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # nothing to configure (for now)
    return ()


class SyncGroupProvider(PlayerProvider):
    """Base/builtin provider for grouping players (of the same ecosystem)."""

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return ()

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config)
        self.mass.register_api_command("players/create_syncgroup", self.create_group)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # temp: migrate old config entries
        # remove this in a future release
        for player_config in await self.mass.config.get_player_configs():
            if not player_config.player_id.startswith(SYNCGROUP_PREFIX):
                continue
            if player_config.provider == self.instance_id:
                continue
            # migrate old syncgroup players to this provider
            player_config.provider = self.instance_id
            self.mass.config.set_raw_player_config_value(
                player_config.player_id, "provider", self.instance_id
            )
        await self._register_all_players()

    async def unload(self) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        # default entries for syncgroups
        base_entries = (
            *BASE_PLAYER_CONFIG_ENTRIES,
            CONF_ENTRY_PLAYER_ICON_GROUP,
        )
        # grab additional details from the first member
        if not (group_player := self.mass.players.get(player_id)):
            return base_entries
        child_player = next((x for x in self.mass.players.iter_group_members(group_player)), None)
        if not child_player:
            return base_entries
        if not (player_provider := self.mass.get_provider(child_player.provider)):
            return base_entries
        if TYPE_CHECKING:
            player_provider = cast(PlayerProvider, player_provider)
        # combine base group entries with (base) player entries for this player type
        allowed_conf_entries = (
            CONF_HTTP_PROFILE,
            CONF_ENABLE_ICY_METADATA,
            CONF_CROSSFADE,
            CONF_CROSSFADE_DURATION,
            CONF_ENFORCE_MP3,
            CONF_FLOW_MODE,
            CONF_SAMPLE_RATES,
        )
        child_config_entries = await player_provider.get_player_config_entries(
            child_player.player_id
        )
        return (
            *base_entries,
            ConfigEntry(
                key=CONF_GROUP_MEMBERS,
                type=ConfigEntryType.STRING,
                label="Group members",
                default_value=[],
                options=tuple(
                    ConfigValueOption(x.display_name, x.player_id)
                    for x in self.mass.players.all(True, False)
                    if x.provider == player_provider.instance_id
                ),
                description="Select all players you want to be part of this group",
                multi_value=True,
                required=True,
            ),
            *(entry for entry in child_config_entries if entry.key in allowed_conf_entries),
        )

    def on_player_config_changed(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        if not config.enabled:
            # edge case: ensure that the player is powered off if the player gets disabled
            self.mass.create_task(self.cmd_power(config.player_id, False))

    def on_player_config_removed(self, player_id: str) -> None:
        """Call (by config manager) when the configuration of a player is removed."""
        if not (group_player := self.mass.players.get(player_id)):
            return
        if group_player.powered:
            # edge case: the group player is powered and being removed
            for member in self.mass.players.iter_group_members(group_player, only_powered=True):
                member.active_group = None
                if member.state == PlayerState.IDLE:
                    continue
                self.mass.create_task(self.mass.players.cmd_stop(member.player_id))
            self.mass.players.remove(group_player.player_id, False)

    def on_group_child_state(
        self, group_player_id: str, child_player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Call (by player manager) when a childplayer in a (active) group changed state."""
        self.update_attributes()

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        group_player = self.mass.players.get(player_id)
        group_player.state = PlayerState.IDLE
        self.mass.players.update(player_id)
        # forward command to player and any connected sync child's
        async with TaskManager(self.mass) as tg:
            for member in self.mass.players.iter_group_members(group_player, only_powered=True):
                if member.state == PlayerState.IDLE:
                    continue
                tg.create_task(self.mass.players.cmd_stop(member.player_id))
        if (stream := self.streams.pop(player_id, None)) and not stream.done:
            await stream.stop()

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Handle POWER command to group player."""
        group_player = self.mass.players.get(player_id, raise_unavailable=True)
        if TYPE_CHECKING:
            group_player = cast(Player, group_player)

        # make sure to (optimistically) update the group power state
        group_player.powered = powered

        # always stop at power off
        if not powered and group_player.state in (PlayerState.PLAYING, PlayerState.PAUSED):
            await self.cmd_stop(group_player.player_id)

        async with TaskManager(self.mass) as tg:
            for member in self.mass.players.iter_group_members(group_player):
                if powered:
                    # handle TURN_ON of the group player by turning on all members
                    if (
                        member.state in (PlayerState.PLAYING, PlayerState.PAUSED)
                        and member.active_source != group_player.active_source
                    ):
                        # stop playing existing content on member if we start the group player
                        tg.create_task(self.cmd_stop(member.player_id))
                    if not member.powered:
                        tg.create_task(self.cmd_power(member.player_id, True))
                    # set active source to group player if the group (is going to be) powered
                    member.active_group = group_player.player_id
                    member.active_source = group_player.active_source
                else:
                    # handle TURN_OFF of the group player by turning off all members
                    if member.active_group != group_player.player_id:
                        # the member is (somehow) not part of this group
                        # bit of an edge case, but still good to guard here
                        continue
                    # reset active group on player when the group is turned off
                    member.active_group = None
                    member.active_source = None
                    # handle TURN_OFF of the group player by turning off all members
                    if member.powered:
                        tg.create_task(self.cmd_power(member.player_id, False))
        if powered:
            await self._sync_syncgroup(group_player)
        # optimistically set the group state
        self.mass.players.update(group_player.player_id)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        # group volume is already handled in the player manager

    async def play_media(
        self,
        player_id: str,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player."""
        group_player = self.mass.players.get(player_id)
        if not group_player.powered:
            await self.cmd_power(player_id, True)
        else:
            await self._sync_syncgroup(group_player)
        if sync_leader := self._get_sync_leader(group_player):
            await self.play_media(sync_leader.player_id, media=media)
            group_player.state = PlayerState.PLAYING

    async def enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """Handle enqueuing of a next media item on the player."""
        group_player = self.mass.players.get(player_id, True)
        if sync_leader := self._get_sync_leader(group_player):
            await self.enqueue_next_media(
                sync_leader.player_id,
                media=media,
            )

    async def create_group(self, name: str, members: list[str]) -> Player:
        """Create new Sync Group Player."""
        base_player = self.mass.players.get(members[0], True)
        # perform basic checks
        if (player_prov := self.mass.get_provider(base_player.provider)) is None:
            msg = f"Provider {base_player.provider} is not available!"
            raise ProviderUnavailableError(msg)
        if ProviderFeature.SYNC_PLAYERS not in player_prov.supported_features:
            msg = f"Provider {player_prov.name} does not support creating groups"
            raise UnsupportedFeaturedException(msg)
        new_group_id = f"{SYNCGROUP_PREFIX}{shortuuid.random(8).lower()}"
        # cleanup list, just in case the frontend sends some garbage
        members = [
            x
            for x in members
            if (x in base_player.provider == base_player.provider)
            and not x.startswith(SYNCGROUP_PREFIX)
        ]
        # create default config with the user chosen name
        self.mass.config.create_default_player_config(
            new_group_id,
            player_prov.instance_id,
            name=name,
            enabled=True,
            values={CONF_GROUP_MEMBERS: members},
        )
        return self._register_group_player(group_player_id=new_group_id, name=name, members=members)

    async def _register_all_players(self) -> None:
        """Register all (virtual/fake) group players in the Player controller."""
        player_configs = await self.mass.config.get_player_configs(
            self.instance_id, include_values=True
        )
        for player_config in player_configs:
            members = player_config.get_value(CONF_GROUP_MEMBERS)
            self._register_group_player(
                player_config.player_id,
                player_config.name or player_config.default_name,
                members,
            )

    def _register_group_player(
        self, group_player_id: str, name: str, members: Iterable[str], provider: str
    ) -> Player:
        """Register a syncgroup player."""
        # extract player features from first/random player
        for member in members:
            if first_player := self.mass.players.get(member):
                supported_features = first_player.supported_features
                if player_prov := self.mass.get_provider(first_player.provider):
                    player_provider_name = player_prov.name
                break
        else:
            # edge case: no child player is (yet) available
            # use default values
            supported_features = {
                PlayerFeature.POWER,
                PlayerFeature.VOLUME_SET,
                PlayerFeature.PAUSE,
                PlayerFeature.VOLUME_MUTE,
            }
            player_provider_name = "SyncGroup"
        player = Player(
            player_id=group_player_id,
            provider=self.instance_id,
            type=PlayerType.GROUP,
            name=name,
            available=first_player is not None,
            powered=False,
            device_info=DeviceInfo(model=player_provider_name, manufacturer=self.name),
            supported_features=supported_features,
            group_childs=set(members),
            active_source=group_player_id,
        )
        self.mass.players.register_or_update(player)
        return player

    def _get_sync_leader(self, group_player: Player) -> Player | None:
        """Get the active sync leader player for the syncgroup."""
        if group_player.synced_to:
            # should not happen but just in case...
            return self.mass.players.get(group_player.synced_to)
        # Return the (first/only) player that has group childs
        for child_player in self.mass.players.iter_group_members(
            group_player, only_powered=False, only_playing=False
        ):
            if child_player.group_childs:
                return child_player
        return None

    def _select_sync_leader(self, group_player: Player) -> Player | None:
        """Select the active sync leader player for a syncgroup."""
        if sync_leader := self._get_sync_leader(group_player):
            return sync_leader
        # select new sync leader: return the first powered player
        for child_player in self.mass.players.iter_group_members(
            group_player, only_powered=True, only_playing=False
        ):
            if child_player.active_group not in (None, group_player.player_id):
                continue
            if (
                child_player.active_source
                and child_player.active_source != group_player.active_source
            ):
                continue
            return child_player
        # fallback select new sync leader: simply return the first (available) player
        for child_player in self.mass.players.iter_group_members(
            group_player, only_powered=False, only_playing=False
        ):
            return child_player
        # this really should not be possible
        raise RuntimeError("Impossible to select sync leader for syncgroup")

    async def _sync_syncgroup(self, group_player: Player) -> None:
        """Sync all (possible) players of a syncgroup."""
        sync_leader = self._select_sync_leader(group_player)
        members_to_sync: list[str] = []
        for member in self.mass.players.iter_group_members(group_player):
            if sync_leader.player_id == member.player_id:
                # skip sync leader
                continue
            if member.synced_to == sync_leader.player_id:
                # already synced
                continue
            if member.synced_to and member.synced_to != sync_leader.player_id:
                # unsync first
                await self.cmd_unsync(member.player_id)
            members_to_sync.append(member.player_id)
        if members_to_sync:
            await self.cmd_sync_many(sync_leader.player_id, members_to_sync)

    def update_attributes(self, player: Player) -> None:
        """Update attributes of a player."""
        sync_leader = self._get_sync_leader(player)
        if sync_leader and sync_leader.active_source == player.active_source:
            player.state = sync_leader.state
            player.active_source = sync_leader.active_source
            player.current_media = sync_leader.current_media
            player.elapsed_time = sync_leader.elapsed_time
            player.elapsed_time_last_updated = sync_leader.elapsed_time_last_updated
        else:
            player.state = PlayerState.IDLE
            player.active_source = player.player_id
