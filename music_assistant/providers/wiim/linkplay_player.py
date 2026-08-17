"""
Generic LinkPlay player: a thin grouping/identity shell over linked protocol players.

Compatible LinkPlay speakers (e.g. Edifier) speak the LinkPlay HTTP API but have no
Music Assistant native audio path. Their playback, transport state, current media and
volume are delegated to their linked DLNA/AirPlay protocol players; this shell only
adds device identity and native LinkPlay multiroom grouping/topology on top.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import (
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.player import DeviceInfo
from pywiim import WiiMClient, WiiMError

from music_assistant.models.protocol_backed_player import ProtocolBackedPlayer

from .constants import BACKEND_GENERIC, PLAYER_ID_PREFIX
from .grouping import NativeGroupRole

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpDevice
    from music_assistant_models.player import PlayerMedia
    from pywiim.models import DeviceInfo as PywiimDeviceInfo

    from music_assistant.models.player import Player

    from .grouping import NativeGroupCoordinator
    from .provider import WiimProvider

# The device is polled for reachability and native group topology only; playback state
# comes from the linked protocol players, so a slow interval is plenty.
POLL_INTERVAL = 30


class LinkPlayPlayer(ProtocolBackedPlayer):
    """
    Generic LinkPlay player shell in Music Assistant.

    Playback/state/volume are delegated to the linked DLNA/AirPlay protocol players via
    the protocol-linking system; this player natively owns only device identity and native
    LinkPlay multiroom grouping, driven by the low-level public WiiMClient. Topology is
    resolved by the provider's shared coordinator across both WiiM backends.
    """

    _attr_type = PlayerType.PLAYER
    linkplay_backend = BACKEND_GENERIC

    def __init__(
        self,
        provider: WiimProvider,
        player_id: str,
        client: WiiMClient,
        upnp_device: UpnpDevice,
        description_url: str,
        mac_address: str | None = None,
        device_info: PywiimDeviceInfo | None = None,
    ) -> None:
        """Initialize the LinkPlay player shell."""
        # Health of the LinkPlay HTTP API, separate from playback availability (which the
        # base derives from linked protocols): it gates the native grouping capability.
        # A device info primed from the discovery probe means the API is reachable.
        # Set before super().__init__ because the base reads supported_features and the
        # follower-suppressed playback state (via the coordinator) during init.
        self._linkplay_available = device_info is not None
        self._native_groups: NativeGroupCoordinator = provider.native_groups
        super().__init__(provider, player_id)
        # Low-level LinkPlay HTTP client over MA's shared aiohttp session. Its close()
        # would close that shared session, so it is never closed here.
        self._client = client
        self._upnp_device = upnp_device
        self._description_url = description_url
        self._mac_address = mac_address
        # Cached pywiim device info, refreshed on poll and passed to a follower's
        # join_slave so it can pick the correct (router vs WiFi-Direct) join mode.
        self._cached_device_info: PywiimDeviceInfo | None = device_info
        self._rebuild_lock = asyncio.Lock()

        self._attr_name = upnp_device.friendly_name or player_id
        self._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        self._attr_needs_poll = True
        self._attr_poll_interval = POLL_INTERVAL
        self._attr_device_info = DeviceInfo(
            model=upnp_device.model_name or "LinkPlay",
            manufacturer=upnp_device.manufacturer or "LinkPlay",
        )
        self._attr_device_info.add_identifier(
            IdentifierType.UUID, player_id.removeprefix(PLAYER_ID_PREFIX).removeprefix("uuid:")
        )
        if client.host:
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, client.host)
        if mac_address:
            self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac_address)

    # --- Lifecycle ---

    async def setup(self) -> None:
        """Handle logic when the player is set up in the Player controller."""
        # The discovery probe already fetched and validated the device info and primed it on
        # this shell, so only re-read reachability when it did not. The provider reads the
        # live topology right after registration (a read taken here would be discarded, as
        # setup runs before the player is registered); the regular poll refreshes both.
        if self._cached_device_info is None:
            await self._refresh_reachability()

    async def poll(self) -> None:
        """Poll the device for reachability and push its native group topology."""
        await self._refresh_reachability()
        await self._native_groups.refresh_leader(self, force=True)

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        self._native_groups.unregister(self.player_id)
        self._native_groups.schedule_reconcile()

    # --- Properties ---

    @property
    def default_output_protocol_domain(self) -> str | None:
        """Prefer a linked DLNA output for this device's default playback path."""
        return "dlna"

    @property
    def cached_device_info(self) -> PywiimDeviceInfo | None:
        """Return the last known pywiim device info (used by peers to pick a join mode)."""
        return self._cached_device_info

    @property
    def native_available(self) -> bool:
        """Return whether the native LinkPlay grouping API is currently reachable."""
        return self._linkplay_available

    @property
    def native_ip(self) -> str | None:
        """Return the device's current IP address, if known."""
        host: str | None = self._client.host
        return host

    @property
    def grouping_rebuild_lock(self) -> asyncio.Lock:
        """Return the lock the coordinator holds so grouping and address rebuilds serialize."""
        return self._rebuild_lock

    @property
    def grouping_locked(self) -> bool:
        """Withdraw ALL grouping only while following a group MA has not discovered."""
        # grouping_locked is the broad, final lock: it suppresses native AND linked-protocol
        # grouping. It must therefore be reserved for a genuinely read-only topology, not for
        # native capability being unavailable. An unknown-leader follower belongs to an
        # external group whose leader MA cannot see, so it can be neither detached nor
        # regrouped, and without this lock core would re-offer it as a grouping target through
        # its linked DLNA/AirPlay protocols. LinkPlay API health only gates NATIVE grouping,
        # which is handled by supported_features and the coordinator's can_group_with, so a
        # reachable linked protocol can still form a group while the LinkPlay API is down.
        return self._native_groups.is_unknown_leader_follower(self.player_id)

    @property
    def playback_state(self) -> PlaybackState:
        """Suppress delegated playback while natively grouped as a follower."""
        if self._is_native_follower:
            # a native follower plays its leader's stream at the hardware level; a known
            # leader's state is mirrored by core through synced_to, an undiscovered one
            # reports idle rather than the shell's stale linked-protocol playback.
            return PlaybackState.IDLE
        return super().playback_state

    @property
    def current_media(self) -> PlayerMedia | None:
        """Suppress delegated media while natively grouped as a follower."""
        if self._is_native_follower:
            return None
        return super().current_media

    @property
    def active_source(self) -> str | None:
        """Suppress the delegated active source while natively grouped as a follower."""
        if self._is_native_follower:
            return None
        return super().active_source

    @property
    def prefer_native_grouping(self) -> bool:
        """Group compatible LinkPlay speakers natively, not via a linked protocol."""
        return self._linkplay_available

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features; native grouping needs a reachable grouping API."""
        features = super().supported_features
        if self._linkplay_available:
            return features
        return features - {PlayerFeature.SET_MEMBERS}

    @property
    def can_group_with(self) -> set[str]:
        """Return the ids of the reachable peers of either backend this player can group with."""
        return self._native_groups.can_group_with(self)

    def is_native_group_compatible(self, other: Player) -> bool:
        """Only reachable coordinator-approved peers of either backend group natively."""
        return other.player_id in self.can_group_with

    def make_command_client(self) -> WiiMClient:
        """
        Return this player's low-level LinkPlay command client.

        The generic backend already owns a client bound to the shared session for the
        device's current address; it is reused for topology reads and grouping commands
        and must never be closed.
        """
        return self._client

    def store_command_capabilities(self, capabilities: dict[str, Any]) -> None:
        """
        Ignore detected capabilities; the generic backend reuses one persistent client.

        :param capabilities: The capabilities a command client detected for this device.
        """

    def on_native_group_update(self) -> None:
        """Re-publish state after the topology coordinator changed this player's role."""
        # role/membership are derived by the coordinator, not from this player's own
        # attributes, so force a recalculation (e.g. synced_to) even when idle.
        self._attr_group_members = self._native_groups.members_of(self.player_id)
        if self._is_native_follower and self.active_output_protocol is not None:
            # a native follower gets audio from its leader at the hardware level, not from a
            # linked protocol. Clear any active output so the final state mirrors the leader
            # (synced_to) or falls back to idle instead of a stale DLNA/AirPlay player, which
            # the base otherwise prioritizes over synced_to. It is left cleared on leaving:
            # normal playback reselects an output DLNA-first / by user preference.
            self.set_active_output_protocol(None)
        self.mark_state_dirty()
        self.update_state()

    # --- Player commands ---

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        await self._native_groups.set_members(self, player_ids_to_add, player_ids_to_remove)

    async def async_handle_address_change(
        self, new_ip: str, upnp_device: UpnpDevice, description_url: str
    ) -> None:
        """
        Rebuild the low-level client against a new device address.

        The replacement is validated before the old client is dropped, so a failed rebuild
        leaves the existing client intact for a later retry.

        :param new_ip: The device's new IP address.
        :param upnp_device: The UPnP device freshly probed at the new location.
        :param description_url: The matched description.xml URL at the new location.
        """
        async with self._rebuild_lock:
            if new_ip == self._client.host:
                return
            new_client = WiiMClient(new_ip, session=self.mass.http_session)
            try:
                device_info = await new_client.get_device_info_model()
            except WiiMError as err:
                self.logger.warning(
                    "Failed to reach LinkPlay device %s at %s: %s", self.name, new_ip, err
                )
                return
            self._client = new_client
            self._cached_device_info = device_info
            self._description_url = description_url
            self._upnp_device = upnp_device
            self._linkplay_available = True
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, new_ip)
        # Re-read the topology from the new address so a stale entry never survives a move.
        await self._native_groups.refresh_leader(self, force=True)
        self.update_state()

    # --- Internals ---

    def _backing_protocol_player_ids(self) -> list[str]:
        """Return the ids of the linked protocol players backing this shell's playback."""
        return [linked.output_protocol_id for linked in self.linked_output_protocols]

    @property
    def _is_native_follower(self) -> bool:
        """Whether the coordinator currently resolves this shell as a native follower."""
        return self._native_groups.role_of(self.player_id) == NativeGroupRole.FOLLOWER

    async def _refresh_reachability(self) -> None:
        """Refresh the LinkPlay grouping API reachability and cached device info."""
        # Serialize with async_handle_address_change so an in-flight read against the old
        # client cannot land after a validated client swap and overwrite it with stale state.
        async with self._rebuild_lock:
            try:
                self._cached_device_info = await self._client.get_device_info_model()
            except WiiMError as err:
                if self._linkplay_available:
                    self.logger.debug("LinkPlay API unreachable for %s: %s", self.name, err)
                self._linkplay_available = False
                self.update_state()
                return
            self._linkplay_available = True
            self.update_state()
