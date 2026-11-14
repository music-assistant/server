"""Tests for the role, the discovery and the player of a Cast device."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID, uuid4

import pytest
from music_assistant_models.enums import IdentifierType, PlayerType, ProtocolRole
from pychromecast.const import CAST_TYPE_AUDIO, CAST_TYPE_GROUP
from pychromecast.controllers.multizone import MultizoneManager
from pychromecast.socket_client import CONNECTION_STATUS_CONNECTED

from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.chromecast.constants import (
    APP_TRACK_CHANGE_TIME,
    CONF_DEFER_IDLE_VOLUME,
)
from music_assistant.providers.chromecast.helpers import ChromecastInfo
from music_assistant.providers.chromecast.player import ChromecastPlayer
from music_assistant.providers.chromecast.provider import ChromecastProvider
from tests.providers.chromecast.test_media_status_updates import _media_status
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

MAC_ADDRESS = "00:11:22:33:44:66"


@pytest.fixture
async def scenario(tmp_path: pathlib.Path) -> AsyncGenerator[Scenario]:
    """
    Boot a real server with fake player providers.

    :param tmp_path: Temporary directory for the server's storage.
    """
    scenario = Scenario(tmp_path)
    try:
        await scenario.start()
        yield scenario
    finally:
        await scenario.close()


def _cast_info(
    manufacturer: str = "Google Inc.",
    cast_type: str = CAST_TYPE_AUDIO,
    multichannel: bool = False,
) -> ChromecastInfo:
    """
    Return the info of a Cast device as discovery fills it out.

    :param manufacturer: The manufacturer the device reports.
    :param cast_type: The cast type of the device.
    :param multichannel: Whether a group is a stereo pair.
    """
    return ChromecastInfo(
        services=set(),
        uuid=uuid4(),
        model_name="Test Model",
        friendly_name="Speaker A",
        host="192.168.1.71",
        port=8009,
        cast_type=cast_type,
        manufacturer=manufacturer,
        mac_address=MAC_ADDRESS,
        is_dynamic_group=False,
        is_multichannel_group=multichannel,
    )


def _cast_player(provider: Any, cast_info: ChromecastInfo) -> ChromecastPlayer:
    """Construct the protocol of a Cast device; it connects when it is attached."""
    return ChromecastPlayer(provider, str(cast_info.uuid), cast_info)


def _connect_client(*_args: object) -> MagicMock:
    """Return a fake client of an idle receiver, as pychromecast connects one."""
    client = MagicMock()
    client.app_id = None
    return client


@pytest.mark.parametrize(
    ("cast_info", "role", "player_type"),
    [
        (_cast_info(), ProtocolRole.NATIVE, PlayerType.PLAYER),
        (_cast_info(manufacturer="Sonos"), ProtocolRole.AUDIO, PlayerType.PLAYER),
        (_cast_info(cast_type=CAST_TYPE_GROUP), ProtocolRole.NATIVE, PlayerType.GROUP),
        (
            _cast_info(cast_type=CAST_TYPE_GROUP, multichannel=True),
            ProtocolRole.NATIVE,
            PlayerType.STEREO_PAIR,
        ),
        # a group of speakers of another brand is still a Cast group
        (
            _cast_info(manufacturer="Sonos", cast_type=CAST_TYPE_GROUP),
            ProtocolRole.NATIVE,
            PlayerType.GROUP,
        ),
    ],
    ids=["google-speaker", "other-brand", "cast-group", "stereo-pair", "other-brand-group"],
)
def test_a_cast_device_reports_its_role(
    cast_info: ChromecastInfo, role: ProtocolRole, player_type: PlayerType
) -> None:
    """Cast is native for a Google device and a Cast group, an audio output of any other."""
    protocol = _cast_player(MagicMock(), cast_info)

    assert protocol.role == role
    assert protocol.type == player_type


def _discovering_provider(attached: set[UUID], known: list[UUID]) -> Any:
    """
    Return a provider whose discovery is running and knows the given devices.

    :param attached: The devices that have an attached protocol.
    :param known: The devices the discovery found.
    """
    provider: Any = ChromecastProvider.__new__(ChromecastProvider)
    provider.mass = MagicMock()
    provider.mass.loop.run_in_executor = AsyncMock(side_effect=lambda _executor, func: func())
    provider.browser = MagicMock(devices=dict.fromkeys(known))
    provider._discovery_running = True
    provider.get_protocol = MagicMock(
        side_effect=lambda protocol_id: MagicMock() if UUID(protocol_id) in attached else None
    )
    provider._on_chromecast_discovered = MagicMock()
    return provider


async def test_discovery_offers_the_known_devices_without_an_attached_protocol() -> None:
    """A removed or re-enabled device is offered from what the running discovery knows."""
    attached, removed = uuid4(), uuid4()
    provider = _discovering_provider(attached={attached}, known=[attached, removed])

    await provider.discover_players()

    provider._on_chromecast_discovered.assert_called_once_with(removed, "")
    provider.browser.start_discovery.assert_not_called()


async def test_the_first_discovery_starts_the_browser() -> None:
    """The first discovery starts the browser, which reports every device it finds."""
    provider = _discovering_provider(attached=set(), known=[uuid4()])
    provider._discovery_running = False

    await provider.discover_players()

    provider.browser.start_discovery.assert_called_once_with()
    provider._on_chromecast_discovered.assert_not_called()


async def test_an_endpoint_found_to_be_passive_is_removed() -> None:
    """A passive multichannel endpoint is removed for good, which takes its bridge along."""
    removals = MagicMock(remove_bridge=AsyncMock(), remove_protocol=AsyncMock())
    provider: Any = ChromecastProvider.__new__(ChromecastProvider)
    provider.mass = MagicMock()
    provider.mass.players.remove_protocol = removals.remove_protocol
    provider.bridge_manager = MagicMock(remove_bridge=removals.remove_bridge)
    provider.logger = MagicMock()
    provider._pending_discoveries = set()
    castplayer = MagicMock(spec=ChromecastPlayer)
    castplayer.cast_info = MagicMock()
    provider.get_protocol = MagicMock(return_value=castplayer)
    protocol_id = str(uuid4())

    with patch("music_assistant.providers.chromecast.provider.ChromecastInfo") as info_class:
        info_class.from_cast_info.return_value.is_multichannel_child = True
        await provider._recheck_multichannel_child(protocol_id, MagicMock())

    # the detach stops its bridge and the removal takes the bridge client along, so the
    # provider has nothing to remove of the bridge itself
    assert removals.mock_calls == [call.remove_protocol(protocol_id)]


async def _register_cast(scenario: Scenario, cast_info: ChromecastInfo) -> ChromecastPlayer:
    """Register the protocol of a Cast device the way discovery sets it up."""
    provider: Any = scenario.mass.get_provider("chromecast") or await scenario.add_provider(
        "chromecast"
    )
    provider.mz_mgr = MagicMock()
    provider.bridge_manager = MagicMock(evaluate_bridge=AsyncMock())
    protocol = _cast_player(provider, cast_info)
    with patch("pychromecast.get_chromecast_from_cast_info", side_effect=_connect_client):
        await scenario.mass.players.register(protocol)
        await scenario.settle()
    return protocol


async def test_a_new_google_device_is_the_native_protocol_of_a_minted_player(
    scenario: Scenario,
) -> None:
    """A Google speaker first seen now gets a player of its own, identified by its Cast UUID."""
    cast_info = _cast_info()
    protocol = await _register_cast(scenario, cast_info)

    player = protocol.player
    assert player is not None
    assert player.player_id.startswith(PLAYER_ID_PREFIX)
    assert player.get_role(protocol.protocol_id) == ProtocolRole.NATIVE
    assert (IdentifierType.CAST_UUID, str(cast_info.uuid)) in protocol.identifiers
    assert (IdentifierType.MAC_ADDRESS, MAC_ADDRESS) in protocol.identifiers


async def test_the_cast_of_a_device_of_another_brand_joins_its_player(
    scenario: Scenario,
) -> None:
    """The Cast output of a speaker of another brand joins the player of its native protocol."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol(
        "RINCON_001122334466", "Speaker A", identifiers={IdentifierType.MAC_ADDRESS: MAC_ADDRESS}
    )
    await scenario.register(speaker)

    protocol = await _register_cast(scenario, _cast_info(manufacturer="Sonos"))

    assert protocol.player is not None
    assert protocol.player.player_id == scenario.player_of(speaker)
    assert protocol.player.get_role(protocol.protocol_id) == ProtocolRole.AUDIO


