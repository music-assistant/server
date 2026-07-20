"""Tests for the pairing offer entries in the player security section."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from aiosendspin.models.core import PairMethodDescriptor
from aiosendspin.models.types import PairMethod

from music_assistant.providers.sendspin.constants import (
    CONF_ACTION_PAIR_PIN_START,
    CONF_ACTION_PAIR_STATIC_PIN_START,
    CONF_ACTION_PAIR_TOKEN,
)
from music_assistant.providers.sendspin.player import SendspinBasePlayer

if TYPE_CHECKING:
    from aiosendspin.models.core import ClientHelloPayload
    from music_assistant_models.config_entries import ConfigEntry


def _desc(method: PairMethod, *, locked_out: bool = False) -> PairMethodDescriptor:
    return PairMethodDescriptor(method=method, locked_out=locked_out)


def _offer_entries(methods: list[PairMethodDescriptor]) -> list[ConfigEntry]:
    info = cast("ClientHelloPayload", SimpleNamespace(supported_pair_methods=methods))
    return SendspinBasePlayer._pair_offer_entries(info, None)


def test_static_action_offered_when_both_pin_methods_usable() -> None:
    """Both PIN methods usable adds the advanced static-PIN action after the default."""
    entries = _offer_entries([_desc(PairMethod.DYNAMIC_PIN), _desc(PairMethod.STATIC_PIN)])
    assert [entry.key for entry in entries] == [
        CONF_ACTION_PAIR_PIN_START,
        CONF_ACTION_PAIR_STATIC_PIN_START,
    ]
    assert entries[1].advanced


def test_no_static_action_for_single_pin_method() -> None:
    """A single usable PIN method gets only the default action."""
    for methods in (
        [_desc(PairMethod.DYNAMIC_PIN)],
        [_desc(PairMethod.STATIC_PIN)],
        [_desc(PairMethod.DYNAMIC_PIN, locked_out=True), _desc(PairMethod.STATIC_PIN)],
        [_desc(PairMethod.DYNAMIC_PIN), _desc(PairMethod.STATIC_PIN, locked_out=True)],
    ):
        assert [entry.key for entry in _offer_entries(methods)] == [CONF_ACTION_PAIR_PIN_START]


def test_locked_out_and_token_entries_unaffected() -> None:
    """Fully locked-out PIN methods alert, and the token action is kept."""
    entries = _offer_entries(
        [
            _desc(PairMethod.DYNAMIC_PIN, locked_out=True),
            _desc(PairMethod.STATIC_PIN, locked_out=True),
            PairMethodDescriptor(method=PairMethod.PAIRING_PSK),
        ]
    )
    assert [entry.key for entry in entries] == ["pin_locked_out", CONF_ACTION_PAIR_TOKEN]
