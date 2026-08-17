"""
Generic LinkPlay player: a thin grouping/identity shell over linked protocol players.

Compatible LinkPlay speakers (e.g. Edifier) speak the LinkPlay HTTP API but have no
Music Assistant native audio path. Their playback, transport state, current media and
volume are delegated to their linked DLNA/AirPlay protocol players; this shell only
adds device identity and native LinkPlay multiroom grouping/topology on top.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant_models.enums import IdentifierType, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed, UnsupportedFeaturedException
from music_assistant_models.player import DeviceInfo
from pywiim import WiiMClient, WiiMError, WiiMGroupCompatibilityError

from music_assistant.models.protocol_backed_player import ProtocolBackedPlayer

from .constants import PLAYER_ID_PREFIX
from .helpers import linkplay_slave_uuid_to_udn

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpDevice
    from pywiim.models import DeviceInfo as PywiimDeviceInfo

    from .provider import WiimProvider

# The device is polled for reachability and native group topology only; playback state
# comes from the linked protocol players, so a slow interval is plenty.
POLL_INTERVAL = 30

# A native multiroom join/leave is fire-and-forget and the group takes a few seconds to
# settle, so the leader's topology is re-read a few times before a change is rejected.
GROUP_VERIFY_ATTEMPTS = 5
GROUP_VERIFY_INTERVAL = 1.5


class LinkPlayPlayer(ProtocolBackedPlayer):
    """
    Generic LinkPlay player shell in Music Assistant.

    Playback/state/volume are delegated to the linked DLNA/AirPlay protocol players via
    the protocol-linking system; this player natively owns only device identity and
    same-backend LinkPlay multiroom grouping, driven by the low-level public WiiMClient.
    """

    _attr_type = PlayerType.PLAYER

    def __init__(
        self,
        provider: WiimProvider,
        player_id: str,
        client: WiiMClient,
        upnp_device: UpnpDevice,
        description_url: str,
        mac_address: str | None = None,
    ) -> None:
        """Initialize the LinkPlay player shell."""
        # Health of the LinkPlay HTTP API, separate from playback availability (which the
        # base derives from linked protocols): it gates the native grouping capability.
        # Set before super().__init__ because the base reads supported_features during init.
        self._linkplay_available = False
        super().__init__(provider, player_id)
        # Low-level LinkPlay HTTP client over MA's shared aiohttp session. Its close()
        # would close that shared session, so it is never closed here.
        self._client = client
        self._upnp_device = upnp_device
        self._description_url = description_url
        self._mac_address = mac_address
        # Cached pywiim device info, refreshed on poll and passed to a follower's
        # join_slave so it can pick the correct (router vs WiFi-Direct) join mode.
        self._cached_device_info: PywiimDeviceInfo | None = None
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
        await self._refresh_linkplay()

    async def poll(self) -> None:
        """Poll the device for reachability and native group topology."""
        await self._refresh_linkplay()

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
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features; native grouping needs a reachable LinkPlay API."""
        features = super().supported_features
        # Native multiroom grouping depends only on the LinkPlay HTTP API being reachable,
        # not on what is playing; the correctly unioned base already carries SET_MEMBERS,
        # so it is only withdrawn while the API is unreachable.
        if self._linkplay_available:
            return features
        return features - {PlayerFeature.SET_MEMBERS}

    @property
    def can_group_with(self) -> set[str]:
        """Return the ids of the reachable generic LinkPlay peers this player can group with."""
        if not self._linkplay_available:
            return set()
        return {
            player.player_id
            for player in self.provider.players
            if isinstance(player, LinkPlayPlayer)
            and player is not self
            and player._linkplay_available
        }

    # --- Player commands ---

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        if not self._linkplay_available:
            raise PlayerCommandFailed(f"{self.name} is not reachable for grouping")
        try:
            for member_id in player_ids_to_add or []:
                member = self._require_linkplay_member(member_id)
                await member._client.join_slave(
                    self._client.host, master_device_info=self._cached_device_info
                )
                await self._verify_group_change(member, member_id, expect_slave=True)
            for member_id in player_ids_to_remove or []:
                member = self._require_linkplay_member(member_id)
                await member._client.leave_group()
                await self._verify_group_change(member, member_id, expect_slave=False)
        except WiiMGroupCompatibilityError as err:
            raise PlayerCommandFailed(
                f"Cannot group {self.name}: incompatible devices ({err})"
            ) from err
        except WiiMError as err:
            raise PlayerCommandFailed(f"set_members failed on {self.name}: {err}") from err
        finally:
            self.mass.create_task(
                self._refresh_linkplay(), task_id=f"linkplay_refresh_{self.player_id}"
            )

    async def async_handle_address_change(
        self, new_ip: str, upnp_device: UpnpDevice, description_url: str
    ) -> None:
        """
        Rebuild the low-level client against a new device address.

        The replacement is validated before the old client is dropped, so a failed
        rebuild leaves the existing client intact for a later retry.

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
            try:
                await self._update_group_members()
            except WiiMError as err:
                # The rebuild succeeded; a stale topology read must not undo it.
                self.logger.debug("Failed to read group topology for %s: %s", self.name, err)
            self.update_state()

    # --- Internals ---

    def _backing_protocol_player_ids(self) -> list[str]:
        """Return the ids of the linked protocol players backing this shell's playback."""
        return [linked.output_protocol_id for linked in self.linked_output_protocols]

    async def _refresh_linkplay(self) -> None:
        """Refresh reachability, cached device info and native group topology."""
        # Serialize with async_handle_address_change so an in-flight poll against the old
        # client cannot land after a validated client swap and overwrite it with stale state.
        async with self._rebuild_lock:
            try:
                self._cached_device_info = await self._client.get_device_info_model()
            except WiiMError as err:
                if self._linkplay_available:
                    self.logger.debug("LinkPlay API unreachable for %s: %s", self.name, err)
                self._linkplay_available = False
                self._attr_group_members = []
                self.update_state()
                return
            self._linkplay_available = True
            try:
                await self._update_group_members()
            except WiiMError as err:
                # A transient topology read must not drop a real hardware group; the last
                # known members are kept until a successful read replaces them.
                self.logger.debug("Failed to read group topology for %s: %s", self.name, err)
            self.update_state()

    async def _update_group_members(self) -> None:
        """Map this device's native slave list onto MA group member ids (leader first)."""
        # get_slaves_info() raises on a failed request (unlike get_device_group_info, which
        # masks a failed slave read as "solo"), so a transient blip cannot silently clear a
        # real group. It returns the slaves only for a master; a follower/solo returns none.
        slaves = await self._client.get_slaves_info()
        members = [self.player_id]
        for slave in slaves:
            if not isinstance(slave, dict):
                continue
            resolved = self._resolve_member_player_id(slave.get("uuid"))
            if resolved and resolved not in members:
                members.append(resolved)
        self._attr_group_members = members if len(members) > 1 else []

    def _require_linkplay_member(self, player_id: str) -> LinkPlayPlayer:
        """
        Return a same-backend member for a grouping command.

        MA-created grouping stays within the generic LinkPlay backend; a request that
        targets any other player is rejected as unsupported rather than cast.
        """
        member = self.mass.players.get_player(player_id)
        if not isinstance(member, LinkPlayPlayer):
            raise UnsupportedFeaturedException(
                f"Cannot group {player_id} with {self.name}: cross-backend grouping is unsupported"
            )
        return member

    async def _verify_group_change(
        self, member: LinkPlayPlayer, member_id: str, expect_slave: bool
    ) -> None:
        """Confirm a grouping operation actually took effect on THIS leader's group."""
        for attempt in range(GROUP_VERIFY_ATTEMPTS):
            try:
                slaves = await self._client.get_slaves_info()
            except WiiMError as err:
                raise PlayerCommandFailed(
                    f"Could not verify grouping of {member_id} on {self.name}: {err}"
                ) from err
            current_members = {
                resolved
                for slave in slaves
                if isinstance(slave, dict)
                and (resolved := self._resolve_member_player_id(slave.get("uuid"))) is not None
            }
            if (member.player_id in current_members) == expect_slave:
                return
            # The group is still settling; wait briefly and re-read before giving up.
            if attempt + 1 < GROUP_VERIFY_ATTEMPTS:
                await asyncio.sleep(GROUP_VERIFY_INTERVAL)
        verb = "join" if expect_slave else "leave"
        raise PlayerCommandFailed(f"{member_id} did not {verb} the group led by {self.name}")

    def _resolve_member_player_id(self, slave_uuid: str | None) -> str | None:
        """
        Resolve a slave's UUID (24-char HTTP or full UDN form) to a registered player id.

        Matches against players already registered by this provider (in either backend)
        and ignores members that do not resolve to a known player.

        :param slave_uuid: The slave's UUID from the native group topology.
        """
        if not slave_uuid or (udn := linkplay_slave_uuid_to_udn(slave_uuid)) is None:
            return None
        target_hex = udn.removeprefix("uuid:").replace("-", "").upper()
        for player in self.provider.players:
            player_id = player.player_id
            if not player_id.startswith(PLAYER_ID_PREFIX):
                continue
            udn_hex = player_id[len(PLAYER_ID_PREFIX) :].removeprefix("uuid:").replace("-", "")
            if udn_hex.upper() == target_hex:
                return player_id
        return None