async def test_a_new_device_reads_its_settings_from_its_own_record(scenario: Scenario) -> None:
    """A setting of the Cast device is read from its protocol record, not the player's."""
    protocol = await _register_cast(scenario, _cast_info())
    assert protocol.player is not None
    assert protocol.player.player_id != protocol.protocol_id
    cast_device = cast("MagicMock", protocol.cc)
    cast_device.app_id = None

    await scenario.mass.config.save_protocol_config(
        protocol.protocol_id, {CONF_DEFER_IDLE_VOLUME: True}
    )
    await scenario.settle()
    await protocol.volume_set(30)

    # an idle receiver keeps the volume back until playback starts
    cast_device.set_volume.assert_not_called()
    assert protocol._pending_volume == 30


def _mocked_provider() -> MagicMock:
    """Return a provider for a protocol driven by hand, whose tasks and callbacks run."""
    provider = MagicMock()
    provider.mass.closing = False
    provider.mass.loop = asyncio.get_running_loop()
    provider.mass.create_task = MagicMock(side_effect=lambda coro, **_: asyncio.ensure_future(coro))
    provider.bridge_manager.evaluate_bridge = AsyncMock()
    return provider


def _report_status(client: MagicMock, volume_level: float) -> None:
    """
    Let a receiver report its connection and its status to the listeners of its client.

    :param client: The client the receiver reports through.
    :param volume_level: The volume level (0..1) the receiver reports.
    """
    client.register_connection_listener.call_args.args[0].new_connection_status(
        MagicMock(status=CONNECTION_STATUS_CONNECTED)
    )
    client.register_status_listener.call_args.args[0].new_cast_status(
        MagicMock(app_id=None, volume_level=volume_level, volume_muted=False)
    )


