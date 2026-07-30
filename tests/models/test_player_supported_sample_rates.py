"""Tests for ``Player.supported_sample_rates`` and ``get_supported_sample_rates``."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant_models.config_entries import MULTI_VALUE_SPLITTER

from music_assistant.constants import CONF_SAMPLE_RATES
from music_assistant.models.player import Player


def test_supported_sample_rates_property_reads_attr() -> None:
    """The default property just exposes _attr_supported_sample_rates."""
    player = _bare_player(attr_supported=[(96000, 24)])
    assert player.supported_sample_rates == [(96000, 24)]


def test_supported_sample_rates_property_returns_none_when_unset() -> None:
    """A player that hasn't declared its rates returns None from the property."""
    player = _bare_player(attr_supported=None)
    assert player.supported_sample_rates is None


def test_get_supported_sample_rates_uses_declared_value_when_set() -> None:
    """``get_supported_sample_rates`` returns the declared list verbatim."""
    player = _bare_player(attr_supported=[(96000, 24), (192000, 24)])
    assert player.get_supported_sample_rates() == [(96000, 24), (192000, 24)]


def test_get_supported_sample_rates_falls_back_to_config() -> None:
    """When the property returns None, the user's CONF_SAMPLE_RATES selection is used."""
    conf = [
        f"44100{MULTI_VALUE_SPLITTER}16",
        f"48000{MULTI_VALUE_SPLITTER}24",
    ]
    player = _bare_player(attr_supported=None, config_value=conf)
    assert player.get_supported_sample_rates() == [(44100, 16), (48000, 24)]


def test_get_supported_sample_rates_falls_back_to_default_when_no_config() -> None:
    """Without any config value, the safe default [(44100, 16)] is returned."""
    player = _bare_player(attr_supported=None, config_value=None)
    assert player.get_supported_sample_rates() == [(44100, 16)]


def test_declares_supported_sample_rates_true_when_attr_set() -> None:
    """``declares_supported_sample_rates`` is True once _attr_* is populated."""
    player = _bare_player(attr_supported=[(44100, 16)])
    assert player.declares_supported_sample_rates is True


def test_declares_supported_sample_rates_false_for_default_player() -> None:
    """A player that hasn't set _attr_* and doesn't override the property returns False."""
    player = _bare_player(attr_supported=None)
    assert player.declares_supported_sample_rates is False


def test_declares_supported_sample_rates_true_when_property_overridden() -> None:
    """A subclass that overrides ``supported_sample_rates`` also counts as declared."""

    class _OverridingPlayer(Player):
        @property
        def supported_sample_rates(self) -> list[tuple[int, int]] | None:
            return [(48000, 24)]

    player = object.__new__(_OverridingPlayer)
    player._cache = {}
    player._attr_supported_sample_rates = None
    assert player.declares_supported_sample_rates is True
    assert player.get_supported_sample_rates() == [(48000, 24)]


def _bare_player(
    *,
    attr_supported: list[tuple[int, int]] | None,
    config_value: list[str] | None = None,
) -> Player:
    """Construct a Player instance without invoking the real __init__."""
    # the Player ABC's __init__ depends on a full provider/mass wiring;
    # for these unit tests we only need the attributes that supported_sample_rates
    # and get_supported_sample_rates actually read.
    player = object.__new__(Player)
    player._cache = {}
    player._attr_supported_sample_rates = attr_supported
    config = MagicMock()
    config.get_value = MagicMock(
        side_effect=lambda key, default=None: config_value if key == CONF_SAMPLE_RATES else default
    )
    player._config = config
    return player
