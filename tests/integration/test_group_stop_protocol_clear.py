"""
End-to-end repro: stale active output protocol after stopping a sync group.

Boots a hermetic MusicAssistant with fake native players ("wiim" + "sonos" style,
different providers so they cannot group natively) that each have a linked fake
AirPlay protocol player. The protocol players report their state with a realistic
delay after a stop command (like real hardware does).

Scenario (as reported):
1. Play on the "wiim" native player solo -> native protocol.
2. Play on a sync group of wiim + sonos -> AirPlay protocol (only common protocol).
3. Stop the group.
4. Play on the "wiim" player solo again -> MUST use native protocol again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, NonCallableMagicMock, patch

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import (
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProviderType,
)
from music_assistant_models.provider import ProviderManifest

from music_assistant.mass import MusicAssistant
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.models.player_provider import PlayerProvider
from tests.common import suppress_auto_loaded_providers
from tests.conftest import _create_mock_zeroconf

if TYPE_CHECKING:
    import pathlib

# how long the fake protocol players keep reporting PLAYING after a stop command,
# mimicking real devices (Wiim/AirPlay) that only report new state on the next
# poll/event cycle
STATE_REPORT_DELAY = 1.5

WIIM_MAC = "00:11:22:33:44:01"
SONOS_MAC = "00:11:22:33:44:02"


class FakePlayerProvider(PlayerProvider):
    """Minimal player provider for fake players."""


def _make_provider(mass: MusicAssistant, domain: str) -> FakePlayerProvider:
    """Create and register a fake player provider on the mass instance."""
    manifest = ProviderManifest(
        type=ProviderType.PLAYER,
        domain=domain,
        name=domain,
        description=domain,
        codeowners=["@music-assistant"],
    )
    config = ProviderConfig(
        values={},
        type=ProviderType.PLAYER,
        domain=domain,
        instance_id=domain,
        name=domain,
    )
    # fake config has no config entries; resolve the log level lookup in Provider.__init__
    object.__setattr__(config, "get_value", lambda *_args, **_kwargs: "GLOBAL")
    provider = FakePlayerProvider(mass, manifest, config)
    provider.available = True
    provider.logger = logging.getLogger(f"test.{domain}")
    mass._providers[domain] = provider
    return provider


class FakeNativePlayer(Player):
    """Fake native player (like a Wiim or Sonos device)."""

    def __init__(self, provider: FakePlayerProvider, player_id: str, name: str, mac: str) -> None:
        """Initialize the fake native player."""
        super().__init__(provider, player_id)
        self._attr_name = name
        self._attr_available = True
        self._attr_powered = True
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.SET_MEMBERS,
        }
        # can only group natively with players of the same provider
        self._attr_can_group_with = {provider.instance_id}
        self._attr_device_info = DeviceInfo(model="Fake", manufacturer="Fake")
        self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac)

    async def play(self) -> None:
        """Play command."""
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def stop(self) -> None:
        """Stop command."""
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        self._attr_current_media = media
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command."""
        members = dict.fromkeys(self._attr_group_members)
        for member_id in player_ids_to_add or []:
            members[member_id] = None
        for member_id in player_ids_to_remove or []:
            members.pop(member_id, None)
        other = [pid for pid in members if pid != self.player_id]
        self._attr_group_members = [self.player_id, *other] if other else []
        self.update_state()


class FakeProtocolPlayer(Player):
    """
    Fake protocol player (like an AirPlay endpoint on a device).

    Reports playback state transitions with a delay after commands, like real
    hardware that only reports state on the next poll/event cycle.
    """

    def __init__(self, provider: FakePlayerProvider, player_id: str, name: str, mac: str) -> None:
        """Initialize the fake protocol player."""
        super().__init__(provider, player_id)
        self._attr_name = name
        self._attr_type = PlayerType.PROTOCOL
        self._attr_available = True
        self._attr_powered = True
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.SET_MEMBERS,
        }
        self._attr_can_group_with = {provider.instance_id}
        self._attr_device_info = DeviceInfo(model="FakeAP", manufacturer="Fake")
        self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac)

    async def play(self) -> None:
        """Play command."""
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def stop(self) -> None:
        """Stop command - state is reported delayed, like real hardware."""

        def _report_idle() -> None:
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_current_media = None
            self.update_state()

        self.mass.loop.call_later(STATE_REPORT_DELAY, _report_idle)

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        self._attr_current_media = media
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command."""
        members = dict.fromkeys(self._attr_group_members)
        for member_id in player_ids_to_add or []:
            members[member_id] = None
        for member_id in player_ids_to_remove or []:
            members.pop(member_id, None)
        other = [pid for pid in members if pid != self.player_id]
        self._attr_group_members = [self.player_id, *other] if other else []
        self.update_state()
        # like real protocol players: children report synced_to the leader
        for member_id in player_ids_to_add or []:
            if (member := self.mass.players.get_player(member_id)) is not None:
                member._attr_synced_to = self.player_id  # type: ignore[attr-defined]
                member.update_state()
        for member_id in player_ids_to_remove or []:
            if (member := self.mass.players.get_player(member_id)) is not None:
                member._attr_synced_to = None  # type: ignore[attr-defined]
                member.update_state()


def _free_port() -> int:
    """Grab a free TCP port from the OS."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for(predicate: Callable[[], object], timeout: float = 10.0) -> bool:
    """Poll predicate until truthy or timeout."""
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(0.1)
        elapsed += 0.1
    return bool(predicate())