@pytest.mark.parametrize("move", ["attach_again", "unlink"])
async def test_a_cast_device_attached_again_follows_its_status_and_takes_commands(
    scenario: Scenario, move: str
) -> None:
    """A Cast device attached again reports its status to its player and takes its commands."""
    provider: Any = await scenario.add_provider("chromecast")
    provider.mz_mgr = MagicMock()
    provider.bridge_manager = MagicMock(evaluate_bridge=AsyncMock())
    if move == "unlink":
        speaker = (await scenario.add_provider("sonos")).protocol(
            "RINCON_001122334466",
            "Speaker A",
            identifiers={IdentifierType.MAC_ADDRESS: MAC_ADDRESS},
        )
        await scenario.register(speaker)
    cast_info = _cast_info(manufacturer="Sonos" if move == "unlink" else "Google Inc.")
    protocol_id = str(cast_info.uuid)

    with patch("pychromecast.get_chromecast_from_cast_info", side_effect=_connect_client):
        protocol = _cast_player(provider, cast_info)
        await scenario.mass.players.register(protocol)
        await scenario.settle()
        if move == "unlink":
            await scenario.mass.players.unlink_protocol(protocol_id)
        else:
            await scenario.mass.players.unregister(protocol_id)
            await scenario.settle()
            await scenario.mass.players.register(protocol)
        await scenario.settle()

    client = cast("MagicMock", protocol.cc)
    _report_status(client, volume_level=0.42)
    await scenario.settle()
    player = protocol.player
    assert player is not None
    assert player.state.available is True
    assert player.state.volume_level == 42
    await scenario.mass.players.cmd_volume_set(player.player_id, 30)
    client.set_volume.assert_called_once_with(0.3)
    client.disconnect.assert_not_called()
    # its Sendspin bridge is set up again with it
    assert provider.bridge_manager.evaluate_bridge.await_args_list == [call(protocol)] * 2


