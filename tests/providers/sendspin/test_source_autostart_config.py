"""Tests for the line-in autostart config entries on a Sendspin player."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.source import ClientHelloSourceFeatures, ClientHelloSourceSupport
from aiosendspin.noise.trust_store import PskCategory
from music_assistant_models.enums import ConfigEntryType, PlayerType

from music_assistant.providers.sendspin.constants import (
    CONF_SOURCE_AUTOSTART_TARGET,
    SOURCE_AUTOSTART_OFF,
)
from music_assistant.providers.sendspin.player import SendspinBasePlayer, SendspinSourcePlayer

if TYPE_CHECKING:
    import pytest
    from aiosendspin.server.client import SendspinClient
    from music_assistant_models.config_entries import ConfigEntry

    from music_assistant.providers.sendspin.provider import SendspinProvider
    from tests.scenarios.harness import FakePlayerProvider, Scenario

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _player(
    *,
    negotiated_role_ids: list[str],
    line_sense: bool,
    known_players: list[str],
    unavailable_players: list[str] | None = None,
    non_audio_players: dict[str, PlayerType] | None = None,
    player_id: str = "turntable",
) -> SendspinBasePlayer:
    player = SendspinBasePlayer.__new__(SendspinBasePlayer)
    player._protocol_id = "turntable"
    # the player the protocol is attached to
    player._player = cast("Any", SimpleNamespace(player_id=player_id))
    player.api = cast(
        "SendspinClient",
        SimpleNamespace(
            negotiated_role_ids=negotiated_role_ids,
            info_or_none=SimpleNamespace(
                source_support=SimpleNamespace(features=SimpleNamespace(line_sense=line_sense))
            ),
        ),
    )
    player.mass = cast(
        "MusicAssistant",
        SimpleNamespace(
            players=SimpleNamespace(
                all_players=lambda return_unavailable=True, *_args: [
                    SimpleNamespace(player_id=pid, name=pid, type=player_type)
                    for pid, player_type in [
                        *((pid, PlayerType.PLAYER) for pid in known_players),
                        *(
                            (pid, PlayerType.PLAYER)
                            for pid in (unavailable_players or [])
                            if return_unavailable
                        ),
                        *(non_audio_players or {}).items(),
                    ]
                ],
            ),
        ),
    )
    return player


def _target_entry(player: SendspinBasePlayer) -> ConfigEntry | None:
    entries = player._get_source_autostart_config_entries()
    return next((e for e in entries if e.key == CONF_SOURCE_AUTOSTART_TARGET), None)


def test_a_source_without_line_sense_gets_a_note_instead_of_the_setting() -> None:
    """A source that cannot report signal presence has no trigger, so it explains why instead."""
    player = _player(negotiated_role_ids=["source@v1"], line_sense=False, known_players=["speaker"])
    entries = player._get_source_autostart_config_entries()
    assert [(e.key, e.type) for e in entries] == [
        ("source_autostart_unavailable", ConfigEntryType.LABEL)
    ]


def test_no_autostart_entries_for_a_client_without_a_source_role() -> None:
    """A plain speaker never shows line-in settings."""
    player = _player(negotiated_role_ids=["player@v1"], line_sense=True, known_players=["speaker"])
    assert player._get_source_autostart_config_entries() == []


def test_capture_only_source_defaults_to_off() -> None:
    """With no player of its own there is no sensible target, so autostart stays off."""
    player = _player(negotiated_role_ids=["source@v1"], line_sense=True, known_players=["speaker"])
    entry = _target_entry(player)
    assert entry is not None
    assert entry.default_value == SOURCE_AUTOSTART_OFF


def test_a_source_that_is_also_a_player_defaults_to_itself() -> None:
    """A turntable that can play defaults to playing its own line-in, with nothing saved."""
    player = _player(
        negotiated_role_ids=["source@v1", "player@v1"],
        line_sense=True,
        known_players=["turntable", "speaker"],
    )
    entry = _target_entry(player)
    assert entry is not None
    assert entry.default_value == "turntable"


def test_an_unavailable_player_is_not_selectable() -> None:
    """Unavailable players are not offered as autostart targets."""
    player = _player(
        negotiated_role_ids=["source@v1"],
        line_sense=True,
        known_players=["other-speaker"],
        unavailable_players=["sleeping-speaker"],
    )
    entry = _target_entry(player)
    assert entry is not None
    assert "sleeping-speaker" not in {option.value for option in entry.options or []}


def test_the_devices_own_player_stays_the_default_while_unavailable() -> None:
    """A device still being set up is unavailable, yet it stays the default it later resolves to."""
    player = _player(
        negotiated_role_ids=["source@v1", "player@v1"],
        line_sense=True,
        known_players=["speaker"],
        unavailable_players=["turntable"],
    )
    entry = _target_entry(player)
    assert entry is not None
    assert entry.default_value == "turntable"
    assert "turntable" in {option.value for option in entry.options or []}


def test_players_that_cannot_render_audio_are_not_offered() -> None:
    """Lights, displays and other capture-only clients are nowhere to play a line-in."""
    player = _player(
        negotiated_role_ids=["source@v1"],
        line_sense=True,
        known_players=["speaker"],
        non_audio_players={
            "lamp": PlayerType.LIGHT,
            "screen": PlayerType.DISPLAY,
            "other-turntable": PlayerType.UNKNOWN,
        },
    )
    entry = _target_entry(player)
    assert entry is not None
    assert {option.value for option in entry.options or []} == {SOURCE_AUTOSTART_OFF, "speaker"}


def test_the_devices_own_player_leads_the_target_list() -> None:
    """Off stays first, then the default, so it is not buried among every other player."""
    player = _player(
        negotiated_role_ids=["source@v1", "player@v1"],
        line_sense=True,
        known_players=["amp", "turntable", "zone"],
    )
    entry = _target_entry(player)
    assert entry is not None
    assert [option.value for option in entry.options or []] == [
        SOURCE_AUTOSTART_OFF,
        "turntable",
        "amp",
        "zone",
    ]
    assert (entry.options or [])[1].translation_key == "this_device"


def test_the_player_the_source_belongs_to_is_the_default_target() -> None:
    """A source defaults to the player it is attached to, whose id is not its client id."""
    player = _player(
        negotiated_role_ids=["source@v1", "player@v1"],
        line_sense=True,
        known_players=["amp", "pl0123456789ab"],
        player_id="pl0123456789ab",
    )
    entry = _target_entry(player)
    assert entry is not None
    assert entry.default_value == "pl0123456789ab"
    assert [option.value for option in entry.options or []][:2] == [
        SOURCE_AUTOSTART_OFF,
        "pl0123456789ab",
    ]
    assert (entry.options or [])[1].translation_key == "this_device"


def test_groups_and_stereo_pairs_are_offered() -> None:
    """A line-in must be routable to a group, not just to a single speaker."""
    player = _player(
        negotiated_role_ids=["source@v1"],
        line_sense=True,
        known_players=["speaker"],
        non_audio_players={"kitchen-group": PlayerType.GROUP, "pair": PlayerType.STEREO_PAIR},
    )
    entry = _target_entry(player)
    assert entry is not None
    assert {"kitchen-group", "pair"} <= {option.value for option in entry.options or []}


LINE_IN_ID = "linein-key-id-0001"


def _line_in(sendspin: FakePlayerProvider) -> SendspinSourcePlayer:
    """
    Connect a line-in that senses a signal, and return the protocol the provider creates.

    :param sendspin: The Sendspin provider.
    """
    hello = ClientHelloPayload(
        client_id=LINE_IN_ID,
        name="Line-in",
        version=1,
        supported_roles=["source@v1"],
        device_info=SendspinDeviceInfo(product_name="ESP32 Line-in", manufacturer="Espressif"),
        source_support=ClientHelloSourceSupport(
            features=ClientHelloSourceFeatures(line_sense=True)
        ),
    )
    client = MagicMock(
        client_id=LINE_IN_ID,
        info=hello,
        info_or_none=hello,
        negotiated_role_ids=["source@v1"],
        connection_security=SimpleNamespace(psk_category=PskCategory.LONG_TERM),
    )
    cast("Any", sendspin).clients[LINE_IN_ID] = client
    return SendspinSourcePlayer(cast("SendspinProvider", sendspin), LINE_IN_ID, hello)


async def test_the_line_in_target_picked_in_the_setup_flow_is_kept(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A line-in that only exists while connected keeps the target the user picked for it.

    Picking it in the setup flow stores the config of the line-in, so it stays listed while
    it is disconnected and the target is there again when it reconnects, also after a restart.
    """

    async def _no_security_entries(_self: SendspinBasePlayer) -> list[ConfigEntry]:
        return []

    monkeypatch.setattr(SendspinBasePlayer, "_get_security_config_entries", _no_security_entries)
    squeezelite = await scenario.add_provider("squeezelite")
    kitchen = squeezelite.protocol("aabbccddeeff", "Kitchen")
    await scenario.register(kitchen)
    kitchen_id = scenario.player_of(kitchen)
    assert kitchen_id is not None
    sendspin = await scenario.add_provider("sendspin")
    clients: dict[str, Any] = {}
    cast("Any", sendspin).clients = clients
    cast("Any", sendspin).server_api = SimpleNamespace(get_client=clients.get)
    cast("Any", sendspin).is_virtual_player = lambda _client_id: False
    first = _line_in(sendspin)
    assert first.session_only is True
    await scenario.mass.players.register(first)
    await scenario.settle()
    session = MagicMock(context=SimpleNamespace(values={}))
    session.form = AsyncMock(return_value={CONF_SOURCE_AUTOSTART_TARGET: kitchen_id})

    await first._run_source_autostart_step(session)
    await scenario.mass.players.unregister(LINE_IN_ID)
    await scenario.settle()
    state = scenario.player(LINE_IN_ID)
    assert state is not None
    assert state.available is False

    again = _line_in(sendspin)
    await scenario.mass.players.register(again)
    await scenario.settle()
    assert again.config.get_value(CONF_SOURCE_AUTOSTART_TARGET) == kitchen_id
    await scenario.restart()
    sendspin = scenario.provider("sendspin")
    clients = {}
    cast("Any", sendspin).clients = clients
    cast("Any", sendspin).server_api = SimpleNamespace(get_client=clients.get)
    cast("Any", sendspin).is_virtual_player = lambda _client_id: False
    after_restart = _line_in(sendspin)
    await scenario.mass.players.register(after_restart)
    await scenario.settle()
    assert after_restart.config.get_value(CONF_SOURCE_AUTOSTART_TARGET) == kitchen_id
    assert after_restart.player is not None
    assert after_restart.player.player_id == LINE_IN_ID
