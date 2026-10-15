"""Tests for turning a guest device's audio input on and off from its settings."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from aiosendspin.models.types import PairMethod
from aiosendspin.noise.trust_store import TrustedUnpairedClient

from music_assistant.providers.sendspin.constants import (
    CONF_ACTION_SOURCE_INPUT_DISABLE,
    CONF_ACTION_SOURCE_INPUT_ENABLE,
    CONF_SOURCE_INPUT_DECLINED,
)
from music_assistant.providers.sendspin.player import SendspinBasePlayer

from .test_setup_flow import _attach_mass, _desc, _FakeApi, _FakeProvider, _make_player

if TYPE_CHECKING:
    from music_assistant.providers.sendspin.provider import SendspinProvider


def _guest_combo(*, input_approved: bool) -> _FakeProvider:
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PAIRING_CODE)], unpaired_access=True)
    api.negotiated_role_ids = ["player@v1", "source@v1"]
    trusted = TrustedUnpairedClient(
        client_id="client-1", approved_roles=frozenset({"source"} if input_approved else ())
    )
    return _FakeProvider(api, trusted=trusted)


async def _action_keys(player: SendspinBasePlayer, provider: _FakeProvider) -> list[str]:
    _status, actions = await player._security_state_entries(cast("SendspinProvider", provider))
    return [entry.key for entry in actions]


async def test_the_input_can_be_turned_off_and_on_again() -> None:
    """Turning the input off sticks as a decline, and turning it on again approves it."""
    provider = _guest_combo(input_approved=True)
    player = _make_player(provider.api, provider)
    mass = _attach_mass(player)
    assert await _action_keys(player, provider) == [CONF_ACTION_SOURCE_INPUT_DISABLE]

    await player._handle_security_action(
        cast("SendspinProvider", provider), CONF_ACTION_SOURCE_INPUT_DISABLE
    )
    mass.config.set_raw_player_config_value.assert_called_with(
        "client-1", CONF_SOURCE_INPUT_DECLINED, True
    )
    assert await _action_keys(player, provider) == [CONF_ACTION_SOURCE_INPUT_ENABLE]

    await player._handle_security_action(
        cast("SendspinProvider", provider), CONF_ACTION_SOURCE_INPUT_ENABLE
    )
    mass.config.set_raw_player_config_value.assert_called_with(
        "client-1", CONF_SOURCE_INPUT_DECLINED, False
    )
    assert provider.trust_calls == [True, True]
    assert provider.allow_input_calls == [False, True]
    assert await _action_keys(player, provider) == [CONF_ACTION_SOURCE_INPUT_DISABLE]


async def test_a_guest_device_without_an_input_has_no_input_action() -> None:
    """Only devices that offer an audio input get the on/off action."""
    provider = _guest_combo(input_approved=False)
    provider.api.negotiated_role_ids = ["player@v1"]
    player = _make_player(provider.api, provider)

    assert await _action_keys(player, provider) == []