@pytest.fixture
async def repro_mass(tmp_path: pathlib.Path) -> AsyncGenerator[MusicAssistant]:
    """Boot a hermetic MusicAssistant for the repro."""
    storage_path = tmp_path / "data"
    cache_path = tmp_path / "cache"
    storage_path.mkdir(parents=True)
    cache_path.mkdir(parents=True)
    logging.getLogger("aiosqlite").level = logging.INFO
    # use free ports for the web/stream servers so the test can run
    # alongside a real MA instance on the same machine
    settings = {
        "core": {
            "webserver": {"domain": "webserver", "values": {"bind_port": _free_port()}},
            "streams": {"domain": "streams", "values": {"bind_port": _free_port()}},
        }
    }
    (storage_path / "settings.json").write_text(json.dumps(settings))
    mass_instance = MusicAssistant(str(storage_path), str(cache_path))
    with (
        patch(
            "music_assistant.controllers.discovery.controller.AsyncZeroconf",
            return_value=_create_mock_zeroconf(),
        ),
        patch(
            "music_assistant.controllers.discovery.controller.AsyncServiceBrowser",
            return_value=NonCallableMagicMock(),
        ),
        patch(
            "music_assistant.controllers.streams.controller.check_ffmpeg_version",
            new=AsyncMock(),
        ),
        patch(
            "music_assistant.controllers.discovery.controller.async_upnp_search",
            new=AsyncMock(),
        ),
        # hermetic + parallel-safe: suppress builtin providers that bind fixed
        # ports (sendspin) or reach into host hardware (local_audio)
        patch("tests.common.SUPPRESSED_BUILTIN_PROVIDERS", {"local_audio", "sendspin"}),
        suppress_auto_loaded_providers(),
    ):
        await mass_instance.start()
        try:
            yield mass_instance
        finally:
            await mass_instance.stop()


@pytest.mark.asyncio
async def test_group_stop_clears_leader_protocol(repro_mass: MusicAssistant) -> None:
    """After stopping a sync group, solo playback on the leader must be native again."""
    mass = repro_mass
    # sync_group is builtin/auto-loaded; the test music provider must be configured
    await mass.config.save_provider_config("test", {})
    assert await _wait_for(lambda: mass.get_provider("sync_group") is not None)
    assert await _wait_for(lambda: mass.get_provider("test") is not None)

    # set up fake providers + players
    wiim_prov = _make_provider(mass, "fakewiim")
    sonos_prov = _make_provider(mass, "fakesonos")
    airplay_prov = _make_provider(mass, "fakeairplay")

    wiim = FakeNativePlayer(wiim_prov, "wiim_1", "Living Room", WIIM_MAC)
    sonos = FakeNativePlayer(sonos_prov, "sonos_1", "Bedroom", SONOS_MAC)
    wiim_ap = FakeProtocolPlayer(airplay_prov, "ap_wiim", "Living Room (AirPlay)", WIIM_MAC)
    sonos_ap = FakeProtocolPlayer(airplay_prov, "ap_sonos", "Bedroom (AirPlay)", SONOS_MAC)
    for player in (wiim, sonos, wiim_ap, sonos_ap):
        await mass.players.register(player)

    # protocol players must be linked to their native parents (matched via MAC)
    assert await _wait_for(lambda: wiim_ap.protocol_parent_id == wiim.player_id), (
        "wiim airplay protocol player not linked"
    )
    assert await _wait_for(lambda: sonos_ap.protocol_parent_id == sonos.player_id), (
        "sonos airplay protocol player not linked"
    )

    # create the sync group player ("Whole Home")
    group = await mass.players.create_group_player(
        "sync_group", "Whole Home", [wiim.player_id, sonos.player_id]
    )

    async def play_test_track(queue_id: str) -> None:
        test_prov = mass.get_provider("test")
        track = await test_prov.get_track("0_0_0")  # type: ignore[union-attr]
        await mass.player_queues.play_media(queue_id, track)

    # 1. play on wiim solo -> native protocol
    await play_test_track(wiim.player_id)
    assert await _wait_for(lambda: wiim.active_output_protocol == "native"), (
        f"expected native protocol, got {wiim.active_output_protocol}"
    )
    await mass.players.cmd_stop(wiim.player_id)
    assert await _wait_for(lambda: wiim.state.playback_state == PlaybackState.IDLE)
    # allow the delayed protocol clear (5s) to pass
    await asyncio.sleep(6)

    # 2. play on the group -> airplay protocol on the leader
    await play_test_track(group.player_id)
    assert await _wait_for(
        lambda: (
            wiim.active_output_protocol == wiim_ap.player_id
            or sonos.active_output_protocol == sonos_ap.player_id
        ),
        timeout=15,
    ), (
        f"group playback did not use airplay: wiim={wiim.active_output_protocol}, "
        f"sonos={sonos.active_output_protocol}"
    )
    leader = wiim if wiim.active_output_protocol == wiim_ap.player_id else sonos
    # the group itself must reflect the leader's playing state
    assert await _wait_for(lambda: group.state.playback_state == PlaybackState.PLAYING), (
        f"group did not report playing: {group.state.playback_state}"
    )

    # 3. stop the group
    await mass.players.cmd_stop(group.player_id)
    # allow delayed state reports and any delayed protocol clear to settle
    await asyncio.sleep(8)

    # 4. the leader's active protocol must be cleared after the group was dissolved
    assert leader.active_output_protocol in (None, "native"), (
        f"stale active output protocol on {leader.display_name}: {leader.active_output_protocol}"
    )

    # 5. playing solo on the leader again must use the native protocol
    await play_test_track(leader.player_id)
    assert await _wait_for(lambda: leader.active_output_protocol == "native"), (
        f"solo playback did not use native protocol: {leader.active_output_protocol}"
    )