async def test_a_detach_during_the_bridge_evaluation_leaves_no_connection() -> None:
    """A detach while the bridge of an attach is evaluated ends both, once."""
    provider = _mocked_provider()
    evaluating = asyncio.Event()
    cancelled = asyncio.Event()

    async def _evaluate_bridge(_protocol: ChromecastPlayer) -> None:
        evaluating.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    provider.bridge_manager.evaluate_bridge = AsyncMock(side_effect=_evaluate_bridge)
    protocol = _cast_player(provider, _cast_info())
    # an object the attach never reached has nothing to end
    await protocol.on_detach()

    with patch("pychromecast.get_chromecast_from_cast_info", side_effect=_connect_client):
        await protocol.on_attach(MagicMock())
        await evaluating.wait()
        await protocol.on_detach()
        # the controller detaches again when a detach overtook the attach
        await protocol.on_detach()

    assert cancelled.is_set()
    client = cast("MagicMock", protocol.cc)
    client.start.assert_called_once_with()
    client.disconnect.assert_called_once_with(10)
    assert client.unregister_handler.call_count == 1


@pytest.mark.parametrize(
    "callback",
    ["group_status", "speaker_status", "connection_status", "multizone_status", "group_left"],
)
async def test_a_status_of_a_detached_client_is_dropped(callback: str) -> None:
    """
    What the socket thread reports around a detach never reaches the detached protocol.

    A status or connection change queued just before the detach is delivered after it; a
    multizone status can still come in once the detach invalidated the listener.
    """
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    provider = _mocked_provider()
    cast_type = CAST_TYPE_GROUP if callback == "group_status" else CAST_TYPE_AUDIO
    protocol = _cast_player(provider, _cast_info(cast_type=cast_type))
    updates = patch.object(protocol, "update_state")
    try:
        with patch("pychromecast.get_chromecast_from_cast_info", side_effect=_connect_client):
            await protocol.on_attach(MagicMock())
        listener = protocol.status_listener
        assert listener is not None
        client = cast("MagicMock", protocol.cc)
        client.status = MagicMock(app_id=None, volume_level=0.42, volume_muted=False)
        volume_level = protocol.volume_level
        update_state = updates.start()
        if callback == "connection_status":
            listener.new_connection_status(MagicMock(status=CONNECTION_STATUS_CONNECTED))
        elif callback == "group_left":
            protocol.active_cast_group = "group-uuid"
            listener.removed_from_multizone("group-uuid")
        elif callback != "multizone_status":
            listener.new_cast_status(client.status)
        await protocol.on_detach()
        if callback == "group_left":
            protocol.active_cast_group = None
        if callback == "multizone_status":
            provider.get_protocol.return_value = MagicMock(
                cc=MagicMock(media_controller=MagicMock(is_active=True))
            )
            listener.multizone_new_cast_status("group-uuid", client.status)
        for _ in range(3):
            await asyncio.sleep(0)
    finally:
        updates.stop()
        loop.set_exception_handler(None)

    assert errors == []
    update_state.assert_not_called()
    assert protocol.volume_level == volume_level
    assert protocol.available is False
    assert protocol.active_cast_group is None


def _connect_group_client(cast_info: Any, *_args: object) -> MagicMock:
    """Return a fake client, as pychromecast connects one, that the multizone manager can track."""
    client = _connect_client()
    client.uuid = cast_info.uuid
    client.status = MagicMock(app_id=None, volume_level=0.42, volume_muted=False)
    client.media_controller.is_active = False
    return client


async def _speaker_in_a_playing_group() -> tuple[ChromecastPlayer, ChromecastPlayer, MagicMock]:
    """
    Return an attached speaker playing from the attached cast group it is a member of.

    The membership runs through a real multizone manager. The speaker's own receiver
    reports media of its own, which the speaker does not show while the group plays.

    :return: The speaker, the group, and the controller that keeps the group's members.
    """
    provider = _mocked_provider()
    provider.mass.streams.base_url = "http://mass"
    provider.mz_mgr = MultizoneManager()
    group = _cast_player(provider, _cast_info(cast_type=CAST_TYPE_GROUP))
    speaker = _cast_player(provider, _cast_info())
    protocols = {group.protocol_id: group, speaker.protocol_id: speaker}
    provider.get_protocol = MagicMock(side_effect=protocols.get)
    with patch("pychromecast.get_chromecast_from_cast_info", side_effect=_connect_group_client):
        await group.on_attach(MagicMock())
        await speaker.on_attach(MagicMock())
    group_client = cast("MagicMock", group.cc)
    group_client.media_controller.is_active = True
    group_client.media_controller.status = _media_status(
        playing=True, content_id="http://mass/group.flac"
    )
    group_listener = provider.mz_mgr._groups[group.protocol_id]["listener"]
    members = group_listener._mz
    members._add_member(speaker.protocol_id, "Speaker A")
    group_listener.new_cast_status(group_client.status)
    _report_own_media(speaker)
    for _ in range(3):
        await asyncio.sleep(0)
    assert speaker.active_cast_group == group.protocol_id
    assert speaker.current_media is not None
    assert speaker.current_media.uri == "http://mass/group.flac"
    return speaker, group, members


