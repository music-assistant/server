"""Tests for applying the web browser players setting without a restart."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

from music_assistant.constants import CONF_ALLOW_BROWSER_PLAYERS, CONF_VOLUME_STEP
from music_assistant.controllers.players.controller import PlayerController

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig


async def test_changing_the_setting_applies_it_to_sendspin_straight_away() -> None:
    """Browser players follow the switch live, and unrelated changes leave them alone."""
    players = MagicMock(spec=PlayerController)
    sendspin = MagicMock()
    players.mass = MagicMock()
    players.mass.get_provider.return_value = sendspin
    config = cast("CoreConfig", SimpleNamespace(values={}))

    await PlayerController.update_config(players, config, {f"values/{CONF_VOLUME_STEP}"})
    sendspin.apply_browser_players_setting.assert_not_called()

    await PlayerController.update_config(players, config, {f"values/{CONF_ALLOW_BROWSER_PLAYERS}"})
    sendspin.apply_browser_players_setting.assert_called_once_with()