def _report_own_media(speaker: ChromecastPlayer) -> None:
    """
    Let the receiver of a speaker report it is connected and plays media of its own.

    :param speaker: The (attached) speaker.
    """
    listener = speaker.status_listener
    assert listener is not None
    client = cast("MagicMock", speaker.cc)
    client.media_controller.status = _media_status(playing=True, content_id="http://mass/solo.flac")
    listener.new_connection_status(MagicMock(status=CONNECTION_STATUS_CONNECTED))
    listener.new_media_status(client.media_controller.status)


@pytest.mark.parametrize("left_group", [True, False], ids=["left-group", "still-member"])
async def test_a_speaker_attached_again_plays_from_the_group_it_is_a_member_of_now(
    left_group: bool,
) -> None:
    """
    A speaker attached again shows its group's media only while it is still in that group.

    The speaker's listener does not hear a group dropping it while its protocol is detached.
    """
    speaker, group, members = await _speaker_in_a_playing_group()

    await speaker.on_detach()
    if left_group:
        members._remove_member(speaker.protocol_id)
    with patch("pychromecast.get_chromecast_from_cast_info", side_effect=_connect_group_client):
        await speaker.on_attach(MagicMock())
    _report_own_media(speaker)
    for _ in range(3):
        await asyncio.sleep(0)

    assert speaker.current_media is not None
    if left_group:
        assert speaker.active_cast_group is None
        assert speaker.current_media.uri == "http://mass/solo.flac"
    else:
        assert speaker.active_cast_group == group.protocol_id
        assert speaker.current_media.uri == "http://mass/group.flac"


async def test_a_speaker_dropped_from_its_group_plays_its_own_media() -> None:
    """An attached speaker that leaves the group it plays from shows its own media again."""
    speaker, _group, members = await _speaker_in_a_playing_group()

    members._remove_member(speaker.protocol_id)
    for _ in range(3):
        await asyncio.sleep(0)

    assert speaker.active_cast_group is None
    assert speaker.current_media is not None
    assert speaker.current_media.uri == "http://mass/solo.flac"


async def test_a_detached_device_ends_the_wait_for_an_app_s_next_track(
    scenario: Scenario,
) -> None:
    """
    A detach ends the wait for another party's app to start its next track.

    The idle status between two tracks of that app is held back for a while; once the
    protocol is detached, nothing of it reaches the players controller anymore.
    """
    protocol = await _register_cast(scenario, _cast_info())
    listener = protocol.status_listener
    assert listener is not None
    client = cast("MagicMock", protocol.cc)
    client.app_id = "SPOTIFY"
    protocol._attr_active_source = "spotify"
    listener.new_media_status(_media_status(playing=True, content_id="http://elsewhere/one"))
    await scenario.settle()
    client.media_controller.status = _media_status()
    listener.new_media_status(client.media_controller.status)
    await scenario.settle()
    timer_id = protocol._app_track_change_task_id
    assert timer_id in scenario.mass._tracked_timers

    await scenario.mass.players.unregister(protocol.protocol_id)
    with patch.object(
        scenario.mass.players,
        "on_unattached_protocol_update",
        wraps=scenario.mass.players.on_unattached_protocol_update,
    ) as unattached_update:
        await scenario.advance(APP_TRACK_CHANGE_TIME + 1)

    unattached_update.assert_not_called()
    assert protocol._app_track_change_ends is None
